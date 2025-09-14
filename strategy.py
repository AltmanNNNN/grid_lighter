import logging
import signal
import time
import threading
import json
import os
from typing import Optional

from grid import build_grid


class ShortGridStrategy:
    """做空网格策略（简化“一吃一补”逻辑）。

    简化规则：
    - 启动：按网格生成价位，离当前价格最近的一档不挂，记为“预挂单价”；其余档位全部挂上（上方挂卖、下方挂买）。
    - 运行：每当账户仓位发生变化（WS 推送）时，推断被吃的挂单价位，并立即在“预挂单价”挂入 1 笔；随后把“预挂单价”更新为被吃价位。
      这样可保证“吃一个补一个”，挂单总量基本恒定。若下单会触发立刻成交，则延后到下一轮再尝试。
    - 极端情形（如同侧同价重复、接口失败）：不做取消，只记录告警并排队重试，确保最终补上。

    说明：
    - 该实现不包含任何自动撤单/复杂同步逻辑，仅依赖“预挂单价轮换”的一进一出保证覆盖。
    - 所有下单受简单的本地限速控制，避免过度提交。
    """
    def __init__(self, cfg, sdk, logger=None):
        self.cfg = cfg
        self.sdk = sdk
        self.log = logger or logging.getLogger(__name__)
        self.running = False

        # 交易所相关
        self.market_id: int = self._resolve_market_id()
        # 从环境变量读取 account_index（必须）。若缺失，将无法在启动时做首轮对齐。
        import os as _os
        try:
            ai_env = _os.environ.get("LIGHTER_ACCOUNT_INDEX")
            self.account_index: Optional[int] = int(ai_env) if ai_env else None
        except Exception:
            self.account_index = None

        # 盘口精度（初始化时探测）
        self.price_decimals: Optional[int] = None
        self.size_decimals: Optional[int] = None

        # 网格价位（升序）
        self.levels: list[float] = []
        self.level_base: list[int] = []  # 与 levels 对应的 base 价格（整数）

        # 本地序号（client_order_index），简单使用自增
        self._coi_seq = int(time.time() % 1_000_000)
        # 上一轮持有的挂单集合（用于侦测“被吃掉”的挂单并补对侧单）
        self._prev_open_sell_prices: set[int] = set()
        self._prev_open_buy_prices: set[int] = set()
        # 本地维护“当前已知挂单方案”（仅作缓存；简化模式下不依赖它做跳过判断）
        self._plan_sell_prices: set[int] = set()
        self._plan_buy_prices: set[int] = set()
        # 可选：强制重建网格（影响 state 文件加载）
        self._force_rebuild_levels: bool = bool(getattr(cfg, "force_rebuild_levels", False))
        self._ws_event_thread: Optional[threading.Thread] = None
        self._processing_lock = threading.Lock()
        # 上一次仓位快照（sign, position, avg_entry_price）
        self._last_position_snapshot: Optional[tuple[int, float, float]] = None
        # 初始化就绪：完成首轮 REST 同步后才允许处理
        self._ready: bool = False
        # 简化：只保留“每秒最多下单数”限制
        self._max_new_orders_per_tick: int = int(getattr(cfg, "max_new_orders_per_tick", 10) or 10)
        # 以“每分钟”为窗口的简单限速计数器
        self._permin_window_start: float = time.time()
        self._permin_count: int = 0
        # 标识：本次 _tick 是否由 WS 账户事件触发（仅用于日志）
        self._last_tick_ws_trigger: bool = False
        # 简化模式（唯一逻辑）：一吃一补，最近价位不挂，其余全部挂上；不做取消（仅在极端场景记录告警）
        self._simple_pre_bpx: Optional[int] = None  # 预挂单价（初始化为最近档，吃单后更新为被吃价）
        self._simple_pending_fill: Optional[tuple[int, bool]] = None  # (eaten_bpx, filled_is_ask)
        self._simple_retry_queue: list[tuple[int, bool]] = []  # 待重试的下单 (base_price, is_ask)
        self._simple_init_complete: bool = False  # 是否已铺满（除预挂单价）
        # 关闭首轮分批挂单等更复杂的调度，统一由每秒限速控制

    def start(self) -> None:
        self._install_signal_handlers()
        self._initialize()
        self.running = True
        self.log.info("ShortGrid started: symbol=%s market_id=%s", getattr(self.cfg, "symbol", None), self.market_id)
        # 首次启动：在正常循环前先处理一次，以“首轮 REST 同步状态”为基准
        try:
            if self._processing_lock.acquire(blocking=False):
                try:
                    self._tick()
                finally:
                    self._processing_lock.release()
        except Exception as e:
            self.log.warning("initial process error: %s", e)
        try:
            while self.running:
                try:
                    # 与 WS 事件线程互斥，避免重入
                    if not self._ready:
                        # 尚未完成首轮同步，等待下一轮
                        pass
                    elif self._processing_lock.acquire(blocking=False):
                        try:
                            self._tick()
                        finally:
                            self._processing_lock.release()
                except Exception as e:
                    self.log.warning("tick error: %s", e)
                time.sleep(float(getattr(self.cfg, "poll_interval_sec", 2)))
        except KeyboardInterrupt:
            self.log.info("KeyboardInterrupt, stopping...")
        finally:
            self._shutdown()

    def stop(self) -> None:
        self.running = False

    def _install_signal_handlers(self) -> None:
        def _h(_sig, _frm):
            self.log.info("Signal received, stopping...")
            self.stop()

        signal.signal(signal.SIGINT, _h)
        signal.signal(signal.SIGTERM, _h)

    def _initialize(self) -> None:
        # 生成网格
        self.levels = build_grid(
            float(getattr(self.cfg, "lower_price", 0)),
            float(getattr(self.cfg, "upper_price", 0)),
            grid_count=getattr(self.cfg, "grid_count", None),
            grid_step=getattr(self.cfg, "grid_step", None),
        )
        self.log.info("Grid levels: %s", self.levels)

        # 设置杠杆 / 保证金模式（若签名参数已配置）
        try:
            lev = int(getattr(self.cfg, "leverage", 0) or 0)
            mmode = getattr(self.cfg, "margin_mode", "isolated")
            if lev > 0:
                txh, _ = self.sdk.set_leverage(int(self.market_id), lev, str(mmode))
                self.log.info("Leverage set: leverage=%s mode=%s tx=%s", lev, mmode, txh)
        except Exception as e:
            self.log.warning("set_leverage failed: %s", e)

        # 读取盘口精度（精确换算价格/数量）。优先使用配置覆盖，其次调用接口，最后兜底 6。
        cfg_pd = getattr(self.cfg, "price_decimals", None)
        cfg_sd = getattr(self.cfg, "size_decimals", None)
        used_cfg_precision = False
        if cfg_pd is not None and cfg_sd is not None:
            try:
                self.price_decimals = int(cfg_pd)
                self.size_decimals = int(cfg_sd)
                used_cfg_precision = True
                self.log.info(
                    "Market precision(from config): price_decimals=%s size_decimals=%s",
                    self.price_decimals,
                    self.size_decimals,
                )
            except Exception as e:
                self.log.warning(
                    "配置中的精度无效 price_decimals=%r size_decimals=%r: %s；将尝试从接口读取",
                    cfg_pd,
                    cfg_sd,
                    e,
                )
        if not used_cfg_precision:
            try:
                pd, sd = self.sdk.get_market_precision(int(self.market_id))
                self.price_decimals = int(pd)
                self.size_decimals = int(sd)
                self.log.info("Market precision: price_decimals=%s size_decimals=%s", pd, sd)
            except Exception as e:
                self.log.warning(
                    "get_market_precision 失败: %s；使用默认精度 6（可在 config.json 配置 price_decimals/size_decimals 以跳过该调用）",
                    e,
                )
                self.price_decimals = 6
                self.size_decimals = 6

        # 预计算每个网格价位的 base 整数价格（首次计算保存，后续复用）
        if not self._load_levels_state():
            self.level_base = [self._to_base_price(lvl) for lvl in self.levels]
            self._save_levels_state()

        # 读取最小下单限制
        self.min_base_amount: int = 0
        try:
            mba, mqa = self.sdk.get_market_limits(int(self.market_id))
            self.min_base_amount = self._to_base_amount(float(mba))
            self.log.info("Market limits: min_base_amount=%s (base units)", self.min_base_amount)
        except Exception as e:
            self.log.warning("get_market_limits failed: %s; 使用默认 min_base_amount=0", e)

        # 若启用 WS，则在此启动 WS（价格与账户推送）
        try:
            if bool(getattr(self.cfg, "use_ws", False)):
                stale_ms = getattr(self.cfg, "ws_stale_ms", None)
                self.sdk.enable_ws(int(self.market_id), int(self.account_index) if self.account_index is not None else None, stale_ms)
                self.log.info("WebSocket enabled: market_id=%s account_index=%s", self.market_id, self.account_index)
                # 启动 WS 事件处理线程：账户更新到来时立即处理一次
                self._start_ws_event_worker()
        except Exception as e:
            self.log.debug("enable_ws failed: %s", e)

        # 启动时同步一次已有挂单，避免重复挂单
        if self.account_index is not None:
            try:
                oo = self.sdk.get_open_orders(account_index=int(self.account_index), market_id=int(self.market_id))
                sells: set[int] = set()
                buys: set[int] = set()
                items = getattr(oo, "orders", None) or []
                for o in items:
                    try:
                        is_ask = bool(getattr(o, "is_ask", False))
                        reduce_only = bool(getattr(o, "reduce_only", False))
                        status = str(getattr(o, "status", "open")).lower()
                        if status not in {"open", "pending", "in-progress"}:
                            continue
                        base_px = getattr(o, "base_price", None)
                        if base_px is None:
                            p_str = getattr(o, "price", None)
                            base_px = int(p_str) if p_str is not None else None
                        if base_px is None:
                            continue
                        if is_ask and not reduce_only:
                            sells.add(int(base_px))
                        if (not is_ask) and not reduce_only:
                            buys.add(int(base_px))
                    except Exception:
                        continue
                self._plan_sell_prices = set(sells)
                self._plan_buy_prices = set(buys)
                # 初始化上一轮快照，用于后续补对侧
                self._prev_open_sell_prices = set(sells)
                self._prev_open_buy_prices = set(buys)
                self.log.info("Warm start with existing orders: sells=%d buys=%d", len(sells), len(buys))
            except Exception as e:
                self.log.warning("init get_open_orders failed: %s", e)
        else:
            self.log.warning(
                "No account_index available at startup; skipping warm-sync of open orders. This may cause duplicate placements."
            )
        # 启动时抓取一次账户仓位，作为对比基准（便于后续 WS 变更推断被吃价位）
        if self.account_index is not None:
            try:
                pos0 = self.sdk.get_positions(account_index=int(self.account_index), market_id=int(self.market_id))
            except Exception as e:
                pos0 = None
                self.log.debug("init get_positions failed: %s", e)
            if pos0 is not None:
                try:
                    snap0 = (
                        int(getattr(pos0, "sign", 0)),
                        float(getattr(pos0, "position", "0") or 0),
                        float(getattr(pos0, "avg_entry_price", "0") or 0),
                    )
                except Exception:
                    snap0 = None
                if snap0 is not None:
                    self._last_position_snapshot = snap0
                    self.log.info(
                        "Initial position snapshot: sign=%d size=%s avg_entry=%s",
                        snap0[0], snap0[1], snap0[2]
                    )
        # 首轮同步完成，允许处理
        self._ready = True

    def _shutdown(self) -> None:
        # 可选：根据配置决定是否在结束时撤销所有挂单
        try:
            if bool(getattr(self.cfg, "cancel_all_on_exit", False)):
                self.sdk.cancel_all()
                self.log.info("Cancel orders success")
        except Exception as e:
            self.log.debug("cancel_all failed: %s", e)

        # 释放 SDK 内部资源（如 SignerClient 会话）
        try:
            if hasattr(self.sdk, "close"):
                self.sdk.close()
        except Exception as e:
            self.log.debug("sdk close ignored: %s", e)

    # --- 后台 WS 日志线程：高频打印 WS 推送的价格（不做下单/查询） ---
    def _start_ws_event_worker(self) -> None:
        """启动 WS 事件处理线程（仅负责“触发”，不做下单一类的重活）。

        设计目标：
        - 当 SDK 的 WS 层收到“账户状态更新”（如成交、资金变更）时，尽快触发一次策略主逻辑 `_tick()`；
        - 通过非阻塞的互斥锁 `_processing_lock` 与主循环互斥，避免重入；
        - 在策略尚未完成首轮初始化（`_ready` 为 False）时，不做任何触发；
        - 线程为守护线程（daemon=True），随进程退出而退出。

        重要约束：
        - 该线程不直接获取价格/下单/撤单，仅检测“是否需要立刻跑一轮 _tick()”；
        - `self.sdk.consume_account_dirty()` 为一次性脏标记读取：若返回 True，表示自上次读取后账户有变更，并会清除该标记。
        """
        if self._ws_event_thread is not None:
            # 已经创建过 WS 事件线程，直接返回（幂等）
            return

        def _loop():
            # 该循环只负责“监听→触发”，所有业务逻辑都在 _tick() 内统一处理
            while self.running:
                try:
                    # 未完成初始化（例如网格/精度/首轮同步未完成），先不触发
                    if not self._ready:
                        time.sleep(0.05)  # 小睡 50ms，降低空转开销
                        continue

                    force = False
                    try:
                        # 若 SDK 暴露 consume_account_dirty()，读取并清除脏标志
                        if hasattr(self.sdk, "consume_account_dirty") and self.sdk.consume_account_dirty():
                            force = True
                    except Exception:
                        # SDK 若不支持或异常，视为不触发
                        force = False

                    if force:
                        # 与主循环互斥，避免重入 _tick()
                        if self._processing_lock.acquire(blocking=False):
                            try:
                                # 标记由 WS 触发（_tick 内部会在结束时复位）
                                self._last_tick_ws_trigger = True
                                # 立即处理一次，使用与主循环一致的逻辑
                                self._tick()
                            finally:
                                # 兜底复位，防止异常时标志残留
                                self._last_tick_ws_trigger = False
                                self._processing_lock.release()

                    # 无论是否触发，短暂休眠以抑制 CPU 占用
                    time.sleep(0.05)
                except Exception:
                    # 任何异常都不应终止该线程，退避更久一点
                    time.sleep(0.1)

        # 单独的守护线程，名称便于日志/诊断
        t = threading.Thread(target=_loop, name="ws-event-worker", daemon=True)
        t.start()
        self._ws_event_thread = t

    def _tick(self) -> None:
        # 价格（严格实时，内部已做轻重试）
        ws_triggered = bool(getattr(self, "_last_tick_ws_trigger", False))
        price = float(self.sdk.get_price_mid(self.market_id))
        src_price = getattr(self.sdk, "get_last_price_source", lambda: "rest")()
        self.log.debug("price(mid) = %.8f (src=%s)", price, src_price)
        price_base = self._to_base_price(price)

        # 预取最优一档（用于避免“预挂单被吃”）
        best_bid_int = None
        best_ask_int = None
        try:
            bb, ba = self.sdk.get_top_of_book(int(self.market_id))
            src_tob = getattr(self.sdk, "get_last_tob_source", lambda: "rest")()
            self.log.debug("top-of-book src=%s bb=%s ba=%s", src_tob, str(bb), str(ba))
            best_bid_int = self._to_base_price(float(bb)) if bb is not None else None
            best_ask_int = self._to_base_price(float(ba)) if ba is not None else None
        except Exception as e:
            self.log.debug("get_top_of_book prefetch failed: %s", e)

        # 止损/暂停判定
        if self._stop_condition(price):
            self.log.warning("Stop condition met at price=%.8f; stopping...", price)
            self.stop()
            return

        # 唯一路径：简化模式（一吃一补）
        try:
            self._tick_simple(
                price=price,
                price_base=price_base,
                best_bid_int=best_bid_int,
                best_ask_int=best_ask_int,
                ws_triggered=ws_triggered,
            )
        except Exception as e:
            self.log.warning("simple mode tick error: %s", e)
        return

    # --- 简化模式：一吃一补 + 除最近档外全部挂上（不做取消） ---
    def _tick_simple(self, price: float, price_base: int,
                     best_bid_int: Optional[int], best_ask_int: Optional[int],
                     ws_triggered: bool) -> None:
        open_ok = False
        open_orders = None
        existing_sell_prices_int: set[int] = set()
        existing_buy_prices_int: set[int] = set()
        # 强制获取 open orders（简化模式需要准确视图）
        if self.account_index is None:
            self.log.info("SimpleMode: no account_index; cannot sync open orders")
            return
        try:
            open_orders = self.sdk.get_open_orders(account_index=int(self.account_index), market_id=int(self.market_id))
            open_ok = True
        except Exception as e:
            self.log.warning("SimpleMode: get_open_orders failed: %s", e)
        if not open_ok or open_orders is None:
            # WS 触发但无法拉取时给出原因
            if ws_triggered:
                self.log.info("WS tick skipped (simple): open orders unavailable")
            return

        # 解析当前接口挂单
        items = getattr(open_orders, "orders", None) or []
        # price -> 该价位上的订单列表（同侧、普通单）
        sells_map: dict[int, list] = {}
        buys_map: dict[int, list] = {}
        for o in items:
            try:
                is_ask = bool(getattr(o, "is_ask", False))
                reduce_only = bool(getattr(o, "reduce_only", False))
                status = str(getattr(o, "status", "open")).lower()
                if reduce_only or status not in {"open", "pending", "in-progress"}:
                    continue
                base_px = getattr(o, "base_price", None)
                if base_px is None:
                    p_str = getattr(o, "price", None)
                    base_px = int(p_str) if p_str is not None else None
                if base_px is None:
                    continue
                bp = int(base_px)
                if is_ask:
                    existing_sell_prices_int.add(bp)
                    sells_map.setdefault(bp, []).append(o)
                else:
                    existing_buy_prices_int.add(bp)
                    buys_map.setdefault(bp, []).append(o)
            except Exception:
                continue

        # 处理同侧同价重复：保留 order_index 最小的一条，其余全部取消
        dup_cancelled = 0
        for mp in (sells_map, buys_map):
            for bp, lst in mp.items():
                try:
                    if len(lst) <= 1:
                        continue
                    try:
                        lst_sorted = sorted(lst, key=lambda x: int(getattr(x, "order_index", 1 << 62)))
                    except Exception:
                        lst_sorted = lst
                    to_cancel = lst_sorted[1:]
                    for o in to_cancel:
                        try:
                            oid = int(getattr(o, "order_index", -1))
                            if oid >= 0:
                                # 简单限速
                                self._before_send()
                                self.sdk.cancel_order(int(self.market_id), oid)
                                dup_cancelled += 1
                        except Exception as ce:
                            self.log.warning("SimpleMode: cancel duplicate failed @ %s: %s; sleep 1s", bp, ce)
                            time.sleep(1.0)
                except Exception:
                    continue
        if dup_cancelled > 0:
            self.log.info("SimpleMode: canceled duplicate orders: %d", dup_cancelled)
            # 取消后等待下一轮，避免与本轮的补单/推断相互影响
            self._prev_open_sell_prices = set(existing_sell_prices_int)
            self._prev_open_buy_prices = set(existing_buy_prices_int)
            return

        # 读取仓位，推断吃单与打印（并设置待补标记）
        try:
            pos = self.sdk.get_positions(account_index=int(self.account_index), market_id=int(self.market_id))
        except Exception as e:
            pos = None
            self.log.debug("SimpleMode: get_positions failed: %s", e)
        if pos is not None:
            try:
                snap = (
                    int(getattr(pos, "sign", 0)),
                    float(getattr(pos, "position", "0") or 0),
                    float(getattr(pos, "avg_entry_price", "0") or 0),
                )
            except Exception:
                snap = None
            if snap is not None and snap != self._last_position_snapshot:
                prev_snap = self._last_position_snapshot
                self._last_position_snapshot = snap
                self.log.info("Position changed: sign=%d size=%s avg_entry=%s", snap[0], snap[1], snap[2])
                try:
                    if prev_snap is not None:
                        prev_signed = float(prev_snap[0]) * float(prev_snap[1])
                        curr_signed = float(snap[0]) * float(snap[1])
                        filled_side = None  # True=SELL, False=BUY
                        if curr_signed > prev_signed:
                            filled_side = False  # BUY 成交
                        elif curr_signed < prev_signed:
                            filled_side = True   # SELL 成交
                        eaten_guess_bpx = None
                        if filled_side is not None:
                            # 从上一轮接口挂单集合中，取离当前价格最近的同侧价位作为“被吃猜测”
                            def _nearest_from_set(cands: set[int], ref: int) -> int | None:
                                best_d, pick = None, None
                                for bp in cands:
                                    d = abs(int(bp) - int(ref))
                                    if best_d is None or d < best_d:
                                        best_d, pick = d, int(bp)
                                return pick
                            eaten_guess_bpx = _nearest_from_set(
                                self._prev_open_sell_prices if filled_side else self._prev_open_buy_prices,
                                int(price_base),
                            )

                        # 最近档位（等距取下方买单）
                        try:
                            arr = sorted(set(int(x) for x in self.level_base))
                            left = None
                            right = None
                            for v in arr:
                                if v < int(price_base):
                                    left = v
                                elif v > int(price_base):
                                    right = v
                                    break
                            if left is None and right is None:
                                nearest_anchor_bpx = None
                            elif left is None:
                                nearest_anchor_bpx = right
                            elif right is None:
                                nearest_anchor_bpx = left
                            else:
                                dl = abs(int(price_base) - int(left))
                                dr = abs(int(right) - int(price_base))
                                nearest_anchor_bpx = left if dl <= dr else right
                        except Exception:
                            nearest_anchor_bpx = None

                        side_txt = ("SELL" if filled_side else ("BUY" if filled_side is not None else "?"))
                        self.log.info(
                            "Fill inference: side=%s eaten_guess_base=%s eaten_guess=%.8f | nearest_base=%s nearest=%.8f",
                            side_txt,
                            str(eaten_guess_bpx),
                            (self._from_base_price(int(eaten_guess_bpx)) if eaten_guess_bpx is not None else float('nan')),
                            str(nearest_anchor_bpx),
                            (self._from_base_price(int(nearest_anchor_bpx)) if nearest_anchor_bpx is not None else float('nan')),
                        )
                        if eaten_guess_bpx is not None and filled_side is not None:
                            # 设定简化模式的一吃一补待处理事件
                            self._simple_pending_fill = (int(eaten_guess_bpx), bool(filled_side))
                            self.log.info("SimpleMode: queued one-in-one-out with eaten_base=%s", int(eaten_guess_bpx))
                except Exception:
                    pass

        # 初始化预挂单价（首次使用最近档）
        if self._simple_pre_bpx is None:
            try:
                arr = sorted(set(int(x) for x in self.level_base))
                left = None
                right = None
                for v in arr:
                    if v < int(price_base):
                        left = v
                    elif v > int(price_base):
                        right = v
                        break
                if left is None and right is None:
                    pre = None
                elif left is None:
                    pre = right
                elif right is None:
                    pre = left
                else:
                    dl = abs(int(price_base) - int(left))
                    dr = abs(int(right) - int(price_base))
                    pre = left if dl <= dr else right
                self._simple_pre_bpx = int(pre) if pre is not None else None
                if self._simple_pre_bpx is not None:
                    self.log.info("SimpleMode: initial pre_base=%s (price=%.8f)", int(self._simple_pre_bpx), price)
            except Exception as e:
                self.log.debug("SimpleMode: init pre_bpx failed: %s", e)

        # 若有待处理的一吃一补事件：只处理“在预挂单价挂 1 笔，并将预挂单价更新为被吃价”
        if self._simple_pending_fill is not None:
            eaten_bpx, _filled_is_ask = self._simple_pending_fill
            place_bpx = self._simple_pre_bpx
            if place_bpx is None:
                # 若尚无 pre，则以最近档作为 place_bpx
                place_bpx = int(eaten_bpx)  # 退化为原地换位（通常不会发生）
            # 方向判定：使用“补单价 vs 被吃价”
            is_ask = bool(int(place_bpx) > int(eaten_bpx))
            # 已存在则直接旋转预挂单价
            already = (int(place_bpx) in (existing_sell_prices_int if is_ask else existing_buy_prices_int))
            if already:
                self.log.info("SimpleMode: pre already placed @ base=%s; rotate pre to eaten=%s",
                              int(place_bpx), int(eaten_bpx))
                self._simple_pre_bpx = int(eaten_bpx)
                self._simple_pending_fill = None
            else:
                # 不再检测是否有吃单风险，直接挂单
                try:
                    base_amount = max(self._to_base_amount(float(getattr(self.cfg, "entry_order_size", 0))), int(self.min_base_amount))
                    tif = self._map_tif(getattr(self.cfg, "time_in_force", "GTC"))
                    coi = self._next_coi()
                    self._before_send()
                    self.sdk.place_order(
                        market_id=int(self.market_id),
                        client_order_index=coi,
                        base_amount=base_amount,
                        price=int(place_bpx),
                        is_ask=is_ask,
                        order_type=self._const_limit(),
                        time_in_force=tif,
                        reduce_only=0,
                        trigger_price=0,
                    )
                    side = "SELL" if is_ask else "BUY"
                    self.log.info(
                        "SimpleMode: placed %s @ base_price=%s (coi=%s); pre -> %s (eaten_base=%s)",
                        side, int(place_bpx), coi, int(eaten_bpx), int(eaten_bpx)
                    )
                    if is_ask:
                        self._plan_sell_prices.add(int(place_bpx))
                    else:
                        self._plan_buy_prices.add(int(place_bpx))
                    # 旋转预挂单价
                    self._simple_pre_bpx = int(eaten_bpx)
                    self._simple_pending_fill = None
                except Exception as e:
                    self.log.warning("SimpleMode: place at pre failed @ %s: %s; will retry", int(place_bpx), e)
                    try:
                        self._simple_retry_queue.append((int(place_bpx), bool(is_ask)))
                    except Exception:
                        pass
                    time.sleep(1.0)

            # 更新上一轮集合
            self._prev_open_sell_prices = set(existing_sell_prices_int)
            self._prev_open_buy_prices = set(existing_buy_prices_int)
            return

        # 非吃单事件：初始化/修复缺失的“非预挂单价”挂单
        targets: list[tuple[int, bool, int]] = []  # (bpx, is_ask, dist)
        for idx, lvl in enumerate(self.levels):
            bpx = int(self.level_base[idx])
            if self._simple_pre_bpx is not None and int(bpx) == int(self._simple_pre_bpx):
                continue
            if lvl > price:
                is_ask = True
            elif lvl < price:
                is_ask = False
            else:
                # 现价恰落在档位：视为预挂单价或跳过
                continue
            exists = (bpx in (existing_sell_prices_int if is_ask else existing_buy_prices_int))
            if not exists:
                targets.append((int(bpx), bool(is_ask), abs(int(bpx) - int(price_base))))

        # 加入重试队列（若仍缺失）
        if self._simple_retry_queue:
            leftover: list[tuple[int, bool]] = []
            for bpx, is_ask in list(self._simple_retry_queue):
                present = (int(bpx) in (existing_sell_prices_int if is_ask else existing_buy_prices_int))
                if not present:
                    targets.append((int(bpx), bool(is_ask), abs(int(bpx) - int(price_base))))
                else:
                    leftover.append((int(bpx), bool(is_ask)))
            self._simple_retry_queue = leftover

        # 最近优先挂，避免一次挂太远
        targets.sort(key=lambda x: x[2])
        placed = 0
        for bpx, is_ask, _d in targets:
            # 避免被立即吃单
            if is_ask and best_ask_int is not None and int(bpx) <= int(best_ask_int):
                self.log.debug("SimpleMode: defer SELL @ %s (<= best_ask %s)", int(bpx), best_ask_int)
                continue
            if (not is_ask) and best_bid_int is not None and int(bpx) >= int(best_bid_int):
                self.log.debug("SimpleMode: defer BUY @ %s (>= best_bid %s)", int(bpx), best_bid_int)
                continue
            try:
                base_amount = max(self._to_base_amount(float(getattr(self.cfg, "entry_order_size", 0))), int(self.min_base_amount))
                tif = self._map_tif(getattr(self.cfg, "time_in_force", "GTC"))
                coi = self._next_coi()
                self._before_send()
                self.sdk.place_order(
                    market_id=int(self.market_id),
                    client_order_index=coi,
                    base_amount=base_amount,
                    price=int(bpx),
                    is_ask=is_ask,
                    order_type=self._const_limit(),
                    time_in_force=tif,
                    reduce_only=0,
                    trigger_price=0,
                )
                side = "SELL" if is_ask else "BUY"
                self.log.info("SimpleMode: placed %s @ base_price=%s (coi=%s)", side, int(bpx), coi)
                if is_ask:
                    self._plan_sell_prices.add(int(bpx))
                else:
                    self._plan_buy_prices.add(int(bpx))
                placed += 1
            except Exception as e:
                self.log.warning("SimpleMode: place failed @ %s: %s; will retry", int(bpx), e)
                try:
                    self._simple_retry_queue.append((int(bpx), bool(is_ask)))
                except Exception:
                    pass
                time.sleep(1.0)

        if placed > 0:
            self.log.info("SimpleMode: placed %d orders to sync coverage (excluding pre)", placed)

        # 更新上一轮集合
        self._prev_open_sell_prices = set(existing_sell_prices_int)
        self._prev_open_buy_prices = set(existing_buy_prices_int)

    # --- 辅助 ---
    def _stop_condition(self, price: float) -> bool:
        gsp = getattr(self.cfg, "global_stop_price", None)
        if gsp is not None and price >= float(gsp):
            return True
        stop_buffer = getattr(self.cfg, "stop_buffer", None)
        if stop_buffer is not None:
            up = float(getattr(self.cfg, "upper_price", 0)) + float(stop_buffer)
            if price >= up:
                return True
        return False

    def _resolve_market_id(self) -> int:
        mid = getattr(self.cfg, "market_id", None)
        if mid is not None:
            return int(mid)
        sym = getattr(self.cfg, "symbol", None)
        mm = getattr(self.cfg, "market_id_map", {}) or {}
        if sym and sym in mm:
            return int(mm[sym])
        # 兜底：0（通常为示例市场）
        return 0

    def _next_coi(self) -> int:
        self._coi_seq += 1
        return self._coi_seq

    def _to_base_price(self, price: float) -> int:
        # 若未知精度，按 1e6 作为兜底；建议后续从 order_book_details 读取 price_decimals
        decimals = self.price_decimals if self.price_decimals is not None else 6
        return int(round(price * (10 ** int(decimals))))

    def _to_base_amount(self, size: float) -> int:
        # 若未知精度，按 1e6 作为兜底；建议后续从 order_book_details 读取 size_decimals
        decimals = self.size_decimals if self.size_decimals is not None else 6
        return int(round(size * (10 ** int(decimals))))

    def _map_tif(self, tif_str: str) -> int:
        s = (tif_str or "").upper()
        # lighter.SignerClient 时间策略常量映射
        if hasattr(self.sdk, "_signer") and self.sdk._signer is not None:
            sc = self.sdk._signer
        else:
            # 临时构造常量引用（类属性）
            sc = getattr(__import__("lighter").SignerClient, "__class__", __import__("lighter").SignerClient)
        try:
            # 若配置了 post_only，则优先使用 post-only
            if bool(getattr(self.cfg, "post_only", False)):
                return __import__("lighter").SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY
            if s in {"GTC", "GOOD_TILL_TIME"}:
                return __import__("lighter").SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
            if s in {"IOC", "IMMEDIATE_OR_CANCEL"}:
                return __import__("lighter").SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL
            if s in {"POST_ONLY"}:
                return __import__("lighter").SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY
        except Exception:
            pass
        return __import__("lighter").SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME

    def _const_limit(self) -> int:
        return __import__("lighter").SignerClient.ORDER_TYPE_LIMIT

    # --- 简化：每秒限速与失败后暂停 ---
    def _before_send(self) -> None:
        # 将 max_new_orders_per_tick 作为“每分钟发送上限”
        cap = int(self._max_new_orders_per_tick)
        while True:
            now = time.time()
            # 如果窗口超过 60 秒，重置
            if now - self._permin_window_start >= 60.0:
                self._permin_window_start = now
                self._permin_count = 0
            # 若未达上限，则计数 +1 并返回
            if self._permin_count < cap:
                self._permin_count += 1
                return
            # 达到上限：按你的要求暂停 1 秒后继续检查
            time.sleep(1.0)

    # （已简化，无需 _nearest_level_index 辅助，逻辑直接在调用处计算最近档）

    def _from_base_price(self, base_price: int) -> float:
        decimals = self.price_decimals if self.price_decimals is not None else 6
        return float(base_price) / (10 ** int(decimals))

    # --- 状态持久化（网格价位） ---
    def _state_path(self) -> Optional[str]:
        path = getattr(self.cfg, "state_file", None)
        if not path or not isinstance(path, str) or not path.strip():
            return None
        return path

    def _load_levels_state(self) -> bool:
        path = self._state_path()
        if not path or not os.path.exists(path):
            return False
        if self._force_rebuild_levels:
            # 显式强制重建
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return False
            if int(data.get("market_id", -1)) != int(self.market_id):
                return False
            sym = data.get("symbol")
            if sym and getattr(self.cfg, "symbol", None) and str(sym) != str(getattr(self.cfg, "symbol", None)):
                return False
            # 网格参数校验：若 lower/upper/grid_count/step 与当前配置不一致，则忽略旧状态
            saved_lower = data.get("lower_price")
            saved_upper = data.get("upper_price")
            saved_gc = data.get("grid_count")
            saved_gs = data.get("grid_step")
            cfg_lower = float(getattr(self.cfg, "lower_price", 0))
            cfg_upper = float(getattr(self.cfg, "upper_price", 0))
            cfg_gc = getattr(self.cfg, "grid_count", None)
            cfg_gs = getattr(self.cfg, "grid_step", None)
            def _eq(a, b):
                try:
                    if a is None and b is None:
                        return True
                    if a is None or b is None:
                        return False
                    return float(a) == float(b)
                except Exception:
                    return a == b
            if not (_eq(saved_lower, cfg_lower) and _eq(saved_upper, cfg_upper) and _eq(saved_gc, cfg_gc) and _eq(saved_gs, cfg_gs)):
                return False
            levels = data.get("levels")
            level_base = data.get("level_base")
            pd = data.get("price_decimals")
            sd = data.get("size_decimals")
            if not isinstance(levels, list) or not isinstance(level_base, list):
                return False
            if len(levels) != len(level_base):
                return False
            self.levels = [float(x) for x in levels]
            self.level_base = [int(x) for x in level_base]
            if pd is not None:
                self.price_decimals = int(pd)
            if sd is not None:
                self.size_decimals = int(sd)
            self.log.info("Loaded levels from state: count=%d", len(self.levels))
            return True
        except Exception as e:
            self.log.debug("load levels state failed: %s", e)
            return False

    def _save_levels_state(self) -> None:
        path = self._state_path()
        if not path:
            return
        try:
            data = {
                "symbol": getattr(self.cfg, "symbol", None),
                "market_id": int(self.market_id),
                "price_decimals": int(self.price_decimals if self.price_decimals is not None else 6),
                "size_decimals": int(self.size_decimals if self.size_decimals is not None else 6),
                "lower_price": float(getattr(self.cfg, "lower_price", 0)),
                "upper_price": float(getattr(self.cfg, "upper_price", 0)),
                "grid_count": getattr(self.cfg, "grid_count", None),
                "grid_step": getattr(self.cfg, "grid_step", None),
                "levels": [float(x) for x in self.levels],
                "level_base": [int(x) for x in self.level_base],
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.log.info("Saved levels to state: %s", path)
        except Exception as e:
            self.log.debug("save levels state failed: %s", e)

    # （删除旧的复杂限速逻辑）

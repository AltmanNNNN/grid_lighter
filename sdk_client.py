import asyncio
import logging
import sys
import time
import threading
import os
import lighter  # type: ignore
from typing import Optional



"""极简 lighter 同步客户端：仅保留做空网格需要的接口。

公开方法：
- get_price_mid(market_id)
- get_open_orders(account_index, market_id, authorization=None, auth=None)
- get_positions(account_index, market_id, authorization=None, auth=None)
- place_order(...)
- cancel_order(market_id, order_index)
- close()
"""


def get_sdk_client(
    api_host: str | None = None,
    account_index: int | None = None,
    api_key_index: int | None = None,
    api_key_private_key: str | None = None,
):
    """构造轻量客户端；若提供签名参数则启用下单/撤单能力。"""
    host = api_host or "https://mainnet.zklighter.elliot.ai"
    return LighterClient(
        host=host,
        account_index=account_index,
        api_key_index=api_key_index,
        api_key_private_key=api_key_private_key,
    )


class LighterClient:
    """对异步 `lighter` SDK 的最小同步适配器。

    目的：
    - 通过在内部使用 `asyncio.run` 运行异步客户端，为外部提供同步的 `get_price()`。
    - 需要 `symbol -> market_id` 的映射，用于定位具体市场。

    当前范围：
    - 实现价格获取：优先用盘口中间价，失败时回退到最近成交价。
    - 下单/撤单等交易方法待确认鉴权流程后再补充。
    """

    def __init__(
        self,
        host: str,
        account_index: int | None = None,
        api_key_index: int | None = None,
        api_key_private_key: str | None = None,
    ):
        self._host = host
        self._account_index = account_index
        self._api_key_index = api_key_index
        self._api_key_private_key = api_key_private_key
        self._signer = None
        self._closed = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # WS state
        self._ws_client = None
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_best_bid: Optional[float] = None
        self._ws_best_ask: Optional[float] = None
        self._ws_ts: float = 0.0
        self._ws_account_dirty: bool = False
        self._ws_stale_ms: int = 1000
        # 简化的 WS 诊断状态
        self._ws_enabled: bool = False
        # Source markers
        self._last_price_source: str = "rest"
        self._last_tob_source: str = "rest"

        # Proxy/env options
        self._proxy_url: str | None = self._resolve_proxy()
        self._no_proxy: str | None = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
        # TLS verify toggle (optional): LIGHTER_VERIFY_SSL=true/false
        self._verify_ssl: bool | None = None
        _v = os.environ.get("LIGHTER_VERIFY_SSL")
        if _v is not None:
            s = str(_v).strip().lower()
            self._verify_ssl = not (s in {"0", "false", "no", "off"})

    # --- 最小 Info/行情方法 ---
    def get_price_mid(self, market_id: int) -> float:
        """获取盘口中间价（优先用 WS，回退 REST）。

        - 若 WS 最新推送在有效期内（_ws_stale_ms），使用 WS 的最优一档；
        - 否则回退到 REST 获取的最优一档（严格实时、带少量重试）。
        """
        # WS 优先
        now = time.time() * 1000.0
        if self._ws_best_bid is not None and self._ws_best_ask is not None and (now - self._ws_ts) <= self._ws_stale_ms:
            self._last_price_source = "ws"
            return (float(self._ws_best_bid) + float(self._ws_best_ask)) / 2.0
        # 回退 REST
        self._last_price_source = "rest"
        return self._run(self._get_price_mid_async(market_id))

    # --- Async 实现 ---
    async def _get_price_mid_async(self, market_id: int) -> float:
        client = lighter.ApiClient(configuration=self._build_configuration())
        order_api = lighter.OrderApi(client)
        try:
            attempts = 3
            delay = 0.2
            for i in range(attempts):
                oba = await order_api.order_book_orders(market_id=market_id, limit=1)
                best_bid = None
                best_ask = None
                if getattr(oba, "bids", None):
                    b0 = oba.bids[0]
                    best_bid = float(getattr(b0, "price", None) or (b0.get("price") if isinstance(b0, dict) else None))
                if getattr(oba, "asks", None):
                    a0 = oba.asks[0]
                    best_ask = float(getattr(a0, "price", None) or (a0.get("price") if isinstance(a0, dict) else None))
                if best_bid is not None and best_ask is not None:
                    return (best_bid + best_ask) / 2.0
                # 等待片刻重试，避免瞬时空档
                if i < attempts - 1:
                    await asyncio.sleep(delay)
        finally:
            await client.close()
        raise RuntimeError("无法从实时盘口获取最优买/卖一档（market_id=%s），已放弃回退以避免使用滞后价格。" % market_id)

    def get_market_precision(self, market_id: int) -> tuple[int, int]:
        """获取市场精度 (price_decimals, size_decimals)。"""
        return self._run(self._get_market_precision_async(market_id))

    async def _get_market_precision_async(self, market_id: int) -> tuple[int, int]:
        async with lighter.ApiClient(self._build_configuration()) as client:
            api = lighter.OrderApi(client)
            obd = await api.order_book_details(market_id=market_id)
            details = getattr(obd, "order_book_details", None) or []
            if not details:
                raise RuntimeError(f"未获取到 market_id={market_id} 的详情")
            d0 = details[0]
            pd = int(getattr(d0, "price_decimals", None))
            sd = int(getattr(d0, "size_decimals", None))
            return pd, sd

    def get_market_limits(self, market_id: int) -> tuple[float, float]:
        """获取市场下单限制 (min_base_amount, min_quote_amount)，浮点形式。

        注意：返回的是人类可读的小数，不是整数 base 单位。
        """
        return self._run(self._get_market_limits_async(market_id))

    async def _get_market_limits_async(self, market_id: int) -> tuple[float, float]:
        async with lighter.ApiClient(self._build_configuration()) as client:
            api = lighter.OrderApi(client)
            obd = await api.order_book_details(market_id=market_id)
            details = getattr(obd, "order_book_details", None) or []
            if not details:
                raise RuntimeError(f"未获取到 market_id={market_id} 的详情")
            d0 = details[0]
            mba = float(getattr(d0, "min_base_amount", 0) or 0)
            mqa = float(getattr(d0, "min_quote_amount", 0) or 0)
            return mba, mqa

    def get_top_of_book(self, market_id: int) -> tuple[Optional[float], Optional[float]]:
        """获取盘口最优一档 (best_bid, best_ask) 为浮点。

        可能有一侧为空，返回 None。优先使用 WS 缓存（若新鲜），否则 REST。
        """
        now = time.time() * 1000.0
        if (self._ws_best_bid is not None or self._ws_best_ask is not None) and (now - self._ws_ts) <= self._ws_stale_ms:
            self._last_tob_source = "ws"
            return self._ws_best_bid, self._ws_best_ask
        self._last_tob_source = "rest"
        return self._run(self._get_top_of_book_async(market_id))

    async def _get_top_of_book_async(self, market_id: int) -> tuple[Optional[float], Optional[float]]:
        async with lighter.ApiClient(self._build_configuration()) as client:
            oba = await lighter.OrderApi(client).order_book_orders(market_id=market_id, limit=1)
            bb = None
            ba = None
            if getattr(oba, "bids", None):
                b0 = oba.bids[0]
                bb = float(getattr(b0, "price", None) or (b0.get("price") if isinstance(b0, dict) else None))
            if getattr(oba, "asks", None):
                a0 = oba.asks[0]
                ba = float(getattr(a0, "price", None) or (a0.get("price") if isinstance(a0, dict) else None))
            return bb, ba

    # --- 查询：未成交订单 ---
    def get_open_orders(
        self,
        account_index: int,
        market_id: int,
        authorization: str | None = None,
        auth: str | None = None,
    ):
        return self._run(
            self._get_open_orders_async(account_index, market_id, authorization=authorization, auth=auth)
        )

    async def _get_open_orders_async(
        self,
        account_index: int,
        market_id: int,
        authorization: str | None = None,
        auth: str | None = None,
    ):
        async with lighter.ApiClient(self._build_configuration()) as client:
            api = lighter.OrderApi(client)
            try:
                # 自动生成短期 auth（如具备签名能力且未显式提供）
                use_auth = auth
                if use_auth is None and self._can_sign():
                    use_auth, _err = await self._create_auth_token()
                print(f"use_auth: {use_auth}")
                return await api.account_active_orders(
                    account_index, market_id, authorization=authorization, auth=use_auth
                )
            except Exception as e:
                raise e

    # --- 下单/撤单（SignerClient） ---
    def place_order(
        self,
        market_id: int,
        client_order_index: int,
        base_amount: int,
        price: int,
        is_ask: bool,
        order_type: int,
        time_in_force: int,
        reduce_only: int = 0,
        trigger_price: int = 0,
    ) -> tuple[str, str]:
        return self._run(
            self._place_order_async(
                market_id,
                client_order_index,
                base_amount,
                price,
                is_ask,
                order_type,
                time_in_force,
                reduce_only,
                trigger_price,
            )
        )

    async def _place_order_async(
        self,
        market_id: int,
        client_order_index: int,
        base_amount: int,
        price: int,
        is_ask: bool,
        order_type: int,
        time_in_force: int,
        reduce_only: int = 0,
        trigger_price: int = 0,
    ) -> tuple[str, str]:
        signer = self._require_signer()
        tx, tx_hash, err = await signer.create_order(
            market_index=market_id,
            client_order_index=client_order_index,
            base_amount=base_amount,
            price=price,
            is_ask=is_ask,
            order_type=order_type,
            time_in_force=time_in_force,
            reduce_only=reduce_only,
            trigger_price=trigger_price,
        )
        if err is not None:
            raise RuntimeError(err)
        return str(tx_hash), str(tx)

    def cancel_order(self, market_id: int, order_index: int) -> tuple[str, str]:
        return self._run(self._cancel_order_async(market_id, order_index))

    async def _cancel_order_async(self, market_id: int, order_index: int) -> tuple[str, str]:
        signer = self._require_signer()
        tx, tx_hash, err = await signer.cancel_order(
            market_index=market_id,
            order_index=order_index,
        )
        if err is not None:
            raise RuntimeError(err)
        return str(tx_hash), str(tx)

    # 资源释放：关闭内部 SignerClient 会话，避免 Unclosed client session 警告
    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._signer is not None:
                self._run(self._signer.close())
        except Exception:
            pass
        self._closed = True

    # --- 其它交易接口 ---
    def set_leverage(self, market_id: int, leverage: int, margin_mode: str = "isolated") -> tuple[str, str]:
        """设置杠杆与保证金模式（通过 SignerClient.update_leverage）。"""
        return self._run(self._set_leverage_async(market_id, leverage, margin_mode))

    async def _set_leverage_async(self, market_id: int, leverage: int, margin_mode: str) -> tuple[str, str]:
        signer = self._require_signer()
        mode = (lighter.SignerClient.ISOLATED_MARGIN_MODE if str(margin_mode).lower() == "isolated"
                else lighter.SignerClient.CROSS_MARGIN_MODE)
        tx_info, tx_hash, err = await signer.update_leverage(market_index=int(market_id), margin_mode=int(mode), leverage=int(leverage))
        if err is not None:
            raise RuntimeError(err)
        return str(tx_hash), str(tx_info)

    def cancel_all(self, tif: Optional[int] = None, when: Optional[int] = None) -> tuple[str, str]:
        """全撤订单（全账号）。

        - 默认立即撤单：tif=CANCEL_ALL_TIF_IMMEDIATE, when=0
        - 也可传入调度时间戳（秒）与对应 TIF 常量
        """
        return self._run(self._cancel_all_async(tif, when))

    async def _cancel_all_async(self, tif: Optional[int], when: Optional[int]) -> tuple[str, str]:
        signer = self._require_signer()
        tif_val = int(tif) if tif is not None else lighter.SignerClient.CANCEL_ALL_TIF_IMMEDIATE
        when_val = int(when) if when is not None else 0
        tx_info, tx_hash, err = await signer.cancel_all_orders(time_in_force=tif_val, time=when_val)
        if err is not None:
            raise RuntimeError(err)
        return str(tx_hash), str(tx_info)

    def get_positions(
        self,
        account_index: int,
        market_id: int,
        authorization: Optional[str] = None,
        auth: Optional[str] = None,
    ):
        """查询账户在指定市场的仓位（基于 AccountPosition 模型）。"""
        return self._run(
            self._get_positions_async(
                account_index, market_id, authorization=authorization, auth=auth
            )
        )

    async def _get_positions_async(
        self,
        account_index: int,
        market_id: int,
        authorization: Optional[str] = None,
        auth: Optional[str] = None,
    ):
        """通过 AccountApi.account 获取账户详情，并从 positions 里选出指定 market_id 的仓位。"""
        async with lighter.ApiClient(lighter.Configuration(host=self._host)) as client:
            api = lighter.AccountApi(client)
            # lighter 文档显示示例：account_api.get_account(by="index", value="1")
            detailed = await api.account(by="index", value=str(account_index))
            # 结构为 DetailedAccounts，其中 accounts 是列表
            accounts = getattr(detailed, "accounts", None) or []
            pos = None
            if accounts:
                positions = getattr(accounts[0], "positions", None) or []
                # 在 positions 中按 market_id 过滤
                for p in positions:
                    try:
                        if int(getattr(p, "market_id", -1)) == int(market_id):
                            pos = p
                            break
                    except Exception:
                        continue
            return pos

    def get_fills(self, account_index: int, market_id: int, since_id: Optional[str] = None):
        raise NotImplementedError("TODO: lighter 成交明细 API（请提供方法名/示例）")

    # --- 内部：签名客户端与鉴权 ---
    def _can_sign(self) -> bool:
        return bool(self._api_key_private_key and self._account_index is not None and self._api_key_index is not None)

    def _require_signer(self):
        if not self._can_sign():
            raise RuntimeError("未配置下单所需的签名参数（account_index, api_key_index, api_key_private_key）。")
        if self._signer is None:
            self._signer = lighter.SignerClient(
                url=self._host,
                private_key=self._api_key_private_key,
                account_index=int(self._account_index),
                api_key_index=int(self._api_key_index),
            )
            err = self._signer.check_client()
            if err is not None:
                raise RuntimeError(f"SignerClient check failed: {err}")
        return self._signer

    async def _create_auth_token(self) -> tuple[str | None, Exception | None]:
        signer = self._require_signer()
        token, err = signer.create_auth_token_with_expiry(lighter.SignerClient.DEFAULT_10_MIN_AUTH_EXPIRY)
        return token, err

    # --- 内部：统一事件循环 ---
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop

    def _run(self, coro):
        loop = self._ensure_loop()
        return loop.run_until_complete(coro)

    # --- WebSocket: 启用推送用于价格/账户事件 ---
    def enable_ws(self, market_id: int, account_index: Optional[int] = None, stale_ms: Optional[int] = None) -> None:
        try:
            from lighter.ws_client import WsClient  # type: ignore
        except Exception:
            return
        if stale_ms is not None:
            try:
                self._ws_stale_ms = int(stale_ms)
            except Exception:
                pass

        def _on_ob(mid, ob):
            try:
                # ob: dict with 'bids'/'asks' levels; each item has 'price' and 'size'
                bb = None
                ba = None
                bids = ob.get("bids") if isinstance(ob, dict) else None
                asks = ob.get("asks") if isinstance(ob, dict) else None
                if bids and len(bids) > 0:
                    p = bids[0].get("price") if isinstance(bids[0], dict) else None
                    bb = float(p) if p is not None else None
                if asks and len(asks) > 0:
                    p = asks[0].get("price") if isinstance(asks[0], dict) else None
                    ba = float(p) if p is not None else None
                if bb is not None:
                    self._ws_best_bid = bb
                if ba is not None:
                    self._ws_best_ask = ba
                self._ws_ts = time.time() * 1000.0
                pass
            except Exception:
                pass

        def _on_acct(aid, msg):
            # 标记账户状态有更新，策略可据此触发一次 open orders 同步
            self._ws_account_dirty = True

        host = self._host.replace("https://", "")
        ob_ids = [int(market_id)]
        acct_ids = [int(account_index)] if account_index is not None else []
        # 尝试为 WS 传入代理（若 ws_client 支持）
        try:
            self._ws_client = WsClient(host=host, order_book_ids=ob_ids, account_ids=acct_ids,
                                       on_order_book_update=_on_ob, on_account_update=_on_acct,
                                       proxy=self._proxy_url)
        except TypeError:
            # 老版本不支持 proxy 参数，退回无代理（但仍可通过系统/环境代理影响）
            self._ws_client = WsClient(host=host, order_book_ids=ob_ids, account_ids=acct_ids,
                                       on_order_book_update=_on_ob, on_account_update=_on_acct)

        def _runner():
            # 连接/断开均打印日志，便于在主流程观察 WS 状态
            log = logging.getLogger("lighter.ws")
            status_log = logging.getLogger("sdk_client.ws")
            url = getattr(self._ws_client, "base_url", f"wss://{host}/stream")
            backoff = 0.5
            while not self._closed:
                try:
                    try:
                        # 首选手动连接以便在成功握手后输出“connected”日志
                        from websockets.sync.client import connect as _ws_connect  # type: ignore

                        log.info("WS connecting: %s", url)
                        status_log.info("WS connecting: %s", url)
                        with _ws_connect(url) as _ws:
                            try:
                                # 将底层 ws 句柄挂到客户端以兼容其回调逻辑
                                setattr(self._ws_client, "ws", _ws)
                            except Exception:
                                pass
                            log.info("WS connected: %s", url)
                            status_log.info("WS connected: %s", url)
                            for _msg in _ws:
                                try:
                                    self._ws_client.on_message(_ws, _msg)
                                except Exception:
                                    # 单条消息异常不应中断连接
                                    pass
                        # 正常关闭（较少见），按退避重连
                        log.info("WS closed; retrying in %.1fs", backoff)
                        status_log.warning("WS closed; retrying in %.1fs", backoff)
                    except ImportError:
                        # 回退到 SDK 自带 run()（无法精准打印 connected）
                        log.info("WS connecting: %s", url)
                        status_log.info("WS connecting via sdk.run(): %s", url)
                        self._ws_client.run()
                        log.info("WS closed; retrying in %.1fs", backoff)
                        status_log.warning("WS closed; retrying in %.1fs", backoff)
                except Exception as e:
                    # 断线或异常：仅报 INFO，并进行指数退避重连
                    try:
                        emsg = str(e)
                    except Exception:
                        emsg = "error"
                    log.info("WS disconnected: %s; retrying in %.1fs", emsg, backoff)
                    status_log.warning("WS disconnected: %s; retrying in %.1fs", emsg, backoff)
                finally:
                    time.sleep(backoff)
                    backoff = min(5.0, backoff * 2)

        self._ws_thread = threading.Thread(target=_runner, name="lighter-ws", daemon=True)
        self._ws_thread.start()
        self._ws_enabled = True

    def consume_account_dirty(self) -> bool:
        if self._ws_account_dirty:
            self._ws_account_dirty = False
            return True
        return False

    def get_last_price_source(self) -> str:
        return self._last_price_source

    def get_last_tob_source(self) -> str:
        return self._last_tob_source

    # 简化：无需对外暴露详细 WS 诊断

    # --- 内部：根据环境构建 Configuration 并尝试注入代理/校验选项 ---
    def _build_configuration(self):
        cfg = lighter.Configuration(host=self._host)
        # 代理（若 SDK 支持这些字段则生效）
        try:
            if self._proxy_url:
                for attr in ("proxy", "proxies", "http_proxy", "https_proxy"):
                    if hasattr(cfg, attr):
                        try:
                            setattr(cfg, attr, self._proxy_url)
                        except Exception:
                            pass
            if self._no_proxy and hasattr(cfg, "no_proxy"):
                try:
                    setattr(cfg, "no_proxy", self._no_proxy)
                except Exception:
                    pass
            if self._verify_ssl is not None and hasattr(cfg, "verify_ssl"):
                try:
                    setattr(cfg, "verify_ssl", bool(self._verify_ssl))
                except Exception:
                    pass
        except Exception:
            pass
        return cfg

    def _resolve_proxy(self) -> str | None:
        # 根据 host 协议与常见环境变量推导代理
        host = str(self._host or "")
        is_https = host.startswith("https://")
        # 优先 HTTPS_PROXY/HTTP_PROXY，再 ALL_PROXY
        https_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        http_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
        all_proxy = os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")
        if is_https and https_proxy:
            return https_proxy
        if (not is_https) and http_proxy:
            return http_proxy
        return https_proxy or http_proxy or all_proxy


if __name__ == "__main__":
    # 最简本地测试（打印中间价 + 可选 open orders/position）。运行：python sdk_client.py
    from config import Config

    print(f"[env] python={sys.executable}")

    cfg = Config.load("config.json")
    host = getattr(cfg, "api_host", "https://testnet.zklighter.elliot.ai")
    market_id = getattr(cfg, "market_id", None)
    if market_id is None:
        # 若未配置，尝试从 market_id_map 里用 symbol 查找；否则用 0（与示例一致）
        sym = getattr(cfg, "symbol", None)
        mm = getattr(cfg, "market_id_map", {}) or {}
        market_id = (mm.get(sym) if isinstance(mm, dict) else None) if sym else None
        if market_id is None:
            market_id = 0

    print(f"[sdk test] host={host} market_id={market_id}")

    sdk = get_sdk_client(
        api_host=host,
        account_index=getattr(cfg, "account_index", None),
        api_key_index=getattr(cfg, "api_key_index", None),
        api_key_private_key=getattr(cfg, "api_key_private_key", None),
    )

    try:
        mid = sdk.get_price_mid(market_id=int(market_id))
        print(f"mid price (market_id={market_id}):", mid)
    except Exception as e:
        print("mid price failed:", e)
    
    # 如果配置了 account_index，则尝试查询未成交订单与仓位
    acc_idx = getattr(cfg, "account_index", None)
    if acc_idx is not None:
        try:
            oo = sdk.get_open_orders(account_index=int(acc_idx), market_id=int(market_id))
            if isinstance(oo, list):
                print(f"open_orders: {len(oo)} items")
            elif isinstance(oo, dict) and "items" in oo:
                print(f"open_orders: {len(oo['items'])} items")
            else:
                print("open_orders:", oo)
        except Exception as e:
            print("get_open_orders failed:", e)

        try:
            pos = sdk.get_positions(account_index=int(acc_idx), market_id=int(market_id))
            print("position:", pos)
        except Exception as e:
            print("get_positions failed:", e)

    # 关闭内部会话，防止 aiohttp 未关闭的警告
    try:
        sdk.close()
    except Exception:
        pass

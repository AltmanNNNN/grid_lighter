Lighter Simple Reactive Grid (One-in-One-Out)

本项目实现了一个极简的“网格做空”策略，核心是“一吃一补”的被动挂单逻辑：
- 启动时生成网格，离当前价格最近的一档不挂（称为“预挂单价”），其余价位全部挂上（上方挂卖、下方挂买）。
- 每当仓位发生变化（通过 WebSocket 推送），推断被吃价，并在“旧预挂单价”挂入 1 笔，然后将“预挂单价”更新为被吃价，保证“吃一个补一个”。
- 自动处理同侧同价的重复挂单（保留最早一条，撤掉其余），避免状态膨胀。
- 避免穿价（会被立即成交）的下单，会延后到下一轮；失败的下单进入重试队列，最终补齐覆盖。

--

功能模块

- strategy.py: 策略主体（唯一执行路径为“简单一吃一补”）。
  - 预挂单价机制：最近档不挂，吃单后预挂单价与被吃价轮换。
  - WS 推送触发：账户仓位变更即刻触发一次 `_tick()`，不依赖固定轮询。
  - 重复挂单清理：同侧同价只保留 `order_index` 最小的一条。
  - 安全与重试：避免穿价；下单失败会进入重试队列。
- sdk_client.py: 极简版 lighter 同步客户端封装（REST + 可选 WS + 签名下单）。
  - 提供价格、盘口、未成交订单、仓位、下单/撤单、设置杠杆、全撤等能力。
- grid.py: 网格生成工具。
  - 支持按 `grid_count` 等分或 `grid_step` 步长生成升序网格价位。
- config.py: 配置加载与数据结构。
- main.py: 启动脚本（读取 .env、加载配置、初始化 SDK 与策略后启动）。

--

安装与运行

- 依赖环境
  - Python 3.10+
  - 通过 `requirements.txt` 安装 lighter SDK 及依赖

- 克隆与环境
  - 建议使用虚拟环境（venv/conda 等），并在其中安装依赖。
  - macos举例：
    生成虚拟环境：python3 -m venv .venv
    激活虚拟环境：source .venv/bin/activate
    安装依赖：pip install -r requirements.txt

  - 代理（HTTP/SOCKS5）
    - HTTP 代理：在 `.env` 或环境变量中设置 `HTTPS_PROXY=http://127.0.0.1:7890`（或 `HTTP_PROXY`）。
    - SOCKS5 代理（含 WebSocket）：设置 `ALL_PROXY=socks5h://127.0.0.1:7891`（或将 `HTTPS_PROXY` 直接设为 `socks5h://...`）。
    - 说明：本项目会把 `HTTPS_PROXY/HTTP_PROXY/ALL_PROXY` 解析成 `proxy` 传给 lighter 的 `WsClient`；
      若使用 SOCKS5，请确保已安装 `PySocks`（已在 `requirements.txt` 中包含）。


- 准备 .env（示例）
  - 在项目根目录创建 `.env`，填入以下必要变量（请使用你的真实参数）：
    - LIGHTER_ACCOUNT_INDEX=1
    - LIGHTER_API_KEY_INDEX=0
    - LIGHTER_API_KEY_PRIVATE_KEY=0x...
    - 可选代理与 TLS：HTTPS_PROXY=http://127.0.0.1:7890、LIGHTER_VERIFY_SSL=true|false、NO_PROXY=...

- 配置 config.json
  - 关键字段：
    - symbol: 市场符号（例如 "ENA"），或直接提供 market_id
    - market_id_map: { "ENA": 29 } 这样的映射（若未直接给 market_id）
    - api_host: API 地址（主网/测试网）
    - lower_price/upper_price + grid_count 或 grid_step: 网格区间与密度
    - entry_order_size: 每笔下单的基础数量（浮点，内部会转换为整数 base 单位）
    - post_only: 是否使用 POST_ONLY（建议 true）
    - time_in_force: 默认 GTC
    - leverage/margin_mode: 杠杆与保证金模式（可选）
    - use_ws: 建议开启（true），用于价格/账户变更推送
    - ws_stale_ms: WS 数据过期阈值（毫秒）
    - state_file: 网格状态持久化文件路径（如 grid_state.json）
    - max_new_orders_per_tick: 简单限速（每分钟上限，策略内部按窗口控制）
    - log_level/log_file: 日志配置（如 INFO / run.log）

- 运行
  - 命令：python main.py
  - 首次启动：
    - 读取精度与网格，初始化折算；
    - 拉取一次未成交订单做重复去重；
    - 拉取一次账户仓位保存首拍快照；
    - 计算预挂单价并补齐“除预挂单价外”的所有缺失挂单。
  - 运行期间：
    - WS 仓位更新会即刻触发 `_tick()`，完成“一吃一补”并打印关键日志。

--

关键日志

- 初始仓位快照: Initial position snapshot: sign=... size=... avg_entry=...
- 预挂单价: SimpleMode: initial pre_base=...
- 仓位变化: Position changed: sign=... size=... avg_entry=...
- 吃单推断: Fill inference: side=... eaten_guess_base=... | nearest_base=...
- 一吃一补: SimpleMode: placed BUY/SELL @ base_price=...; pre -> ...
- 撤重: SimpleMode: canceled duplicate orders: N
- 延后说明: SimpleMode: defer ... / WS tick skipped (simple): open orders unavailable

--

运行原理（简述）

- 预挂单价：始终“最近档不挂”。吃单发生时，在“旧预挂单价”挂 1 笔，然后把“预挂单价”更新为被吃价。
- 轨迹稳定：每次吃单都会有对应的补单，理论上挂单总量（除预挂单价外）保持不变。
- 保护与鲁棒性：
  - 避免穿价：若价格将跨越 best bid/ask，推迟到下一轮挂单。
  - 失败重试：下单失败加入重试队列，后续继续尝试直至成功。
  - 自动撤重：同侧同价多笔只保留一笔，防止异常膨胀。

--

常见问题（FAQ）

- 启动很慢、网格很多时，中途有吃单怎么办？
  - 当前一轮持锁期间的吃单，会在本轮结束后、下一轮立即“一吃一补”。如需更及时，可将 max_new_orders_per_tick 调小，缩短单轮挂单时长。

- 如何切换市场？
  - 在 config.json 设置 symbol 并在 market_id_map 中提供映射，或直接设置 market_id。

- 如何查看日志？
  - 默认输出到控制台与 run.log（若 log_file 配置）。可调整 log_level 为 DEBUG 获取更详细信息。

--

目录结构（简要）

- main.py: 程序入口
- strategy.py: 策略主体（简化模式）
- sdk_client.py: 同步封装的 lighter SDK 客户端
- grid.py: 网格生成工具
- config.py: 配置装载
- config.json: 运行配置
- grid_state.json: 网格/精度持久化（自动生成）
- .env: 私钥/账号等敏感配置（本地）
- run.log: 运行日志（可选）

--

注意事项

- 请妥善保管私钥，避免将 .env 与 run.log 提交到版本库。
- lighter SDK 的接口/模型可能会有版本差异，若遇到不兼容错误，请根据日志提示调整配置或升级依赖。
- 本策略默认使用 WS 推送来触发快速响应，请确保网络与代理（若有）可用。

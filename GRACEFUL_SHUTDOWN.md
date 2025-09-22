# 优雅退出功能说明

## 问题描述

原程序在接收到退出信号（Ctrl+C 或 SIGTERM）时，会反复打印 "Signal received, stopping..." 日志，无法正常退出。

## 解决方案

### 1. 信号处理改进

- **防止重复处理**: 添加 `_shutdown_initiated` 标志，避免重复执行信号处理逻辑
- **强制退出机制**: 如果再次收到信号，直接调用 `os._exit(1)` 强制退出
- **详细日志**: 改善日志输出，显示具体的信号编号和退出阶段

### 2. 主循环优化

- **响应性改进**: 将长时间的 `time.sleep()` 分割成小块，提高对停止信号的响应速度
- **精确计时**: 使用时间戳控制 tick 执行频率，而不是依赖固定的睡眠间隔
- **锁管理**: 改进处理锁的获取和释放逻辑

### 3. WebSocket 线程管理

- **优雅退出**: WebSocket 工作线程在主循环结束时自动退出
- **超时等待**: 主线程等待 WebSocket 线程退出，最多等待3秒
- **退出日志**: 线程退出时打印确认日志

### 4. 关闭序列

完整的关闭序列包括：
1. 设置 `running = False` 停止主循环
2. 等待 WebSocket 线程退出
3. 可选：取消所有挂单（根据配置）
4. 关闭 SDK 连接
5. 打印完成日志

### 5. 超时保护

在 `main.py` 中添加了超时保护机制：
- 默认30秒超时（可通过配置 `shutdown_timeout_sec` 调整）
- 如果程序在超时时间内无法正常退出，将强制终止

## 配置选项

在 `config.json` 中可以添加以下配置：

```json
{
  "shutdown_timeout_sec": 30,
  "cancel_all_on_exit": false
}
```

- `shutdown_timeout_sec`: 强制退出超时时间（秒）
- `cancel_all_on_exit`: 是否在退出时取消所有挂单

## 测试

可以使用以下命令测试退出功能：

```bash
# 运行测试脚本
python test_graceful_shutdown.py

# 等待几秒后按 Ctrl+C，观察是否优雅退出
```

## 预期行为

1. 按下 Ctrl+C 后，程序应该立即开始退出流程
2. 显示 "Signal X received, initiating graceful shutdown..." 日志
3. 显示 "Starting shutdown sequence..." 日志
4. 等待 WebSocket 线程退出
5. 关闭 SDK 连接
6. 显示 "Shutdown sequence completed" 日志
7. 程序正常退出，不再打印重复日志

## 故障排除

如果程序仍然无法正常退出：

1. 检查是否有其他代码阻塞了退出流程
2. 增加调试日志查看卡住的位置
3. 减少 `shutdown_timeout_sec` 值以更快强制退出
4. 检查是否有非守护线程阻止程序退出

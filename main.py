import logging
import os
import signal
import threading
import time
from typing import Optional


def setup_logging(level: str, log_file: Optional[str] = None) -> None:
    # 清理已有 handler，避免第三方包提前设置的 DEBUG 干扰
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(getattr(logging, (level or "INFO").upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if log_file:
        try:
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except Exception:
            pass
    # 降低 lighter 相关日志噪音
    logging.getLogger("lighter").setLevel(logging.WARNING)
    # 静音 websockets 的 keepalive 错误，只保留我们自定义的 INFO 提示
    logging.getLogger("websockets").setLevel(logging.CRITICAL)
    logging.getLogger("websockets.client").setLevel(logging.CRITICAL)
    logging.getLogger("websockets.sync.client").setLevel(logging.CRITICAL)


def _load_dotenv(path: str = ".env") -> None:
    try:
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if "=" not in s:
                    continue
                k, v = s.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                # Do not override already-set env vars
                if k and (os.environ.get(k) is None):
                    os.environ[k] = v
    except Exception:
        pass


def _to_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def main() -> None:
    from config import Config
    from sdk_client import get_sdk_client
    from strategy import ShortGridStrategy

    # Load .env first for secrets
    _load_dotenv()

    cfg = Config.load("config.json")
    setup_logging(getattr(cfg, "log_level", "INFO"), getattr(cfg, "log_file", "run.log"))

    # 从 .env/环境变量读取账号与私钥（必须）
    pk_env = os.environ.get("LIGHTER_API_KEY_PRIVATE_KEY")
    acc_env = _to_int(os.environ.get("LIGHTER_ACCOUNT_INDEX"))
    key_idx_env = _to_int(os.environ.get("LIGHTER_API_KEY_INDEX"))

    if acc_env is None:
        raise RuntimeError("LIGHTER_ACCOUNT_INDEX 未设置，请在 .env 或环境变量中配置该值")
    if key_idx_env is None:
        raise RuntimeError("LIGHTER_API_KEY_INDEX 未设置，请在 .env 或环境变量中配置该值")
    if not pk_env:
        raise RuntimeError("LIGHTER_API_KEY_PRIVATE_KEY 未设置，请在 .env 或环境变量中配置该值")

    sdk = get_sdk_client(
        api_host=getattr(cfg, "api_host", None),
        account_index=acc_env,
        api_key_index=key_idx_env,
        api_key_private_key=pk_env,
    )

    logging.getLogger(__name__).info(
        "启动做空网格: symbol=%s host=%s", getattr(cfg, "symbol", None), getattr(cfg, "api_host", None)
    )

    strategy = ShortGridStrategy(cfg=cfg, sdk=sdk)
    
    # 设置一个超时机制，防止程序无法正常退出
    shutdown_timeout = getattr(cfg, "shutdown_timeout_sec", 30)  # 默认30秒超时
    
    def force_exit_after_timeout():
        """在超时后强制退出程序"""
        time.sleep(shutdown_timeout)
        if strategy.running:
            logging.getLogger(__name__).error(
                "Program failed to exit gracefully within %d seconds, forcing exit...", 
                shutdown_timeout
            )
            os._exit(1)
    
    # 启动超时线程（守护线程）
    timeout_thread = threading.Thread(target=force_exit_after_timeout, daemon=True)
    timeout_thread.start()
    
    try:
        strategy.start()
    finally:
        # 确保程序最终退出
        logging.getLogger(__name__).info("Main strategy loop completed")
        if strategy.running:
            strategy.stop()
        logging.getLogger(__name__).info("Program exiting normally")


if __name__ == "__main__":
    main()

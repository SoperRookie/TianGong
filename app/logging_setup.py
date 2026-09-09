"""统一日志（Loguru）：控制台 + 按日滚动文件，并接管 uvicorn/标准库日志。

- 控制台：人类可读彩色格式，级别由 TIANGONG_LOG_LEVEL 控制（默认 INFO）。
- 文件：logs/tiangong_YYYY-MM-DD.log，DEBUG 全量，每日零点滚动，保留 14 天。
- uvicorn / fastapi 等标准库 logging 输出统一路由进 Loguru，格式一致。
"""

import atexit
import inspect
import logging
import sys
from pathlib import Path

from loguru import logger

_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <7}</level> | "
    "<cyan>{name}</cyan>:<cyan>{line}</cyan> - {message}"
)


class InterceptHandler(logging.Handler):
    """把标准库 logging 记录转发给 Loguru，保留原级别与调用位置。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


_file_handler: dict[str, int | None] = {"id": None}


def close_file_handler() -> None:
    """清空日志队列并移除文件 handler（释放 multiprocessing 信号量）；控制台输出保留。幂等。"""
    hid = _file_handler.get("id")
    if hid is None:
        return
    _file_handler["id"] = None
    try:
        logger.complete()
        logger.remove(hid)
    except Exception:
        pass


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    logger.remove()
    logger.add(sys.stderr, level=level.upper(), format=_FORMAT)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        _file_handler["id"] = logger.add(
            log_dir / "tiangong_{time:YYYY-MM-DD}.log",
            level="DEBUG",
            format=_FORMAT,
            rotation="00:00",
            retention="14 days",
            encoding="utf-8",
            enqueue=True,  # 后台线程写文件，异步任务下不阻塞事件循环
        )
        # enqueue 队列底层是 8 个 multiprocessing 信号量。uvicorn --reload 的服务子进程在收到 SIGTERM 后
        # 会在服务结束时重新抛出信号自杀，不执行 atexit / finalizer，每重载一次就向资源追踪器留下
        # 8 个「leaked semaphore」告警；因此文件 handler 在 lifespan 关闭阶段显式关闭（见 app/main.py），
        # atexit 兜底覆盖非 reload 的正常退出。
        atexit.register(close_file_handler)

    # 接管标准库与 uvicorn 的日志，统一走 Loguru。
    # 阈值取 INFO：openai/httpx 等三方库的 DEBUG 会携带完整请求体（含 base64 图片），
    # 曾把日志文件刷出 MB 级单行；应用自身经 loguru 记录的 DEBUG 不受此影响
    logging.basicConfig(handlers=[InterceptHandler()], level=logging.INFO, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        std_logger = logging.getLogger(name)
        std_logger.handlers = [InterceptHandler()]
        std_logger.propagate = False

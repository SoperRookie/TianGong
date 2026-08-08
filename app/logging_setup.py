"""统一日志（Loguru）：控制台 + 按日滚动文件，并接管 uvicorn/标准库日志。

- 控制台：人类可读彩色格式，级别由 TIANGONG_LOG_LEVEL 控制（默认 INFO）。
- 文件：logs/tiangong_YYYY-MM-DD.log，DEBUG 全量，每日零点滚动，保留 14 天。
- uvicorn / fastapi 等标准库 logging 输出统一路由进 Loguru，格式一致。
"""

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


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    logger.remove()
    logger.add(sys.stderr, level=level.upper(), format=_FORMAT)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_dir / "tiangong_{time:YYYY-MM-DD}.log",
            level="DEBUG",
            format=_FORMAT,
            rotation="00:00",
            retention="14 days",
            encoding="utf-8",
            enqueue=True,  # 后台线程写文件，异步任务下不阻塞事件循环
        )

    # 接管标准库与 uvicorn 的日志，统一走 Loguru。
    # 阈值取 INFO：openai/httpx 等三方库的 DEBUG 会携带完整请求体（含 base64 图片），
    # 曾把日志文件刷出 MB 级单行；应用自身经 loguru 记录的 DEBUG 不受此影响
    logging.basicConfig(handlers=[InterceptHandler()], level=logging.INFO, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        std_logger = logging.getLogger(name)
        std_logger.handlers = [InterceptHandler()]
        std_logger.propagate = False

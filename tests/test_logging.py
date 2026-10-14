"""文件日志轮转与保留：每日零点或超过大小上限即轮转，轮转后 gzip；控制台可关闭。"""

import gzip
import sys

from loguru import logger

from app.logging_setup import DailyOrSizeRotator, close_file_handler, setup_logging


def test_按大小轮转并压缩(tmp_path):
    setup_logging("INFO", tmp_path, max_mb=1, retention_days=7, compress=True, console=False)
    try:
        line = "x" * 10000
        for _ in range(120):  # 约 1.2MB，超过 1MB 上限
            logger.info(line)
        logger.complete()
    finally:
        close_file_handler()
    files = sorted(tmp_path.iterdir())
    assert any(f.suffix == ".gz" for f in files), [f.name for f in files]  # 轮转出的旧文件已压缩
    live = [f for f in files if f.suffix == ".log"]
    assert len(live) == 1 and live[0].stat().st_size < 1024 * 1024
    with gzip.open(next(f for f in files if f.suffix == ".gz"), "rt", encoding="utf-8") as fh:
        assert "xxxx" in fh.readline()


def test_跨日轮转判定():
    import io
    from datetime import date

    class Msg(str):
        pass

    def msg(d):
        m = Msg("line\n")
        m.record = {"time": type("T", (), {"date": staticmethod(lambda: d)})()}
        return m

    rot = DailyOrSizeRotator(max_bytes=0)
    buf = io.StringIO()
    assert rot(msg(date(2026, 10, 14)), buf) is False
    assert rot(msg(date(2026, 10, 14)), buf) is False
    assert rot(msg(date(2026, 10, 15)), buf) is True  # 日期变了


def test_文件级别与控制台开关(tmp_path, capsys):
    setup_logging("INFO", tmp_path, file_level="WARNING", console=False)
    try:
        logger.info("info 不进文件")
        logger.warning("warning 进文件")
        logger.complete()
    finally:
        close_file_handler()
    text = "".join(f.read_text(encoding="utf-8") for f in tmp_path.glob("*.log"))
    assert "warning 进文件" in text and "info 不进文件" not in text
    assert "进文件" not in capsys.readouterr().err  # 控制台已关
    # 还原测试进程的日志配置（conftest 已设日志目录环境变量，这里只恢复控制台）
    logger.remove()
    logger.add(sys.stderr, level="INFO")

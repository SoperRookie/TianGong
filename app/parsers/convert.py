"""老版 Office / WPS / OpenDocument / 矢量图 → 现代格式转换（LibreOffice 无头模式）。

.doc .wps .rtf .odt … → .docx；.xls .et .ods … → .xlsx；.ppt .dps .odp … → .pptx；.eps .ai .svg .emf .wmf → .png / .pdf。
服务器需安装 LibreOffice（Ubuntu：apt install libreoffice-writer libreoffice-calc libreoffice-impress fonts-noto-cjk）；
未安装时给出明确提示，文件仍保留可下载。
"""

import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

from loguru import logger

from app.parsers.base import ParsedDocument, UnsupportedFormatError

# 源后缀 → 转换目标（LibreOffice 过滤器名 / 目标后缀）
OFFICE_CONVERT: dict[str, str] = {
    # 文档
    ".doc": "docx", ".docm": "docx", ".dot": "docx", ".dotx": "docx", ".dotm": "docx",
    ".rtf": "docx", ".odt": "docx", ".wps": "docx", ".wpt": "docx",
    # 表格
    ".xls": "xlsx", ".xlsb": "xlsx", ".xlt": "xlsx", ".xltx": "xlsx", ".xltm": "xlsx", ".xlam": "xlsx",
    ".ods": "xlsx", ".et": "xlsx", ".ett": "xlsx",
    # 演示
    ".ppt": "pptx", ".pptm": "pptx", ".pps": "pptx", ".ppsx": "pptx", ".ppsm": "pptx",
    ".pot": "pptx", ".potx": "pptx", ".potm": "pptx", ".sldx": "pptx", ".sldm": "pptx",
    ".odp": "pptx", ".dps": "pptx", ".dpt": "pptx",
    # 矢量 / 设计源文件：转 PDF 后按 PDF（含内嵌图）解析；纯图形的走 Vision
    ".eps": "pdf", ".ai": "pdf",
}
VECTOR_TO_PNG = (".svg", ".emf", ".wmf")

_SOFFICE_CANDIDATES = (
    "soffice", "libreoffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/opt/libreoffice/program/soffice", "/usr/lib/libreoffice/program/soffice",
)


class ConversionUnavailableError(UnsupportedFormatError):
    """LibreOffice 未安装或转换失败：文件保留可下载，提示转成现代格式后重传。"""


def soffice_path() -> str | None:
    for c in _SOFFICE_CANDIDATES:
        found = shutil.which(c) if not c.startswith("/") else (c if os.access(c, os.X_OK) else None)
        if found:
            return found
    return None


# 按文件头识别「扩展名是老格式、内容其实不是」的文件：网页 / Confluence / WPS「导出为 Word」常给出 .doc 后缀的 HTML、
# MHTML，或把 docx 直接改名为 .doc。这类文件交给 LibreOffice 会按网页加载并逐个拉取外链图片，内网机器无外网时
# 每个链接都要等到网络超时，整体轻易超过转换时限；这里先识别出来走本地解析器，根本不启动 LibreOffice。
_ZIP_TO_MODERN = {"docx": ".docx", "xlsx": ".xlsx", "pptx": ".pptx"}


def sniff_real_format(path: Path) -> str:
    """返回 ole（真正的老版二进制）/ zip（改名的现代 Office）/ html / mht / rtf / text / unknown。"""
    with open(path, "rb") as f:
        head = f.read(8192)
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "ole"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.lstrip().startswith(b"{\\rtf"):
        return "rtf"
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = head.decode("utf-16", errors="ignore")
    else:
        text = head.decode("utf-8", errors="ignore")  # 标签都是 ASCII，GBK 正文按 ignore 丢掉即可
    low = text.lstrip("\ufeff \t\r\n").lower()
    if low.startswith("mime-version:") or ("content-type: multipart/related" in low[:2048]):
        return "mht"
    if low.startswith("<") and any(tag in low for tag in ("<html", "<!doctype html", "<body", "<head", "<meta ", "<table", "<div", "<p>", "<p ")):
        return "html"
    if "\x00" not in text[:4096] and any(ch.isprintable() for ch in text[:200]):
        return "text"
    return "unknown"


def convert(path: str | Path, target: str, timeout: float | None = None) -> Path:
    """用 LibreOffice 把 path 转成 target 后缀，返回转换后文件（放在临时目录，调用方用后可不清理）。

    超时后强杀整个进程组（soffice 启动脚本 → oosplash → soffice.bin 三层），不留后台残留进程。
    """
    from app.config import get_settings

    path = Path(path)
    if timeout is None:
        timeout = float(get_settings().office_convert_timeout)
    exe = soffice_path()
    if exe is None:
        raise ConversionUnavailableError(
            f"{path.name} 是 {path.suffix} 格式，需要服务器安装 LibreOffice 才能自动读取"
            "（apt install libreoffice-writer libreoffice-calc libreoffice-impress）；"
            "文件已保留可下载，也可另存为 docx / xlsx / pptx / pdf 后重新上传"
        )
    outdir = Path(tempfile.mkdtemp(prefix="tg-convert-"))
    profile = Path(tempfile.mkdtemp(prefix="tg-lo-profile-"))  # 独立 profile：并发转换互不阻塞
    cmd = [exe, "--headless", "--norestore", "--nologo", "--nolockcheck", f"-env:UserInstallation=file://{profile}",
           "--convert-to", target, "--outdir", str(outdir), str(path)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True, stdin=subprocess.DEVNULL)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        logger.warning("LibreOffice 转换超时被强杀：{} → {}（{} 秒）", path.name, target, int(timeout))
        raise ConversionUnavailableError(
            f"{path.name} 转换超时（{int(timeout)} 秒）。常见原因：文件是网页导出的「伪 Word」且含外链图片、文件损坏、"
            "或服务器 LibreOffice 首次启动过慢；请另存为 docx / pdf 后重传，或联系管理员在服务器上手工执行 "
            f"soffice --headless --convert-to {target.split(':')[0]} 排查"
        ) from None
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    outputs = list(outdir.glob(f"*.{target.split(':')[0]}"))
    if proc.returncode != 0 or not outputs:
        msg = (err or out or "").strip()[-300:]
        raise ConversionUnavailableError(f"{path.name} 转换失败：{msg or '未生成输出文件'}；可另存为现代格式后重传")
    return outputs[0]


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


class OfficeConvertParser:
    """老版 / 兼容格式：先转换再交给对应的现代格式解析器。"""

    suffixes = tuple(OFFICE_CONVERT)

    def parse(self, path: Path) -> ParsedDocument:
        from app.parsers.base import parse_file

        target = OFFICE_CONVERT[path.suffix.lower()]
        kind = sniff_real_format(path)
        if kind in ("zip", "html", "mht", "text") and target in _ZIP_TO_MODERN:
            # 后缀是老格式、内容不是：按真实内容走本地解析器，不启动 LibreOffice
            real_suffix = {"zip": _ZIP_TO_MODERN[target], "html": ".html", "mht": ".mht", "text": ".txt"}[kind]
            tmpdir = Path(tempfile.mkdtemp(prefix="tg-sniff-"))
            alias = tmpdir / (path.stem + real_suffix)
            try:
                shutil.copyfile(path, alias)
                logger.info("{} 按文件头识别为 {}，直接解析不经 LibreOffice", path.name, real_suffix)
                doc = parse_file(alias)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            converted = convert(path, target)
            try:
                doc = parse_file(converted)
            finally:
                shutil.rmtree(converted.parent, ignore_errors=True)
        doc.source = path.name
        doc.doc_type = path.suffix.lstrip(".").lower()
        return doc


def rasterize_vector(path: str | Path) -> bytes:
    """svg / emf / wmf → PNG 字节（LibreOffice Draw），供 Vision 理解。"""
    out = convert(path, "png")
    try:
        return out.read_bytes()
    finally:
        shutil.rmtree(out.parent, ignore_errors=True)

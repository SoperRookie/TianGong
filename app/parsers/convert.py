"""老版 Office / WPS / OpenDocument / 矢量图 → 现代格式转换（LibreOffice 无头模式）。

.doc .wps .rtf .odt … → .docx；.xls .et .ods … → .xlsx；.ppt .dps .odp … → .pptx；.eps .ai .svg .emf .wmf → .png / .pdf。
服务器需安装 LibreOffice（Ubuntu：apt install libreoffice-writer libreoffice-calc libreoffice-impress fonts-noto-cjk）；
未安装时给出明确提示，文件仍保留可下载。
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

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


def convert(path: str | Path, target: str, timeout: float = 180.0) -> Path:
    """用 LibreOffice 把 path 转成 target 后缀，返回转换后文件（放在临时目录，调用方用后可不清理）。"""
    path = Path(path)
    exe = soffice_path()
    if exe is None:
        raise ConversionUnavailableError(
            f"{path.name} 是 {path.suffix} 格式，需要服务器安装 LibreOffice 才能自动读取"
            "（apt install libreoffice-writer libreoffice-calc libreoffice-impress）；"
            "文件已保留可下载，也可另存为 docx / xlsx / pptx / pdf 后重新上传"
        )
    outdir = Path(tempfile.mkdtemp(prefix="tg-convert-"))
    profile = Path(tempfile.mkdtemp(prefix="tg-lo-profile-"))  # 独立 profile：并发转换互不阻塞
    cmd = [exe, "--headless", "--norestore", "--nologo", f"-env:UserInstallation=file://{profile}",
           "--convert-to", target, "--outdir", str(outdir), str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ConversionUnavailableError(f"{path.name} 转换超时（{int(timeout)} 秒），请另存为现代格式后重传") from None
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    outputs = list(outdir.glob(f"*.{target.split(':')[0]}"))
    if proc.returncode != 0 or not outputs:
        err = (proc.stderr or proc.stdout or "").strip()[-300:]
        raise ConversionUnavailableError(f"{path.name} 转换失败：{err or '未生成输出文件'}；可另存为现代格式后重传")
    return outputs[0]


class OfficeConvertParser:
    """老版 / 兼容格式：先转换再交给对应的现代格式解析器。"""

    suffixes = tuple(OFFICE_CONVERT)

    def parse(self, path: Path) -> ParsedDocument:
        from app.parsers.base import parse_file

        converted = convert(path, OFFICE_CONVERT[path.suffix.lower()])
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

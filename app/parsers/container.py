"""容器类文件：压缩包（zip / tar / gz / bz2 / xz / 7z / rar）与邮件（eml / msg）。

递归解析内部成员：文档按各自解析器读取，图片作为内嵌图片占位由 Vision 理解，视频 / 字体等只记录名称。
解压受总大小与路径穿越校验约束（不限制文件数）。
"""

import email
import shutil
import tarfile
import tempfile
import zipfile
from email import policy
from pathlib import Path

from app.parsers.base import EmbeddedImage, ParsedDocument, Section, UnsafeFileError, UnsupportedFormatError

MAX_DEPTH = 2
_SKIP_PREFIX = ("__MACOSX/", ".", "~$")
_NOISE = {"Thumbs.db", ".DS_Store", "desktop.ini"}


def _limit_bytes() -> int:
    from app.config import get_settings

    return get_settings().max_zip_uncompressed_mb * 1024 * 1024


def _safe_member(name: str) -> bool:
    p = Path(name)
    if name.startswith(_SKIP_PREFIX) or p.name in _NOISE or p.name.startswith("._"):
        return False
    return not (p.is_absolute() or ".." in p.parts)


def zip_member_name(info: zipfile.ZipInfo) -> str:
    """zip 条目的真实文件名。没有 UTF-8 标志位（0x800）的条目 Python 一律按 cp437 解码，Windows 资源管理器 / Axure /
    WinRAR 打出来的中文名就成了「µ┤╗σè¿」这类乱码；后果不只是难看：Axure 站点地图里的页面 url 对不上包内文件，页面名退回
    乱码、每页 data.js 的说明备注全部找不到。按原字节依次试 utf-8、gb18030 还原，都不行才保留原样。"""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return info.filename


def _extract(path: Path, dest: Path) -> list[Path]:
    """解压到 dest，返回成员文件列表（按名称排序）；超限 / 危险路径直接拒绝。"""
    suffix = path.suffix.lower()
    limit = _limit_bytes()
    total = 0
    out: list[Path] = []
    if suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            infos = [(i, zip_member_name(i)) for i in zf.infolist() if not i.is_dir()]
            for i, name in infos:
                if not _safe_member(name):
                    continue
                total += i.file_size
                if total > limit:
                    raise UnsafeFileError(f"{path.name} 解压后超过 {limit // (1024 * 1024)}MB 上限")
                target = dest / name   # 不用 zf.extract：它会按乱码名落盘
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(i) as src, target.open("wb") as w:
                    shutil.copyfileobj(src, w, 1024 * 1024)
                out.append(target)
    elif suffix in (".tar", ".gz", ".tgz", ".bz2", ".xz", ".tbz2", ".txz"):
        try:
            tf = tarfile.open(path)
        except tarfile.ReadError:
            if suffix in (".gz", ".bz2", ".xz"):  # 单文件压缩（非 tar）
                import bz2
                import gzip
                import lzma

                opener = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open}[suffix]
                inner = dest / path.with_suffix("").name
                with opener(path) as fh, inner.open("wb") as w:
                    while chunk := fh.read(1024 * 1024):
                        total += len(chunk)
                        if total > limit:
                            raise UnsafeFileError(f"{path.name} 解压后超过 {limit // (1024 * 1024)}MB 上限")
                        w.write(chunk)
                return [inner]
            raise UnsupportedFormatError(f"{path.name} 不是有效的压缩文件") from None
        with tf:
            members = [m for m in tf.getmembers() if m.isfile() and _safe_member(m.name)]
            for m in members:
                total += m.size
                if total > limit:
                    raise UnsafeFileError(f"{path.name} 解压后超过 {limit // (1024 * 1024)}MB 上限")
                tf.extract(m, dest, filter="data")
                out.append(dest / m.name)
    elif suffix == ".7z":
        try:
            import py7zr
        except ImportError:
            raise UnsupportedFormatError("读取 .7z 需要安装 py7zr（pip install py7zr）") from None
        with py7zr.SevenZipFile(path) as z:
            names = [n for n in z.getnames() if _safe_member(n)]
            if sum(i.uncompressed for i in z.list() if not i.is_directory) > limit:
                raise UnsafeFileError(f"{path.name} 解压后超过 {limit // (1024 * 1024)}MB 上限")
            z.extract(dest, targets=names)
            out = [p for p in dest.rglob("*") if p.is_file()]
    elif suffix == ".rar":
        try:
            import rarfile
        except ImportError:
            raise UnsupportedFormatError("读取 .rar 需要安装 rarfile 与 unrar / bsdtar") from None
        try:
            with rarfile.RarFile(path) as rf:
                infos = [i for i in rf.infolist() if not i.is_dir() and _safe_member(i.filename)]
                if sum(i.file_size for i in infos) > limit:
                    raise UnsafeFileError(f"{path.name} 解压后超过 {limit // (1024 * 1024)}MB 上限")
                for i in infos:
                    rf.extract(i, dest)
                    out.append(dest / i.filename)
        except rarfile.RarCannotExec as e:
            raise UnsupportedFormatError(f"读取 .rar 需要服务器安装 unrar 或 libarchive-tools（bsdtar）：{e}") from None
    else:
        raise UnsupportedFormatError(f"不支持的压缩格式 {suffix}")
    return sorted(p for p in out if p.is_file())


# 网页 / 原型导出包里的脚本、样式、字体映射等资源：当文本读进去只会污染需求（模型看到的是代码），统一跳过
ASSET_SUFFIXES = (".js", ".mjs", ".cjs", ".css", ".scss", ".less", ".map", ".ts", ".tsx", ".jsx", ".vue")


def parse_members(files: list[tuple[str, Path]], source: str, depth: int) -> ParsedDocument:
    """把一组成员文件合成一份文档：每个文件一节；图片转为内嵌图片占位；不可读的只记名称。

    Axure 原型导出包整体识别后按页面提取（见 parsers.axure）；其余包内的脚本 / 样式资源只汇总数量不解析。
    """
    from app.parsers.axure import axure_root, parse_axure_export
    from app.parsers.base import parse_file
    from app.parsers.image import IMAGE_SUFFIXES, load_image_bytes

    root = axure_root(files)
    if root is not None:
        return parse_axure_export(files, source, root)
    sections: list[Section] = []
    images: list[EmbeddedImage] = []
    tables: list[list[list[str]]] = []
    assets = [name for name, p in files if p.suffix.lower() in ASSET_SUFFIXES]
    if assets:
        sections.append(Section(level=0, content=f"（{len(assets)} 个脚本 / 样式资源文件不参与解析：{'、'.join(assets[:5])}"
                                                 f"{' 等' if len(assets) > 5 else ''}）"))
    for name, p in files:
        suffix = p.suffix.lower()
        if suffix in ASSET_SUFFIXES:
            continue
        sections.append(Section(level=1, title=f"文件：{name}"))
        try:
            if suffix in IMAGE_SUFFIXES:
                data, mime = load_image_bytes(p)
                placeholder = f"[[图片:{len(images) + 1}]]"
                sections.append(Section(level=0, content=placeholder))
                images.append(EmbeddedImage(placeholder=placeholder, data=data, mime=mime))
                continue
            if suffix in CONTAINER_SUFFIXES and depth >= MAX_DEPTH:
                sections.append(Section(level=0, content="（嵌套压缩包层级过深，未展开）"))
                continue
            doc = parse_file(p, _depth=depth + 1) if suffix in CONTAINER_SUFFIXES else parse_file(p)
        except (UnsupportedFormatError, UnsafeFileError, ValueError, UnicodeDecodeError) as e:
            sections.append(Section(level=0, content=f"（未读取：{e}）"))
            continue
        # 合并：子文档的图片占位重新编号，避免与外层冲突。
        # 先算好全部旧 → 新映射再一次性替换：逐张边改边替会把刚改成 [[图片:5]] 的占位在处理子文档第 5 张图时再改一次，
        # 连锁覆盖后前面几张图的理解结果找不到占位而丢失（只剩最后几张各重复多次）
        mapping: dict[str, str] = {}
        for img in doc.embedded_images:
            new_ph = f"[[图片:{len(images) + 1}]]"
            mapping[img.placeholder] = new_ph
            images.append(EmbeddedImage(placeholder=new_ph, data=img.data, mime=img.mime))
        for s in doc.sections:
            if s.content in mapping:
                s.content = mapping[s.content]
        for s in doc.sections:
            sections.append(Section(level=min(s.level + 1, 6) if s.title else 0, title=s.title, content=s.content))
        tables.extend(doc.tables)
    return ParsedDocument(source=source, doc_type="archive", sections=sections, tables=tables, embedded_images=images)


CONTAINER_SUFFIXES = (".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".tbz2", ".txz", ".7z", ".rar")


class ArchiveParser:
    suffixes = CONTAINER_SUFFIXES

    def parse(self, path: Path, _depth: int = 0) -> ParsedDocument:
        dest = Path(tempfile.mkdtemp(prefix="tg-archive-"))
        try:
            files = _extract(path, dest)
            if not files:
                raise ValueError(f"{path.name} 是空压缩包")
            members = [(str(p.relative_to(dest)), p) for p in files]
            doc = parse_members(members, path.name, _depth)
        finally:
            shutil.rmtree(dest, ignore_errors=True)
        doc.doc_type = path.suffix.lstrip(".").lower()
        return doc


class EmailParser:
    """邮件：正文（文本优先，否则网页抽正文）+ 附件递归解析。"""

    suffixes = (".eml", ".msg")

    def parse(self, path: Path, _depth: int = 0) -> ParsedDocument:
        dest = Path(tempfile.mkdtemp(prefix="tg-mail-"))
        try:
            if path.suffix.lower() == ".msg":
                subject, meta, body, atts = self._read_msg(path, dest)
            else:
                subject, meta, body, atts = self._read_eml(path, dest)
            sections = [Section(level=1, title=f"邮件：{subject or path.name}"), Section(level=0, content=meta)]
            if body.strip():
                sections.append(Section(level=0, content=body.strip()))
            doc = ParsedDocument(source=path.name, doc_type=path.suffix.lstrip(".").lower(), sections=sections)
            if atts:
                sub = parse_members(atts, path.name, _depth)
                doc.sections.extend(sub.sections)
                doc.tables.extend(sub.tables)
                doc.embedded_images.extend(sub.embedded_images)
        finally:
            shutil.rmtree(dest, ignore_errors=True)
        return doc

    @staticmethod
    def _html_text(html: str) -> str:
        from app.parsers.link import html_to_markdown

        try:
            return html_to_markdown(html)[1]
        except Exception:
            return html

    def _read_eml(self, path: Path, dest: Path):
        msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
        meta = "\n".join(f"{k}：{msg.get(h, '')}" for k, h in (("发件人", "From"), ("收件人", "To"), ("日期", "Date")) if msg.get(h))
        body = ""
        plain = msg.get_body(preferencelist=("plain",))
        if plain is not None:
            body = plain.get_content()
        else:
            html = msg.get_body(preferencelist=("html",))
            if html is not None:
                body = self._html_text(html.get_content())
        atts: list[tuple[str, Path]] = []
        for part in msg.iter_attachments():
            name = part.get_filename() or "attachment"
            if not _safe_member(name):
                continue
            p = dest / f"{len(atts) + 1}_{Path(name).name}"
            p.write_bytes(part.get_payload(decode=True) or b"")
            atts.append((name, p))
        return msg.get("Subject", ""), meta, body, atts

    def _read_msg(self, path: Path, dest: Path):
        try:
            import extract_msg
        except ImportError:
            raise UnsupportedFormatError("读取 Outlook .msg 需要安装 extract-msg（pip install extract-msg）") from None
        with extract_msg.openMsg(str(path)) as m:
            meta = "\n".join(f"{k}：{v}" for k, v in (("发件人", m.sender), ("收件人", m.to), ("日期", m.date)) if v)
            body = m.body or ""
            if not body.strip() and getattr(m, "htmlBody", None):
                raw = m.htmlBody
                body = self._html_text(raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw)
            atts: list[tuple[str, Path]] = []
            for a in m.attachments:
                name = getattr(a, "longFilename", None) or getattr(a, "shortFilename", None) or "attachment"
                data = getattr(a, "data", None)
                if not isinstance(data, (bytes, bytearray)) or not _safe_member(name):
                    continue
                p = dest / f"{len(atts) + 1}_{Path(name).name}"
                p.write_bytes(data)
                atts.append((name, p))
            return m.subject or "", meta, body, atts

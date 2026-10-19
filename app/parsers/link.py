"""链接抓取（需求原文可能只是一个网页 / 图片 / 文档的 URL）。

把 http(s) 链接下载为本地文件后交给既有解析器：图片走 Vision，PDF / Word / 文本走对应解析器，
HTML 网页抽取正文另存为 Markdown。下载受大小与超时限制，并拒绝指向内网 / 本机 / 云元数据地址
的链接（SSRF 防护）。
"""

import html as _html
import ipaddress
import re
import socket
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

_URL_RE = re.compile(r"https?://[^\s<>\"'()（）\]\[，。；、]+", re.IGNORECASE)
_TRAILING = ".,;:!?)）】」』>》"

# Content-Type → 落盘后缀；后缀决定后续走哪个解析器
_CT_SUFFIX = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/webp": ".webp",
    "image/gif": ".gif", "image/bmp": ".bmp", "image/tiff": ".tif", "image/svg+xml": ".svg",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls", "text/csv": ".csv",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-powerpoint": ".ppt", "application/zip": ".zip", "application/json": ".json",
    "text/markdown": ".md", "text/plain": ".txt",
}
_HTML_TYPES = ("text/html", "application/xhtml+xml")
_KNOWN_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".svg", ".pdf", ".docx", ".doc",
                   ".xlsx", ".xls", ".csv", ".pptx", ".ppt", ".md", ".markdown", ".txt", ".json", ".xml", ".zip")


class LinkFetchError(ValueError):
    pass


def extract_urls(text: str) -> list[str]:
    """从任意文本里提取 http(s) 链接，去掉尾随标点，按出现顺序去重。"""
    seen: list[str] = []
    for m in _URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(_TRAILING)
        if url and url not in seen:
            seen.append(url)
    return seen


def _assert_public_host(host: str) -> None:
    """拒绝内网 / 本机 / 链路本地 / 云元数据地址：需求链接只应指向公网或用户可达的文档站。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise LinkFetchError(f"域名无法解析：{host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise LinkFetchError(f"链接指向内网或本机地址（{host} → {ip}），已拒绝抓取")


def _guess_name(url: str, suffix: str) -> str:
    path = unquote(urlparse(url).path or "")
    base = Path(path).name if path and path != "/" else ""
    if base and Path(base).suffix.lower() in _KNOWN_SUFFIXES:
        return base[:120]
    host = urlparse(url).hostname or "link"
    stem = (Path(base).stem if base else host)[:80] or host
    return f"{stem}{suffix}"


_INLINE_TAGS = frozenset((
    "span", "b", "i", "u", "s", "strong", "em", "a", "font", "sup", "sub", "code", "small", "mark", "abbr",
    "time", "del", "ins", "label", "q", "cite", "kbd", "var", "br", "img", "wbr",
))


def _has_block(el) -> bool:
    """元素内部是否含块级后代；只含行内后代的元素按一段文本处理。"""
    return any(isinstance(c.tag, str) and c.tag not in _INLINE_TAGS for c in el.iterdescendants())


def inline_lines(el) -> list[str]:
    """只含行内元素的段落 → 文本行：<br> 处断行，其余 span / b / a 的文字原样拼接（不加空格，中文句子不被拆开）。"""
    parts: list[str] = []

    def collect(node) -> None:
        if isinstance(node.tag, str) and node.tag == "br":
            parts.append("\n")
        elif node.text:
            parts.append(node.text)
        for child in node:
            if not isinstance(child.tag, str):
                if child.tail:
                    parts.append(child.tail)
                continue
            collect(child)
            if child.tail:
                parts.append(child.tail)

    collect(el)
    out = []
    for line in "".join(parts).split("\n"):
        line = " ".join(line.split())
        if line:
            out.append(line)
    return out


def html_to_markdown(raw: str) -> tuple[str, str]:
    """网页 → (标题, 正文文本)：去脚本 / 样式 / 导航，块级元素换行，标题转 Markdown 井号。"""
    import lxml.html

    try:
        root = lxml.html.fromstring(raw)
    except Exception as e:  # lxml 对空文档 / 非 HTML 抛各种异常
        raise LinkFetchError(f"网页内容无法解析：{e}") from e
    title_el = root.find(".//title")
    title = (title_el.text or "").strip() if title_el is not None else ""
    for bad in root.xpath("//script|//style|//noscript|//nav|//header|//footer|//iframe|//svg"):
        bad.getparent().remove(bad)
    main = root.xpath("//main|//article") or [root.body if root.body is not None else root]
    lines: list[str] = []

    _BLOCK = ("p", "div", "section", "article", "main", "body", "pre", "blockquote", "tr", "table", "ul", "ol",
              "dl", "form", "fieldset", "aside", "figure", "details", "summary")

    def walk(el) -> None:
        if not isinstance(el.tag, str):  # 注释 / 处理指令节点
            return
        tag = el.tag
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = " ".join(el.text_content().split())
            if text:
                lines.append(f"{'#' * int(tag[1])} {text}")
            return
        if tag == "li":
            text = " ".join(el.text_content().split())
            if text:
                lines.append(f"- {text}")
            return
        if len(el) == 0 or not _has_block(el):
            # 叶子元素，或只含行内元素（span / b / a …）的段落：整段作为一行。Axure 等工具按字体切 span，
            # 一句话会拆成「…是否」「≤」「0，则…」多个 span，逐 span 成行会把符号和编号拆离句子
            lines.extend(inline_lines(el))
            return
        if el.text and el.text.strip():
            lines.append(" ".join(el.text.split()))
        for child in el:
            walk(child)
            if child.tail and child.tail.strip():
                lines.append(" ".join(child.tail.split()))
        if tag in _BLOCK:
            lines.append("")

    for m in main:
        walk(m)
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return _html.unescape(title), _html.unescape(body)


async def fetch_link(url: str, save_dir: Path, max_bytes: int, timeout: float = 30.0) -> Path:
    """下载链接到 save_dir（文件名带随机前缀，与上传附件同规则），返回落盘路径。

    图片 / PDF / Word / 文本按 Content-Type 或 URL 后缀原样保存；HTML 抽正文存为 .md 并在文首注明来源链接。
    """
    import httpx

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise LinkFetchError(f"不是有效的 http(s) 链接：{url}")
    _assert_public_host(parsed.hostname)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; TianGong/1.0; requirement-fetch)",
               "Accept": "text/html,application/pdf,image/*,*/*;q=0.8"}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers=headers) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    raise LinkFetchError(f"链接返回 HTTP {resp.status_code}")
                final = str(resp.url)
                if urlparse(final).hostname and urlparse(final).hostname != parsed.hostname:
                    _assert_public_host(urlparse(final).hostname)
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                encoding = resp.encoding or "utf-8"
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise LinkFetchError(f"链接内容超过 {max_bytes // (1024 * 1024)}MB 上限")
                    chunks.append(chunk)
    except httpx.HTTPError as e:
        raise LinkFetchError(f"下载失败：{e.__class__.__name__}: {e}") from e
    data = b"".join(chunks)
    if not data:
        raise LinkFetchError("链接内容为空")

    save_dir.mkdir(parents=True, exist_ok=True)
    prefix = uuid.uuid4().hex[:8]
    url_suffix = Path(unquote(parsed.path or "")).suffix.lower()
    if ctype in _HTML_TYPES or (not ctype and url_suffix in (".html", ".htm", "")):
        try:
            raw = data.decode(encoding, errors="replace")
        except LookupError:
            raw = data.decode("utf-8", errors="replace")
        title, body = html_to_markdown(raw)
        if not body:
            raise LinkFetchError("网页没有可提取的正文（可能需要登录或由脚本渲染）")
        name = _guess_name(url, ".md")
        if not name.endswith(".md"):
            name = Path(name).stem + ".md"
        dest = save_dir / f"{prefix}_{name}"
        head = f"来源链接：{url}\n\n" + (f"# {title}\n\n" if title else "")
        dest.write_text(head + body, encoding="utf-8")
        return dest
    suffix = _CT_SUFFIX.get(ctype) or (url_suffix if url_suffix in _KNOWN_SUFFIXES else "")
    if not suffix and ctype.startswith("image/"):
        suffix = ".png"
    if not suffix:
        raise LinkFetchError(f"不支持的链接内容类型：{ctype or '未知'}（支持网页、图片、PDF、Word、文本）")
    dest = save_dir / f"{prefix}_{_guess_name(url, suffix)}"
    if dest.suffix.lower() != suffix:
        dest = dest.with_suffix(suffix)
    dest.write_bytes(data)
    return dest

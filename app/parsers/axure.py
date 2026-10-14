"""Axure 原型导出包解析：按站点地图顺序逐页提取页面可见文字与说明备注，跳过脚本 / 样式 / 切图资源。

Axure RP 导出的 HTML 包结构：index.html / start*.html（播放器框架）、data/document.js（站点地图）、
<页面>.html（页面内容，文字内联在 div.text 里）、files/<页面>/data.js（页面对象与说明，RP9+ 为压缩写法的 JS，
不能当文本读）、resources/ images/ plugins/（播放器脚本、样式、切图）。
直接把这些 js 当需求文本会让模型只看到 "$axure.loadCurrentPage((function() {" 之类的脚本而拆不出测试点。
"""

import json
import re
from pathlib import Path

from app.parsers.base import EmbeddedImage, ParsedDocument, Section

_PLAYER_PAGES = {"index.html", "start.html", "start_c_1.html", "start_g_0.html", "start_with_pages.html"}
_SKIP_DIRS = ("resources/", "plugins/", "images/", "files/", "data/")
_RICH_TEXT_RE = re.compile(r'"(<p>.*?</p>)"', re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _norm(name: str) -> str:
    return name.replace("\\", "/").lstrip("./")


def axure_root(files: list[tuple[str, Path]]) -> str | None:
    """判断是否 Axure 导出包：存在 data/document.js 或 resources/scripts/axure/；返回包内根目录前缀（可能为空串）。"""
    for name, _ in files:
        n = _norm(name)
        if n.endswith("data/document.js"):
            return n[: -len("data/document.js")]
        if "resources/scripts/axure/" in n:
            return n.split("resources/scripts/axure/", 1)[0]
    return None


def _sitemap(document_js: str) -> list[tuple[str, str]]:
    """document.js 的站点地图 → [(页面名, url)] 按目录顺序（文件夹节点无 url 时只作为分组名）。"""
    start = document_js.find("{")
    if start == -1:
        return []
    depth = 0
    end = -1
    for i in range(start, len(document_js)):
        c = document_js[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    pages: list[tuple[str, str]] = []
    try:
        data = json.loads(document_js[start:end + 1])
    except Exception:
        # 兜底：按出现顺序配对 pageName / url
        last = None
        for m in re.finditer(r'"(pageName|url)"\s*:\s*"((?:[^"\\]|\\.)*)"', document_js):
            key, val = m.group(1), json.loads(f'"{m.group(2)}"')
            if key == "pageName":
                last = val
            elif last is not None:
                pages.append((last, val))
                last = None
        return pages

    def walk(nodes, prefix: str) -> None:
        for n in nodes or []:
            if not isinstance(n, dict):
                continue
            name = str(n.get("pageName") or "").strip()
            url = str(n.get("url") or "").strip()
            label = f"{prefix}{name}" if name else prefix.rstrip(" / ")
            if url:
                pages.append((label, url))
            walk(n.get("children"), f"{label} / " if name else prefix)

    sitemap = data.get("sitemap") if isinstance(data, dict) else None
    walk((sitemap or {}).get("rootNodes") if isinstance(sitemap, dict) else None, "")
    return pages


def _page_text(html: str) -> tuple[str, list[str]]:
    from app.parsers.link import html_to_markdown

    title, body = html_to_markdown(html)
    lines, seen = [], set()
    for line in body.splitlines():
        line = line.strip()
        if line and line not in seen:  # 同一页里母版 / 动态面板各状态会重复出现同样文字，去重
            seen.add(line)
            lines.append(line)
    return title, lines


def _notes(data_js: str, known: set[str]) -> list[str]:
    """页面 data.js 里的说明 / 备注（富文本 <p>…</p> 字符串），去掉页面正文里已有的文字。"""
    out, seen = [], set()
    for m in _RICH_TEXT_RE.finditer(data_js):
        raw = m.group(1)
        try:
            raw = json.loads(f'"{raw}"')
        except Exception:
            pass
        text = " ".join(_TAG_RE.sub(" ", raw).replace("&nbsp;", " ").split())
        if text and text not in seen and text not in known:
            seen.add(text)
            out.append(text)
    return out


def parse_axure_export(files: list[tuple[str, Path]], source: str, root: str) -> ParsedDocument:
    from app.parsers.base import read_text_any

    by_name = {_norm(n)[len(root):]: p for n, p in files if _norm(n).startswith(root)}
    doc_js = by_name.get("data/document.js")
    pages = _sitemap(read_text_any(doc_js)) if doc_js else []
    html_names = [n for n in by_name if n.lower().endswith(".html") and "/" not in n and n.lower() not in _PLAYER_PAGES]
    ordered: list[tuple[str, str]] = []
    used: set[str] = set()
    for label, url in pages:
        if url in by_name and url not in used:
            ordered.append((label, url))
            used.add(url)
    ordered.extend((Path(n).stem, n) for n in sorted(html_names) if n not in used)  # 站点地图没列的页面补在后面

    sections = [Section(level=1, title=f"Axure 原型：{source}"),
                Section(level=0, content=f"共 {len(ordered)} 个页面，按站点地图顺序列出各页可见文字与说明备注；"
                                         "脚本、样式与切图碎片不参与解析，页面里的整幅设计稿 / 截图按图片理解。")]
    images: list[EmbeddedImage] = []
    for label, url in ordered:
        try:
            title, lines = _page_text(read_text_any(by_name[url]))
        except Exception as e:
            sections.append(Section(level=2, title=f"页面：{label}"))
            sections.append(Section(level=0, content=f"（未读取：{e}）"))
            continue
        sections.append(Section(level=2, title=f"页面：{label or title or Path(url).stem}"))
        sections.append(Section(level=0, content="\n".join(lines) if lines else "（页面无文字内容）"))
        data_js = by_name.get(f"files/{Path(url).stem}/data.js")
        if data_js is not None:
            try:
                notes = _notes(read_text_any(data_js), set(lines))
            except Exception:
                notes = []
            if notes:
                sections.append(Section(level=0, content="【说明 / 备注】\n" + "\n".join(f"- {n}" for n in notes)))
        for data, mime in _page_pictures(by_name, Path(url).stem, len(images)):
            placeholder = f"[[图片:{len(images) + 1}]]"
            sections.append(Section(level=0, content=placeholder))
            images.append(EmbeddedImage(placeholder=placeholder, data=data, mime=mime))
    return ParsedDocument(source=source, doc_type="axure", sections=sections, embedded_images=images)


# 页面图片控件：短边 ≥ PICTURE_MIN_SIDE 且 ≥ PICTURE_MIN_BYTES 才当作整幅设计稿 / 截图送 Vision；按钮、图标等切图碎片跳过。
# 全包最多 PICTURE_MAX 张，svg 切片一律跳过（Axure 把矢量控件逐个导出成 svg，数量多且无业务信息）
PICTURE_MIN_SIDE = 400
PICTURE_MIN_BYTES = 20 * 1024
PICTURE_MAX = 30
_PICTURE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _page_pictures(by_name: dict[str, Path], page_stem: str, taken: int) -> list[tuple[bytes, str]]:
    import io

    from PIL import Image

    from app.parsers.image import _MIME

    prefix = f"images/{page_stem}/".lower()
    out: list[tuple[bytes, str]] = []
    for name in sorted(by_name):
        if taken + len(out) >= PICTURE_MAX:
            break
        low = name.lower()
        if not low.startswith(prefix) or not low.endswith(_PICTURE_SUFFIXES):
            continue
        path = by_name[name]
        try:
            if path.stat().st_size < PICTURE_MIN_BYTES:
                continue
            data = path.read_bytes()
            with Image.open(io.BytesIO(data)) as img:
                if min(img.size) < PICTURE_MIN_SIDE:
                    continue
        except Exception:
            continue
        out.append((data, _MIME.get(path.suffix.lower(), "image/png")))
    return out

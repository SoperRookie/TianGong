"""Axure 原型导出包解析：按站点地图顺序逐页、按控件带坐标提取页面文字，坐标还原阅读顺序，整页字符多重集自校验。

Axure RP 导出的 HTML 包结构：index.html / start*.html（播放器框架）、data/document.js（站点地图）、
<页面>.html（页面内容：每个控件是一个绝对定位的 div，文字在 #uNNN_text 的 <p> 里，表单控件在 #uNNN_input）、
files/<页面>/styles.css（每个控件的 left / top / width / height，子控件坐标相对父控件）、files/<页面>/data.js
（页面对象与说明，RP9+ 为压缩写法的 JS，不能当文本读）、resources/ images/ plugins/（播放器脚本、样式、切图）。

Axure 页面的 DOM 顺序不是阅读顺序：表格单元格、流程框、判断分支标签都是散落的控件，位置由坐标决定。所以按
《Axure 需求提取流程》的做法：把每个控件当最小单元，带坐标提取，按（行 4px 容差，列）排序还原自上而下、自左而右的
阅读顺序（同一父控件下的子控件排在一起：动态面板各状态、组合各自连续，不与页面其它内容按坐标穿插）；下拉框的选项、
输入框的值 / 占位提示单独抓（文字不在 _text 里，否则枚举值整页丢）；隐藏态（display:none）照样提取并标记；
每页用与控件解析无关的方法（整页 get_text 的字符多重集）核对提取结果，缺字即报，不允许静默过滤。
"""

import json
import re
from collections import Counter
from pathlib import Path

from app.parsers.base import EmbeddedImage, ParsedDocument, Section

_PLAYER_PAGES = {"index.html", "start.html", "start_c_1.html", "start_g_0.html", "start_with_pages.html"}
_SKIP_DIRS = ("resources/", "plugins/", "images/", "files/", "data/")
_RICH_TEXT_RE = re.compile(r'"(<p>.*?</p>)"', re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_CONTROL_ID_RE = re.compile(r"^(u\d+)$")
_TEXT_ID_RE = re.compile(r"^(u\d+)_text$")
_LINE_ID_RE = re.compile(r"^\[(u\d+)\]\s?")
_CSS_RULE_RE = re.compile(r"#(u\d+)\s*\{([^}]*)\}")
_CSS_PX_RE = {k: re.compile(rf"(?<![\w-]){k}:\s*(-?[\d.]+)px") for k in ("left", "top", "width", "height")}
_HIDDEN_RE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden")
ROW_TOLERANCE = 4   # 同一行控件的 top 常差 1～3px：按 4px 归并成一行再按列排
OPTIONS_MARK, VALUE_MARK, PLACEHOLDER_MARK = "下拉选项：", "输入值：", "占位提示："
_FORM_MARKS = (OPTIONS_MARK, VALUE_MARK, PLACEHOLDER_MARK)
_ATTR_MARKS = (VALUE_MARK, PLACEHOLDER_MARK)   # 来自属性、不在页面文字里的行，自校验时不计入提取侧


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


def control_id(element_id: str) -> str:
    """文字控件容器 id「u1125_text」→ 控件编号「u1125」；不是 Axure 命名的返回空。"""
    m = _TEXT_ID_RE.match(element_id or "")
    return m.group(1) if m else ""


def strip_control_id(line: str) -> str:
    """去掉行首的控件编号「[u1125] 」。"""
    return _LINE_ID_RE.sub("", line, count=1)


def css_positions(css: str) -> dict[str, dict[str, float]]:
    """files/<页面>/styles.css 的 `#uNNN { left; top; width; height }` → {uid: {left, top, width, height}}（相对父控件）。"""
    pos: dict[str, dict[str, float]] = {}
    for m in _CSS_RULE_RE.finditer(css or ""):
        uid, body = m.group(1), m.group(2)
        d = {k: float(r.group(1)) for k, rx in _CSS_PX_RE.items() if (r := rx.search(body))}
        if "left" in d and "top" in d:
            pos[uid] = d
    return pos


def _short_class(cls: str) -> str:
    return " ".join(c for c in (cls or "").split() if c != "ax_default")


def _form_lines(el, uid: str) -> tuple[list[str], list[str]]:
    """表单控件的文字（不在 _text 里）：下拉框全部选项、输入框 / 文本域的值与占位提示。
    返回 (行, 页面文字里能看到的原文片段——选项文字与文本域内容；value / placeholder 是属性，不算页面文字)。"""
    lines, visible = [], []
    sel = el.find(f".//select[@id='{uid}_input']")
    if sel is not None:
        opts = [" ".join((o.text_content() or "").split()) for o in sel.findall(".//option")]
        opts = [o for o in opts if o]
        if opts:
            lines.append(OPTIONS_MARK + " / ".join(opts))
            visible.extend(opts)
    for inp in el.findall(f".//input[@id='{uid}_input']"):
        if (inp.get("type") or "text").lower() in ("checkbox", "radio", "hidden", "submit", "button"):
            continue
        val = " ".join((inp.get("value") or "").split())
        if val and val.lower() != "checkbox":
            lines.append(VALUE_MARK + val)
        ph = " ".join((inp.get("placeholder") or "").split())
        if ph:
            lines.append(PLACEHOLDER_MARK + ph)
    for ta in el.findall(f".//textarea[@id='{uid}_input']"):
        val = " ".join((ta.text_content() or "").split())
        if val:
            lines.append(VALUE_MARK + val)
            visible.append(val)
        ph = " ".join((ta.get("placeholder") or "").split())
        if ph:
            lines.append(PLACEHOLDER_MARK + ph)
    return lines, visible


TABLE_SEP = " ｜ "   # 表格同一行各单元格之间的分隔符（竖线不计入字符自校验）


def _table_row(cells: list[dict]) -> dict | None:
    """同一行的单元格 → 一条控件记录：文字「单元格1 ｜ 单元格2 ｜ …」，编号取第一个单元格，cls=table_row，
    raw_lines 保留各单元格原行给字符自校验用。只有一个单元格有文字时仍按行记（便于与表头对齐）。"""
    if not cells:
        return None
    texts = [" ".join(c["lines"]) for c in cells if c["lines"]]
    if not texts:
        return cells[0]
    first = cells[0]
    return {**first, "cls": "table_row", "lines": [TABLE_SEP.join(texts)], "raw_lines": [l for c in cells for l in c["lines"]],
            "visible": [v for c in cells for v in c["visible"]], "cells": [c["id"] for c in cells]}


def _page_controls(html: str, css: str = "") -> tuple[str, list[dict]]:
    """页面 → (标题, 按阅读顺序排好的控件 [{id, cls, x, y, hidden, lines, img, visible}])。

    控件 = id 形如 uNNN 的元素；绝对坐标沿父控件链累加；同一父控件下的子控件按（round(y/4), x）排序，先列控件自己的
    文字再列子控件（动态面板各状态、组合各自连续）。文字取 #uNNN_text 里每个 <p> 一行（按字体拆开的 span 拼回同一行，
    <br> 断行），表单控件另取选项 / 值 / 占位提示；隐藏（控件或其文字容器 display:none / visibility:hidden，含祖先）
    照样提取并标 hidden；图片控件记 #uNNN_img 的 src；表格同一行（top 相同）的单元格并成一条「A ｜ B ｜ C」（cls=table_row，
    编号取第一个单元格），表头行与数据行各自一条，行内对应关系不丢。
    """
    import lxml.html

    from app.parsers.link import inline_lines

    root = lxml.html.fromstring(html)
    title_el = root.find(".//title")
    title = (title_el.text or "").strip() if title_el is not None else ""
    pos = css_positions(css)
    out: list[dict] = []

    def hidden_of(el) -> bool:
        return bool(_HIDDEN_RE.search((el.get("style") or "").replace(" ", "")))

    def own(el, uid: str, x: float, y: float, hidden: bool) -> dict | None:
        tdiv = el.find(f".//*[@id='{uid}_text']")
        lines: list[str] = []
        visible: list[str] = []
        if tdiv is not None:
            hidden = hidden or hidden_of(tdiv)
            paras = tdiv.xpath(".//p") or [tdiv]
            for p in paras:
                lines.extend(inline_lines(p))
            visible.append(tdiv.text_content())
        form, form_visible = _form_lines(el, uid)
        lines.extend(form)
        visible.extend(form_visible)
        img = el.find(f".//img[@id='{uid}_img']")
        src = (img.get("src") or "") if img is not None else ""
        if not lines and not src:
            return None
        return {"id": uid, "cls": _short_class(el.get("class") or ""), "x": int(round(x)), "y": int(round(y)),
                "hidden": hidden, "lines": lines, "img": src, "visible": visible}

    def children(el) -> list:
        """el 下的直接子控件（跨过不带控件编号的包装层）。"""
        found = []
        for c in el:
            if not isinstance(c.tag, str):
                continue
            if _CONTROL_ID_RE.match(c.get("id") or ""):
                found.append(c)
            else:
                found.extend(children(c))
        return found

    def is_cell(el) -> bool:
        return "table_cell" in (el.get("class") or "")

    def walk(el, ax: float, ay: float, hidden: bool) -> None:
        rows = []
        for c in children(el):
            uid = c.get("id")
            p = pos.get(uid)
            x = ax + (p["left"] if p else 0.0)
            y = ay + (p["top"] if p else 0.0)
            rows.append((round(y / ROW_TOLERANCE), x, c, uid, x, y))
        rows.sort(key=lambda r: (r[0], r[1]))
        i = 0
        while i < len(rows):
            key, _, c, uid, x, y = rows[i]
            group = [rows[i]]
            if is_cell(c):   # 表格：同一行的单元格并成一行（表头行也一样），行与行之间的对应关系不丢
                while i + len(group) < len(rows) and rows[i + len(group)][0] == key and is_cell(rows[i + len(group)][2]):
                    group.append(rows[i + len(group)])
            if len(group) > 1:
                cells = [own(g[2], g[3], g[4], g[5], hidden or hidden_of(g[2])) for g in group]
                rec = _table_row([r for r in cells if r is not None])
            else:
                rec = own(c, uid, x, y, hidden or hidden_of(c))
            if rec is not None:
                out.append(rec)
            for g in group:
                walk(g[2], g[4], g[5], hidden or hidden_of(g[2]))
            i += len(group)

    walk(root, 0.0, 0.0, False)
    return title, out


def _page_text(html: str, css: str = "") -> tuple[str, list[str], list[dict], dict]:
    """页面可见文字：按控件、按阅读顺序，每行前面带控件编号「[u1125] 」——需求提取的底稿按这个编号逐控件核对去向。

    去重按整个控件的全文，而不是按行：同一页里母版 / 动态面板各状态会整块重复同样的控件，整块去掉即可；
    按行去重会把「各字段详细说明如下」「≤」这类在多个控件里都出现的短行从后面的句子里抠掉。
    没有文字控件的页面（非 Axure 页）退回通用的 HTML 转文本（不带编号，不做自校验）。
    返回 (标题, 行, 控件索引 [{id, cls, x, y, hidden, img}]（只含有文字或图片的控件）, 自校验结果)。
    """
    from app.parsers.link import html_to_markdown

    try:
        title, controls = _page_controls(html, css)
    except Exception:
        title, body = html_to_markdown(html)
        return title, [l for l in body.splitlines() if l.strip()], [], {}
    if not any(c["lines"] for c in controls):
        _, body = html_to_markdown(html)
        lines, seen = [], set()
        for line in body.splitlines():
            line = line.strip()
            if line and line not in seen:
                seen.add(line)
                lines.append(line)
        return title, lines, [], {}
    check = _page_check(html, controls)
    lines, seen = [], set()
    index: list[dict] = []
    for c in controls:
        key = "\n".join(c["lines"])
        if key and key in seen:
            continue   # 整块重复的控件（母版 / 面板状态）只留一份
        if key:
            seen.add(key)
        lines.extend(f"[{c['id']}] {l}" for l in c["lines"])
        meta = {"id": c["id"], "cls": c["cls"], "x": c["x"], "y": c["y"]}
        if c["hidden"]:
            meta["hidden"] = True
        if c["img"]:
            meta["img"] = c["img"]
        index.append(meta)
    return title, lines, index, check


_MAX_DIFF_CHARS = 30


def _page_check(html: str, controls: list[dict]) -> dict:
    """第一级校验（与控件解析相互独立）：整页去掉 script / style / title / 注释后 get_text 的非空白字符多重集，
    减去提取行（去重前的全部控件）的字符多重集，差集就是漏掉的字（允许文件头 BOM）；反向差集是多出来的。
    另列出不在任何 _text / option / textarea 里的文本节点样例，便于定位漏在哪。"""
    import lxml.html

    raw = Counter()
    outside: list[str] = []
    try:
        root = lxml.html.fromstring(html)
        for bad in root.xpath("//script|//style|//title|//comment()"):
            bad.getparent().remove(bad)
        raw = Counter(ch for ch in root.text_content() if not ch.isspace() and ch != "﻿")
        for node in root.xpath("//text()"):
            s = str(node).strip()
            if not s:
                continue
            p = node.getparent()   # 尾部文本的 getparent 是它跟在后面的那个元素，判祖先时一并算上
            if p is None or p.xpath("ancestor-or-self::*[substring(@id, string-length(@id) - 4) = '_text']"
                                    " | ancestor-or-self::option | ancestor-or-self::textarea"):
                continue
            if len(outside) < 10:
                outside.append(s[:60])
    except Exception as e:   # 自校验自己出错也要让人看见，不能当成「没问题」
        return {"error": f"自校验失败：{e}"}
    got = Counter()
    for c in controls:
        for line in c.get("raw_lines") or c["lines"]:
            if line.startswith(_ATTR_MARKS):
                continue
            if line.startswith(OPTIONS_MARK):
                line = line[len(OPTIONS_MARK):].replace(" / ", "")
            got.update(ch for ch in line if not ch.isspace())
    missing, extra = raw - got, got - raw
    out = {"raw": sum(raw.values()), "got": sum(got.values()), "missing": sum(missing.values()), "extra": sum(extra.values())}
    if missing:
        out["missing_chars"] = "".join(f"{ch}×{n}" if n > 1 else ch for ch, n in list(missing.most_common(_MAX_DIFF_CHARS)))
    if extra:
        out["extra_chars"] = "".join(f"{ch}×{n}" if n > 1 else ch for ch, n in list(extra.most_common(_MAX_DIFF_CHARS)))
    if outside:
        out["outside"] = outside
    return out


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
                                         "页面文字按控件带编号、按坐标还原阅读顺序，下拉选项 / 输入值 / 占位提示单独列出；"
                                         "脚本、样式与切图碎片不参与解析，页面里的整幅设计稿 / 截图按图片理解。")]
    images: list[EmbeddedImage] = []
    controls: dict[str, dict] = {}
    checks: list[dict] = []
    for label, url in ordered:
        stem = Path(url).stem
        css_path = by_name.get(f"files/{stem}/styles.css")
        try:
            css = read_text_any(css_path) if css_path is not None else ""
        except Exception:
            css = ""
        try:
            title, lines, index, check = _page_text(read_text_any(by_name[url]), css)
        except Exception as e:
            sections.append(Section(level=2, title=f"页面：{label}"))
            sections.append(Section(level=0, content=f"（未读取：{e}）"))
            checks.append({"page": label, "error": str(e)})
            continue
        page_label = label or title or stem
        sections.append(Section(level=2, title=f"页面：{page_label}"))
        sections.append(Section(level=0, content="\n".join(lines) if lines else "（页面无文字内容）"))
        data_js = by_name.get(f"files/{stem}/data.js")
        if data_js is not None:
            try:
                notes = _notes(read_text_any(data_js), {strip_control_id(l) for l in lines})
            except Exception:
                notes = []
            if notes:
                sections.append(Section(level=0, content="【说明 / 备注】\n" + "\n".join(f"- {n}" for n in notes)))
        sent = 0
        for data, mime in _page_pictures(by_name, stem, len(images)):
            placeholder = f"[[图片:{len(images) + 1}]]"
            sections.append(Section(level=0, content=placeholder))
            images.append(EmbeddedImage(placeholder=placeholder, data=data, mime=mime))
            sent += 1
        for meta in index:
            controls[meta.pop("id")] = meta
        if check:
            referenced = len({m["img"] for m in index if m.get("img") and not m["img"].lower().endswith(".svg")})
            checks.append({"page": page_label, **check, "controls": len(index),
                           "hidden": sum(1 for m in index if m.get("hidden")), "images": referenced, "vision": sent})
    doc_check = {"ok": all(not c.get("missing") and not c.get("error") for c in checks), "pages": checks,
                 "missing": sum(c.get("missing", 0) for c in checks)} if checks else None
    return ParsedDocument(source=source, doc_type="axure", sections=sections, embedded_images=images,
                          controls=controls, check=doc_check)


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

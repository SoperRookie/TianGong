"""需求提取（AI 需求分析的前置节点）：从用户上传的全部材料里逐字摘录需求内容，按模块归整成一份整理稿。

为什么要有这一步：原型导出包、整页截图的图片理解结果、多份文档拼在一起时，需求事实散落在大量导航 / 框架文字 /
样式脚本 / 重复页面之间，直接送去分析会被噪音稀释，分片后还会把同一条规则在不同片里各写一遍。先把材料压成只含需求
事实的整理稿，分析与测试设计都以整理稿为准，原始材料仍保留在需求页可查。

流水线分三层再加一轮补漏：
1. 结构分层 `structure_units`：按「文件 › 页面 / 章节」切材料，一页（或几页打包）一次调用，不再按字数机械切片，
   切片不会落在页面中间，每次调用都知道自己在哪个文件哪个页面；
2. 分层摘录：每次调用先列出页面的区块清单，再按区块逐字摘录，模块名「页面-区块」直接来自材料；
3. 合并核验：按模块合并、条目去重、每条用与分析相同的 quote_found 对照材料，没逐字找到的标 ⚠；
4. 底稿双向核验 `app.agents.ledger`：材料的每一行是一个带编号的单元（Axure 行带控件编号），程序核算每个单元的去向；
   还没进整理稿、程序规则也判不了的单元送回模型逐句判「补进 / 排除（写原因）」，最多两轮；模型没判的按原句直接收进
   整理稿（〔兜底〕）。提取完成时每个单元要么收录要么有排除原因，待处理为 0；覆盖率与底稿统计写进 meta 供页面显示。

整理稿可逐条审核（核对通过 / 修改 / 删除），底稿页可把排除的翻回收录、把兜底的改成排除；改过之后分析结果标过期。
"""

from __future__ import annotations

import hashlib
import re

from loguru import logger

from app.agents.analysis import normalize, quote_found
from app.agents.context import profile_block
from app.agents.graph import _chat_json
from app.agents import ledger as ledger_mod
from app.agents.ledger import _NUMERIC_RE, _strict_found, strict_normalize   # 严格规范化与底稿共用，两个方向口径一致
from app.agents.prompts import wrap_data
from app.config import get_settings
from app.llm.client import LLMClient
from app.parsers.chunking import split_text
from app.prompts import prompt_text

MAX_SOURCES = 5
MAX_DROPPED = 10   # 各片都会报一遍剔除类型，去重后最多列这么多
UNVERIFIED_MARK = "⚠"
GAP_MARK = "〔补漏〕"
FORCED_MARK = "〔兜底〕"     # 模型没判定去向、程序按原句直接收进整理稿的单元
DOUBT_MARK = "〔疑点："      # 原文疑点（错别字 / 前后口径不一致 / 占位符），原句照抄、疑点单列
DRAFT_MARK = "〔疑似草稿〕"   # 画布上与主体不相连、与正式内容重复却有出入的区块：照样摘录但标出来，用例不要照它写
CROSSCHECK_MAX_CHARS = 150000   # 整稿交叉核对的输入上限，超过就跳过（极少见）
GAP_ROUNDS = 2            # 补漏最多轮数（有待处理单元就问模型，不设最少字数门槛：每句都要有去向）
GAP_BATCH_CHARS = 15000   # 一次补漏调用最多送这么多字的未覆盖句
MIN_CJK = 8               # 少于这么多汉字的行不算需求句（按钮名 / 表头 / 样例数据）


def _item_id(module: str, text: str) -> str:
    return hashlib.sha1(f"{module}\n{ledger_mod.unit_key(text)}".encode()).hexdigest()[:8]   # 逐字键：「=0」「>0」两条不能撞 id


def _merge_modules(outputs) -> tuple[list[dict], list[str], str]:
    """多片结果按模块名合并，条目按规范化文本去重（来源合并）。"""
    modules: dict[str, dict] = {}
    dropped: list[str] = []
    model_name = ""
    for data, result in outputs:
        model_name = (getattr(result, "model_name", "") or model_name) if result is not None else model_name
        for d in data.get("dropped") or []:
            d = str(d).strip()
            if d and normalize(d) not in {normalize(x) for x in dropped} and len(dropped) < MAX_DROPPED:
                dropped.append(d)
        for m in data.get("modules") or []:
            if not isinstance(m, dict):
                continue
            name = str(m.get("name") or "").strip() or "未归类"
            mod = modules.setdefault(normalize(name), {"name": name, "items": [], "_seen": {}})
            for it in m.get("items") or []:
                if isinstance(it, str):
                    it = {"text": it}
                if not isinstance(it, dict):
                    continue
                text = ledger_mod.UNIT_ID_RE.sub("", str(it.get("text") or "").strip()).strip()
                if not text:
                    continue
                srcs = it.get("sources") or []
                if isinstance(srcs, str):
                    srcs = [srcs]
                srcs = [str(x).strip() for x in srcs if str(x).strip()]
                key = ledger_mod.unit_key(text)   # 逐字去重（只去空白）：「输赢金额=0」「输赢金额>0」是两条，不能并掉
                if key in mod["_seen"]:
                    prev = mod["_seen"][key]
                    for sx in srcs:
                        if sx not in prev["sources"] and len(prev["sources"]) < MAX_SOURCES:
                            prev["sources"].append(sx)
                    continue
                entry = {"text": text, "sources": srcs[:MAX_SOURCES]}
                for k in ("gap", "forced", "manual", "unit", "verified", "id", "review"):
                    if it.get(k) is not None:
                        entry[k] = it[k]
                if it.get("draft") is True:
                    entry["draft"] = True   # 疑似遗留草稿（模型按独立区域 / 重复出入判的）
                doubt = " ".join(str(it.get("doubt") or "").split())[:200]
                if doubt:
                    entry["doubt"] = doubt   # 原文疑点（错别字 / 前后口径不一致 / 占位符）：原句照抄，疑点单列
                mod["_seen"][key] = entry
                mod["items"].append(entry)
    out = []
    for mod in modules.values():
        if mod["items"]:
            out.append({"name": mod["name"], "items": mod["items"]})
    return out, dropped, model_name


def verify_modules(modules: list[dict], materials: str) -> dict:
    """逐条对照材料：摘录是否逐字出现在材料里。含数字 / 运算符 / 百分号的条目（规则、边界、公式）必须整句逐字命中且
    运算符一致；其余条目允许分段 ≥70% 命中（模型可能把一行拆成几条、去掉序号）。"""
    source_norm, source_strict = normalize(materials), strict_normalize(materials)
    checked = verified = 0
    for mod in modules:
        for it in mod["items"]:
            checked += 1
            it["verified"] = verify_text(it["text"], source_norm, source_strict)
            verified += int(it["verified"])
    return {"checked": checked, "verified": verified, "unverified": checked - verified}


def verify_text(text: str, source_norm: str, source_strict: str | None = None) -> bool:
    """单条核验：过短的条目（按钮名 / 表头之类）quote_found 一律判否，这里按子串判，是否该单独成条交给人看；
    含数字 / 运算符的条目整句严格命中（≤ 与 > 不同、35% 与 75% 不同），不走分段放行。"""
    norm = normalize(text)
    if source_strict is not None and _NUMERIC_RE.search(unicodedata_nfkc(text)):
        return _strict_found(strict_normalize(text), source_strict)
    return (norm in source_norm) if len(norm) < 4 else quote_found(text, source_norm)


def unicodedata_nfkc(text: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFKC", text or "")


def assign_ids(modules: list[dict]) -> None:
    """给每条摘录一个稳定 id（模块 + 规范化文本的哈希），逐条审核按 id 操作。"""
    for mod in modules:
        for it in mod["items"]:
            it["id"] = _item_id(mod["name"], it["text"])


def _module_source(mod: dict) -> str:
    """模块的主来源：条目里出现最多的那个来源，写在节标题上；条目只在来源不同时单独标注，整理稿不被来源标注撑大。"""
    counts: dict[str, int] = {}
    for it in mod["items"]:
        for sx in it.get("sources") or []:
            counts[sx] = counts.get(sx, 0) + 1
    return max(counts, key=counts.get) if counts else ""


TABLE_SEP = " ｜ "   # 解析器把表格同一行单元格并成的分隔符（app.parsers.axure.TABLE_SEP）
IMAGE_SECTION = "仅出现在图片中的文案"
_LABEL_RE = re.compile(r"[:：]\s*$")
_FLOW_RE = re.compile(r"\s—[^→]*→\s|\s→\s")   # 解析器还原的流程行「A → B」「A —是→ B」
_SUB_RE = re.compile(r"^(?:注|备注|说明|示例|例)[:：]|^(?:\d{1,2}|[一二三四五六七八九十]{1,2})\s*[、.．:：)）]|^[①-⑳]|^[1-9]\ufe0f?\u20e3|^[（(]示例")
_IMG_SRC_RE = re.compile(r"图片\s*[:：]?\s*\d+")   # 「图片:7」「图片 7」「图片10」都算只在图片里的文案
_PAGE_RANK = [re.compile(r"更新记录|版本记录|修订记录|变更记录|更新日志|版本历史"), re.compile(r"需求说明|需求背景|需求概述|总体说明|概述|总览")]
_PAGE_PREFIX = re.compile(r"^页面[:：]\s*")
_MAX_LABEL = 30


def _page_title(source: str) -> str:
    """来源「文件 › 页面」的页面名（去掉「页面：」前缀）；只有文件名时就是文件名。"""
    last = source.split(" › ")[-1].strip() if source else ""
    return _PAGE_PREFIX.sub("", last) or source


_EDGE_RE = re.compile(r"^(.*?)\s(?:—([^→]*)→|→)\s(.*)$")
FLOW_ART_MAX_LINES = 80


def _width(text: str) -> int:
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def flow_ascii(lines: list[str]) -> list[str] | None:
    """流程行「A → B」「A —是→ B」→ 文字流程图（与手工稿同一画法）：
    用户报名 ──┬─ 直接报名 ──► 用户游戏投注 ──► 日/月榜排名派奖
               └─ 盈利报名 ──► 当日报名费是否扣除完毕 ─ Y ─► 日/月榜排名派奖
                                                       └─ N ─► 取消当日参赛资格
    从起点（没有入边的框）深度优先；多条出边第一条接在本行、其余换行对齐到分叉；汇合的框在各分支里重复出现；
    回到路径上已有的框写「回到：…」止住。画不出来（不是流程行 / 行数超限）返回 None，成稿退回边列表。"""
    edges = []
    for l in lines:
        m = _EDGE_RE.match(l.strip())
        if not m:
            return None
        edges.append((m.group(1).strip(), (m.group(2) or "").strip(), m.group(3).strip()))
    order: list[str] = []
    for a, _l, b in edges:
        for n in (a, b):
            if n not in order:
                order.append(n)
    outs: dict[str, list[tuple[str, str]]] = {}
    for a, l, b in edges:
        outs.setdefault(a, []).append((l, b))
    incoming = {b for _a, _l, b in edges}
    starts = [n for n in order if n not in incoming] or [order[0]]

    def render(n: str, path: tuple[str, ...]) -> list[str]:
        kids = outs.get(n, [])
        if not kids:
            return [n]
        if len(kids) == 1:
            label, child = kids[0]
            head = n + (f" ─ {label} ─► " if label else " ──► ")
            sub = [f"（回到：{child}）"] if child in path else render(child, path + (n,))
            pad = " " * _width(head)
            return [head + sub[0]] + [pad + x for x in sub[1:]]
        out: list[str] = []
        stem = n + " ──"
        for i, (label, child) in enumerate(kids):
            glyph = "┬─" if i == 0 else ("└─" if i == len(kids) - 1 else "├─")
            lead = (stem if i == 0 else " " * _width(stem)) + glyph + (f" {label} ─► " if label else " ")
            sub = [f"（回到：{child}）"] if child in path else render(child, path + (n,))
            pad = " " * _width(lead)
            out.append(lead + sub[0])
            out.extend(pad + x for x in sub[1:])
        return out

    art: list[str] = []
    for st in starts:
        art.extend(render(st, ()))
        if len(art) > FLOW_ART_MAX_LINES:
            return None
    return art


def _block_rows(items: list[dict]) -> list[dict]:
    """区块内条目的层级：表格行（含「 ｜ 」，连续且列数相同的并成一张表，表头是第一行）、小标签（以冒号结尾的短句，
    加粗成子标题）、子项（「1、」「①」「注：」「示例」开头且上一条是普通条目 / 标签 / 表格的，缩进一级）、普通条目。"""
    rows: list[dict] = []
    for it in items:
        text = it["text"]
        if _FLOW_RE.search(text):   # 流程行：连续的并成一块（成稿为代码块，保留箭头与分支）
            if rows and rows[-1]["t"] == "flow":
                rows[-1]["items"].append(it)
            else:
                rows.append({"t": "flow", "items": [it]})
            continue
        if TABLE_SEP in text:
            cols = text.count(TABLE_SEP) + 1
            if rows and rows[-1]["t"] == "table" and rows[-1]["cols"] == cols:
                rows[-1]["items"].append(it)
            else:
                rows.append({"t": "table", "cols": cols, "items": [it]})
            continue
        if _LABEL_RE.search(text) and len(text) <= _MAX_LABEL:
            rows.append({"t": "label", "item": it})
            continue
        sub = bool(_SUB_RE.match(text))
        lv = 0
        if sub and rows:
            prev = rows[-1]
            if prev["t"] in ("label", "table") or (prev["t"] == "item" and (prev["lv"] == 1 or not prev["sub"])):
                lv = 1
        rows.append({"t": "item", "item": it, "lv": lv, "sub": sub})
    out: list[dict] = []
    for r in rows:
        if r["t"] == "table" and len(r["items"]) == 1:   # 孤零零一行不成表
            out.append({"t": "item", "item": r["items"][0], "lv": 0, "sub": False})
        else:
            out.append(r)
    return out


def _page_rank(title: str) -> int:
    """页面在整理稿里的先后：版本 / 更新记录最前，需求说明（背景、总体规则）其次，各功能页面按材料顺序，只在图片里的文案最后。"""
    if title == IMAGE_SECTION:
        return 3
    for i, rx in enumerate(_PAGE_RANK):
        if rx.search(title):
            return i
    return 2


_FLOW_SPLIT = re.compile(r"\s—([^→]*)→\s|\s→\s")


def dedupe_flow_nodes(modules: list[dict]) -> int:
    """模块里有程序还原的流程行时，各流程框的文字与分支标签（是 / 否 / Y / N）不再单独成条：它们已经在「A —是→ B」里，
    单列只是重复（模型常把框文字也逐条抄一遍）。底稿按流程行也能对回这些框。返回去掉的条数。"""
    n = 0
    for mod in modules:
        flows = [it for it in mod["items"] if _FLOW_RE.search(it["text"])]
        if not flows:
            continue
        nodes: set[str] = set()
        for it in flows:
            for part in _FLOW_SPLIT.split(it["text"]):
                if part and ledger_mod.unit_key(part):
                    nodes.add(ledger_mod.unit_key(part))
        def dup(it: dict) -> bool:   # 整条等于某个框，或（≥ 4 字）是某个多行框文字的一段（模型把一个框按行拆成几条）
            k = ledger_mod.unit_key(it["text"])
            return k in nodes or (len(k) >= 4 and any(k in nd for nd in nodes))

        kept = [it for it in mod["items"] if _FLOW_RE.search(it["text"]) or not dup(it)]
        n += len(mod["items"]) - len(kept)
        mod["items"] = kept
    return n


def enforce_draft(modules: list[dict], keys: set[str]) -> int:
    """疑似草稿只认独立区域里的句子：模型在别处标的 draft 去掉（程序判定优先于模型判定）。返回去掉的条数。"""
    n = 0
    for mod in modules:
        for it in mod["items"]:
            if it.get("draft") and not ledger_mod.in_islands(it["text"], keys):
                del it["draft"]
                n += 1
    return n


def extraction_outline(modules: list[dict]) -> list[dict]:
    """整理稿层级：页面（按模块主来源分组，编号 1. 2. …）› 区块（模块，编号 1.1 …，名字去掉「页面-」前缀）› 条目层级
    （见 _block_rows）。只在图片里出现的文案（来源「图片:N」）归到最后一节「仅出现在图片中的文案」。
    返回 [{no, title, source, blocks: [{no, name, module, source, rows}]}]，rows 里是条目对象本身（供成稿与接口两用）。"""
    sections: list[dict] = []
    index: dict[str, dict] = {}
    for mod in modules:
        if not mod["items"]:
            continue
        main = _module_source(mod)
        if main and (_IMG_SRC_RE.search(main) or main.split(" › ")[-1].strip().startswith("图片")):
            page, src = IMAGE_SECTION, ""
        elif main:
            page, src = _page_title(main), main
        else:
            page, src = (mod["name"].split("-", 1)[0] if "-" in mod["name"] else mod["name"]), ""
        sec = index.get(normalize(page))
        if sec is None:
            sec = {"title": page, "source": src, "blocks": []}
            index[normalize(page)] = sec
            sections.append(sec)
        elif not sec["source"] and src:
            sec["source"] = src
        name = mod["name"]
        if page != IMAGE_SECTION:
            for sep in ("-", "－", "—", "–", "：", ":", " › ", "›"):
                if name.startswith(page + sep) and len(name) > len(page) + len(sep):
                    name = name[len(page) + len(sep):].strip()
                    break
        sec["blocks"].append({"name": name or mod["name"], "module": mod["name"], "source": "" if page == IMAGE_SECTION else main,
                              "draft": all(it.get("draft") for it in mod["items"]),   # 整块都是疑似草稿：区块标题标出来
                              "rows": _block_rows(mod["items"])})   # 图片文案节每条都标明来自哪张图
    sections.sort(key=lambda s: _page_rank(s["title"]))   # 稳定排序：更新记录 → 需求说明 → 其它页面（原顺序）→ 图片文案
    for i, sec in enumerate(sections, 1):
        sec["no"] = str(i)
        for j, b in enumerate(sec["blocks"], 1):
            b["no"] = f"{i}.{j}"
    return sections


def outline_view(modules: list[dict]) -> list[dict]:
    """给接口用的层级：条目换成 id（逐条审核按 id 操作），表格行给 ids。"""
    out = []
    for sec in extraction_outline(modules):
        blocks = []
        for b in sec["blocks"]:
            rows = []
            for r in b["rows"]:
                if r["t"] == "table":
                    rows.append({"t": "table", "cols": r["cols"], "ids": [it.get("id") for it in r["items"]]})
                elif r["t"] == "flow":
                    rows.append({"t": "flow", "ids": [it.get("id") for it in r["items"]]})
                elif r["t"] == "label":
                    rows.append({"t": "label", "id": r["item"].get("id")})
                else:
                    rows.append({"t": "item", "id": r["item"].get("id"), "lv": r["lv"]})
            blocks.append({"no": b["no"], "name": b["name"], "module": b["module"], "source": b["source"], "draft": b["draft"], "rows": rows})
        out.append({"no": sec["no"], "title": sec["title"], "source": sec["source"], "blocks": blocks})
    return out


def _item_marks(it: dict, main: str) -> tuple[str, str]:
    """条目的前缀（⚠）与后缀（〔补漏〕〔兜底〕〔疑点〕〔来源〕）。"""
    mark = "" if it.get("verified", True) else f"{UNVERIFIED_MARK} "
    others = [sx for sx in it.get("sources") or [] if sx != main]
    src = f" 〔来源：{'；'.join(others)}〕" if others else ""
    gap = f" {FORCED_MARK}" if it.get("forced") else (f" {GAP_MARK}" if it.get("gap") else "")
    draft = f" {DRAFT_MARK}" if it.get("draft") else ""
    doubt = f" {DOUBT_MARK}{it['doubt']}〕" if it.get("doubt") else ""
    return mark, f"{gap}{draft}{doubt}{src}"


def extraction_markdown(modules: list[dict], dropped: list[str] | None = None, head: dict | None = None) -> str:
    """整理稿正文，层级与《连赢挑战赛_需求提取》一致：可选文头（标题 / 来源 / 提取原则 / 校验）→ `## N. 页面 〔来源〕` →
    `### N.M 区块` → 条目（表格行还原成 Markdown 表格、以冒号结尾的短句加粗成子标题、「1、」「注：」类子项缩进一级）；
    未逐字找到的加 ⚠ 前缀，补漏 / 兜底 / 疑点各有标记，来源与区块主来源不同时才单独标注；末尾疑点清单与已剔除说明。"""
    lines: list[str] = []
    if head:
        lines += [f"# 「{head.get('title') or '需求'}」需求整理稿", "", f"- 来源：{head.get('sources') or '需求原文'}",
                  "- 提取原则：全部需求语句逐字摘录原文（含原文错别字，不作改写），按「页面 › 区块」归类；表格按行还原；只在图片里出现的文案单列；原文疑点另列、不改原句。",
                  f"- 校验：{head.get('check') or '—'}", ""]
    sections = extraction_outline(modules)
    for sec in sections:
        lines.append(f"## {sec['no']}. {sec['title']}" + (f" 〔来源：{sec['source']}〕" if sec["source"] else ""))
        lines.append("")
        for b in sec["blocks"]:
            main = b["source"]
            lines.append(f"### {b['no']} {b['name']}" + (f" {DRAFT_MARK.replace('疑似草稿', '疑似遗留草稿')}" if b["draft"] else ""))
            for r in b["rows"]:
                if r["t"] == "table":
                    for k, it in enumerate(r["items"]):
                        mark, tail = _item_marks(it, main)
                        cells = [c.strip().replace("|", "\\|") for c in it["text"].split(TABLE_SEP)]
                        cells[0] = f"{mark}{cells[0]}"
                        cells[-1] = f"{cells[-1]}{tail}"
                        lines.append("| " + " | ".join(cells) + " |")
                        if k == 0:
                            lines.append("|" + "---|" * r["cols"])
                elif r["t"] == "flow":
                    lines.append("```")
                    art = flow_ascii([it["text"] for it in r["items"]])
                    if art:   # 画成分支树；带标记（⚠ / 补漏 / 疑点）的流程行另列在树下面
                        lines.extend(art)
                        marked = [(it, *_item_marks(it, main)) for it in r["items"]]
                        marked = [(it, mk, tl) for it, mk, tl in marked if mk or tl]
                        if marked:
                            lines.append("")
                            lines.extend(f"· {mk}{it['text']}{tl}" for it, mk, tl in marked)
                    else:
                        for it in r["items"]:
                            mark, tail = _item_marks(it, main)
                            lines.append(f"{mark}{it['text']}{tail}")
                    lines.append("```")
                elif r["t"] == "label":
                    mark, tail = _item_marks(r["item"], main)
                    lines.append(f"**{mark}{r['item']['text']}**{tail}")
                else:
                    mark, tail = _item_marks(r["item"], main)
                    lines.append(("  " if r["lv"] else "") + f"- {mark}{r['item']['text']}{tail}")
            lines.append("")
    doubts = [(mod["name"], it) for mod in modules for it in mod["items"] if it.get("doubt")]
    if doubts:
        lines.append(f"## {len(sections) + 1}. 疑点 / 不一致清单 〔原句已按原文保留在上文，此处只列疑点，供需求澄清与用例设计〕")
        lines.append("")
        lines.extend(f"- {it['text']} —— {it['doubt']}（{name}）" for name, it in doubts)
        lines.append("")
    drafts = [b for sec in sections for b in sec["blocks"] if b["draft"]]
    if drafts:
        lines.append(f"## {len(sections) + (2 if doubts else 1)}. 疑似遗留草稿 〔画布上与页面主体不相连、与正式内容重复却有出入的区块，已照抄收录但用例不要照它写，请与产品确认〕")
        lines.append("")
        lines.extend(f"- {b['no']} {b['name']}（{b['module']}）" for b in drafts)
        lines.append("")
    if dropped:
        lines.append(f"（已剔除：{'、'.join(dropped)}）")
    return "\n".join(lines).strip()


# ---------- 第一层：按材料结构切 ----------

_FILE_HEAD = re.compile(r"^【(文件|链接)：(.+?)】\s*$")
_MD_HEAD = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_IMG_HEAD = re.compile(r"^【文档内图片理解 \[\[图片:\d+\]\]】\s*$")
UNIT_LEVEL = 2   # 切到二级标题（Axure 包的「## 页面：…」、文档的章节）


def _heading_of(line: str) -> tuple[int, str] | None:
    """材料里的标题行：【文件：…】/【链接：…】为第 0 级，Markdown # 标题按井号数分级。"""
    s = line.strip()
    m = _FILE_HEAD.match(s)
    if m:
        return 0, m.group(2).strip()
    m = _MD_HEAD.match(s)
    if m:
        return len(m.group(1)), m.group(2).strip()
    return None


def chunk_contexts(materials: str, chunks: list[str]) -> list[str]:
    """每片开头所处的标题路径（文件 › 页面 › 章节）。片自身开头就是标题的，路径只到它的上一级。"""
    lines = materials.split("\n")
    stack: dict[int, str] = {}
    cursor = 0
    out: list[str] = []
    for chunk in chunks:
        first = next((l.strip() for l in chunk.split("\n") if l.strip()), "")
        idx = next((i for i in range(cursor, len(lines)) if lines[i].strip() == first), None) if first else None
        if idx is None:
            out.append("")
            continue
        for line in lines[cursor:idx]:
            h = _heading_of(line)
            if h:
                stack[h[0]] = h[1]
                for lv in [k for k in stack if k > h[0]]:
                    del stack[lv]
        cursor = idx
        own = _heading_of(first)
        limit = own[0] if own else 99
        out.append(" › ".join(stack[k] for k in sorted(stack) if k < limit))
    return out


def structure_units(materials: str, max_chars: int) -> list[dict]:
    """把材料按结构切成调用单元：先按【文件】切，文件内按一 / 二级标题切成页面 / 章节单元；同一文件里相邻的小单元打包到
    不超过 max_chars，单元本身超限才按三级标题 / 段落再切（路径加「第 k 段」）。返回 [{path, pages: [路径…], text}]。"""
    lines = materials.split("\n")
    units: list[tuple[str, str, list[str]]] = []   # (file, path, lines)
    stack: dict[int, str] = {}
    file_name = "需求原文"
    cur: list[str] = []
    cur_path = file_name

    def _flush():
        body = [l for l in cur if l.strip()]
        if body and all(_heading_of(l) for l in body) and units and units[-1][0] == file_name:
            units[-1][2].extend(cur)          # 只有标题的空单元并给同文件的上一单元
        elif body and all(_heading_of(l) for l in body):
            pending.extend(cur)               # 文件开头只有标题：挂到该文件的第一个正文单元前面
        elif body:
            units.append((file_name, cur_path, pending + list(cur)))
            pending.clear()
        cur.clear()

    pending: list[str] = []

    for line in lines:
        h = _heading_of(line)
        if h and h[0] <= UNIT_LEVEL:
            _flush()
            if h[0] == 0:
                file_name = h[1]
                stack.clear()
            else:
                stack[h[0]] = h[1]
                for lv in [k for k in stack if k > h[0]]:
                    del stack[lv]
            cur_path = " › ".join([file_name] + [stack[k] for k in sorted(stack)])
        cur.append(line)
    _flush()
    if pending:
        if units:
            units[-1][2].extend(pending)
        else:
            units.append((file_name, cur_path, list(pending)))

    pieces: list[tuple[str, str, str]] = []   # (file, path, text)
    for fname, path, ls in units:
        text = "\n".join(ls).strip()
        if len(text) <= max_chars:
            pieces.append((fname, path, text))
            continue
        parts = split_text(text, max_chars)
        for k, part in enumerate(parts, 1):
            pieces.append((fname, f"{path}（第 {k}/{len(parts)} 段）", part))
    out: list[dict] = []
    buf: list[tuple[str, str, str]] = []

    def _pack():
        if buf:
            out.append({"path": buf[0][0], "pages": [p for _, p, _ in buf], "text": "\n\n".join(t for _, _, t in buf)})
            buf.clear()

    for piece in pieces:
        if buf and (piece[0] != buf[0][0] or sum(len(t) for _, _, t in buf) + len(piece[2]) + 2 > max_chars):
            _pack()
        buf.append(piece)
    _pack()
    return out


# ---------- 第四层：自动补漏 ----------

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_NOISE_RE = re.compile(r"(状态[:：]\s*可见|颜色|色值|样式|描边|填充|高亮|加粗|图中未|图片未|截图|遮挡|无法辨认|无法确定|看不清|图标|背景|字体|字号|"
                       r"布局|位置[:：]|像素|\bpx\b|以设计输出为准|实际效果|仅为.*示例|示例参考|版权|©|按从.*(整理|转录)|可辨认|这是一[张个].*(界面|截图|原型)|"
                       r"截断|装饰|未在图中|图中(没有|未)|图片边[缘界]|箭头|分隔条|结构相似|当前可见|本块内|界面中换行|"
                       r"^[\s\-•·]*(时间|日期|单号|账号|昵称)[:：]?\s*[\dA-Za-z:\-\. ]+$)")
_SEG_RE = re.compile(r"[，。；：！？,.;:!?、()（）「」【】\[\]]+")
_HEADER_LINE_RE = re.compile(r"([:：]\s*$|^[^，。；,.;]{0,24}(示例|说明|参考图例|清单|区域|入口位)[：:]?\s*$)")   # 「未报名用户B面展开示例：」「入口设计要求说明」这类小标题


def uncovered_lines(materials: str, text: str) -> tuple[list[dict], dict]:
    """材料里还没进整理稿的需求句：按行比对，剔除标题 / 占位 / 汉字不足 / 只讲外观与样例数据的行；
    规范化后是整理稿子串、或按标点切段后命中字数过半的算已覆盖。返回 ([{path, line}], {lines, covered})。"""
    exn = normalize(text)
    stack: dict[int, str] = {}
    file_name = "需求原文"
    out: list[dict] = []
    total = covered = 0
    in_flow = False
    for raw in materials.split("\n"):
        s = raw.strip()
        if s.startswith(ledger_mod.FLOW_BEGIN) or s == ledger_mod.FLOW_END:
            in_flow = s.startswith(ledger_mod.FLOW_BEGIN)
            continue
        if in_flow:
            continue   # 流程块是程序从流程框还原的派生内容，框里的文字另有原行
        h = _heading_of(s)
        if h:
            if h[0] == 0:
                file_name, stack = h[1], {}
            else:
                stack[h[0]] = h[1]
                for lv in [k for k in stack if k > h[0]]:
                    del stack[lv]
            continue
        if not s or _IMG_HEAD.match(s) or s.startswith(("[[图片", ">", "```", "|---", "【")):
            continue   # 【独立区域 …】【说明 / 备注】这类程序标记不是需求句
        s = ledger_mod.UNIT_ID_RE.sub("", s).strip()
        if len(_CJK_RE.findall(s)) < MIN_CJK or _NOISE_RE.search(s) or _HEADER_LINE_RE.search(s):
            continue
        total += 1
        n = normalize(s)
        if n in exn:
            covered += 1
            continue
        segs = [normalize(x) for x in _SEG_RE.split(s)]
        segs = [x for x in segs if len(x) >= 3]
        if segs:
            tot = sum(len(x) for x in segs)
            hit = sum(len(x) for x in segs if x in exn)
            if hit / tot >= 0.5:
                covered += 1
                continue
        path = " › ".join([file_name] + [stack[k] for k in sorted(stack) if k <= UNIT_LEVEL])
        out.append({"path": path, "line": s})
    return out, {"lines": total, "covered": covered}


def _gap_batches(missing: list[dict]) -> list[str]:
    """待判定单元按页面分组、按字数分批，每批一次调用。每句带单元编号，模型回 add / skip 时可以只回编号。"""
    batches: list[str] = []
    buf: list[str] = []
    size = 0
    last_path = None
    for m in missing:
        if buf and size + len(m["text"]) + 40 > GAP_BATCH_CHARS:
            batches.append("\n".join(buf))
            buf, size, last_path = [], 0, None
        if m["path"] != last_path:
            buf.append(f"〔{m['path']}〕")
            last_path = m["path"]
        part = f"　〔整理稿已有其中 {int(m['partial'] * 100)}%，缺的部分多半是句尾条件，要么按原句整句补进要么判排除〕" if m.get("partial") else ""
        times = f"　〔出现 {m['count']} 次〕" if m.get("count", 1) > 1 else ""
        buf.append(f"- [{m['uid']}] {m['text']}{part}{times}")
        size += len(m["text"]) + len(part) + 2
    if buf:
        batches.append("\n".join(buf))
    return batches


def _strip_bullet(s: str) -> str:
    return re.sub(r"^[\s\-•·*、，,。.；;]+|^\s*\d+[.、)]\s*", "", s).strip()


def _decide(units_sent: list[dict], entries, by: str) -> dict:
    """模型回的 add / skip 条目对到单元：优先按编号（uid 字段或 text 里的 [uNNN]），否则按原句对。返回 {uid: 条目}。"""
    by_uid = {u["uid"]: u for u in units_sent}
    out: dict = {}
    loose: list[dict] = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        uid = str(e.get("uid") or "").strip()
        t = str(e.get("text") or "").strip()
        m = ledger_mod.UNIT_ID_RE.match(t)
        if not uid and m:
            uid = m.group(1)
        if uid in by_uid:
            out[uid] = e
        else:
            loose.append(e)
    if loose:   # 没带编号的：按原句对（逐字等于，或 ≥ 8 字且互为子串）；模型自己概括的一句对不上就不算判定
        index = [(u["uid"], normalize(u["text"])) for u in units_sent]
        for e in loose:
            t = normalize(ledger_mod.UNIT_ID_RE.sub("", _strip_bullet(str(e.get("text") or ""))))
            if len(t) < 2:
                continue
            for uid, n in index:
                if t == n or (len(t) >= 8 and len(n) >= 8 and (t in n or n in t)):
                    out.setdefault(uid, e)
    return out


async def _gap_fill(materials: str, modules: list[dict], llm: LLMClient, model: str | None, extra: str) -> tuple[dict, dict]:
    """底稿核验 + 补漏：核算每个单元去向，待处理的送回模型逐句判「补进 / 排除」，最多 GAP_ROUNDS 轮；模型没判的按原句
    收进整理稿（〔兜底〕）。原地改 modules，返回 (补漏统计, 底稿 {stats, decisions})。"""
    from app.agents.service import gather_or_cancel
    from app.llm.calllog import used_prompts

    used_prompts.set({})   # 上一步提取的 Prompt 登记还留在当前上下文，不清掉补漏调用会被记成「需求提取（分析前置）」
    system = prompt_text("requirement_extract_gap")
    units = ledger_mod.build_units(materials)
    decisions: dict = {}
    _missing, before = uncovered_lines(materials, extraction_markdown(modules))
    stats = {"rounds": 0, "before": before, "after": before, "added": 0, "skipped": 0, "forced": 0}
    acct = ledger_mod.account(units, extraction_markdown(modules), decisions)
    for _ in range(GAP_ROUNDS):
        todo = ledger_mod.todo_units(acct)
        if not todo:
            break
        names = [mod["name"] for mod in modules]
        user_head = ("已有整理稿的模块清单：\n" + "\n".join(f"- {n}" for n in names) +
                     "\n\n下面是程序对照发现尚未进入整理稿的材料原句（按「文件 › 页面」分组，行首方括号是单元编号）。"
                     "每一句都必须出现在 add 或 skip 里：\n")
        outputs = await gather_or_cancel([
            _chat_json(llm, [{"role": "system", "content": system},
                             {"role": "user", "content": user_head + wrap_data("未覆盖原句", b) + extra}],
                       model, reasoning_effort="medium")
            for b in _gap_batches(todo)
        ])
        stats["rounds"] += 1
        by_uid = {u["uid"]: u for u in todo}
        adds: list[dict] = []
        new_decisions: dict = {}
        for data, _r in outputs:
            for uid, e in _decide(todo, data.get("skip") or [], ledger_mod.BY_MODEL).items():
                new_decisions[uid] = {"r": (str(e.get("reason") or "").strip() or "模型判定不是需求")[:80], "by": ledger_mod.BY_MODEL}
            for uid, a in _decide(todo, data.get("add") or [], ledger_mod.BY_MODEL).items():
                u = by_uid[uid]
                t = ledger_mod.UNIT_ID_RE.sub("", _strip_bullet(str(a.get("text") or ""))).strip()
                tn, un = normalize(t), normalize(u["text"])
                if len(tn) < 4 or not (tn == un or (len(tn) >= 8 and tn in un)):
                    t = u["text"]   # 模型改了写法 / 只回了编号：按原句收
                srcs = a.get("sources") or []
                if isinstance(srcs, str):
                    srcs = [srcs]
                item = {"text": t, "sources": srcs or [ledger_mod.source_of(u["path"])], "gap": True, "unit": uid}
                if a.get("draft") is True:
                    item["draft"] = True
                adds.append({"name": str(a.get("module") or "").strip() or f"{ledger_mod.page_of(u['path'])}-待归类", "items": [item]})
        stats["skipped"] += len(new_decisions)
        decisions.update(new_decisions)
        if adds:
            before_n = sum(len(m["items"]) for m in modules)
            merged, _d, _m = _merge_modules([({"modules": [{"name": mod["name"], "items": mod["items"]} for mod in modules] + adds}, None)])
            stats["added"] += sum(len(m["items"]) for m in merged) - before_n
            modules[:] = merged
        acct = ledger_mod.account(units, extraction_markdown(modules), decisions)
        if not adds and not new_decisions:
            break
    # 兜底：模型没判定去向的单元按原句直接收进整理稿，保证每个单元都有去向
    todo = ledger_mod.todo_units(acct)
    if todo:
        adds = [{"name": f"{ledger_mod.page_of(u['path'])}-待归类",
                 "items": [{"text": u["text"], "sources": [ledger_mod.source_of(u["path"])], "gap": True, "forced": True, "unit": u["uid"]}]}
                for u in todo]
        merged, _d, _m = _merge_modules([({"modules": [{"name": mod["name"], "items": mod["items"]} for mod in modules] + adds}, None)])
        modules[:] = merged
        stats["forced"] = len(todo)
        acct = ledger_mod.account(units, extraction_markdown(modules), decisions)
    stats["after"] = uncovered_lines(materials, extraction_markdown(modules))[1]
    return stats, {"stats": acct["stats"], "image": acct["image"], "decisions": decisions}


# ---------- 第五层：整稿交叉核对 ----------

def _crosscheck_input(modules: list[dict]) -> str:
    lines: list[str] = []
    for mod in modules:
        lines.append(f"## {mod['name']}")
        for it in mod["items"]:
            lines.append(f"- [{it['id']}] {it['text']}" + (f" {DRAFT_MARK}" if it.get("draft") else "") + (f" {DOUBT_MARK}{it['doubt']}〕" if it.get("doubt") else ""))
    return "\n".join(lines)


def apply_crosscheck(modules: list[dict], data: dict, keys: set[str] | None = None) -> dict:
    """把交叉核对结果写回条目：doubts 按 id 追加到条目 doubt（已有的用「；」接上，≤ 200 字）；drafts 按模块名把整块标 draft，
    理由写进该块第一条的 doubt。只认存在的 id / 模块名；给了 keys（独立区域句子）时，只有半数以上条目来自独立区域的模块才能标草稿
    ——模型单凭「内容相似」判草稿误报太多，画布位置这个硬信号才算数。返回 {doubts, drafts}。"""
    by_id = {it["id"]: it for mod in modules for it in mod["items"] if it.get("id")}
    mod_of = {it["id"]: mod["name"] for mod in modules for it in mod["items"] if it.get("id")}
    n_doubt = 0
    for d in data.get("doubts") or []:
        if not isinstance(d, dict):
            continue
        it = by_id.get(str(d.get("id") or "").strip())
        text = " ".join(str(d.get("doubt") or "").split())
        if it is None or not text:
            continue
        text = re.sub(r"[（(]?\b([0-9a-f]{8})\b[）)]?", lambda m: f"「{mod_of[m.group(1)]}」" if m.group(1) in mod_of else m.group(0), text)   # 模型爱引条目 id，读者看不懂：换成模块名
        it["doubt"] = (f"{it['doubt']}；{text}" if it.get("doubt") and text not in it["doubt"] else (it.get("doubt") or text))[:200]
        n_doubt += 1
    by_name = {normalize(mod["name"]): mod for mod in modules}
    n_draft = 0
    for d in data.get("drafts") or []:
        if not isinstance(d, dict):
            continue
        mod = by_name.get(normalize(str(d.get("module") or "")))
        if mod is None or not mod["items"]:
            continue
        if keys is not None and sum(1 for it in mod["items"] if ledger_mod.in_islands(it["text"], keys)) * 2 < len(mod["items"]):
            continue   # 不在独立区域里：不算草稿
        reason = " ".join(str(d.get("reason") or "").split())
        for it in mod["items"]:
            it["draft"] = True
        first = mod["items"][0]
        note = f"疑似遗留草稿：{reason}" if reason else "疑似遗留草稿"
        if note not in (first.get("doubt") or ""):
            first["doubt"] = (f"{first['doubt']}；{note}" if first.get("doubt") else note)[:200]
        n_draft += 1
    return {"doubts": n_doubt, "drafts": n_draft}


async def _crosscheck(modules: list[dict], llm: LLMClient, model: str | None, extra: str, keys: set[str] | None = None) -> dict | None:
    """整稿一次交叉核对（跨页面矛盾、疑似遗留草稿）。关了、稿子超长或模型出错都跳过，不影响提取结果。"""
    from app.llm.calllog import used_prompts

    if not get_settings().requirement_extract_crosscheck:
        return None
    body = _crosscheck_input(modules)
    if len(body) > CROSSCHECK_MAX_CHARS:
        logger.info("需求提取交叉核对跳过：整理稿 {} 字超过上限 {}", len(body), CROSSCHECK_MAX_CHARS)
        return None
    used_prompts.set({})
    system = prompt_text("requirement_extract_crosscheck")
    try:
        data, _r = await _chat_json(llm, [{"role": "system", "content": system},
                                          {"role": "user", "content": wrap_data("整理稿条目清单", body) + extra}], model, reasoning_effort="medium")
    except Exception as e:   # 核对只加疑点，失败不拦提取
        logger.warning("需求提取交叉核对失败（已跳过）：{}", e)
        return None
    return apply_crosscheck(modules, data, keys)


# ---------- 主流程 ----------

async def run_requirement_extraction(materials: str, llm: LLMClient, model: str | None = None) -> dict:
    """提取流水线：结构分层切单元 → 各单元并行分层摘录 → 按模块合并去重 → 底稿核验 + 补漏 + 兜底 → 逐条对照材料核验 → 整理稿。

    返回 {text, modules, dropped, model_name, chunks, checked, verified, unverified, source_chars, gap, ledger}。
    """
    settings = get_settings()
    units = structure_units(materials, settings.chunk_max_chars)
    system = prompt_text("requirement_extract")
    extra = profile_block()

    def _user(i: int, unit: dict) -> str:
        pages = unit["pages"]
        where = "、".join(f"「{p}」" for p in pages[:12]) + ("…" if len(pages) > 12 else "")
        head = (f"这是全部材料的第 {i + 1}/{len(units)} 份，包含：{where}。"
                f"只摘录本份实际包含的内容，本份未涉及的模块不要生成；模块名用「页面-区块」，来源用「文件 › 页面」。\n\n"
                if len(units) > 1 else "")
        return f"{head}需求材料：\n{wrap_data('需求材料', unit['text'])}{extra}"

    from app.agents.service import gather_or_cancel

    outputs = await gather_or_cancel([
        _chat_json(llm, [{"role": "system", "content": system}, {"role": "user", "content": _user(i, u)}],
                   model, reasoning_effort="medium")
        for i, u in enumerate(units)
    ])
    modules, dropped, model_name = _merge_modules(outputs)
    if not modules:
        raise ValueError("模型没有从材料里提取出任何需求内容（材料可能不是需求文档）")
    islands = ledger_mod.island_keys(materials)
    enforce_draft(modules, islands)
    dedupe_flow_nodes(modules)
    gap, ledger = await _gap_fill(materials, modules, llm, model, extra)
    enforce_draft(modules, islands)
    dedupe_flow_nodes(modules)
    stats = verify_modules(modules, materials)
    assign_ids(modules)
    crosscheck = await _crosscheck(modules, llm, model, extra, islands)
    text = extraction_markdown(modules, dropped)
    cov = gap["after"]
    ls = ledger["stats"]
    logger.info("需求提取：材料 {} 字 → 整理稿 {} 字，{} 个模块 {} 条；逐字核验通过 {}，未找到 {}；补漏 {} 轮 +{} 条、兜底 {} 条，"
                "需求句覆盖 {}/{}；底稿 {} 句（去重）：收录 {} 排除 {}（模型 {} / 程序 {}）需复核 {} 待处理 {}；图中文字 {} 句（分 {} 份，{}）",
                len(materials), len(text), len(modules), stats["checked"], stats["verified"], stats["unverified"],
                gap["rounds"], gap["added"], gap["forced"], cov["covered"], cov["lines"],
                ls["total"], ls["in"], ls["ex"], ls["ex_by"].get("model", 0), ls["ex_by"].get("auto", 0), ls["review"], ls["todo"],
                (ledger.get("image") or {}).get("total", 0), len(units), model_name)
    if crosscheck:
        logger.info("需求提取交叉核对：跨页面疑点 +{}，疑似遗留草稿区块 {}", crosscheck["doubts"], crosscheck["drafts"])
    return {"text": text, "modules": modules, "dropped": dropped, "model_name": model_name, "chunks": len(units),
            "source_chars": len(materials), "gap": gap, "ledger": ledger, "crosscheck": crosscheck, **stats}

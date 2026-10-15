"""需求提取（AI 需求分析的前置节点）：从用户上传的全部材料里逐字摘录需求内容，按模块归整成一份整理稿。

为什么要有这一步：原型导出包、整页截图的图片理解结果、多份文档拼在一起时，需求事实散落在大量导航 / 框架文字 /
样式脚本 / 重复页面之间，直接送去分析会被噪音稀释，分片后还会把同一条规则在不同片里各写一遍。先把材料压成只含需求
事实的整理稿，分析与测试设计都以整理稿为准，原始材料仍保留在需求页可查。

流水线分三层再加一轮补漏：
1. 结构分层 `structure_units`：按「文件 › 页面 / 章节」切材料，一页（或几页打包）一次调用，不再按字数机械切片，
   切片不会落在页面中间，每次调用都知道自己在哪个文件哪个页面；
2. 分层摘录：每次调用先列出页面的区块清单，再按区块逐字摘录，模块名「页面-区块」直接来自材料；
3. 合并核验：按模块合并、条目去重、每条用与分析相同的 quote_found 对照材料，没逐字找到的标 ⚠；
4. 自动补漏 `uncovered_lines`：程序找出材料里还没进整理稿的需求句（剔除外观描述 / 样例数据），送回模型判「补进 / 跳过」，
   最多两轮；覆盖率写进 meta 供页面显示。

整理稿可逐条审核（核对通过 / 修改 / 删除），改过之后分析结果标过期。
"""

from __future__ import annotations

import hashlib
import re

from loguru import logger

from app.agents.analysis import normalize, quote_found
from app.agents.context import profile_block
from app.agents.graph import _chat_json
from app.agents.prompts import wrap_data
from app.config import get_settings
from app.llm.client import LLMClient
from app.parsers.chunking import split_text
from app.prompts import prompt_text

MAX_SOURCES = 5
MAX_DROPPED = 10   # 各片都会报一遍剔除类型，去重后最多列这么多
UNVERIFIED_MARK = "⚠"
GAP_MARK = "〔补漏〕"
GAP_ROUNDS = 2            # 补漏最多轮数
GAP_MIN_CHARS = 300       # 未覆盖的需求句累计不到这么多字就不补（零星几句不值一次调用）
GAP_BATCH_CHARS = 15000   # 一次补漏调用最多送这么多字的未覆盖句
MIN_CJK = 8               # 少于这么多汉字的行不算需求句（按钮名 / 表头 / 样例数据）


def _item_id(module: str, text: str) -> str:
    return hashlib.sha1(f"{module}\n{normalize(text)}".encode()).hexdigest()[:8]


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
                text = str(it.get("text") or "").strip()
                if not text:
                    continue
                srcs = it.get("sources") or []
                if isinstance(srcs, str):
                    srcs = [srcs]
                srcs = [str(x).strip() for x in srcs if str(x).strip()]
                key = normalize(text)
                if key in mod["_seen"]:
                    prev = mod["_seen"][key]
                    for sx in srcs:
                        if sx not in prev["sources"] and len(prev["sources"]) < MAX_SOURCES:
                            prev["sources"].append(sx)
                    continue
                entry = {"text": text, "sources": srcs[:MAX_SOURCES]}
                for k in ("gap", "verified", "id", "review"):
                    if it.get(k) is not None:
                        entry[k] = it[k]
                mod["_seen"][key] = entry
                mod["items"].append(entry)
    out = []
    for mod in modules.values():
        if mod["items"]:
            out.append({"name": mod["name"], "items": mod["items"]})
    return out, dropped, model_name


def verify_modules(modules: list[dict], materials: str) -> dict:
    """逐条对照材料：摘录是否逐字（或分段 ≥70%）出现在材料里。"""
    source_norm = normalize(materials)
    checked = verified = 0
    for mod in modules:
        for it in mod["items"]:
            checked += 1
            it["verified"] = verify_text(it["text"], source_norm)
            verified += int(it["verified"])
    return {"checked": checked, "verified": verified, "unverified": checked - verified}


def verify_text(text: str, source_norm: str) -> bool:
    """单条核验：过短的条目（按钮名 / 表头之类）quote_found 一律判否，这里按子串判，是否该单独成条交给人看。"""
    norm = normalize(text)
    return (norm in source_norm) if len(norm) < 4 else quote_found(text, source_norm)


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


def extraction_markdown(modules: list[dict], dropped: list[str] | None = None) -> str:
    """整理稿正文：按模块分节（标题带主来源），每条一行，来源与模块主来源不同时才单独标注；未逐字找到的加 ⚠ 前缀供人核对；
    补漏进来的条目加〔补漏〕标记。"""
    lines: list[str] = []
    for mod in modules:
        if not mod["items"]:
            continue
        main = _module_source(mod)
        lines.append(f"## {mod['name']}" + (f" 〔来源：{main}〕" if main else ""))
        for it in mod["items"]:
            mark = "" if it.get("verified", True) else f"{UNVERIFIED_MARK} "
            others = [sx for sx in it.get("sources") or [] if sx != main]
            src = f" 〔来源：{'；'.join(others)}〕" if others else ""
            gap = f" {GAP_MARK}" if it.get("gap") else ""
            lines.append(f"- {mark}{it['text']}{gap}{src}")
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
                       r"布局|位置[:：]|像素|\bpx\b|以设计输出为准|实际效果|仅为.*示例|示例参考|版权|©|按从.*整理|可辨认|这是一[张个].*(界面|截图|原型)|"
                       r"^[\s\-•·]*(时间|日期|单号|账号|昵称)[:：]?\s*[\dA-Za-z:\-\. ]+$)")
_SEG_RE = re.compile(r"[，。；：！？,.;:!?、()（）「」【】\[\]]+")


def uncovered_lines(materials: str, text: str) -> tuple[list[dict], dict]:
    """材料里还没进整理稿的需求句：按行比对，剔除标题 / 占位 / 汉字不足 / 只讲外观与样例数据的行；
    规范化后是整理稿子串、或按标点切段后命中字数过半的算已覆盖。返回 ([{path, line}], {lines, covered})。"""
    exn = normalize(text)
    stack: dict[int, str] = {}
    file_name = "需求原文"
    out: list[dict] = []
    total = covered = 0
    for raw in materials.split("\n"):
        s = raw.strip()
        h = _heading_of(s)
        if h:
            if h[0] == 0:
                file_name, stack = h[1], {}
            else:
                stack[h[0]] = h[1]
                for lv in [k for k in stack if k > h[0]]:
                    del stack[lv]
            continue
        if not s or _IMG_HEAD.match(s) or s.startswith(("[[图片", ">", "```", "|---")):
            continue
        if len(_CJK_RE.findall(s)) < MIN_CJK or _NOISE_RE.search(s):
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
    """未覆盖句按页面分组、按字数分批，每批一次调用。"""
    batches: list[str] = []
    buf: list[str] = []
    size = 0
    last_path = None
    for m in missing:
        if buf and size + len(m["line"]) + 40 > GAP_BATCH_CHARS:
            batches.append("\n".join(buf))
            buf, size, last_path = [], 0, None
        if m["path"] != last_path:
            buf.append(f"〔{m['path']}〕")
            last_path = m["path"]
        buf.append(f"- {m['line']}")
        size += len(m["line"]) + 2
    if buf:
        batches.append("\n".join(buf))
    return batches


def _strip_bullet(s: str) -> str:
    return re.sub(r"^[\s\-•·*]+|^\s*\d+[.、)]\s*", "", s).strip()


async def _gap_fill(materials: str, modules: list[dict], llm: LLMClient, model: str | None, extra: str) -> dict:
    """补漏：找出未覆盖的需求句送回模型判「补进 / 跳过」，最多 GAP_ROUNDS 轮。原地改 modules，返回统计。"""
    from app.agents.service import gather_or_cancel

    system = prompt_text("requirement_extract_gap")
    missing, before = uncovered_lines(materials, extraction_markdown(modules))
    stats = {"rounds": 0, "before": before, "after": before, "added": 0, "skipped": 0}
    for _ in range(GAP_ROUNDS):
        if sum(len(m["line"]) for m in missing) < GAP_MIN_CHARS:
            break
        names = [mod["name"] for mod in modules]
        user_head = ("已有整理稿的模块清单：\n" + "\n".join(f"- {n}" for n in names) +
                     "\n\n下面是程序对照发现尚未进入整理稿的材料原句（按「文件 › 页面」分组）：\n")
        outputs = await gather_or_cancel([
            _chat_json(llm, [{"role": "system", "content": system},
                             {"role": "user", "content": user_head + wrap_data("未覆盖原句", b) + extra}],
                       model, reasoning_effort="medium")
            for b in _gap_batches(missing)
        ])
        stats["rounds"] += 1
        known_norm = [normalize(_strip_bullet(m["line"])) for m in missing]
        adds: list[dict] = []
        for data, _r in outputs:
            stats["skipped"] += len(data.get("skip") or [])
            for a in data.get("add") or []:
                if not isinstance(a, dict):
                    continue
                t = _strip_bullet(str(a.get("text") or ""))
                tn = normalize(t)
                if len(tn) < 4 or not any(tn == k or (len(tn) >= 8 and tn in k) for k in known_norm):
                    continue   # 只认我们送过去的原句（可去掉列表符号、可取其中一段），模型自己编的不要
                srcs = a.get("sources") or []
                if isinstance(srcs, str):
                    srcs = [srcs]
                adds.append({"name": str(a.get("module") or "").strip() or "未归类",
                             "items": [{"text": t, "sources": srcs, "gap": True}]})
        if not adds:
            break
        before_n = sum(len(m["items"]) for m in modules)
        merged, _d, _m = _merge_modules([({"modules": [{"name": mod["name"], "items": mod["items"]} for mod in modules] + adds}, None)])
        added_now = sum(len(m["items"]) for m in merged) - before_n
        modules[:] = merged
        stats["added"] += added_now
        if added_now == 0:
            break
        missing, _after = uncovered_lines(materials, extraction_markdown(modules))
    stats["after"] = uncovered_lines(materials, extraction_markdown(modules))[1]
    return stats


# ---------- 主流程 ----------

async def run_requirement_extraction(materials: str, llm: LLMClient, model: str | None = None) -> dict:
    """提取流水线：结构分层切单元 → 各单元并行分层摘录 → 按模块合并去重 → 自动补漏 → 逐条对照材料核验 → 整理稿。

    返回 {text, modules, dropped, model_name, chunks, checked, verified, unverified, source_chars, gap}。
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
    gap = await _gap_fill(materials, modules, llm, model, extra)
    stats = verify_modules(modules, materials)
    assign_ids(modules)
    text = extraction_markdown(modules, dropped)
    cov = gap["after"]
    logger.info("需求提取：材料 {} 字 → 整理稿 {} 字，{} 个模块 {} 条；逐字核验通过 {}，未找到 {}；补漏 {} 轮 +{} 条，需求句覆盖 {}/{}（分 {} 份，{}）",
                len(materials), len(text), len(modules), stats["checked"], stats["verified"], stats["unverified"],
                gap["rounds"], gap["added"], cov["covered"], cov["lines"], len(units), model_name)
    return {"text": text, "modules": modules, "dropped": dropped, "model_name": model_name, "chunks": len(units),
            "source_chars": len(materials), "gap": gap, **stats}

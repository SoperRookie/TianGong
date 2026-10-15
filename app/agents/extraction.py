"""需求提取（AI 需求分析的前置节点）：从用户上传的全部材料里逐字摘录需求内容，按模块归整成一份整理稿。

为什么要有这一步：原型导出包、整页截图的图片理解结果、多份文档拼在一起时，需求事实散落在大量导航 / 框架文字 /
样式脚本 / 重复页面之间，直接送去分析会被噪音稀释，分片后还会把同一条规则在不同片里各写一遍。先把材料压成只含需求
事实的整理稿，分析与测试设计都以整理稿为准，原始材料仍保留在需求页可查。

提取只摘录不改写：每条摘录都用与分析相同的 quote_found 逐条对照材料，没在材料里逐字找到的条目保留但标 ⚠，
由人在整理稿里核对；整理稿可人工修改，改过之后分析结果标过期。
"""

from __future__ import annotations

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


def _merge_modules(outputs) -> tuple[list[dict], list[str], str]:
    """多片结果按模块名合并，条目按规范化文本去重（来源合并）。"""
    modules: dict[str, dict] = {}
    dropped: list[str] = []
    model_name = ""
    for data, result in outputs:
        model_name = result.model_name or model_name
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
            norm = normalize(it["text"])
            # 过短的条目（按钮名 / 表头之类）quote_found 一律判否；这里按子串判，是否该单独成条交给人看
            it["verified"] = (norm in source_norm) if len(norm) < 4 else quote_found(it["text"], source_norm)
            verified += int(it["verified"])
    return {"checked": checked, "verified": verified, "unverified": checked - verified}


def _module_source(mod: dict) -> str:
    """模块的主来源：条目里出现最多的那个来源，写在节标题上；条目只在来源不同时单独标注，整理稿不被来源标注撑大。"""
    counts: dict[str, int] = {}
    for it in mod["items"]:
        for sx in it.get("sources") or []:
            counts[sx] = counts.get(sx, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def extraction_markdown(modules: list[dict], dropped: list[str] | None = None) -> str:
    """整理稿正文：按模块分节（标题带主来源），每条一行，来源与模块主来源不同时才单独标注；未逐字找到的加 ⚠ 前缀供人核对。"""
    lines: list[str] = []
    for mod in modules:
        main = _module_source(mod)
        lines.append(f"## {mod['name']}" + (f" 〔来源：{main}〕" if main else ""))
        for it in mod["items"]:
            mark = "" if it.get("verified", True) else f"{UNVERIFIED_MARK} "
            others = [sx for sx in it.get("sources") or [] if sx != main]
            src = f" 〔来源：{'；'.join(others)}〕" if others else ""
            lines.append(f"- {mark}{it['text']}{src}")
        lines.append("")
    if dropped:
        lines.append(f"（已剔除：{'、'.join(dropped)}）")
    return "\n".join(lines).strip()


_FILE_HEAD = re.compile(r"^【(文件|链接)：(.+?)】\s*$")
_MD_HEAD = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _heading_of(line: str) -> tuple[int, str] | None:
    """材料里的标题行：【文件：…】/【链接：…】为第 0 级，Markdown # 标题按井号数分级。"""
    m = _FILE_HEAD.match(line.strip())
    if m:
        return 0, m.group(2).strip()
    m = _MD_HEAD.match(line.strip())
    if m:
        return len(m.group(1)), m.group(2).strip()
    return None


def chunk_contexts(materials: str, chunks: list[str]) -> list[str]:
    """每片开头所处的标题路径（文件 › 页面 › 章节）。分片切在章节中间时，后面的片不知道自己属于哪个文件哪个页面，
    来源只能写「需求材料」、模块也只能猜；把路径随片送给模型。片自身开头就是标题的，路径只到它的上一级。"""
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


async def run_requirement_extraction(materials: str, llm: LLMClient, model: str | None = None) -> dict:
    """提取流水线：材料分片 → 各片并行摘录 → 按模块合并去重 → 逐条对照材料核验 → 整理稿。

    返回 {text, modules, dropped, model_name, chunks, checked, verified, unverified, source_chars}。
    """
    settings = get_settings()
    chunks = split_text(materials, settings.chunk_max_chars)
    system = prompt_text("requirement_extract")
    extra = profile_block()

    contexts = chunk_contexts(materials, chunks) if len(chunks) > 1 else [""]

    def _user(i: int, chunk: str) -> str:
        head = ""
        if len(chunks) > 1:
            where = f"本片开头的内容位于「{contexts[i]}」之下（上一片在这个标题下切开）；" if contexts[i] else ""
            head = (f"这是全部材料的第 {i + 1}/{len(chunks)} 片，{where}只摘录本片实际包含的内容，本片未涉及的模块不要生成；"
                    f"模块命名与来源沿用材料里的文件名 / 页面名 / 章节名，便于各片合并。\n\n")
        return f"{head}需求材料：\n{wrap_data('需求材料', chunk)}{extra}"

    from app.agents.service import gather_or_cancel

    outputs = await gather_or_cancel([
        _chat_json(llm, [{"role": "system", "content": system}, {"role": "user", "content": _user(i, chunk)}],
                   model, reasoning_effort="medium")
        for i, chunk in enumerate(chunks)
    ])
    modules, dropped, model_name = _merge_modules(outputs)
    if not modules:
        raise ValueError("模型没有从材料里提取出任何需求内容（材料可能不是需求文档）")
    stats = verify_modules(modules, materials)
    text = extraction_markdown(modules, dropped)
    logger.info("需求提取：材料 {} 字 → 整理稿 {} 字，{} 个模块 {} 条；逐字核验通过 {}，未找到 {}（分 {} 片，{}）",
                len(materials), len(text), len(modules), stats["checked"], stats["verified"], stats["unverified"],
                len(chunks), model_name)
    return {"text": text, "modules": modules, "dropped": dropped, "model_name": model_name, "chunks": len(chunks),
            "source_chars": len(materials), **stats}

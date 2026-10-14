"""需求中心 AI 需求分析流水线：分析 → 原文核验 → 独立复核。

准确率是系统指标而不是某一次模型输出的属性，这里按三类错误分别设防：
- 捏造：每条分析要点带原文依据（quote），程序逐条核对 quote 是否真在原文里。业务规则 / 边界条件 /
  状态变化 / 权限要求四项无依据的条目不进结果，降级为「必须确认」的待确认事项交人工拍板；
- 理解错：分析用最高推理强度，注入项目画像与知识库中的历史需求 / 业务规则（只用于理解术语与背景）；
  第二个独立 Agent 对照原文逐条复核，把数值 / 方向 / 条件写反的条目标出来并给出按原文改正的建议；
- 遗漏：复核 Agent 对照原文找明文写出但分析漏掉的内容补入（补入的条目同样过原文核验）；
  超长需求分片分析时先出全文大纲，跨章节引用不丢。
复核意见只标注不覆盖：由人工在页面上采纳、修改或删除，修改留痕进评测集与学习规则。
"""

from __future__ import annotations

import re
import unicodedata

from loguru import logger

from app.agents.context import profile_block
from app.agents.graph import _chat_json, outline_requirement
from app.agents.prompts import REQUIREMENT_ANALYSIS_KEYS, REQUIREMENT_ANALYSIS_LABELS, wrap_data
from app.config import get_settings
from app.llm.client import LLMClient
from app.parsers.chunking import split_text
from app.prompts import prompt_text

# 必须有原文依据的四项：进测试设计与业务规则库的"事实"，无依据即降级为待确认事项
EVIDENCE_KEYS = ("rules", "boundaries", "state_changes", "permissions")
ITEM_KEYS = tuple(k for k in REQUIREMENT_ANALYSIS_KEYS if k != "open_questions")
REVIEW_VERDICTS = ("wrong", "unsupported", "added")

# 全文送复核 Agent 的上限：超过只做原文核验不做复核（复核需要整篇对照）
REVIEW_MAX_CHARS = 60_000
# 原文核验：quote 规范化后最少字数（太短的片段到处都能匹配上，不算依据）
_MIN_QUOTE_CHARS = 4
# 分段容错：quote 按标点切段后命中段落字数占比达到该值即视为找到（PDF / Vision 解析的原文有断行与个别字差）
_SEGMENT_HIT_RATIO = 0.7

_STRIP_RE = re.compile(r"[\W_]+")
# 只按标点与换行切段，不按空格：按空格切会把「5 次」拆成孤立数字而失去数值校验
_SEG_SPLIT_RE = re.compile(r"[，。；：、,.;:!?！？\n\r()（）「」『』【】\[\]<>《》\"'“”‘’]+")


def item_text(x) -> str:
    """分析条目的文本：兼容旧数据（纯字符串）与新结构（{text, quote, …}）。"""
    if isinstance(x, dict):
        for k in ("text", "point", "content", "rule"):
            if isinstance(x.get(k), str) and x[k].strip():
                return x[k].strip()
        return ""
    return str(x).strip() if x is not None else ""


def as_item(x) -> dict:
    """规整为条目对象：text / quote / verified（True 有依据 / False 依据对不上 / None 未给依据）/ review（复核意见）。"""
    if isinstance(x, dict):
        quote = x.get("quote") or x.get("evidence") or ""
        out = {"text": item_text(x), "quote": str(quote).strip() if isinstance(quote, (str, int, float)) else "",
               "verified": x.get("verified") if isinstance(x.get("verified"), bool) else None}
        review = x.get("review")
        if isinstance(review, dict) and review.get("verdict") in REVIEW_VERDICTS:
            out["review"] = {"verdict": review["verdict"], "fix": str(review.get("fix") or ""), "note": str(review.get("note") or "")}
        return out
    return {"text": item_text(x), "quote": "", "verified": None}


def normalize(text: str) -> str:
    """核验用规范化：全角转半角、去掉空白与标点、小写。只比内容不比排版。"""
    return _STRIP_RE.sub("", unicodedata.normalize("NFKC", text or "")).lower()


def quote_found(quote: str, source_norm: str) -> bool:
    """quote 是否出现在（已规范化的）原文里：整段命中，或按标点切段后命中段落字数占比 ≥ 70%。"""
    q = normalize(quote)
    if len(q) < _MIN_QUOTE_CHARS or not source_norm:
        return False
    if q in source_norm:
        return True
    segs = [normalize(s) for s in _SEG_SPLIT_RE.split(quote)]
    segs = [s for s in segs if len(s) >= 3]
    if not segs:
        return False
    total = sum(len(s) for s in segs)
    hit = sum(len(s) for s in segs if s in source_norm)
    return hit / total >= _SEGMENT_HIT_RATIO


def _question(text: str, kind: str, key: str, item: dict, reason: str) -> dict:
    return {"question": text, "level": "must", "reason": reason, "assumption": "",
            "kind": kind, "key": key, "text": item["text"], "quote": item.get("quote", "")}


def verify_analysis(analysis: dict, source: str) -> dict:
    """原文核验：逐条核对 quote；四个事实项无依据的条目移出结果、降级为待确认事项。返回新的分析结果（含 verification 统计）。"""
    src = normalize(source)
    out: dict = {}
    stats = {"checked": 0, "verified": 0, "unverified": 0, "demoted": 0}
    demoted: list[tuple[str, dict]] = []
    for key in ITEM_KEYS:
        items: list[dict] = []
        seen: set[str] = set()
        for raw in analysis.get(key) or []:
            it = as_item(raw)
            if not it["text"] or normalize(it["text"]) in seen:
                continue
            seen.add(normalize(it["text"]))
            if it["quote"]:
                stats["checked"] += 1
                it["verified"] = quote_found(it["quote"], src)
                stats["verified" if it["verified"] else "unverified"] += 1
            else:
                it["verified"] = None
            if key in EVIDENCE_KEYS and not it["verified"]:
                stats["demoted"] += 1
                demoted.append((key, it))
                continue
            items.append(it)
        out[key] = items
    questions = [q for q in (analysis.get("open_questions") or []) if q]
    for key, it in demoted:
        label = REQUIREMENT_ANALYSIS_LABELS[key]
        why = (f"AI 给出的依据「{it['quote']}」在原文中对不上" if it["quote"] else "AI 未给出原文依据")
        questions.append(_question(
            f"AI 整理的{label}「{it['text']}」在需求原文中找不到依据，是否确实如此？", "unverified", key, it,
            f"{why}；无依据的内容不作为事实进入测试设计，请确认是否成立（成立请补充到需求原文或人工补充说明后重新分析）"))
    out["open_questions"] = questions
    out["verification"] = stats
    return out


def _item_listing(analysis: dict) -> str:
    parts = []
    for key in ITEM_KEYS:
        items = analysis.get(key) or []
        if items:
            parts.append(f"## {key}（{REQUIREMENT_ANALYSIS_LABELS[key]}）\n" + "\n".join(
                f"{i + 1}. {it['text']}" + (f"　（依据：{it['quote']}）" if it.get("quote") else "")
                for i, it in enumerate(items)))
    return "\n\n".join(parts)


def apply_review(analysis: dict, data: dict, source: str, model_name: str = "") -> dict:
    """把复核 Agent 的意见合并进分析结果：问题标在条目上（不改原文本），遗漏项核验后补入，无依据的补入项转待确认事项。"""
    src = normalize(source)
    stats = {"model": model_name, "wrong": 0, "unsupported": 0, "added": 0, "unverified_added": 0}
    for issue in data.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        key, verdict = issue.get("key"), str(issue.get("verdict") or "").strip().lower()
        try:
            n = int(issue.get("n") or 0) - 1
        except (TypeError, ValueError):
            continue
        items = analysis.get(key) or []
        if key not in ITEM_KEYS or verdict not in ("wrong", "unsupported") or not (0 <= n < len(items)):
            continue
        items[n]["review"] = {"verdict": verdict, "fix": str(issue.get("fix") or "").strip(),
                              "note": str(issue.get("note") or "").strip()}
        stats[verdict] += 1
    for m in data.get("missing") or []:
        if not isinstance(m, dict):
            continue
        key, it = m.get("key"), as_item(m)
        if key not in ITEM_KEYS or not it["text"]:
            continue
        if normalize(it["text"]) in {normalize(x["text"]) for x in analysis.get(key) or []}:
            continue
        it["verified"] = quote_found(it["quote"], src) if it["quote"] else None
        label = REQUIREMENT_ANALYSIS_LABELS[key]
        if key in EVIDENCE_KEYS and not it["verified"]:
            stats["unverified_added"] += 1
            analysis.setdefault("open_questions", []).append(_question(
                f"独立复核认为需求中还有{label}「{it['text']}」，但未找到原文依据，是否成立？", "review", key, it,
                "复核 Agent 补充的内容没有通过原文核验，请人工判断"))
            continue
        it["review"] = {"verdict": "added", "fix": "", "note": "独立复核认为首轮分析遗漏了这一条"}
        analysis.setdefault(key, []).append(it)
        stats["added"] += 1
    analysis["review"] = stats
    return analysis


async def review_analysis(llm: LLMClient, text: str, analysis: dict, model: str | None) -> dict:
    """独立复核 Agent：对照原文挑理解错 / 无依据 / 遗漏（与分析 Agent 分离，可用不同模型）。"""
    user = (wrap_data("需求原文", text) + "\n\n" + wrap_data("需求分析结果", _item_listing(analysis)) + profile_block())
    data, result = await _chat_json(
        llm, [{"role": "system", "content": prompt_text("requirement_analysis_review")},
              {"role": "user", "content": user}], model, reasoning_effort="high")
    return apply_review(analysis, data, text, result.model_name)


def _merge_outputs(outputs) -> tuple[dict, str]:
    merged: dict = {k: [] for k in REQUIREMENT_ANALYSIS_KEYS}
    model_name = ""
    seen_q: set[str] = set()
    for data, result in outputs:
        model_name = result.model_name
        for key in REQUIREMENT_ANALYSIS_KEYS:
            for item in data.get(key) or []:
                if key == "open_questions":
                    meta = item if isinstance(item, dict) else {"question": str(item)}
                    q = str(meta.get("question", "")).strip()
                    if not q or q in seen_q:
                        continue
                    seen_q.add(q)
                    level = str(meta.get("level", "must")).strip().lower()
                    merged[key].append({"question": q, "level": level if level in ("must", "suggest") else "must",
                                        "reason": str(meta.get("reason", "")).strip(),
                                        "assumption": str(meta.get("assumption", "")).strip()})
                else:
                    it = as_item(item)
                    if it["text"]:
                        merged[key].append(it)
    return merged, model_name


async def run_requirement_analysis(
    text: str, llm: LLMClient, model: str | None = None, *, knowledge_refs: str | None = None,
    review: bool | None = None, reviewer_model: str | None = None,
) -> dict:
    """需求中心 AI 需求分析：11 项结构化输出 → 原文核验 → 独立复核。超长需求先出全文大纲再分片，逐项合并去重。"""
    from app.agents.service import _outline_for, gather_or_cancel

    settings = get_settings()
    chunks = split_text(text, settings.chunk_max_chars)
    extra = profile_block()
    if knowledge_refs:
        extra += prompt_text("knowledge_refs_block").format(knowledge=wrap_data("相关历史需求与业务规则", knowledge_refs))
    outline = await _outline_for(llm, text, chunks, model)
    system = prompt_text("requirement_analysis")

    def _user(i: int, chunk: str) -> str:
        head = ""
        if len(chunks) > 1:
            head = (f"这是一份长需求的第 {i + 1}/{len(chunks)} 片，只整理本片实际包含的内容，本片未涉及的不要凭空生成；"
                    f"quote 必须取自本片原文。")
            if outline:
                head += "\n全文模块大纲（供理解上下文与统一命名）：\n" + wrap_data("全文模块大纲", "\n".join(outline))
            head += "\n\n"
        return f"{head}需求原文：\n{wrap_data('需求原文', chunk)}{extra}"

    outputs = await gather_or_cancel([
        _chat_json(llm, [{"role": "system", "content": system}, {"role": "user", "content": _user(i, chunk)}],
                   model, reasoning_effort="high")
        for i, chunk in enumerate(chunks)
    ])
    merged, model_name = _merge_outputs(outputs)
    analysis = verify_analysis(merged, text)
    v = analysis["verification"]
    logger.info("需求分析原文核验：核对 {} 条，通过 {}，不符 {}，降级为待确认 {}", v["checked"], v["verified"], v["unverified"], v["demoted"])
    do_review = settings.analysis_review if review is None else review
    if do_review and len(text) <= REVIEW_MAX_CHARS:
        try:
            analysis = await review_analysis(llm, text, analysis, reviewer_model or model)
            r = analysis["review"]
            logger.info("需求分析独立复核（{}）：理解错 {}，无依据 {}，补充 {}，补充未通过核验 {}",
                        r["model"], r["wrong"], r["unsupported"], r["added"], r["unverified_added"])
        except Exception as e:  # 复核是增强环节：失败不影响首轮结果
            logger.warning("需求分析独立复核失败，保留首轮结果：{}", e)
            analysis["review"] = {"model": reviewer_model or model or "", "error": str(e)[:200]}
    elif do_review:
        analysis["review"] = {"model": "", "skipped": f"原文 {len(text)} 字超过复核上限 {REVIEW_MAX_CHARS} 字"}
    analysis["model_name"] = model_name
    analysis["chunks"] = len(chunks)
    return analysis

"""需求盲区闭环：拆解时模型标出的「疑似需求盲区」不再只读展示，而是逐条有状态、有去向。

- 盲区条目来源：拆解结果 analysis.blind_spots / 生成结果 result.blind_spots（去重），
  以及分片盲区复核剔除项 resolved_blind_spots（AI 已用需求其他分片说明，默认标为「需求已说明」）；
- 状态：open（待处置）/ question（已转需求待确认事项）/ point（已转测试点）/
  explained（需求已说明，不是盲区）/ ignored（忽略）；
- 处置记录存 TaskRecord.blind_spot_reviews（bs_id -> 记录），条目本身仍以文本为准，
  bs_id 由规范化文本哈希得到，拆解重跑后同一文本的处置记录仍然有效。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from functools import lru_cache

STATUSES = ("open", "question", "point", "explained", "ignored")
ACTIONS = {"to_question": "question", "to_point": "point", "explain": "explained", "ignore": "ignored", "reopen": "open"}
STATUS_TEXT = {"open": "待处置", "question": "已转待确认事项", "point": "已转测试点", "explained": "需求已说明", "ignored": "已忽略"}


def _norm(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(text or "")).lower()


def bs_id(text: str) -> str:
    return "bs" + hashlib.sha1(_norm(text).encode("utf-8")).hexdigest()[:8]


def _texts(record, extra: list[str] | None = None) -> list[str]:
    seen: dict[str, str] = {}
    for source in ((record.analysis or {}).get("blind_spots"), (record.result or {}).get("blind_spots"), extra):
        for b in source or []:
            text = str(b).strip()
            if text and _norm(text) not in seen:
                seen[_norm(text)] = text
    return list(seen.values())


def _ai_resolved(record) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for source in ((record.analysis or {}).get("resolved_blind_spots"), (record.result or {}).get("resolved_blind_spots")):
        for item in source or []:
            if isinstance(item, dict) and str(item.get("text", "")).strip():
                out.setdefault(bs_id(item["text"]), item)
    return out


# ---- 盲区出处：模型给的 {module, ref} 优先，没有的按文字里的章节号在整理稿标题里反查 ----

_SEC_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+){1,3})(?![\d.])")
_HEAD_RE = re.compile(r"^(#{1,4}) (?:(\d+(?:\.\d+)*)\.? )?(.+?)\s*$")
_ANNOT_RE = re.compile(r"\s*〔[^〕]*〕")
_REQ_TITLE_RE = re.compile(r"^# 「(.+?)」需求整理稿")


@lru_cache(maxsize=16)
def _headings(requirement: str) -> tuple[tuple[int, str, str, str], ...]:
    """整理稿的编号标题 (层级, 编号, 标题, 所属需求标题)；多需求合并的简报每份整理稿各自从 1 编号，靠需求标题区分。"""
    out: list[tuple[int, str, str, str]] = []
    req_title = ""
    for line in requirement.splitlines():
        m = _REQ_TITLE_RE.match(line)
        if m:
            req_title = m.group(1)
            continue
        h = _HEAD_RE.match(line)
        if h and h.group(2):
            out.append((len(h.group(1)), h.group(2), _ANNOT_RE.sub("", h.group(3)).strip(), req_title))
    return tuple(out)


def locate_blind_spot(text: str, requirement: str, modules: list[dict] | None = None, hint_module: str = "") -> dict:
    """按盲区文字开头的章节号（如「3.4.7“按天排重”标为待定」）反查出处：
    ref = 需求标题 › 2.1 活动列表 › 2.1.3 创建功能说明（祖先链），module = 拆解结果里名字含该页面 / 区块标题的模块。
    找不到章节号或标题时返回空 dict。"""
    if not text or not requirement:
        return {}
    heads = _headings(requirement)
    nums = _SEC_RE.findall(text[:80])
    for num in nums:
        hits = [h for h in heads if h[1] == num]
        if not hits:
            continue
        hit = next((h for h in hits if hint_module and h[3] and hint_module.startswith(h[3])), hits[0])
        level, _, _, req_title = hit
        chain = []
        parts = num.split(".")
        for k in range(1, len(parts) + 1):
            prefix = ".".join(parts[:k])
            anc = next((h for h in heads if h[1] == prefix and h[3] == req_title), None)
            if anc:
                chain.append(f"{anc[1]} {anc[2]}")
        ref = " › ".join(([req_title] if req_title else []) + chain)
        titles = [h[2] for h in heads if h[3] == req_title and num.startswith(h[1]) and num[len(h[1]):len(h[1]) + 1] in ("", ".")]
        module = ""
        best = 0
        for m in modules or []:
            name = str(m.get("module", "")) if isinstance(m, dict) else str(m)
            score = sum(1 for t in titles if t and t in name)
            if score > best:
                best, module = score, name
        return {"module": module, "ref": ref}
    return {}


def _refs(record) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for source in ((record.analysis or {}).get("blind_spot_refs"), (record.result or {}).get("blind_spot_refs")):
        for text, loc in (source or {}).items():
            if isinstance(loc, dict):
                out.setdefault(_norm(text), {k: str(loc.get(k) or "") for k in ("module", "ref")})
    return out


def blind_spot_entities(record, extra_texts: list[str] | None = None) -> list[dict]:
    """任务的盲区条目视图：待处置项在前；人工处置优先于 AI 复核结论。每条带出处 module / ref（有才带）。

    extra_texts：尚未写入 record 的盲区文本（生成完成落盘前算质量摘要时传入）。
    """
    reviews = record.blind_spot_reviews or {}
    resolved = _ai_resolved(record)
    refs = _refs(record)
    requirement = str((record.context or {}).get("requirement") or "")
    modules = (record.analysis or {}).get("test_points") or []
    entities: list[dict] = []
    seen: set[str] = set()

    def _entity(text: str, default: dict) -> dict:
        key = bs_id(text)
        base = {"bs_id": key, "text": text, "status": "open", "note": "", "by": None, "at": None, "target": None}
        loc = refs.get(_norm(text)) or {}
        if not loc.get("ref"):
            found = locate_blind_spot(text, requirement, modules, loc.get("module", ""))
            loc = {"module": loc.get("module") or found.get("module", ""), "ref": found.get("ref", "")}
        base.update({k: v for k, v in loc.items() if v})
        base.update(default)
        rv = reviews.get(key)
        if rv:
            base.update({k: v for k, v in rv.items() if k != "text"})
        return base

    for text in _texts(record, extra_texts):
        seen.add(bs_id(text))
        entities.append(_entity(text, {}))
    for key, item in resolved.items():
        if key in seen:
            continue
        seen.add(key)
        entities.append(_entity(item["text"], {
            "status": "explained", "by": "AI 复核", "at": None,
            "note": f"需求第 {item.get('chunk', '?')} 片已说明：{item.get('evidence', '')}".rstrip("："),
            "ai_resolved": True,
        }))
    for key, rv in reviews.items():  # 处置过但文本已不在当前结果里（拆解重跑）：仍保留记录
        if key not in seen and rv.get("text"):
            seen.add(key)
            entities.append(_entity(rv["text"], {"stale": True}))
    order = {s: i for i, s in enumerate(STATUSES)}
    entities.sort(key=lambda e: order.get(e["status"], 9))
    return entities


def open_blind_spots(record, texts: list[str] | None = None) -> list[str]:
    """仍待处置的盲区文本（质量摘要「待确认问题」只数这些）；AI 复核剔除后被人工重开的也算。"""
    return [e["text"] for e in blind_spot_entities(record, texts) if e["status"] == "open" and not e.get("stale")]


def find_blind_spot(record, key: str) -> dict | None:
    return next((e for e in blind_spot_entities(record) if e["bs_id"] == key), None)


def record_blind_spot_action(record, entity: dict, action: str, by: str | None, note: str = "",
                             target: dict | None = None) -> dict:
    """落一条处置记录并返回更新后的条目。reopen 清空去向，但保留上一次处置的备注供追溯。"""
    if action not in ACTIONS:
        raise ValueError(f"不支持的盲区处置动作: {action}")
    status = ACTIONS[action]
    reviews = record.blind_spot_reviews if record.blind_spot_reviews is not None else {}
    prev = reviews.get(entity["bs_id"]) or {}
    history = list(prev.get("history") or [])
    if prev.get("status"):
        history.append({k: prev.get(k) for k in ("status", "note", "by", "at", "target")})
    reviews[entity["bs_id"]] = {
        "text": entity["text"], "status": status, "note": (note or "").strip(),
        "by": by, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target": None if status == "open" else target, "history": history[-10:],
    }
    record.blind_spot_reviews = reviews
    return find_blind_spot(record, entity["bs_id"]) or entity

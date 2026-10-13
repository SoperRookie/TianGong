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


def blind_spot_entities(record, extra_texts: list[str] | None = None) -> list[dict]:
    """任务的盲区条目视图：待处置项在前；人工处置优先于 AI 复核结论。

    extra_texts：尚未写入 record 的盲区文本（生成完成落盘前算质量摘要时传入）。
    """
    reviews = record.blind_spot_reviews or {}
    resolved = _ai_resolved(record)
    entities: list[dict] = []
    seen: set[str] = set()

    def _entity(text: str, default: dict) -> dict:
        key = bs_id(text)
        base = {"bs_id": key, "text": text, "status": "open", "note": "", "by": None, "at": None, "target": None}
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

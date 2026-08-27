"""用例差异对比（F-6-8）：离线评审终稿 vs 系统生成结果。

匹配策略：优先按用例标题（离线编辑常重排编号），同题多条时按出现顺序配对。
产出字段级差异与采纳统计，是离线通道学习语料（归因/Prompt 优化）的数据源。
"""

from collections import defaultdict

# 参与字段级对比的规范字段
_DIFF_FIELDS = ("module", "priority", "precondition", "keywords", "remark")


def _steps_text(case: dict) -> str:
    return "\n".join(
        f"{s.get('action', '')} => {s.get('expected', '')}" for s in case.get("steps") or []
    )


def _norm_title(case: dict) -> str:
    return str(case.get("title", "")).strip()


def diff_cases(generated: list[dict], final: list[dict]) -> dict:
    """对比生成结果与人工终稿，返回 {added, deleted, modified, unchanged, stats}。"""
    gen_by_title: dict[str, list[dict]] = defaultdict(list)
    for case in generated:
        gen_by_title[_norm_title(case)].append(case)

    added: list[dict] = []
    modified: list[dict] = []
    unchanged = 0
    for case in final:
        pool = gen_by_title.get(_norm_title(case))
        if not pool:
            added.append({"title": _norm_title(case), "case": case})
            continue
        origin = pool.pop(0)  # 同题多条按顺序配对
        changes = _field_changes(origin, case)
        if changes:
            modified.append({"title": _norm_title(case), "changes": changes})
        else:
            unchanged += 1

    deleted = [
        {"title": title, "case": case}
        for title, pool in gen_by_title.items()
        for case in pool
    ]

    total = len(generated)
    stats = {
        "generated": total,
        "final": len(final),
        "added": len(added),
        "deleted": len(deleted),
        "modified": len(modified),
        "unchanged": unchanged,
        # 完全采纳率：未被删改的用例占生成总数比例（试点验收口径：≥50%）
        "adoption_rate": round(unchanged / total, 3) if total else 0.0,
    }
    return {
        "added": added,
        "deleted": deleted,
        "modified": modified,
        "stats": stats,
    }


def _field_changes(origin: dict, final: dict) -> list[dict]:
    changes: list[dict] = []
    for field in _DIFF_FIELDS:
        before = str(origin.get(field, "") or "").strip()
        after = str(final.get(field, "") or "").strip()
        if before != after:
            changes.append({"field": field, "before": before, "after": after})
    before_steps = _steps_text(origin).strip()
    after_steps = _steps_text(final).strip()
    if before_steps != after_steps:
        changes.append({"field": "steps", "before": before_steps, "after": after_steps})
    return changes

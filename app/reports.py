"""报表聚合：从任务留痕计算平台运行指标（纯确定性统计，不调模型）。

口径说明：
- AI 一次通过率：completed 任务中 result.passed 的占比（评审回环内收敛）；
- 完全采纳率：离线终稿回传 diff 的 adoption_rate 均值（试点验收口径 ≥50%）；
- 人工审核动作：用例审核 review_log 与测试点审核 point_review_log 合并计数；
- 趋势按任务创建日聚合。
"""

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from app.tasks.store import TaskRecord

_PRIORITIES = ("P0", "P1", "P2", "P3")
_STATUSES = ("completed", "awaiting_confirmation", "running", "queued", "failed")
_ACTIONS = ("approve", "reject", "modify", "delete")


def _day(iso: str) -> str:
    return (iso or "")[:10]


def summarize(
    records: list[TaskRecord], days: int = 30, project: str | None = None
) -> dict:
    """聚合报表数据。days=0 表示全部历史；project 过滤指定项目。"""
    since = None
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = [
        r for r in records
        if (since is None or r.created_at >= since)
        and (project is None or (r.context or {}).get("project") == project)
    ]

    totals = {
        "tasks": len(rows), "cases": 0, "review_rounds_sum": 0, "completed": 0,
        "passed": 0, "adoption_sum": 0.0, "adoption_samples": 0, "fix_runs": 0,
    }
    status_counter: Counter = Counter()
    priority: Counter = Counter({p: 0 for p in _PRIORITIES})
    case_states: Counter = Counter({"approved": 0, "pending": 0, "rejected": 0})
    review_actions: Counter = Counter({a: 0 for a in _ACTIONS})
    trend: dict[str, dict] = defaultdict(lambda: {"tasks": 0, "cases": 0})
    projects: dict[str, dict] = defaultdict(lambda: {"tasks": 0, "cases": 0})
    creators: dict[str, dict] = defaultdict(lambda: {"tasks": 0, "cases": 0, "reviews": 0})

    for r in rows:
        status_counter[r.status] += 1
        cases = (r.result or {}).get("cases", [])
        day = _day(r.created_at)
        trend[day]["tasks"] += 1
        trend[day]["cases"] += len(cases)
        proj = (r.context or {}).get("project") or "（未指定）"
        projects[proj]["tasks"] += 1
        projects[proj]["cases"] += len(cases)
        creator = r.created_by or "（未记录）"
        creators[creator]["tasks"] += 1
        creators[creator]["cases"] += len(cases)
        totals["cases"] += len(cases)
        for c in cases:
            if c.get("priority") in priority:
                priority[c["priority"]] += 1
        if r.status == "completed" and r.result:
            totals["completed"] += 1
            totals["review_rounds_sum"] += int(r.result.get("review_rounds", 0) or 0)
            if r.result.get("passed"):
                totals["passed"] += 1
        for state in r.case_reviews.values():
            case_states[state.get("status", "pending")] += 1
        for log in (r.review_log, r.point_review_log):
            for entry in log:
                action = entry.get("action")
                if action in review_actions:
                    review_actions[action] += 1
                by = entry.get("by")
                if by:
                    creators[by]["reviews"] += 1
        adoption = ((r.offline_review or {}).get("stats") or {}).get("adoption_rate")
        if adoption is not None:
            totals["adoption_sum"] += float(adoption)
            totals["adoption_samples"] += 1
        totals["fix_runs"] += len(r.fix_log)

    completed = totals["completed"]
    return {
        "totals": {
            "tasks": totals["tasks"],
            "cases": totals["cases"],
            "ai_pass_rate": round(totals["passed"] / completed, 3) if completed else None,
            "avg_review_rounds": round(totals["review_rounds_sum"] / completed, 2) if completed else None,
            "adoption_rate": (
                round(totals["adoption_sum"] / totals["adoption_samples"], 3)
                if totals["adoption_samples"] else None
            ),
            "adoption_samples": totals["adoption_samples"],
            "fix_runs": totals["fix_runs"],
        },
        "status": {s: status_counter.get(s, 0) for s in _STATUSES},
        "priority": dict(priority),
        "case_states": dict(case_states),
        "review_actions": dict(review_actions),
        "trend": [
            {"date": d, **trend[d]} for d in sorted(trend)
        ],
        "projects": sorted(
            ({"project": p, **v} for p, v in projects.items()),
            key=lambda x: -x["cases"],
        )[:10],
        "creators": sorted(
            ({"user": u, **v} for u, v in creators.items()),
            key=lambda x: -x["tasks"],
        )[:10],
    }

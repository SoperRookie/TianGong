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


UNASSIGNED = "（未指定）"


def _case_exec(record: TaskRecord, uid: str) -> dict | None:
    """用例最新执行结果（旧任务级轮次，仅剩未迁移的存量数据）。"""
    for run in reversed(record.executions):
        result = run["results"].get(uid)
        if result:
            return {
                "status": result["status"], "note": result.get("note", ""),
                "run": run.get("name", ""), "at": result.get("at", ""), "by": result.get("by", ""),
            }
    return None


def _legacy_exec_count(record: TaskRecord, uid: str) -> int:
    return sum(
        1 + len(r.get("history") or [])
        for run in record.executions for u, r in (run.get("results") or {}).items() if u == uid
    )


def plan_exec_index(plans_list: list[dict]) -> dict[tuple[str, str], dict]:
    """(task_id, uid) -> {count, latest}：来自测试计划执行（M4 起执行统一挂计划）。

    count 为被执行总次数（同轮复测按 history 计入）；latest 为最近一次结果。
    """
    idx: dict[tuple[str, str], dict] = {}
    for plan in plans_list:
        by_item = {i["item_id"]: i for i in plan["items"]}
        for run in plan["runs"]:
            for item_id, res in (run.get("results") or {}).items():
                item = by_item.get(item_id)
                if item is None:
                    continue
                key = (item["task_id"], item["uid"])
                slot = idx.setdefault(key, {"count": 0, "latest": None})
                slot["count"] += 1 + len(res.get("history") or [])
                entry = {
                    "status": res["status"], "note": res.get("note", ""),
                    "reason": res.get("reason", ""), "run": run.get("name", ""),
                    "plan": plan.get("name", ""), "at": res.get("at", ""), "by": res.get("by", ""),
                }
                if slot["latest"] is None or entry["at"] >= slot["latest"]["at"]:
                    slot["latest"] = entry
    return idx


def project_rollup(records: list[TaskRecord]) -> list[dict]:
    """项目汇总：任务/用例产出 + 用例生命周期分布 + 执行情况 + 最近活动。"""
    projects: dict[str, dict] = {}
    for r in records:
        name = (r.context or {}).get("project") or UNASSIGNED
        p = projects.setdefault(name, {
            "project": name, "tasks": 0, "cases": 0,
            "pending": 0, "rejected": 0, "approved": 0,
            "executed": 0, "exec_pass": 0, "last_activity": "",
        })
        p["tasks"] += 1
        p["last_activity"] = max(p["last_activity"], r.created_at or "")
        cases = (r.result or {}).get("cases", [])
        p["cases"] += len(cases)
        for c in cases:
            uid = str(c.get("uid") or "")
            state = (r.case_reviews.get(uid) or {}).get("status", "pending")
            p[state if state in ("pending", "rejected", "approved") else "pending"] += 1
            exec_result = _case_exec(r, uid)
            if exec_result:
                p["executed"] += 1
                if exec_result["status"] == "pass":
                    p["exec_pass"] += 1
    return sorted(projects.values(), key=lambda x: x["last_activity"], reverse=True)


def project_cases(
    records: list[TaskRecord], project: str | None, plans: list[dict] | None = None
) -> list[dict]:
    """用例库：跨任务聚合全部用例，标注生命周期阶段与执行情况（project=None 为全库）。

    生命周期：待审核（AI 生成/修改后）→ 已驳回（待定点修改）→ 正式（通过锁定）；
    执行情况来自测试计划（exec 最新结果 + exec_count 被执行总次数），兼容旧任务级轮次。
    """
    exec_idx = plan_exec_index(plans) if plans else {}
    rows: list[dict] = []
    for r in sorted(records, key=lambda x: x.created_at or "", reverse=True):
        name = (r.context or {}).get("project") or UNASSIGNED
        if project is not None and name != project:
            continue
        for c in (r.result or {}).get("cases", []):
            uid = str(c.get("uid") or "")
            state = r.case_reviews.get(uid) or {}
            planned = exec_idx.get((r.task_id, uid))
            rows.append({
                "task_id": r.task_id, "project": name,
                "case_id": c.get("case_id"), "uid": uid,
                "version": int(c.get("version", 1) or 1),
                "steps": c.get("steps") or [],
                "module": c.get("module", ""), "title": c.get("title", ""),
                "priority": c.get("priority", ""), "keywords": c.get("keywords", ""),
                "review": state.get("status", "pending"),
                "locked": bool(state.get("locked")),
                "review_comment": state.get("comment", ""),
                "exec": (planned or {}).get("latest") or _case_exec(r, uid),
                "exec_count": (planned or {}).get("count", 0) + _legacy_exec_count(r, uid),
                "created_at": r.created_at,
                "created_by": r.created_by,
            })
    return rows


def _execution_summary(
    plans_list: list[dict], project: str | None, since: str | None
) -> dict:
    """用例执行情况统计（测试计划口径）：轮次/已执行用例/总次数/结果分布/失败分类。"""
    status: Counter = Counter({s: 0 for s in ("pass", "fail", "blocked", "skipped")})
    fail_reasons: Counter = Counter()
    people: dict[str, dict] = {}  # 人员维度（18 章）：执行人 -> 执行次数/结果分布/代执行
    executed: set[tuple[str, str]] = set()
    runs = 0
    executions = 0
    plans_hit: set[str] = set()
    for plan in plans_list:
        if project is not None and plan.get("project") != project:
            continue
        by_item = {i["item_id"]: i for i in plan["items"]}
        for run in plan["runs"]:
            if not since or (run.get("started_at") or "") >= since:
                runs += 1
                plans_hit.add(plan["plan_id"])
            for item_id, res in (run.get("results") or {}).items():
                if since and (res.get("at") or "") < since:
                    continue
                plans_hit.add(plan["plan_id"])
                status[res["status"]] += 1
                executions += 1 + len(res.get("history") or [])
                for rec in [res, *(res.get("history") or [])]:
                    who = rec.get("by") or ""
                    if not who:
                        continue
                    pp = people.setdefault(who, {"username": who, "executions": 0, "cases": set(),
                                                 "proxied": 0, "pass": 0, "fail": 0, "blocked": 0, "skipped": 0})
                    pp["executions"] += 1
                    pp["cases"].add((plan["plan_id"], item_id))
                    if rec.get("status") in ("pass", "fail", "blocked", "skipped"):
                        pp[rec["status"]] += 1
                    if rec.get("on_behalf_of"):
                        pp["proxied"] += 1
                item = by_item.get(item_id)
                executed.add((item["task_id"], item["uid"]) if item else (plan["plan_id"], item_id))
                if res["status"] == "fail" and res.get("reason"):
                    fail_reasons[res["reason"]] += 1
    total = sum(status.values())
    return {
        "plans": len(plans_hit),
        "runs": runs,
        "executed_cases": len(executed),
        "executions": executions,
        "status": dict(status),
        "pass_rate": round(status["pass"] / total, 3) if total else None,
        "fail_reasons": sorted(
            ({"reason": k, "count": v} for k, v in fail_reasons.items()),
            key=lambda x: -x["count"],
        ),
        "people": sorted(
            ({**p, "cases": len(p["cases"]),
              "pass_rate": round(p["pass"] / (p["pass"] + p["fail"] + p["blocked"] + p["skipped"]), 3)
              if (p["pass"] + p["fail"] + p["blocked"] + p["skipped"]) else None}
             for p in people.values()),
            key=lambda x: -x["executions"],
        ),
    }


def summarize(
    records: list[TaskRecord], days: int = 30, project: str | None = None,
    plans: list[dict] | None = None,
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
        "execution": _execution_summary(plans or [], project, since),
    }

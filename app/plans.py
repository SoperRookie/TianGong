"""测试计划（完整需求 12/13 章）：计划实体 + 用例快照 + 任务分配 + 计划执行。

- 计划实体：名称/项目/负责人/周期/状态；
- 用例快照（M3 冻结约定「快照引用方式」）：加入计划即拷贝完整内容快照，并引用
  entity_versions 的 (task_id, "case", uid, version_no)；正式用例后续修改不影响计划快照；
- 只能加入「已通过」（approved）用例，支持模块/优先级/关键词筛选；
- 任务分配（13 章）：按用例/按模块分配与重新分配，留痕原执行人/新执行人/操作人；
- 计划执行：执行轮次挂计划（旧任务级轮次在启动时迁移为自动建的计划），执行附件挂轮次。
"""

import copy
import uuid
from datetime import datetime, timezone

PLAN_STATUSES = {
    "not_started": "未开始",
    "in_progress": "进行中",
    "done": "已完成",
    "archived": "已归档",
}


class PlanError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hex8() -> str:
    return uuid.uuid4().hex[:8]


class PlanStore:
    def __init__(self):
        from app.db import DocStore

        self._doc = DocStore("plans")
        self._plans: dict[str, dict] = self._doc.load_all()

    def _persist(self, plan: dict) -> None:
        self._doc.put(plan["plan_id"], plan)

    def save(self, plan: dict) -> None:
        self._plans[plan["plan_id"]] = plan
        self._persist(plan)

    def get(self, plan_id: str) -> dict | None:
        return self._plans.get(plan_id)

    def list(self, project: str | None = None) -> list[dict]:
        plans = sorted(self._plans.values(), key=lambda p: p["created_at"], reverse=True)
        if project:
            plans = [p for p in plans if p.get("project") == project]
        return plans

    def create(
        self, name: str, project: str, owner: str = "", start_date: str = "",
        end_date: str = "", created_by: str | None = None,
    ) -> dict:
        name = (name or "").strip()
        if not name:
            raise PlanError("计划名称不能为空")
        if not (project or "").strip():
            raise PlanError("计划必须挂在项目下")
        plan = {
            "plan_id": _hex8(),
            "name": name,
            "project": project.strip(),
            "owner": (owner or "").strip() or (created_by or ""),
            "start_date": (start_date or "").strip(),
            "end_date": (end_date or "").strip(),
            "status": "not_started",
            "created_by": created_by,
            "created_at": _now(),
            "items": [],   # 用例快照
            "runs": [],    # 执行轮次（结果按 item_id 记录）
        }
        self.save(plan)
        return plan

    def update(self, plan_id: str, fields: dict) -> dict:
        plan = self._plans.get(plan_id)
        if plan is None:
            raise PlanError(f"测试计划不存在: {plan_id}")
        if "name" in fields:
            name = (fields["name"] or "").strip()
            if not name:
                raise PlanError("计划名称不能为空")
            plan["name"] = name
        for key in ("owner", "start_date", "end_date"):
            if key in fields and fields[key] is not None:
                plan[key] = str(fields[key]).strip()
        if "status" in fields and fields["status"]:
            if fields["status"] not in PLAN_STATUSES:
                raise PlanError(
                    f"未知计划状态: {fields['status']}（可用 {'/'.join(PLAN_STATUSES)}）"
                )
            plan["status"] = fields["status"]
        self._persist(plan)
        return plan

    def delete(self, plan_id: str) -> None:
        plan = self._plans.get(plan_id)
        if plan is None:
            raise PlanError(f"测试计划不存在: {plan_id}")
        if plan["runs"]:
            raise PlanError("计划已有执行记录，不可删除，请改为归档")
        self._plans.pop(plan_id)
        self._doc.remove(plan_id)


# ---- 用例快照（12 章 / M3 冻结约定「快照引用方式」）----


def snapshot_item(task_id: str, case: dict, version_no: int, by: str | None) -> dict:
    """加入计划即定格：完整内容快照 + 版本记录引用，此后正式用例任何修改不回写。"""
    return {
        "item_id": _hex8(),
        "task_id": task_id,
        "uid": str(case.get("uid") or ""),
        "case_id": str(case.get("case_id") or ""),
        "title": case.get("title", ""),
        "module": case.get("module", ""),
        "priority": case.get("priority", ""),
        "version_no": version_no,  # 引用 entity_versions(task_id, "case", uid, version_no)
        "snapshot": copy.deepcopy(case),
        "added_by": by,
        "added_at": _now(),
        "assignee": None,
        "assign_log": [],
    }


def match_case(case: dict, module: str = "", priority: str = "", keyword: str = "") -> bool:
    """加入计划前的筛选（12.3）：模块 / 优先级 / 关键词（标题、keywords 字段）。"""
    if module and case.get("module", "") != module:
        return False
    if priority and case.get("priority", "") != priority:
        return False
    if keyword:
        haystack = f"{case.get('title', '')} {case.get('keywords', '')} {case.get('case_id', '')}"
        if keyword.lower() not in haystack.lower():
            return False
    return True


def assign_items(plan: dict, item_ids: list[str], assignee: str, by: str) -> list[dict]:
    """分配 / 重新分配（13 章）：留痕原执行人 → 新执行人 + 操作人。"""
    by_id = {i["item_id"]: i for i in plan["items"]}
    missing = [x for x in item_ids if x not in by_id]
    if missing:
        raise PlanError(f"用例不在计划中: {'、'.join(missing)}")
    changed = []
    now = _now()
    for item_id in item_ids:
        item = by_id[item_id]
        if item.get("assignee") == assignee:
            continue
        item["assign_log"].append(
            {"prev": item.get("assignee"), "assignee": assignee, "by": by, "at": now}
        )
        item["assignee"] = assignee
        changed.append(item)
    return changed


# ---- 计划执行（执行轮次挂计划；结果按 item_id 记录）----

EXEC_STATUSES = ("pass", "fail", "blocked", "skipped")


def get_run(plan: dict, run_id: str) -> dict | None:
    return next((r for r in plan["runs"] if r["run_id"] == run_id), None)


def new_run(plan: dict, name: str, by: str) -> dict:
    if any(not r.get("finished_at") for r in plan["runs"]):
        raise PlanError("存在未结束的执行轮次，请先结束后再新建")
    if not plan["items"]:
        raise PlanError("计划内还没有用例，请先加入用例")
    run = {
        "run_id": _hex8(),
        "name": (name or "").strip() or f"第 {len(plan['runs']) + 1 } 轮执行",
        "by": by,
        "started_at": _now(),
        "finished_at": None,
        "results": {},      # item_id -> {case_id, title, status, note, by, at, history}
        "attachments": [],  # {att_id, item_id?, filename, stored, content_type, size, by, at}
    }
    plan["runs"].append(run)
    if plan["status"] == "not_started":
        plan["status"] = "in_progress"
    return run


def run_summary(plan: dict, run: dict) -> dict:
    total = len(plan["items"])
    counts = {s: 0 for s in EXEC_STATUSES}
    for r in run["results"].values():
        if r["status"] in counts:
            counts[r["status"]] += 1
    executed = sum(counts.values())
    return {
        **counts, "executed": executed, "total": total,
        "pass_rate": round(counts["pass"] / executed, 3) if executed else None,
    }


def plan_summary(plan: dict) -> dict:
    """计划列表行的汇总：用例数 / 已分配数 / 最近一轮执行进度。"""
    latest = plan["runs"][-1] if plan["runs"] else None
    return {
        "cases": len(plan["items"]),
        "assigned": sum(1 for i in plan["items"] if i.get("assignee")),
        "runs": len(plan["runs"]),
        "latest_run": (
            {"run_id": latest["run_id"], "name": latest["name"],
             "finished_at": latest["finished_at"], **run_summary(plan, latest)}
            if latest else None
        ),
    }


# ---- 旧任务级执行轮次迁移（M4：执行统一挂到计划）----


def migrate_task_executions(tasks, plans: PlanStore) -> int:
    """启动时一次性迁移：把任务上的历史执行轮次搬进自动创建的计划。

    每个带轮次的任务生成一个计划，用例全量快照入计划（执行历史覆盖的是当时全部用例），
    旧轮次结果按 uid 对应到快照 item_id；任务上的轮次清空并记录去向（exec_migrated_to）。
    """
    from app.versions import case_entities, ensure_versions, latest_version_no

    migrated = 0
    for record in tasks.list(limit=100000):
        if not record.executions:
            continue
        cases = (record.result or {}).get("cases", [])
        src = record.sources[0] if record.sources else record.task_id
        project = (record.context or {}).get("project") or "（未指定）"
        ensure_versions(record.task_id, "case", case_entities(cases), by=record.created_by)
        plan = plans.create(
            name=f"「{src}」执行（迁移）", project=project,
            owner=record.created_by or "", created_by=record.created_by or "system",
        )
        uid_to_item: dict[str, str] = {}
        for case in cases:
            uid = str(case.get("uid") or case.get("case_id") or "")
            item = snapshot_item(
                record.task_id, case, latest_version_no(record.task_id, "case", uid),
                by=record.created_by,
            )
            plan["items"].append(item)
            uid_to_item[uid] = item["item_id"]
        for old in record.executions:
            run = {
                "run_id": old.get("run_id") or _hex8(),
                "name": old.get("name", ""),
                "by": old.get("by"),
                "started_at": old.get("started_at"),
                "finished_at": old.get("finished_at"),
                "results": {
                    uid_to_item.get(uid, uid): entry
                    for uid, entry in (old.get("results") or {}).items()
                },
                "attachments": [],
            }
            plan["runs"].append(run)
        plan["status"] = (
            "done" if plan["runs"] and all(r.get("finished_at") for r in plan["runs"])
            else "in_progress"
        )
        plans.save(plan)
        record.executions = []
        record.exec_migrated_to = plan["plan_id"]
        tasks.save(record)
        migrated += 1
    return migrated

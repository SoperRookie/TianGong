"""报表聚合口径测试。"""

from datetime import datetime, timedelta, timezone

from app.reports import summarize
from app.tasks.store import TaskRecord


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")


def _record(days_ago=1, **kw) -> TaskRecord:
    return TaskRecord(task_id=f"t{days_ago}{kw.get('created_by','x')}", created_at=_iso(days_ago), **kw)


def test_project_rollup_与用例库生命周期():
    from app.reports import project_cases, project_rollup

    records = [
        _record(1, status="completed", created_by="makino",
                context={"project": "德州"},
                result={"cases": [
                    {"uid": "u1", "case_id": "TC-登录-001", "module": "登录", "title": "正常登录",
                     "priority": "P0", "keywords": "登录"},
                    {"uid": "u2", "case_id": "TC-登录-002", "module": "登录", "title": "密码错误",
                     "priority": "P1"},
                    {"uid": "u3", "case_id": "TC-登录-003", "module": "登录", "title": "账号锁定",
                     "priority": "P1"},
                ], "passed": True, "review_rounds": 1},
                case_reviews={"u1": {"status": "approved", "locked": True},
                              "u2": {"status": "rejected", "comment": "预期不可验证"},
                              "u3": {"status": "pending"}},
                executions=[
                    {"run_id": "r1", "name": "冒烟", "results": {
                        "u1": {"status": "fail", "note": "BUG-1", "by": "makino", "at": "t1"}}},
                    {"run_id": "r2", "name": "回归", "results": {
                        "u1": {"status": "pass", "by": "makino", "at": "t2"}}},
                ]),
        _record(2, status="completed", context={"project": "斗地主"},
                result={"cases": [{"uid": "u9", "case_id": "TC-发牌-001", "module": "发牌",
                                   "title": "正常发牌", "priority": "P0"}], "passed": True, "review_rounds": 1}),
    ]
    rollup = {p["project"]: p for p in project_rollup(records)}
    dz = rollup["德州"]
    assert (dz["cases"], dz["approved"], dz["rejected"], dz["pending"]) == (3, 1, 1, 1)
    assert dz["executed"] == 1 and dz["exec_pass"] == 1  # 最新轮次结果为准（复测通过）

    cases = project_cases(records, "德州")
    assert len(cases) == 3
    c1 = next(c for c in cases if c["uid"] == "u1")
    assert c1["review"] == "approved" and c1["locked"] is True
    assert c1["exec"]["status"] == "pass" and c1["exec"]["run"] == "回归"  # 取最新执行
    c2 = next(c for c in cases if c["uid"] == "u2")
    assert c2["review"] == "rejected" and c2["review_comment"] == "预期不可验证"
    assert c2["exec"] is None
    assert project_cases(records, "斗地主")[0]["module"] == "发牌"


def test_summarize_核心口径():
    records = [
        _record(1, status="completed", created_by="makino",
                context={"project": "德州"},
                result={"cases": [{"priority": "P0"}, {"priority": "P1"}], "passed": True, "review_rounds": 2},
                case_reviews={"u1": {"status": "approved"}, "u2": {"status": "rejected"}},
                review_log=[{"action": "approve", "by": "makino"}, {"action": "reject", "by": "admin"}],
                offline_review={"stats": {"adoption_rate": 0.8}},
                fix_log=[{"kind": "cases"}]),
        _record(2, status="completed", created_by="admin",
                result={"cases": [{"priority": "P2"}], "passed": False, "review_rounds": 4}),
        _record(3, status="failed", created_by="makino"),
        _record(60, status="completed", created_by="makino",  # 超出 30 天窗口
                result={"cases": [{"priority": "P0"}], "passed": True, "review_rounds": 1}),
    ]
    d = summarize(records, days=30)
    assert d["totals"]["tasks"] == 3 and d["totals"]["cases"] == 3
    assert d["totals"]["ai_pass_rate"] == 0.5           # 2 个 completed，1 个 passed
    assert d["totals"]["avg_review_rounds"] == 3.0      # (2+4)/2
    assert d["totals"]["adoption_rate"] == 0.8 and d["totals"]["adoption_samples"] == 1
    assert d["totals"]["fix_runs"] == 1
    assert d["priority"] == {"P0": 1, "P1": 1, "P2": 1, "P3": 0}
    assert d["status"]["completed"] == 2 and d["status"]["failed"] == 1
    assert d["case_states"] == {"approved": 1, "pending": 0, "rejected": 1}
    assert d["review_actions"]["approve"] == 1 and d["review_actions"]["reject"] == 1
    # 成员榜：任务归创建人，审核动作归操作人
    creators = {c["user"]: c for c in d["creators"]}
    assert creators["makino"]["tasks"] == 2 and creators["makino"]["reviews"] == 1
    assert creators["admin"]["reviews"] == 1
    # days=0 全量 + 项目过滤
    assert summarize(records, days=0)["totals"]["tasks"] == 4
    assert summarize(records, days=0, project="德州")["totals"]["tasks"] == 1

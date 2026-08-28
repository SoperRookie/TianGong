"""报表聚合口径测试。"""

from datetime import datetime, timedelta, timezone

from app.reports import summarize
from app.tasks.store import TaskRecord


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")


def _record(days_ago=1, **kw) -> TaskRecord:
    return TaskRecord(task_id=f"t{days_ago}{kw.get('created_by','x')}", created_at=_iso(days_ago), **kw)


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

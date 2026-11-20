"""测试报告图表用的失败与阻塞分布快照。"""
from app.api.routes import _failure_breakdown


def test_失败与阻塞分布():
    failures = [
        {"status": "fail", "reason": "系统缺陷", "bug": {"severity": "严重"}, "triage": {"category": "产品缺陷"}},
        {"status": "fail", "reason": "系统缺陷", "bug": {"severity": "一般"}, "triage": {"category": "产品缺陷"}},
        {"status": "blocked", "reason": "系统缺陷", "bug": None, "triage": None},
        {"status": "blocked", "reason": "环境问题", "bug": None, "triage": {"category": "环境"}},
        {"status": "fail", "reason": "", "bug": None, "triage": None},
    ]
    out = _failure_breakdown(failures)
    # 按条数从多到少；失败 / 阻塞分开计；没填分类的归「未填分类」
    assert out["by_reason"][0] == {"name": "系统缺陷", "fail": 2, "blocked": 1}
    assert {r["name"]: (r["fail"], r["blocked"]) for r in out["by_reason"]} == {"系统缺陷": (2, 1), "环境问题": (0, 1), "未填分类": (1, 0)}
    # 没有缺陷单 / 没有归因的不进对应分布
    assert {r["name"]: r["fail"] for r in out["by_severity"]} == {"严重": 1, "一般": 1}
    assert sum(r["fail"] + r["blocked"] for r in out["by_category"]) == 3
    assert _failure_breakdown([]) == {"by_reason": [], "by_severity": [], "by_category": []}

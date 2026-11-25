"""测试报告 PDF（带图表）：能生成、内容对、极端输入不崩。"""
try:
    import pymupdf as fitz
except ImportError:
    import fitz

from app.report_pdf import build_report_pdf


def _bucket(total, p, f, b, s):
    return {"total": total, "pass": p, "fail": f, "blocked": b, "skipped": s, "not_executed": total - p - f - b - s}


def _text(data: bytes) -> tuple[int, str]:
    doc = fitz.open(stream=data, filetype="pdf")
    return len(doc), "\n".join(page.get_text() for page in doc)


SCOPE = {"plan": "V2.3 发布回归计划", "project": "演示项目", "owner": "admin", "period": "2026-11-01 ～ 2026-11-20",
         "run_name": "第 3 轮", "round": 3, "finished": False}


def test_报告PDF内容():
    stats = {**_bucket(96, 70, 7, 4, 3),
             "by_module": [{"module": "充值", **_bucket(7, 5, 1, 1, 0)}, {"module": "", **_bucket(6, 5, 0, 1, 0)}],
             "by_priority": [{"priority": "P0", **_bucket(22, 18, 1, 2, 0)}]}
    report = {"conclusion": "有条件发布", "verdict_basis": "P0 用例无系统缺陷类失败。", "summary": "主流程通过。", "adjusted": ["由可发布校正"],
              "stats": stats, "by": "admin", "at": "2026-11-13T02:00:00+00:00", "interim": True,
              "risks": [{"text": "充值回调偶发重复入账", "impact": "资金"}], "open_items": [{"kind": "缺陷", "ref": "TC-012", "text": "余额未刷新"}],
              "suggestions": [{"when": "must", "text": "修复后回归"}, {"when": "later", "text": "报表口径排入下个迭代"}],
              "failure_breakdown": {"by_reason": [{"name": "系统缺陷", "fail": 5, "blocked": 3}], "by_severity": [{"name": "严重", "fail": 3, "blocked": 0}], "by_category": []}}
    rounds = [{"name": "第 1 轮", "executed": 78, "pass": 51}, {"name": "第 3 轮", "executed": 84, "pass": 70, "current": True}]
    failures = [{"ref": "TC-002", "title": "充值成功后余额未刷新", "module": "充值", "priority": "P0", "status": "fail", "reason": "系统缺陷", "note": "接口返回 500"}]
    data = build_report_pdf(SCOPE, report, rounds, failures, "2026-11-20 07:00 UTC")
    assert data[:5] == b"%PDF-"
    pages, text = _text(data)
    assert pages >= 1
    # 通过率 = 通过 / 已执行（70 / 84），不是除以总数
    for want in ("V2.3 发布回归计划", "有条件发布", "83.3%", "84 / 96", "按优先级", "按模块", "（未分模块）", "失败与阻塞分类", "系统缺陷",
                 "缺陷严重程度", "各轮次通过率", "65.4%", "充值回调偶发重复入账", "TC-012", "发布前必须", "可延后", "失败与阻塞用例清单", "TC-002", "阶段性报告"):
        assert want in text, want
    assert len(data) < 600_000   # 字体做了子集化，不会把整套中文字体带进去


def test_报告PDF极端输入():
    # 没有统计、没有风险 / 遗留 / 建议、只有一轮、没有失败：照样出一份，不画不存在的图
    pages, text = _text(build_report_pdf({"plan": "", "run_name": ""}, {"conclusion": "可发布", "stats": {}}, [], []))
    assert pages == 1 and "可发布" in text and "各轮次通过率" not in text and "失败与阻塞用例清单" not in text
    # 老报告没有 failure_breakdown：分类按传入的当前失败清单现算；长文本折行、上百模块与上百条失败自动分页
    long = "这是一段很长的判据，没有任何空格，用来验证中文按字符宽度折行不会画出页面。" * 12
    stats = {**_bucket(2000, 1500, 300, 100, 50), "by_module": [{"module": f"模块{i}" * 6, **_bucket(20, 15, 3, 1, 1)} for i in range(100)], "by_priority": []}
    failures = [{"ref": f"TC-{i:04d}", "title": long[:80], "module": "模块", "priority": "P1", "status": "fail" if i % 2 else "blocked", "reason": "环境问题" if i % 3 else "", "note": long[:60]} for i in range(400)]
    data = build_report_pdf(SCOPE, {"conclusion": "不可发布", "verdict_basis": long, "summary": long, "stats": stats,
                                    "risks": [{"text": long}], "open_items": [], "suggestions": []},
                            [{"name": f"第 {i} 轮", "executed": 10 * i, "pass": 7 * i} for i in range(20)], failures)
    pages, text = _text(data)
    assert pages > 5 and "按导出时的当前结果统计" in text and "环境问题" in text and "未填分类" in text and "列出前 150 条" in text

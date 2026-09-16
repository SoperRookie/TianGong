"""结构化驳回（完整需求 6.4/9.2/9.3/9.4）：

驳回类型多选必填 + 原因必填 + 修改要求/范围，用例字段级/步骤级定位，
AI 定向修改透传结构化信息，字段/步骤最小修改原则的确定性兜底。
"""

import json

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.main import app
from app.tasks.points import (
    PointReviewError,
    apply_point_review,
    assign_entities,
    validate_rejection,
)
from tests.stubs import StubLLM, generator_reply, make_case, review_reply


@pytest.fixture
async def client():
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


def _modules():
    return assign_entities([
        {"module": "登录", "points": [
            {"point": "正确账号密码登录成功", "dimension": "正常流程"},
        ]},
    ])


# ---- 校验规则 ----


def test_validate_rejection_类型必填且须在枚举内():
    with pytest.raises(PointReviewError, match="驳回原因"):
        validate_rejection({"reject_types": ["其他"]}, "测试点 TP001")
    with pytest.raises(PointReviewError, match="驳回类型"):
        validate_rejection({"comment": "写得不对"}, "测试点 TP001")
    with pytest.raises(PointReviewError, match="不合法"):
        validate_rejection({"comment": "x", "reject_types": ["瞎写的类型"]}, "测试点 TP001")
    with pytest.raises(PointReviewError, match="修改范围"):
        validate_rejection({"comment": "x", "reject_types": ["其他"], "fix_scope": "全宇宙"}, "测试点 TP001")


def test_validate_rejection_默认范围与规范化():
    out = validate_rejection(
        {"comment": " 描述太泛 ", "reject_types": ["描述不清晰", "范围过大"],
         "fix_request": "拆成两条", "fix_note": ""},
        "测试点 TP001",
    )
    assert out == {"comment": "描述太泛", "reject_types": ["描述不清晰", "范围过大"],
                   "fix_request": "拆成两条", "fix_note": "", "fix_scope": "当前项"}


# ---- 测试点：结构化字段落到实体与留痕 ----


def test_point_reject_结构化字段入实体_通过后清空():
    modules = _modules()
    out = apply_point_review(modules, [{
        "tp_id": "TP001", "action": "reject", "comment": "范围太大",
        "reject_types": ["范围过大"], "fix_request": "按登录成功/失败拆分", "fix_scope": "当前项重生成",
    }])
    p = modules[0]["points"][0]
    assert p["status"] == "rejected"
    assert p["reject_types"] == ["范围过大"]
    assert p["fix_request"] == "按登录成功/失败拆分"
    assert p["fix_scope"] == "当前项重生成"
    assert out["log"][0]["reject_types"] == ["范围过大"]  # 留痕带结构化字段
    apply_point_review(modules, [{"tp_id": "TP001", "action": "approve"}])
    assert p["reject_types"] == [] and p["fix_request"] == "" and p["comment"] == ""


def test_二次归一化不丢结构化驳回字段():
    modules = _modules()
    apply_point_review(modules, [{"tp_id": "TP001", "action": "reject", "comment": "x",
                                  "reject_types": ["其他"], "fix_request": "改"}])
    again = assign_entities(modules)
    assert again[0]["points"][0]["reject_types"] == ["其他"]
    assert again[0]["points"][0]["fix_request"] == "改"


# ---- 用例：字段/步骤最小修改原则的确定性兜底 ----


def _two_step_case(**kw):
    return make_case(steps=[
        {"action": "打开登录页", "expected": "页面正常展示"},
        {"action": "输入正确账号密码并提交", "expected": "跳转首页"},
    ], **kw)


def test_merge_case_fix_字段级定位_越界字段还原():
    from app.agents.quality import merge_case_fix

    cases = [dict(_two_step_case(), uid="u1")]
    reviews = {"u1": {"status": "rejected", "comment": "预期空泛", "reject_count": 1,
                      "locked": False, "fields": ["expected"], "steps": []}}
    ai_case = _two_step_case(title="越权改标题", priority="P0")
    ai_case["steps"] = [
        {"action": "越权改操作", "expected": "页面展示且加载完成"},
        {"action": "输入正确账号密码并提交", "expected": "跳转首页并展示昵称"},
    ]
    out = merge_case_fix(cases, {"cases": [ai_case], "deleted": []},
                         allowed={"TC-登录-001"}, reviews=reviews)
    got = out["cases"][0]
    assert got["title"] == "验证正确账号密码登录成功"  # 字段定位外：还原
    assert got["priority"] == "P1"
    assert got["steps"][0]["action"] == "打开登录页"  # 只允许改预期：操作还原
    assert got["steps"][0]["expected"] == "页面展示且加载完成"
    assert got["steps"][1]["expected"] == "跳转首页并展示昵称"


def test_merge_case_fix_步骤级定位_其余步骤与步骤数保护():
    from app.agents.quality import merge_case_fix

    cases = [dict(_two_step_case(), uid="u1")]
    reviews = {"u1": {"status": "rejected", "comment": "第2步预期不可验证", "reject_count": 1,
                      "locked": False, "fields": [], "steps": [2]}}
    ai_case = _two_step_case()
    ai_case["steps"] = [
        {"action": "越权改第1步", "expected": "越权改第1步预期"},
        {"action": "输入正确账号密码并提交", "expected": "跳转首页并生成有效登录态"},
    ]
    out = merge_case_fix(cases, {"cases": [ai_case], "deleted": []},
                         allowed={"TC-登录-001"}, reviews=reviews)
    got = out["cases"][0]
    assert got["steps"][0] == {"action": "打开登录页", "expected": "页面正常展示"}  # 非定位步骤原样
    assert got["steps"][1]["expected"] == "跳转首页并生成有效登录态"

    # 步骤级定位下模型增删步骤：整体还原
    cases2 = [dict(_two_step_case(), uid="u1")]
    reviews2 = {"u1": {"status": "rejected", "comment": "第2步预期不可验证", "reject_count": 1,
                       "locked": False, "fields": [], "steps": [2]}}
    ai_case2 = _two_step_case()
    ai_case2["steps"] = ai_case2["steps"] + [{"action": "多加一步", "expected": "x"}]
    out2 = merge_case_fix(cases2, {"cases": [ai_case2], "deleted": []},
                          allowed={"TC-登录-001"}, reviews=reviews2)
    assert len(out2["cases"][0]["steps"]) == 2


# ---- API：用例结构化驳回校验、存储与 AI 透传 ----


async def _completed_task(client, *cases):
    from tests.stubs import ANALYST_REPLY

    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(*cases), review_reply(True)])
    resp = await client.post("/api/v1/tasks", data={"text": "登录需求"})
    return resp.json()["task_id"]


async def test_case_reject_校验_类型必填与定位合法性(client):
    task_id = await _completed_task(client, _two_step_case())
    # 缺驳回类型
    resp = await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [
        {"case_id": "TC-登录-001", "action": "reject", "comment": "不行"},
    ]})
    assert resp.status_code == 400 and "驳回类型" in resp.json()["detail"]
    # 非法字段
    resp = await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [
        {"case_id": "TC-登录-001", "action": "reject", "comment": "不行",
         "reject_types": ["其他"], "fields": ["case_name"]},
    ]})
    assert resp.status_code == 400 and "指定字段不合法" in resp.json()["detail"]
    # 步骤越界
    resp = await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [
        {"case_id": "TC-登录-001", "action": "reject", "comment": "不行",
         "reject_types": ["其他"], "steps": [5]},
    ]})
    assert resp.status_code == 400 and "指定步骤越界" in resp.json()["detail"]


async def test_case_reject_结构化入库并透传AI(client):
    task_id = await _completed_task(client, _two_step_case())
    resp = await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [
        {"case_id": "TC-登录-001", "action": "reject", "comment": "第2步预期不可验证",
         "reject_types": ["预期不可验证", "描述不清晰"], "fix_request": "预期写明可断言的结果",
         "fields": ["expected"], "steps": [2], "fix_scope": "当前项"},
    ]})
    assert resp.status_code == 200, resp.text
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    state = list(task["case_reviews"].values())[0]
    assert state["reject_types"] == ["预期不可验证", "描述不清晰"]
    assert state["fields"] == ["expected"] and state["steps"] == [2]
    assert state["fix_request"] == "预期写明可断言的结果"

    fixed = _two_step_case()
    fixed["steps"][1]["expected"] = "跳转首页并生成有效登录态"
    app.state.llm = StubLLM([json.dumps({
        "fixes": [{"case_id": "TC-登录-001", "comment_type": "预期不可验证", "note": "补预期"}],
        "cases": [fixed], "deleted": [],
    }, ensure_ascii=False)])
    resp = await client.post(f"/api/v1/tasks/{task_id}/cases/fix")
    assert resp.status_code == 200
    sent = app.state.llm.calls[0]["messages"][1]["content"]
    assert "驳回类型" in sent and "预期不可验证" in sent
    assert "修改要求" in sent and "预期写明可断言的结果" in sent
    assert "指定步骤" in sent and "指定字段" in sent


# ---- AI 修改确认流（完整需求 7.3/9.4）----


async def test_fix确认流_逐项接受拒绝与再优化(client):
    task_id = await _completed_task(
        client, _two_step_case(),
        _two_step_case(case_id="TC-登录-002", title="验证密码错误提示"))
    await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [
        {"case_id": "TC-登录-001", "action": "reject", "comment": "预期空泛", "reject_types": ["预期不可验证"]},
        {"case_id": "TC-登录-002", "action": "reject", "comment": "标题不清晰", "reject_types": ["描述不清晰"]},
    ]})
    fixed1 = _two_step_case()
    fixed1["steps"][1]["expected"] = "跳转首页并生成有效登录态"
    fixed2 = _two_step_case(case_id="TC-登录-002", title="验证密码错误时提示剩余尝试次数")
    app.state.llm = StubLLM([json.dumps({
        "fixes": [], "cases": [fixed1, fixed2], "deleted": [],
    }, ensure_ascii=False)])
    resp = await client.post(f"/api/v1/tasks/{task_id}/cases/fix")
    pending = resp.json()["pending_fix"]
    assert len(pending["proposals"]) == 2
    # 提案未确认前不落地
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert all("登录态" not in c["steps"][1]["expected"] for c in task["result"]["cases"])
    # 提案存在时禁止再次发起
    assert (await client.post(f"/api/v1/tasks/{task_id}/cases/fix")).status_code == 409

    # 接受 P1、拒绝 P2
    pid1 = next(p["proposal_id"] for p in pending["proposals"] if p["case_id"] == "TC-登录-001")
    pid2 = next(p["proposal_id"] for p in pending["proposals"] if p["case_id"] == "TC-登录-002")
    resp = await client.post(f"/api/v1/tasks/{task_id}/fix/confirm", json={"decisions": [
        {"proposal_id": pid1, "decision": "accept"}, {"proposal_id": pid2, "decision": "reject"},
    ]})
    assert resp.status_code == 200
    assert resp.json()["applied"] == 1 and resp.json()["rejected"] == 1
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    cases = {c["case_id"]: c for c in task["result"]["cases"]}
    assert "登录态" in cases["TC-登录-001"]["steps"][1]["expected"]  # 接受项已应用
    assert cases["TC-登录-002"]["title"] == "验证密码错误提示"        # 拒绝项原样
    reviews = task["case_reviews"]
    uid1 = cases["TC-登录-001"]["uid"]
    uid2 = cases["TC-登录-002"]["uid"]
    assert reviews[uid1]["status"] == "pending"   # 接受后重新提交评审
    assert reviews[uid2]["status"] == "rejected"  # 拒绝项保持驳回，可继续 AI 优化
    # 继续 AI 优化：再次发起 fix 只处理仍驳回的 TC-登录-002
    app.state.llm = StubLLM([json.dumps({"fixes": [], "cases": [], "deleted": []}, ensure_ascii=False)])
    resp = await client.post(f"/api/v1/tasks/{task_id}/cases/fix")
    assert resp.status_code == 200
    sent = app.state.llm.calls[0]["messages"][1]["content"]
    assert "TC-登录-002" in sent and "TC-登录-001" not in sent

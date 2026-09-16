"""M5-D：我的工作台、全局搜索（按成员关系过滤）、项目覆盖追溯视图。"""

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.config import get_settings
from app.main import app
from tests.stubs import ANALYST_REPLY, StubLLM, generator_reply, make_case, review_reply


@pytest.fixture
async def client():
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
def auth_on():
    settings = get_settings()
    settings.auth_enabled = True
    yield
    settings.auth_enabled = False


async def _login(client, username, password="pass123") -> dict:
    resp = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


async def test_工作台_搜索_覆盖视图(client, auth_on):
    admin = await _login(client, "admin", "admin123")
    for u in ("lead", "t1", "outsider"):
        await client.post("/api/v1/auth/users", headers=admin, json={"username": u, "password": "pass123"})
    await client.post("/api/v1/projects", headers=admin, json={"name": "A"})
    await client.post("/api/v1/projects", headers=admin, json={"name": "B"})
    await client.put("/api/v1/projects/A/members", headers=admin, json={"username": "lead", "role": "test_lead"})
    await client.put("/api/v1/projects/A/members", headers=admin, json={"username": "t1", "role": "tester"})
    await client.put("/api/v1/projects/B/members", headers=admin, json={"username": "outsider", "role": "test_lead"})
    lead, t1, outsider = await _login(client, "lead"), await _login(client, "t1"), await _login(client, "outsider")

    # 需求（待确认）+ 从需求设计的任务（拆解待确认）+ 直接生成任务（用例待评审）
    req = (await client.post("/api/v1/requirements", headers=lead, data={"project": "A", "title": "登录与锁定", "text": "登录需求 锁定"})).json()
    import json
    app.state.llm = StubLLM([json.dumps({"features": ["登录"], "rules": [], "preconditions": [], "normal_flows": [], "exception_flows": [],
                                         "boundaries": [], "state_changes": [], "permissions": [], "dependencies": [], "risks": [],
                                         "open_questions": ["锁定多久？"]}, ensure_ascii=False)])
    await client.post(f"/api/v1/requirements/{req['req_id']}/analyze", headers=lead, json={})
    case = make_case()
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(case), review_reply(True)])
    t_done = (await client.post("/api/v1/tasks", headers=t1, data={"text": "支付需求", "project": "A"})).json()["task_id"]
    app.state.llm = StubLLM([ANALYST_REPLY])
    t_wait = (await client.post("/api/v1/tasks", headers=t1, data={"text": "结算需求", "project": "A", "confirm_points": "true"})).json()["task_id"]
    plan = (await client.post("/api/v1/plans", headers=lead, json={"name": "A 冒烟", "project": "A"})).json()
    await client.post(f"/api/v1/tasks/{t_done}/review", headers=lead, json={"items": [{"case_id": case["case_id"], "action": "accept"}]})
    await client.post(f"/api/v1/plans/{plan['plan_id']}/cases", headers=lead, json={"task_id": t_done})
    item = (await client.get(f"/api/v1/plans/{plan['plan_id']}", headers=lead)).json()["items"][0]
    await client.post(f"/api/v1/plans/{plan['plan_id']}/assign", headers=lead, json={"assignee": "t1", "item_ids": [item["item_id"]]})
    await client.post(f"/api/v1/plans/{plan['plan_id']}/runs", headers=lead, json={})

    # 负责人工作台：待评审（拆解待确认的测试点）、待确认需求、我的项目
    wb = (await client.get("/api/v1/workbench", headers=lead)).json()
    assert [p["project"] for p in wb["projects"]] == ["A"] and wb["projects"][0]["role_label"] == "测试负责人"
    assert wb["pending_review"]["count"] == 1 and wb["pending_review"]["items"][0]["task_id"] == t_wait and wb["pending_review"]["items"][0]["kind"] == "point"
    assert wb["pending_requirements"]["count"] == 1 and wb["pending_requirements"]["items"][0]["open_questions"] == 1
    assert wb["to_execute"]["count"] == 0 and any(o["action"] == "任务分配" for o in wb["recent_ops"])
    # 测试人员工作台：待执行 1、我的 AI 任务 2（含 1 待确认），不可评审
    wb = (await client.get("/api/v1/workbench", headers=t1)).json()
    assert wb["to_execute"]["count"] == 1 and wb["pending_review"]["count"] == 0
    assert wb["my_ai_tasks"]["awaiting"] == 1 and len(wb["my_ai_tasks"]["items"]) == 2
    # 局外人：空
    wb = (await client.get("/api/v1/workbench", headers=outsider)).json()
    assert [p["project"] for p in wb["projects"]] == ["B"] and wb["pending_review"]["count"] == 0 and wb["to_execute"]["count"] == 0

    # 全局搜索：成员命中需求/任务/用例/计划；局外人一无所获
    r = (await client.get("/api/v1/search", headers=t1, params={"q": "登录"})).json()
    kinds = {g["kind"]: g for g in r["groups"]}
    assert kinds["requirement"]["items"][0]["title"] == "登录与锁定"
    assert kinds["case"]["items"][0]["case_id"] == case["case_id"] and kinds["point"]["total"] >= 1
    assert "plan" in {g["kind"] for g in (await client.get("/api/v1/search", headers=t1, params={"q": "冒烟"})).json()["groups"]}
    assert (await client.get("/api/v1/search", headers=outsider, params={"q": "登录"})).json()["groups"] == []

    # 覆盖追溯视图
    cov = (await client.get("/api/v1/projects/A/coverage", headers=lead)).json()
    assert cov["summary"] == {"requirements": 1, "with_points": 0, "with_cases": 0, "in_plan": 0, "executed": 0}
    assert {t["task_id"] for t in cov["unlinked_tasks"]} == {t_done, t_wait}
    assert (await client.get("/api/v1/projects/A/coverage", headers=outsider)).status_code == 403

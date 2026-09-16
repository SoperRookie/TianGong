"""执行精确到人：执行人约束与代执行留痕、执行即认领、我的执行任务、按人统计。"""

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


async def _setup(client):
    """项目 A：lead（测试负责人）、t1/t2（测试人员）；计划含 3 条已通过用例。"""
    admin = await _login(client, "admin", "admin123")
    for u in ("lead", "t1", "t2"):
        await client.post("/api/v1/auth/users", headers=admin, json={"username": u, "password": "pass123"})
    await client.post("/api/v1/projects", headers=admin, json={"name": "A"})
    for u, r in (("lead", "test_lead"), ("t1", "tester"), ("t2", "tester")):
        await client.put("/api/v1/projects/A/members", headers=admin, json={"username": u, "role": r})
    lead = await _login(client, "lead")
    cases = [make_case(), make_case(case_id="TC-下单-001", module="下单", title="下单成功"),
             make_case(case_id="TC-下单-002", module="下单", title="下单失败")]
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(*cases), review_reply(True)])
    task_id = (await client.post("/api/v1/tasks", headers=lead, data={"text": "需求", "project": "A"})).json()["task_id"]
    await client.post(f"/api/v1/tasks/{task_id}/review", headers=lead,
                      json={"items": [{"case_id": c["case_id"], "action": "accept"} for c in cases]})
    plan = (await client.post("/api/v1/plans", headers=lead, json={"name": "冒烟", "project": "A"})).json()
    pid = plan["plan_id"]
    await client.post(f"/api/v1/plans/{pid}/cases", headers=lead, json={"task_id": task_id})
    detail = (await client.get(f"/api/v1/plans/{pid}", headers=lead)).json()
    items = {i["case_id"]: i["item_id"] for i in detail["items"]}
    await client.post(f"/api/v1/plans/{pid}/assign", headers=lead,
                      json={"assignee": "t1", "item_ids": [items["TC-登录-001"]]})
    await client.post(f"/api/v1/plans/{pid}/assign", headers=lead,
                      json={"assignee": "t2", "item_ids": [items["TC-下单-001"]]})
    run = (await client.post(f"/api/v1/plans/{pid}/runs", headers=lead, json={})).json()
    return admin, lead, pid, run["run_id"], items


async def test_执行人约束_代执行_认领(client, auth_on):
    admin, lead, pid, rid, items = await _setup(client)
    t1, t2 = await _login(client, "t1"), await _login(client, "t2")
    url = f"/api/v1/plans/{pid}/runs/{rid}/results"

    # t2 不能执行分给 t1 的用例
    resp = await client.post(url, headers=t2, json={"items": [{"item_id": items["TC-登录-001"], "status": "pass"}]})
    assert resp.status_code == 403 and "t1" in resp.json()["detail"]
    # t1 执行自己的
    resp = await client.post(url, headers=t1, json={"items": [{"item_id": items["TC-登录-001"], "status": "pass"}]})
    assert resp.status_code == 200
    res = resp.json()["results"][items["TC-登录-001"]]
    assert res["by"] == "t1" and res["on_behalf_of"] is None
    # 负责人代执行 t2 的：by=lead，on_behalf_of=t2
    resp = await client.post(url, headers=lead, json={"items": [{"item_id": items["TC-下单-001"], "status": "skipped"}]})
    assert resp.status_code == 200
    res = resp.json()["results"][items["TC-下单-001"]]
    assert (res["by"], res["on_behalf_of"]) == ("lead", "t2")
    # 未分配的用例：t2 执行即认领
    resp = await client.post(url, headers=t2, json={"items": [{"item_id": items["TC-下单-002"], "status": "fail",
                                                                "reason": "系统缺陷", "note": "BUG-1"}]})
    assert resp.status_code == 200
    detail = (await client.get(f"/api/v1/plans/{pid}", headers=lead)).json()
    it = next(i for i in detail["items"] if i["item_id"] == items["TC-下单-002"])
    assert it["assignee"] == "t2" and it["assign_log"][-1]["note"] == "执行时认领"
    # 按人统计
    people = {p["username"]: p for p in detail["people"]}
    assert people["t1"] == {**people["t1"], "assigned": 1, "executed": 1, "pass": 1, "pending": 0}
    assert people["t2"] == {**people["t2"], "assigned": 2, "executed": 1, "fail": 1, "pending": 0}
    assert people["lead"] == {**people["lead"], "assigned": 0, "executed": 1, "skipped": 1, "proxied": 1}
    # 报表人员维度
    rep = (await client.get("/api/v1/reports/summary?days=0&project=A", headers=admin)).json()
    by = {p["username"]: p for p in rep["execution"]["people"]}
    assert by["lead"]["proxied"] == 1 and by["t1"]["pass_rate"] == 1.0 and by["t2"]["fail"] == 1


async def test_我的执行任务(client, auth_on):
    admin, lead, pid, rid, items = await _setup(client)
    t1 = await _login(client, "t1")
    mine = (await client.get("/api/v1/plans/my-items", headers=t1)).json()
    assert mine["pending"] == 1 and mine["items"][0]["case_id"] == "TC-登录-001"
    assert mine["items"][0]["run_active"] and mine["items"][0]["run_id"] == rid
    await client.post(f"/api/v1/plans/{pid}/runs/{rid}/results", headers=t1,
                      json={"items": [{"item_id": items["TC-登录-001"], "status": "pass"}]})
    assert (await client.get("/api/v1/plans/my-items", headers=t1)).json()["items"] == []
    done = (await client.get("/api/v1/plans/my-items?include_done=true", headers=t1)).json()
    assert done["executed"] == 1 and done["items"][0]["result"]["status"] == "pass"
    # 非成员看不到该计划条目
    t2 = await _login(client, "t2")
    assert (await client.get("/api/v1/plans/my-items", headers=t2)).json()["pending"] == 1  # 自己的那条

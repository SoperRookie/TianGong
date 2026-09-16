"""验收周：存量迁移正式执行——遗留「未指定项目」任务归属项目后自动需求实体化、迁移计划跟随，迁移报告闭合。"""

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


async def _login(client, username="admin", password="admin123") -> dict:
    resp = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


async def _legacy_task(client, headers) -> str:
    """管理员建的无项目任务 = 项目化之前的遗留数据形态。"""
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(make_case()), review_reply(True)])
    resp = await client.post("/api/v1/tasks", headers=headers, data={"text": "历史需求：账号密码登录"})
    assert resp.status_code == 200, resp.text
    return resp.json()["task_id"]


async def test_遗留任务归属项目_需求实体化_报告闭合(client, auth_on):
    admin = await _login(client)
    await client.post("/api/v1/auth/users", headers=admin, json={"username": "t1", "password": "pass123"})
    t1 = await _login(client, "t1", "pass123")
    await client.post("/api/v1/projects", headers=admin, json={"name": "老项目"})
    await client.put("/api/v1/projects/老项目/members", headers=admin, json={"username": "t1", "role": "tester"})
    tid = await _legacy_task(client, admin)

    # 迁移报告：1 个未归属，未完成；普通用户不可见报告与遗留任务
    rep = (await client.get("/api/v1/admin/migration", headers=admin)).json()
    assert rep["tasks_unassigned"] == 1 and rep["unassigned"][0]["task_id"] == tid and not rep["complete"]
    assert (await client.get("/api/v1/admin/migration", headers=t1)).status_code == 403
    assert (await client.get(f"/api/v1/tasks/{tid}", headers=t1)).status_code == 403
    assert (await client.put(f"/api/v1/tasks/{tid}/project", headers=t1, json={"project": "老项目"})).status_code == 403

    # 归属：项目必须存在；归属后需求实体自动建立并回填关联
    assert (await client.put(f"/api/v1/tasks/{tid}/project", headers=admin, json={"project": "不存在"})).status_code == 404
    r = (await client.put(f"/api/v1/tasks/{tid}/project", headers=admin, json={"project": "老项目"})).json()
    assert r["requirement_migrated"] is True and r["requirement_id"]
    task = (await client.get(f"/api/v1/tasks/{tid}", headers=t1)).json()   # 成员现在可见
    assert task["context"]["project"] == "老项目" and task["context"]["requirement_id"] == r["requirement_id"]
    req = (await client.get(f"/api/v1/requirements/{r['requirement_id']}", headers=admin)).json()
    assert req["source_type"] == "migrated" and req["tasks"] == [tid] and req["status"] == "designing"
    assert "账号密码登录" in req["raw_text"]

    # 已归属不允许改挂；报告闭合
    assert (await client.put(f"/api/v1/tasks/{tid}/project", headers=admin, json={"project": "老项目"})).status_code == 409
    rep = (await client.get("/api/v1/admin/migration", headers=admin)).json()
    assert rep["tasks_unassigned"] == 0 and rep["requirements_migrated"] == 1 and rep["complete"]
    logs = (await client.get("/api/v1/audit?days=0", headers=admin)).json()["items"]
    assert any(x["action"] == "遗留任务归属项目" and x["target"] == tid for x in logs)


async def test_迁移计划随任务归属改挂(client, auth_on):
    admin = await _login(client)
    await client.post("/api/v1/projects", headers=admin, json={"name": "老项目"})
    tid = await _legacy_task(client, admin)
    # 模拟迁移期自动建的执行计划（挂在「未指定」）
    from app.plans import migrate_task_executions
    record = app.state.tasks.get(tid)
    case = record.result["cases"][0]
    record.executions = [{"run_id": "r1", "name": "历史轮次", "results": {case["uid"]: {"status": "pass"}}, "finished_at": record.created_at}]
    app.state.tasks.save(record)
    assert migrate_task_executions(app.state.tasks, app.state.plans) == 1
    plan_id = app.state.tasks.get(tid).exec_migrated_to
    assert app.state.plans.get(plan_id)["project"] == "（未指定）"
    rep = (await client.get("/api/v1/admin/migration", headers=admin)).json()
    assert rep["plans_migrated"] == 1 and rep["plans_unassigned"] == 1

    r = (await client.put(f"/api/v1/tasks/{tid}/project", headers=admin, json={"project": "老项目"})).json()
    assert r["plans_moved"] == 1
    assert app.state.plans.get(plan_id)["project"] == "老项目"
    listed = (await client.get("/api/v1/plans", headers=admin, params={"project": "老项目"})).json()["plans"]
    assert [p["plan_id"] for p in listed] == [plan_id]


async def test_批量归属_逐行失败不影响其他(client, auth_on):
    admin = await _login(client)
    await client.post("/api/v1/projects", headers=admin, json={"name": "老项目"})
    a = await _legacy_task(client, admin)
    b = await _legacy_task(client, admin)
    await client.put(f"/api/v1/tasks/{b}/project", headers=admin, json={"project": "老项目"})  # b 已归属
    r = (await client.post("/api/v1/admin/migration/assign", headers=admin,
                           json={"task_ids": [a, b, "nope"], "project": "老项目"})).json()
    assert [x["task_id"] for x in r["assigned"]] == [a]
    assert {x["task_id"]: x["reason"] for x in r["failed"]}.keys() == {b, "nope"}
    assert (await client.post("/api/v1/admin/migration/assign", headers=admin,
                              json={"task_ids": [a], "project": "不存在"})).status_code == 404
    rep = (await client.get("/api/v1/admin/migration", headers=admin)).json()
    assert rep["complete"] and rep["requirements_migrated"] == 2

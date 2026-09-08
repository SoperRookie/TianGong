"""M5-B 操作日志与安全日志：写接口自动留痕（动作/对象/项目/批量明细）、登录成功失败与权限变更、可见范围。"""

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


async def _login(client, username, password) -> dict:
    resp = await client.post("/api/v1/auth/login", json={"username": username, "password": password},
                             headers={"User-Agent": "pytest-agent"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


async def test_业务操作自动留痕_批量明细_项目归属(client):
    await client.post("/api/v1/projects", json={"name": "P", "code": "P1"})
    case = make_case()
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(case), review_reply(True)])
    task_id = (await client.post("/api/v1/tasks", data={"text": "登录需求", "project": "P"})).json()["task_id"]
    await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [{"case_id": case["case_id"], "action": "accept"}]})
    await client.post("/api/v1/projects/P/modules", json={"name": "登录"})
    logs = (await client.get("/api/v1/audit?days=0")).json()
    actions = [(x["kind"], x["action"], x["target"], x["project"], x["detail"]) for x in logs["items"]]
    assert ("case", "用例评审", task_id, "P", "批量 1 条") in actions
    assert ("ai", "创建生成任务", "", "P", "") in actions
    assert ("project", "新建模块", "P", "P", "") in actions
    assert ("project", "创建项目", "", None, "") in actions
    assert all(x["user"] == "anonymous" and x["status"] == 200 for x in logs["items"])
    assert logs["kinds"]["case"] == "测试用例"
    # 筛选：类型 / 关键词 / 项目
    assert (await client.get("/api/v1/audit?days=0&kind=case")).json()["total"] == 1
    assert (await client.get("/api/v1/audit?days=0&keyword=模块")).json()["total"] == 1
    assert (await client.get("/api/v1/audit?days=0&project=P")).json()["total"] == 3
    # 读接口与噪音接口不记录
    await client.get("/api/v1/tasks")
    await client.post("/api/v1/projects/P/favorite")
    assert (await client.get("/api/v1/audit?days=0")).json()["total"] == logs["total"]


async def test_安全日志_登录失败_权限变更_可见范围(client, auth_on):
    admin = await _login(client, "admin", "admin123")
    assert (await client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"})).status_code == 401
    await client.post("/api/v1/auth/users", headers=admin, json={"username": "u1", "password": "pass123"})
    await client.put("/api/v1/auth/users/u1", headers=admin, json={"role": "admin"})
    await client.put("/api/v1/auth/users/u1", headers=admin, json={"role": "member", "status": "disabled"})
    await client.put("/api/v1/auth/users/u1", headers=admin, json={"status": "active"})
    await client.post("/api/v1/projects", headers=admin, json={"name": "P"})
    await client.put("/api/v1/projects/P/members", headers=admin, json={"username": "u1", "role": "tester"})
    sec = (await client.get("/api/v1/audit?days=0&security=true", headers=admin)).json()
    rows = {(x["action"], x["target"]): x for x in sec["items"]}
    assert rows[("登录失败", "admin")]["status"] == 401 and rows[("登录失败", "admin")]["ip"]
    assert rows[("登录成功", "admin")]["ua"] == "pytest-agent"
    assert "角色→admin" in rows[("修改用户", "u1")]["detail"] or any("角色→admin" in x["detail"] for x in sec["items"])
    assert rows[("设置成员角色", "P")]["detail"] == "u1 → 测试人员" and rows[("设置成员角色", "P")]["security"] == 1
    assert all(x["security"] == 1 for x in sec["items"])
    # 普通成员：看不到安全日志；只看所属项目业务日志与自己的操作
    u1 = await _login(client, "u1", "pass123")
    assert (await client.get("/api/v1/audit?security=true", headers=u1)).status_code == 403
    await client.post("/api/v1/projects", headers=admin, json={"name": "Q"})
    await client.post("/api/v1/projects/Q/modules", headers=admin, json={"name": "M"})
    await client.post("/api/v1/projects/P/modules", headers=admin, json={"name": "M"})
    await client.put("/api/v1/auth/me", headers=u1, json={"name": "用户一"})
    mine = (await client.get("/api/v1/audit?days=0", headers=u1)).json()
    acts = {(x["action"], x["project"]) for x in mine["items"]}
    assert ("新建模块", "P") in acts and ("新建模块", "Q") not in acts
    assert all(x["security"] == 0 for x in mine["items"]) and ("修改个人资料", None) not in acts  # 个人资料属安全类
    assert (await client.get("/api/v1/audit?days=0&failed_only=true", headers=admin)).json()["total"] >= 1

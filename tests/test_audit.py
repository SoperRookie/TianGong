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


async def test_操作日志_类型计数_候选与关键词_分页收敛(client):
    await client.post("/api/v1/projects", json={"name": "P", "code": "P1"})
    await client.post("/api/v1/projects", json={"name": "Q", "code": "Q1"})
    for name in ["登录", "注册", "支付"]:
        await client.post("/api/v1/projects/P/modules", json={"name": name})
    await client.post("/api/v1/projects/Q/modules", json={"name": "M"})
    assert (await client.post("/api/v1/projects/Q/modules", json={"name": "M"})).status_code >= 400  # 重名失败留痕
    logs = (await client.get("/api/v1/audit?days=0")).json()
    assert logs["kind_counts"]["project"] == logs["total"] >= 7
    assert logs["users"] == ["anonymous"] and logs["projects"] == ["P", "Q"]
    # 类型计数 / 候选按范围统计，不随关键词、类型、只看失败筛选变化；指定项目后不再返回项目候选
    by_kw = (await client.get("/api/v1/audit?days=0&keyword=模块")).json()
    assert by_kw["total"] == 5 and by_kw["kind_counts"] == logs["kind_counts"] and by_kw["projects"] == ["P", "Q"]
    failed = (await client.get("/api/v1/audit?days=0&failed_only=true")).json()
    assert failed["total"] == 1 and failed["items"][0]["project"] == "Q" and failed["kind_counts"] == logs["kind_counts"]
    scoped = (await client.get("/api/v1/audit?days=0&project=P")).json()
    assert scoped["total"] == 3 and scoped["projects"] == [] and scoped["kind_counts"] == {"project": 3}  # 创建项目本身不归属项目
    # 关键词覆盖操作人 / 项目 / 类型中文名
    assert (await client.get("/api/v1/audit?days=0&keyword=anonymous")).json()["total"] == logs["total"]
    assert (await client.get("/api/v1/audit?days=0&keyword=Q")).json()["total"] == 2
    assert (await client.get("/api/v1/audit?days=0&keyword=项目")).json()["total"] == logs["total"]
    assert (await client.get("/api/v1/audit?days=0&keyword=不存在的关键词")).json()["total"] == 0
    # 分页：页码超过总页数时收敛到最后一页
    page2 = (await client.get("/api/v1/audit?days=0&page=2&page_size=5")).json()
    assert page2["page"] == 2 and len(page2["items"]) == logs["total"] - 5
    over = (await client.get("/api/v1/audit?days=0&page=99&page_size=5")).json()
    assert over["page"] == 2 and over["items"] == page2["items"]

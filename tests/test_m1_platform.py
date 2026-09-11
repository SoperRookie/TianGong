"""M1 平台底座与权限：用户资料/禁用、项目字段与成员角色、接口级项目隔离、版本与模块树。"""

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


async def _user(client, admin, username, role="member", **kw) -> dict:
    resp = await client.post("/api/v1/auth/users", headers=admin,
                             json={"username": username, "password": "pass123", "role": role, **kw})
    assert resp.status_code == 200, resp.text
    return await _login(client, username, "pass123")


async def _project(client, admin, name, members: dict | None = None, **kw) -> None:
    resp = await client.post("/api/v1/projects", headers=admin, json={"name": name, **kw})
    assert resp.status_code == 200, resp.text
    for username, role in (members or {}).items():
        resp = await client.put(f"/api/v1/projects/{name}/members", headers=admin,
                                json={"username": username, "role": role})
        assert resp.status_code == 200, resp.text


async def _task(client, headers, project, approve=True) -> str:
    case = make_case()
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(case), review_reply(True)])
    resp = await client.post("/api/v1/tasks", headers=headers,
                             data={"text": "登录需求", "project": project})
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["task_id"]
    if approve:
        resp = await client.post(f"/api/v1/tasks/{task_id}/review", headers=headers,
                                 json={"items": [{"case_id": case["case_id"], "action": "accept"}]})
        assert resp.status_code == 200, resp.text
    return task_id


# ---- 3.1 用户管理 ----


async def test_用户资料_禁用_最后登录(client, auth_on):
    admin = await _login(client)
    resp = await client.post("/api/v1/auth/users", headers=admin, json={
        "username": "zhangsan", "password": "pass123", "name": "张三",
        "email": "zs@example.com", "phone": "13800000000"})
    assert resp.status_code == 200, resp.text
    u = resp.json()
    assert (u["name"], u["email"], u["status"], u["last_login_at"]) == ("张三", "zs@example.com", "active", None)

    assert (await client.post("/api/v1/auth/users", headers=admin, json={
        "username": "bad", "password": "pass123", "email": "not-an-email"})).status_code == 400

    zs = await _login(client, "zhangsan", "pass123")
    me = (await client.get("/api/v1/auth/me", headers=zs)).json()
    assert me["last_login_at"] and me["last_login_ip"] is not None
    # 自助改资料
    me = (await client.put("/api/v1/auth/me", headers=zs, json={"name": "张三丰", "phone": "13900000000"})).json()
    assert (me["name"], me["phone"]) == ("张三丰", "13900000000")

    # 禁用：现有会话立即失效、拒绝登录；管理员不能禁用自己
    assert (await client.put("/api/v1/auth/users/admin", headers=admin,
                             json={"status": "disabled"})).status_code == 400
    resp = await client.put("/api/v1/auth/users/zhangsan", headers=admin, json={"status": "disabled"})
    assert resp.status_code == 200 and resp.json()["status"] == "disabled"
    assert (await client.get("/api/v1/auth/me", headers=zs)).status_code == 401
    assert (await client.post("/api/v1/auth/login",
                              json={"username": "zhangsan", "password": "pass123"})).status_code == 401
    # 启用后恢复
    await client.put("/api/v1/auth/users/zhangsan", headers=admin, json={"status": "active"})
    await _login(client, "zhangsan", "pass123")
    users = (await client.get("/api/v1/auth/users", headers=admin)).json()["users"]
    zs_row = next(x for x in users if x["username"] == "zhangsan")
    assert zs_row["last_login_ip"] is not None and zs_row["projects"] == []


# ---- 3.2 / 3.4 项目字段与成员 ----


async def test_项目字段_状态_收藏_最近访问(client, auth_on):
    admin = await _login(client)
    await _user(client, admin, "owner1")
    resp = await client.post("/api/v1/projects", headers=admin,
                             json={"name": "商城", "code": "MALL", "owner": "owner1", "description": "电商"})
    p = resp.json()
    assert (p["code"], p["owner"], p["status"], p["members"]) == \
        ("MALL", "owner1", "active", {"admin": "project_admin", "owner1": "project_admin"})
    assert (await client.post("/api/v1/projects", headers=admin,
                              json={"name": "另一个", "code": "MALL"})).status_code == 400  # 编码唯一
    assert (await client.post("/api/v1/projects", headers=admin,
                              json={"name": "X", "owner": "nobody"})).status_code == 400  # 负责人须存在

    # 收藏 + 最近访问（详情接口记入）
    assert (await client.post("/api/v1/projects/商城/favorite", headers=admin)).json()["favorite"] is True
    detail = (await client.get("/api/v1/projects/商城", headers=admin)).json()
    assert detail["favorite"] and detail["last_visited"] and detail["versions"] == 0
    listed = (await client.get("/api/v1/projects", headers=admin)).json()["projects"]
    assert listed[0]["project"] == "商城" and listed[0]["favorite"]
    me = (await client.get("/api/v1/auth/me", headers=admin)).json()
    assert me["prefs"]["favorites"] == ["商城"] and me["prefs"]["recent"][0]["project"] == "商城"

    # 归档：写操作被拒，恢复后可写；筛选与搜索
    await client.put("/api/v1/projects/商城", headers=admin, json={"status": "archived"})
    assert (await client.post("/api/v1/plans", headers=admin,
                              json={"name": "计划", "project": "商城"})).status_code == 409
    assert (await client.get("/api/v1/projects", headers=admin,
                             params={"include_archived": "false"})).json()["projects"] == []
    assert (await client.put("/api/v1/projects/商城", headers=admin,
                             json={"status": "active"})).json()["status"] == "active"
    assert [x["project"] for x in (await client.get(
        "/api/v1/projects", headers=admin, params={"keyword": "mall"})).json()["projects"]] == ["商城"]


async def test_成员管理与项目角色(client, auth_on):
    admin = await _login(client)
    lead = await _user(client, admin, "lead")
    tester = await _user(client, admin, "tester1")
    await _project(client, admin, "商城", {"lead": "test_lead"})

    # 负责人（测试负责人角色）不能管成员；项目管理员可以
    assert (await client.put("/api/v1/projects/商城/members", headers=lead,
                             json={"username": "tester1", "role": "tester"})).status_code == 403
    await client.put("/api/v1/projects/商城/members", headers=admin,
                     json={"username": "tester1", "role": "tester"})
    assert (await client.put("/api/v1/projects/商城/members", headers=admin,
                             json={"username": "ghost", "role": "tester"})).status_code == 400
    assert (await client.put("/api/v1/projects/商城/members", headers=admin,
                             json={"username": "tester1", "role": "boss"})).status_code == 400
    members = (await client.get("/api/v1/projects/商城/members", headers=tester)).json()["members"]
    assert {m["username"]: m["role"] for m in members} == \
        {"admin": "project_admin", "lead": "test_lead", "tester1": "tester"}
    # 至少保留一名项目管理员
    assert (await client.delete("/api/v1/projects/商城/members/admin", headers=admin)).status_code == 400
    me = (await client.get("/api/v1/auth/me", headers=tester)).json()
    assert me["projects"] == [{"project": "商城", "role": "tester", "status": "active"}]
    # 同一用户在不同项目可拥有不同角色
    await _project(client, admin, "后台", {"tester1": "project_admin"})
    me = (await client.get("/api/v1/auth/me", headers=tester)).json()
    assert {p["project"]: p["role"] for p in me["projects"]} == {"商城": "tester", "后台": "project_admin"}
    # 删除用户清理成员关系
    await client.delete("/api/v1/auth/users/tester1", headers=admin)
    members = (await client.get("/api/v1/projects/商城/members", headers=admin)).json()["members"]
    assert "tester1" not in {m["username"] for m in members}


# ---- 3.3 / 3.5 接口级项目隔离与权限粒度 ----


async def test_跨项目隔离_非成员不可见不可访问(client, auth_on):
    admin = await _login(client)
    a_user = await _user(client, admin, "ua")
    b_user = await _user(client, admin, "ub")
    await _project(client, admin, "A", {"ua": "project_admin"})
    await _project(client, admin, "B", {"ub": "project_admin"})
    task_a = await _task(client, a_user, "A")
    plan_a = (await client.post("/api/v1/plans", headers=a_user,
                                json={"name": "A计划", "project": "A"})).json()["plan_id"]

    # 列表只见所属项目
    assert [p["project"] for p in (await client.get("/api/v1/projects", headers=b_user)).json()["projects"]] == ["B"]
    assert (await client.get("/api/v1/tasks", headers=b_user)).json()["tasks"] == []
    assert (await client.get("/api/v1/plans", headers=b_user)).json()["plans"] == []
    assert (await client.get("/api/v1/cases", headers=b_user)).json()["total"] == 0
    # 直接拿 ID / 项目名访问被封禁
    for path in (f"/api/v1/tasks/{task_a}", f"/api/v1/tasks/{task_a}/versions?kind=case&entity_id=x", f"/api/v1/plans/{plan_a}",
                 "/api/v1/projects/A", "/api/v1/projects/A/members", "/api/v1/projects/A/modules",
                 "/api/v1/recycle-bin?project=A", "/api/v1/projects/cases?project=A",
                 "/api/v1/reports/summary?project=A", "/api/v1/cases?project=A"):
        resp = await client.get(path, headers=b_user)
        assert resp.status_code == 403, (path, resp.status_code, resp.text)
    assert (await client.post(f"/api/v1/tasks/{task_a}/review", headers=b_user,
                              json={"items": []})).status_code == 403
    assert (await client.post("/api/v1/tasks", headers=b_user,
                              data={"text": "x", "project": "A"})).status_code == 403
    assert (await client.post("/api/v1/plans", headers=b_user,
                              json={"name": "偷建", "project": "A"})).status_code == 403
    # 不存在的项目名对非管理员同样 403（不暴露存在性）
    assert (await client.post("/api/v1/tasks", headers=b_user,
                              data={"text": "x", "project": "不存在"})).status_code == 403
    # 系统管理员不受限
    assert (await client.get(f"/api/v1/tasks/{task_a}", headers=admin)).status_code == 200
    assert len((await client.get("/api/v1/projects", headers=admin)).json()["projects"]) == 2


async def test_项目角色权限矩阵(client, auth_on):
    admin = await _login(client)
    lead = await _user(client, admin, "lead")
    tester = await _user(client, admin, "tester1")
    viewer = await _user(client, admin, "viewer1")
    await _project(client, admin, "A", {"lead": "test_lead", "tester1": "tester", "viewer1": "viewer"})
    task_id = await _task(client, lead, "A", approve=False)

    # 只读：可看不可改
    assert (await client.get(f"/api/v1/tasks/{task_id}", headers=viewer)).status_code == 200
    assert (await client.post(f"/api/v1/tasks/{task_id}/review", headers=viewer,
                              json={"items": []})).status_code == 403
    assert (await client.post("/api/v1/tasks", headers=viewer,
                              data={"text": "x", "project": "A"})).status_code == 403
    assert (await client.post("/api/v1/projects/A/modules", headers=viewer,
                              json={"name": "登录"})).status_code == 403
    # 测试人员：可编辑/AI 生成/导出，不可评审、不可建计划、不可分配
    assert (await client.post(f"/api/v1/tasks/{task_id}/review", headers=tester,
                              json={"items": []})).status_code == 403
    assert (await client.post("/api/v1/plans", headers=tester,
                              json={"name": "p", "project": "A"})).status_code == 403
    resp = await client.post(f"/api/v1/tasks/{task_id}/editing", headers=tester,
                             json={"kind": "case", "entity_id": "x", "action": "start"})
    assert resp.status_code != 403, resp.text
    # 测试负责人：评审、计划、分配、版本模块可管；不可编辑项目/成员
    assert (await client.post(f"/api/v1/tasks/{task_id}/review", headers=lead,
                              json={"items": []})).status_code == 200
    plan = (await client.post("/api/v1/plans", headers=lead, json={"name": "p", "project": "A"})).json()
    assert "plan_id" in plan
    assert (await client.post("/api/v1/projects/A/modules", headers=lead,
                              json={"name": "登录"})).status_code == 200
    assert (await client.put("/api/v1/projects/A", headers=lead, json={"description": "x"})).status_code == 403
    assert (await client.put("/api/v1/projects/A", headers=lead, json={"status": "paused"})).status_code == 403
    # 测试人员可执行
    resp = await client.post(f"/api/v1/plans/{plan['plan_id']}/runs", headers=tester, json={})
    assert resp.status_code != 403, resp.text
    # 计划分配仅负责人/管理员
    assert (await client.post(f"/api/v1/plans/{plan['plan_id']}/assign", headers=tester,
                              json={"assignee": "tester1", "item_ids": []})).status_code == 403
    # 项目创建/删除仅系统管理员
    assert (await client.post("/api/v1/projects", headers=lead, json={"name": "新"})).status_code == 403
    assert (await client.delete("/api/v1/projects/A", headers=lead)).status_code == 403


# ---- 4.1 / 4.2 版本与模块树 ----


async def test_版本管理(client):
    await client.post("/api/v1/projects", json={"name": "P"})
    resp = await client.post("/api/v1/projects/P/versions", json={
        "name": "4.2.0", "code": "v420", "start_date": "2026-09-01", "planned_end": "2026-09-30"})
    assert resp.status_code == 200, resp.text
    vid = resp.json()["version_id"]
    assert (await client.post("/api/v1/projects/P/versions", json={"name": "4.2.0"})).status_code == 400
    assert (await client.post("/api/v1/projects/P/versions",
                              json={"name": "x", "status": "weird"})).status_code == 400
    v = (await client.put(f"/api/v1/projects/P/versions/{vid}",
                          json={"status": "in_progress", "actual_end": ""})).json()
    assert v["status"] == "in_progress"
    listed = (await client.get("/api/v1/projects/P/versions")).json()
    assert [x["name"] for x in listed["versions"]] == ["4.2.0"] and "archived" in listed["statuses"]
    # 版本随项目改名联动
    await client.put("/api/v1/projects/P", json={"name": "P2"})
    assert (await client.get("/api/v1/projects/P2/versions")).json()["versions"][0]["project"] == "P2"
    assert (await client.delete(f"/api/v1/projects/P2/versions/{vid}")).status_code == 200
    assert (await client.get("/api/v1/projects/P2/versions")).json()["versions"] == []


async def test_模块树_层级_移动_排序_逻辑删除(client):
    await client.post("/api/v1/projects", json={"name": "P"})

    async def add(name, parent=None):
        resp = await client.post("/api/v1/projects/P/modules", json={"name": name, "parent_id": parent})
        assert resp.status_code == 200, resp.text
        return resp.json()

    m1 = await add("登录")
    m2 = await add("密码找回", m1["module_id"])
    m3 = await add("短信", m2["module_id"])
    m4 = await add("验证码", m3["module_id"])
    m5 = await add("重发", m4["module_id"])
    assert m5["path"] == "登录/密码找回/短信/验证码/重发"
    assert (await client.post("/api/v1/projects/P/modules",
                              json={"name": "第六级", "parent_id": m5["module_id"]})).status_code == 400
    assert (await client.post("/api/v1/projects/P/modules",
                              json={"name": "登录"})).status_code == 400  # 同级重名
    assert (await client.post("/api/v1/projects/P/modules",
                              json={"name": "a/b"})).status_code == 400
    order = await add("下单")
    tree = (await client.get("/api/v1/projects/P/modules")).json()["tree"]
    assert [n["name"] for n in tree] == ["登录", "下单"] and tree[0]["children"][0]["name"] == "密码找回"

    # 排序 / 移动 / 禁止移到自身子树
    resp = await client.post("/api/v1/projects/P/modules/reorder",
                             json={"parent_id": None, "ordered_ids": [order["module_id"], m1["module_id"]]})
    assert [n["name"] for n in resp.json()["tree"]] == ["下单", "登录"]
    assert (await client.put(f"/api/v1/projects/P/modules/{m1['module_id']}",
                             json={"move": True, "parent_id": m3["module_id"]})).status_code == 400
    resp = await client.put(f"/api/v1/projects/P/modules/{m2['module_id']}",
                            json={"move": True, "parent_id": order["module_id"], "name": "找回"})
    assert resp.json()["path"] == "下单/找回"
    assert (await client.put(f"/api/v1/projects/P/modules/{m3['module_id']}",
                             json={"move": True, "parent_id": m5["module_id"]})).status_code == 400  # 超 5 级

    # 逻辑删除含子树 → 回收站 → 恢复；未删除不可永久删除
    resp = await client.delete(f"/api/v1/projects/P/modules/{m2['module_id']}")
    assert set(resp.json()["removed"]) == {m2["module_id"], m3["module_id"], m4["module_id"], m5["module_id"]}
    data = (await client.get("/api/v1/projects/P/modules", params={"include_deleted": "true"})).json()
    assert len(data["deleted"]) == 4 and [n["name"] for n in data["tree"]] == ["下单", "登录"]
    assert (await client.delete(f"/api/v1/projects/P/modules/{order['module_id']}",
                                params={"permanent": "true"})).status_code == 400
    restored = (await client.post(f"/api/v1/projects/P/modules/{m2['module_id']}/restore")).json()
    assert restored["path"] == "下单/找回"
    # 子模块仍在回收站；永久删除子树
    assert (await client.delete(f"/api/v1/projects/P/modules/{m3['module_id']}",
                                params={"permanent": "true"})).json()["permanent"] is True
    data = (await client.get("/api/v1/projects/P/modules", params={"include_deleted": "true"})).json()
    assert data["deleted"] == []


async def test_模块被用例引用禁止物理删除(client):
    await client.post("/api/v1/projects", json={"name": "P"})
    case = make_case(module="登录")
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(case), review_reply(True)])
    resp = await client.post("/api/v1/tasks", data={"text": "登录需求", "project": "P"})
    assert resp.status_code == 200, resp.text
    m = (await client.post("/api/v1/projects/P/modules", json={"name": "登录"})).json()
    await client.delete(f"/api/v1/projects/P/modules/{m['module_id']}")
    data = (await client.get("/api/v1/projects/P/modules", params={"include_deleted": "true"})).json()
    assert data["deleted"][0]["referenced"] is True
    resp = await client.delete(f"/api/v1/projects/P/modules/{m['module_id']}", params={"permanent": "true"})
    assert resp.status_code == 400 and "引用" in resp.json()["detail"]
    assert (await client.post(f"/api/v1/projects/P/modules/{m['module_id']}/restore")).status_code == 200


async def test_成员联想_项目管理员可查用户名(client, auth_on):
    admin = await _login(client)
    pa = await _user(client, admin, "pa")
    t1 = await _user(client, admin, "t1")
    await _project(client, admin, "A", {"pa": "project_admin", "t1": "tester"})
    r = await client.get("/api/v1/auth/users/lookup", headers=pa)
    assert r.status_code == 200 and {u["username"] for u in r.json()["users"]} >= {"admin", "pa", "t1"}
    assert set(r.json()["users"][0]) == {"username", "name"}   # 不暴露邮箱/手机/登录信息
    assert (await client.get("/api/v1/auth/users/lookup", headers=t1)).status_code == 403

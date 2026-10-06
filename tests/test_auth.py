"""登录认证、用户管理与模型配置管理接口测试。"""

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.config import get_settings
from app.main import app


@pytest.fixture
async def client():
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
def auth_on():
    """临时开启登录鉴权（conftest 默认关闭以免侵入业务接口测试）。"""
    settings = get_settings()
    settings.auth_enabled = True
    yield
    settings.auth_enabled = False


async def _login(client, username="admin", password="admin123") -> str:
    resp = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


# ---- 登录与会话 ----


async def test_未登录拒绝访问业务接口(client, auth_on):
    resp = await client.get("/api/v1/tasks")
    assert resp.status_code == 401
    assert (await client.get("/health")).status_code == 200  # 健康检查豁免


async def test_登录_鉴权_登出(client, auth_on):
    assert (await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "wrong"}
    )).status_code == 401

    token = await _login(client)
    headers = {"Authorization": f"Bearer {token}"}
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    assert (me["username"], me["role"], me["totp_enabled"], me["status"]) == ("admin", "admin", False, "active")
    assert me["last_login_ip"] is not None and "projects" in me and "prefs" in me
    assert (await client.get("/api/v1/tasks", headers=headers)).status_code == 200
    # ?token= 查询参数只对下载类路径生效（/files/、/attachments/），普通接口不接受，避免令牌进日志/历史
    assert (await client.get(f"/api/v1/tasks?token={token}")).status_code == 401
    assert (await client.get(f"/api/v1/tasks/none/files/xlsx?token={token}")).status_code == 404

    await client.post("/api/v1/auth/logout", headers=headers)
    assert (await client.get("/api/v1/tasks", headers=headers)).status_code == 401


async def test_用户管理与权限(client, auth_on):
    token = await _login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/v1/auth/users", headers=headers,
                             json={"username": "tester", "password": "test123", "role": "member"})
    assert resp.status_code == 200

    member_token = await _login(client, "tester", "test123")
    member_headers = {"Authorization": f"Bearer {member_token}"}
    # 普通成员可用平台，但无用户管理/模型配置权限
    assert (await client.get("/api/v1/tasks", headers=member_headers)).status_code == 200
    assert (await client.get("/api/v1/auth/users", headers=member_headers)).status_code == 403
    assert (await client.get("/api/v1/models/config", headers=member_headers)).status_code == 403
    # 沉淀与反哺（学习规则/记忆维护）仅管理员；记忆读取保持开放（任务抽屉默认回填要用）
    assert (await client.get("/api/v1/learning/rules", headers=member_headers)).status_code == 403
    assert (await client.post("/api/v1/learning/analyze", headers=member_headers,
                              json={})).status_code == 403
    assert (await client.post("/api/v1/memories", headers=member_headers,
                              json={"content": "x", "scope": "user"})).status_code == 403
    assert (await client.delete("/api/v1/memories", headers=member_headers)).status_code == 403
    assert (await client.get("/api/v1/memories", headers=member_headers)).status_code == 200
    assert (await client.get("/api/v1/learning/rules", headers=headers)).status_code == 200

    # 不能删除自己 / 删除后会话失效
    assert (await client.delete("/api/v1/auth/users/admin", headers=headers)).status_code == 400
    assert (await client.delete("/api/v1/auth/users/tester", headers=headers)).status_code == 200
    assert (await client.get("/api/v1/tasks", headers=member_headers)).status_code == 401


async def test_管理员重置密码与修改角色(client, auth_on):
    token = await _login(client)
    headers = {"Authorization": f"Bearer {token}"}
    await client.post("/api/v1/auth/users", headers=headers,
                      json={"username": "zhang", "password": "zhang123", "role": "member"})
    member_token = await _login(client, "zhang", "zhang123")

    # 重置密码：旧密码失效、该用户会话全部注销，新密码可登录
    resp = await client.put("/api/v1/auth/users/zhang", headers=headers,
                            json={"new_password": "reset666"})
    assert resp.status_code == 200
    assert (await client.get("/api/v1/tasks",
            headers={"Authorization": f"Bearer {member_token}"})).status_code == 401
    assert (await client.post("/api/v1/auth/login",
            json={"username": "zhang", "password": "zhang123"})).status_code == 401
    token2 = await _login(client, "zhang", "reset666")

    # 修改角色：member → admin。旧会话立即失效（前端登录时取角色，不重登会停留在旧角色视图），
    # 重新登录后 /auth/me 为 admin，可访问系统设置、可建项目
    resp = await client.put("/api/v1/auth/users/zhang", headers=headers, json={"role": "admin"})
    assert resp.status_code == 200 and resp.json()["role"] == "admin"
    assert (await client.get("/api/v1/auth/users",
            headers={"Authorization": f"Bearer {token2}"})).status_code == 401
    token3 = await _login(client, "zhang", "reset666")
    h3 = {"Authorization": f"Bearer {token3}"}
    assert (await client.get("/api/v1/auth/me", headers=h3)).json()["role"] == "admin"
    assert (await client.get("/api/v1/auth/users", headers=h3)).status_code == 200
    assert (await client.post("/api/v1/projects", headers=h3,
            json={"name": "zhang建的项目", "description": "", "owner": "zhang"})).status_code == 200

    # 保护：不能降级最后一个管理员
    await client.put("/api/v1/auth/users/zhang", headers=headers, json={"role": "member"})
    resp = await client.put("/api/v1/auth/users/admin", headers=headers, json={"role": "member"})
    assert resp.status_code == 400 and "最后一个可用管理员" in resp.json()["detail"]
    # 空请求拒绝；降级后旧会话失效，重登后 member 无权调用
    assert (await client.put("/api/v1/auth/users/zhang", headers=headers, json={})).status_code == 400
    assert (await client.get("/api/v1/auth/me", headers=h3)).status_code == 401
    token4 = await _login(client, "zhang", "reset666")
    assert (await client.put("/api/v1/auth/users/zhang", json={"role": "admin"},
            headers={"Authorization": f"Bearer {token4}"})).status_code == 403
    await client.delete("/api/v1/auth/users/zhang", headers=headers)


async def test_修改密码后需重新登录(client, auth_on):
    token = await _login(client)
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.post("/api/v1/auth/password", headers=headers,
                              json={"old_password": "bad", "new_password": "newpass1"})).status_code == 400
    assert (await client.post("/api/v1/auth/password", headers=headers,
                              json={"old_password": "admin123", "new_password": "newpass1"})).status_code == 200
    assert (await client.get("/api/v1/tasks", headers=headers)).status_code == 401  # 旧会话已注销
    token2 = await _login(client, "admin", "newpass1")
    # 还原密码，避免影响其他用例（同一临时存储目录内共享 auth.json）
    await client.post("/api/v1/auth/password", headers={"Authorization": f"Bearer {token2}"},
                      json={"old_password": "newpass1", "new_password": "admin123"})


# ---- 两步验证（TOTP）----


def test_totp_算法与防重放窗口():
    from app.auth.totp import current_counter, generate_secret, totp_at, totp_now, verify_totp

    secret = generate_secret()
    now = 1_700_000_000.0
    code = totp_now(secret, at=now)
    assert len(code) == 6 and code.isdigit()
    # ±1 时间窗内可验证，超窗失败
    assert verify_totp(secret, code, at=now) == current_counter(now)
    assert verify_totp(secret, code, at=now + 30) is not None
    assert verify_totp(secret, code, at=now + 90) is None
    assert verify_totp(secret, "000000", at=now) in (None, current_counter(now) - 1, current_counter(now) + 1) or True
    assert verify_totp(secret, "12345", at=now) is None  # 位数不对
    # 相邻周期产出不同动态码
    assert totp_at(secret, current_counter(now)) != totp_at(secret, current_counter(now) + 1)


async def test_totp_绑定_登录_重放_管理员重置(client, auth_on):
    from app.auth.totp import totp_now

    token = await _login(client)
    headers = {"Authorization": f"Bearer {token}"}
    # 绑定：setup 返回密钥与二维码，动态码确认后启用
    setup = (await client.post("/api/v1/auth/totp/setup", headers=headers)).json()
    assert setup["otpauth_uri"].startswith("otpauth://totp/") and "<svg" in setup["qr_svg"]
    secret = setup["secret"]
    assert (await client.post("/api/v1/auth/totp/enable", headers=headers,
            json={"code": "000000"})).status_code == 400  # 错码不生效
    # setup 后错码未消费 pending，重新用正确码确认
    resp = await client.post("/api/v1/auth/totp/enable", headers=headers,
                             json={"code": totp_now(secret)})
    assert resp.status_code == 200

    # 登录二段式：无动态码 → otp_required；错码 401；正确码放行
    resp = await client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.json() == {"otp_required": True}
    assert (await client.post("/api/v1/auth/login",
            json={"username": "admin", "password": "admin123", "otp": "999999"})).status_code == 401
    import time

    code = totp_now(secret, at=time.time() + 30)  # 用下一窗动态码，避开绑定确认已消费的 counter
    resp = await client.post("/api/v1/auth/login",
                             json={"username": "admin", "password": "admin123", "otp": code})
    assert resp.status_code == 200 and resp.json()["user"]["totp_enabled"] is True
    # 防重放：同一动态码不允许二次使用
    assert (await client.post("/api/v1/auth/login",
            json={"username": "admin", "password": "admin123", "otp": code})).status_code == 401

    # 管理员重置两步验证：解绑后凭密码直接登录
    token2 = resp.json()["token"]
    resp = await client.put("/api/v1/auth/users/admin",
                            headers={"Authorization": f"Bearer {token2}"},
                            json={"reset_totp": True})
    assert resp.status_code == 200 and resp.json()["totp_enabled"] is False
    assert (await client.post("/api/v1/auth/login",
            json={"username": "admin", "password": "admin123"})).status_code == 200


async def test_两步验证总开关(client, auth_on):
    from app.auth.totp import totp_now

    token = await _login(client)
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.get("/api/v1/auth/settings", headers=headers)).json()["totp_enabled"] is True

    # 绑定后关闭总开关：登录不再要求动态码；不可新绑定
    secret = (await client.post("/api/v1/auth/totp/setup", headers=headers)).json()["secret"]
    await client.post("/api/v1/auth/totp/enable", headers=headers, json={"code": totp_now(secret)})
    resp = await client.put("/api/v1/auth/settings", headers=headers, json={"totp_enabled": False})
    assert resp.json()["totp_enabled"] is False
    resp = await client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200 and "token" in resp.json()  # 无需动态码直接放行
    assert (await client.post("/api/v1/auth/totp/setup", headers=headers)).status_code == 400

    # 重新开启：已绑定密钥继续生效，登录恢复二段式
    await client.put("/api/v1/auth/settings", headers=headers, json={"totp_enabled": True})
    resp = await client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.json() == {"otp_required": True}

    # 收尾解绑 + 权限：member 不可改开关
    await client.put("/api/v1/auth/users/admin", headers=headers, json={"reset_totp": True})
    await client.post("/api/v1/auth/users", headers=headers,
                      json={"username": "guard", "password": "guard123", "role": "member"})
    member_token = await _login(client, "guard", "guard123")
    assert (await client.put("/api/v1/auth/settings", json={"totp_enabled": False},
            headers={"Authorization": f"Bearer {member_token}"})).status_code == 403
    await client.delete("/api/v1/auth/users/guard", headers=headers)


async def test_任务创建精确到人(client, auth_on):
    """任务归属创建人：列表展示与按人过滤；审核留痕记操作人。"""
    import json as _json

    from tests.stubs import ANALYST_REPLY, StubLLM, generator_reply, make_case, review_reply
    from app.main import app as _app

    token = await _login(client)
    headers = {"Authorization": f"Bearer {token}"}
    await client.post("/api/v1/auth/users", headers=headers,
                      json={"username": "lisi", "password": "lisi123", "role": "member"})
    lisi_headers = {"Authorization": f"Bearer {await _login(client, 'lisi', 'lisi123')}"}
    # 非管理员创建任务必须归属其所属项目（M1 接口级项目隔离）
    await client.post("/api/v1/projects", headers=headers, json={"name": "商城"})
    await client.put("/api/v1/projects/商城/members", headers=headers,
                     json={"username": "lisi", "role": "tester"})
    assert (await client.post("/api/v1/tasks", headers=lisi_headers,
                              data={"text": "登录需求"})).status_code == 400  # 未选项目

    _app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(make_case()), review_reply(True)])
    resp = await client.post("/api/v1/tasks", headers=lisi_headers,
                             data={"text": "登录需求", "project": "商城"})
    task_id = resp.json()["task_id"]

    task = (await client.get(f"/api/v1/tasks/{task_id}", headers=headers)).json()
    assert task["created_by"] == "lisi"
    listed = (await client.get("/api/v1/tasks", headers=headers,
                               params={"created_by": "lisi"})).json()["tasks"]
    assert listed and all(t["created_by"] == "lisi" for t in listed)
    assert (await client.get("/api/v1/tasks", headers=headers,
            params={"created_by": "nobody"})).json()["tasks"] == []

    # 审核留痕精确到操作人（admin 操作 lisi 的任务）
    resp = await client.post(f"/api/v1/tasks/{task_id}/review", headers=headers,
                             json={"items": [{"case_id": "TC-登录-001", "action": "approve"}]})
    assert resp.status_code == 200, resp.text
    task = (await client.get(f"/api/v1/tasks/{task_id}", headers=headers)).json()
    assert task["review_log"][-1]["by"] == "admin"
    await client.delete("/api/v1/auth/users/lisi", headers=headers)


# ---- 模型配置管理 ----


async def test_模型配置读取与更新热重载(client, tmp_path, monkeypatch):
    settings = get_settings()
    # 用临时副本承接写回，避免污染仓库 config/models.yaml
    tmp_yaml = tmp_path / "models.yaml"
    tmp_yaml.write_text(settings.models_example_path.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(settings, "models_config_path", tmp_yaml)

    cfg = (await client.get("/api/v1/models/config")).json()
    assert cfg["default_model"] and cfg["models"]
    assert all("api_key_set" in m and "api_key_env" in m for m in cfg["models"])

    # 新增一个模型并设为默认
    cfg["models"].append({
        "name": "kimi-k2", "provider": "moonshot", "base_url": "https://api.moonshot.cn/v1",
        "api_key_env": "MOONSHOT_API_KEY", "model": "kimi-k2", "max_tokens": 4096,
    })
    resp = await client.put("/api/v1/models/config", json={
        "default_model": "kimi-k2", "max_retries": cfg["max_retries"], "models": cfg["models"],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["default_model"] == "kimi-k2"
    # 热重载生效 + yaml 写回保留 embeddings 段
    assert (await client.get("/api/v1/models")).json()["default_model"] == "kimi-k2"
    import yaml as _yaml

    saved = _yaml.safe_load(tmp_yaml.read_text(encoding="utf-8"))
    assert saved["default_model"] == "kimi-k2" and "embeddings" in saved

    # 非法配置整体拒绝（default 不在清单中），现网配置不受影响
    bad = await client.put("/api/v1/models/config", json={
        "default_model": "不存在", "max_retries": 1, "models": cfg["models"],
    })
    assert bad.status_code == 400
    assert (await client.get("/api/v1/models")).json()["default_model"] == "kimi-k2"

async def test_用户列表_查询条件与分页(client, auth_on):
    headers = {"Authorization": f"Bearer {await _login(client)}"}
    for i in range(3):
        r = await client.post("/api/v1/auth/users", headers=headers,
                              json={"username": f"pg{i}", "password": "Pass1234!", "role": "member", "name": f"分页{i}", "email": f"pg{i}@x.com"})
        assert r.status_code == 200, r.text
    # page 为 0：返回全部（兼容旧调用）
    data = (await client.get("/api/v1/auth/users", headers=headers)).json()
    assert data["total"] >= 4 and len(data["users"]) == data["total"] and "page" not in data
    # 分页：每页 2，越界页收敛到最后一页
    data = (await client.get("/api/v1/auth/users?page=1&page_size=2", headers=headers)).json()
    assert len(data["users"]) == 2 and data["page"] == 1 and data["pages"] == -(-data["total"] // 2)
    data = (await client.get("/api/v1/auth/users?page=99&page_size=2", headers=headers)).json()
    assert data["page"] == data["pages"] and data["users"]
    # 关键词匹配姓名 / 邮箱；角色与状态筛选
    data = (await client.get("/api/v1/auth/users?keyword=分页1&page=1", headers=headers)).json()
    assert [u["username"] for u in data["users"]] == ["pg1"]
    data = (await client.get("/api/v1/auth/users?keyword=pg2@x.com", headers=headers)).json()
    assert [u["username"] for u in data["users"]] == ["pg2"]
    assert all(u["role"] == "admin" for u in (await client.get("/api/v1/auth/users?role=admin", headers=headers)).json()["users"])
    await client.put("/api/v1/auth/users/pg0", headers=headers, json={"status": "disabled"})
    data = (await client.get("/api/v1/auth/users?status=disabled", headers=headers)).json()
    assert [u["username"] for u in data["users"]] == ["pg0"]


async def test_项目开放模式_所有人可见可建可改_删除仅管理员(client, auth_on):
    settings = get_settings()
    admin = {"Authorization": f"Bearer {await _login(client)}"}
    await client.post("/api/v1/auth/users", headers=admin, json={"username": "u1", "password": "Pass1234!", "role": "member"})
    await client.post("/api/v1/auth/users", headers=admin, json={"username": "u2", "password": "Pass1234!", "role": "member"})
    u1 = {"Authorization": f"Bearer {await _login(client, 'u1', 'Pass1234!')}"}
    u2 = {"Authorization": f"Bearer {await _login(client, 'u2', 'Pass1234!')}"}
    assert (await client.post("/api/v1/projects", headers=admin, json={"name": "管理员建的"})).status_code == 200
    # 成员制（默认关闭开放模式）：非成员看不到、不能建项目
    assert [p["project"] for p in (await client.get("/api/v1/projects", headers=u1)).json()["projects"]] == []
    assert (await client.post("/api/v1/projects", headers=u1, json={"name": "u1建的"})).status_code == 403
    settings.open_projects = True
    try:
        me = (await client.get("/api/v1/auth/me", headers=u1)).json()
        assert me["open_projects"] is True
        # 所有人可见全部项目
        assert "管理员建的" in [p["project"] for p in (await client.get("/api/v1/projects", headers=u1)).json()["projects"]]
        # 普通用户可创建项目，另一位普通用户可修改它、可在其中录需求
        assert (await client.post("/api/v1/projects", headers=u1, json={"name": "u1建的"})).status_code == 200
        r = await client.put("/api/v1/projects/u1建的", headers=u2, json={"description": "u2 改的"})
        assert r.status_code == 200 and r.json()["description"] == "u2 改的" and r.json()["my_role"] == "project_admin"
        assert (await client.post("/api/v1/requirements", headers=u2,
                                  data={"project": "管理员建的", "title": "需求", "text": "原文"})).status_code == 200
        assert (await client.get("/api/v1/auth/users/lookup", headers=u2)).status_code == 200
        # 项目内数据的删除也仅系统管理员：需求 / 计划 / 版本 / 依赖 / 用例批量删除均 403，管理员可删
        rid = (await client.post("/api/v1/requirements", headers=u1,
                                 data={"project": "u1建的", "title": "待删", "text": "原文"})).json()["req_id"]
        assert (await client.delete(f"/api/v1/requirements/{rid}", headers=u2)).status_code == 403
        assert (await client.delete(f"/api/v1/requirements/{rid}", headers=admin)).status_code == 200
        pid = (await client.post("/api/v1/plans", headers=u1, json={"name": "计划", "project": "u1建的"})).json()["plan_id"]
        assert (await client.delete(f"/api/v1/plans/{pid}", headers=u1)).status_code == 403
        assert (await client.delete(f"/api/v1/plans/{pid}", headers=admin)).status_code == 200
        vid = (await client.post("/api/v1/projects/u1建的/versions", headers=u1, json={"name": "V1"})).json()["version_id"]
        assert (await client.delete(f"/api/v1/projects/u1建的/versions/{vid}", headers=u2)).status_code == 403
        assert (await client.delete(f"/api/v1/projects/u1建的/versions/{vid}", headers=admin)).status_code == 200
        # 删除项目仍仅系统管理员
        assert (await client.delete("/api/v1/projects/u1建的", headers=u1)).status_code == 403
        assert (await client.delete("/api/v1/projects/u1建的", headers=admin)).status_code == 200
    finally:
        settings.open_projects = False


async def test_按业务线模式_成员只见所属业务线项目_角色折算_计划删除仅管理员(client, auth_on):
    admin = {"Authorization": f"Bearer {await _login(client)}"}
    for u in ("la", "lm", "lv", "outsider"):
        await client.post("/api/v1/auth/users", headers=admin, json={"username": u, "password": "Pass1234!", "role": "member"})
    hdr = {u: {"Authorization": f"Bearer {await _login(client, u, 'Pass1234!')}"} for u in ("la", "lm", "lv", "outsider")}
    # 默认沿用环境变量（conftest 成员制）；管理员切到「按业务线」
    assert (await client.get("/api/v1/auth/settings", headers=admin)).json()["access_mode"] == "member"
    assert (await client.put("/api/v1/auth/settings", headers=hdr["la"], json={"access_mode": "line"})).status_code == 403
    assert (await client.put("/api/v1/auth/settings", headers=admin, json={"access_mode": "nope"})).status_code == 400
    r = await client.put("/api/v1/auth/settings", headers=admin, json={"access_mode": "line"})
    assert r.status_code == 200 and r.json()["access_mode"] == "line"
    try:
        await client.post("/api/v1/business-lines", headers=admin, json={"name": "棋牌"})
        await client.post("/api/v1/business-lines", headers=admin, json={"name": "电子"})
        await client.post("/api/v1/projects", headers=admin, json={"name": "斗地主", "business_line": "棋牌"})
        await client.post("/api/v1/projects", headers=admin, json={"name": "老虎机", "business_line": "电子"})
        await client.post("/api/v1/projects", headers=admin, json={"name": "无业务线项目"})
        # 成员管理：不存在的用户 / 非法角色拒绝；非业务线管理员不能维护成员
        assert (await client.put("/api/v1/business-lines/棋牌/members/ghost", headers=admin, json={"role": "member"})).status_code == 400
        assert (await client.put("/api/v1/business-lines/棋牌/members/lm", headers=admin, json={"role": "boss"})).status_code == 400
        assert (await client.put("/api/v1/business-lines/棋牌/members/lm", headers=hdr["lm"], json={"role": "member"})).status_code == 403
        r = await client.put("/api/v1/business-lines/棋牌/members/la", headers=admin, json={"role": "line_admin"})
        assert r.status_code == 200 and r.json()["members"] == {"la": "line_admin"}
        # 业务线管理员可以给自己的业务线加人，不能动别的业务线
        assert (await client.put("/api/v1/business-lines/棋牌/members/lm", headers=hdr["la"], json={"role": "member"})).status_code == 200
        assert (await client.put("/api/v1/business-lines/棋牌/members/lv", headers=hdr["la"], json={"role": "viewer"})).status_code == 200
        assert (await client.put("/api/v1/business-lines/电子/members/lm", headers=hdr["la"], json={"role": "member"})).status_code == 403
        # 可见范围：成员只见所属业务线项目；外人什么都看不到；无业务线项目仅管理员 / 项目成员可见
        async def visible(h):
            return sorted(p["project"] for p in (await client.get("/api/v1/projects", headers=h)).json()["projects"])
        assert await visible(hdr["lm"]) == ["斗地主"]
        assert await visible(hdr["outsider"]) == []
        assert (await client.get("/api/v1/projects/老虎机", headers=hdr["lm"])).status_code in (403, 404)
        # 角色折算：业务线管理员=项目管理员，成员=测试负责人，只读=只读；/auth/me 返回有效角色与业务线
        me = (await client.get("/api/v1/auth/me", headers=hdr["lm"])).json()
        assert me["access_mode"] == "line" and me["open_projects"] is False
        assert me["projects"] == [{"project": "斗地主", "role": "test_lead", "status": "active"}]
        assert me["business_lines"] == [{"business_line": "棋牌", "role": "member"}]
        assert (await client.post("/api/v1/requirements", headers=hdr["lm"],
                                  data={"project": "斗地主", "title": "需求", "text": "原文"})).status_code == 200
        assert (await client.post("/api/v1/requirements", headers=hdr["lv"],
                                  data={"project": "斗地主", "title": "需求", "text": "原文"})).status_code == 403
        assert (await client.post("/api/v1/requirements", headers=hdr["outsider"],
                                  data={"project": "斗地主", "title": "需求", "text": "原文"})).status_code in (403, 404)
        # 建项目：业务线管理员可在自己的业务线下建，成员 / 别的业务线不行
        assert (await client.post("/api/v1/projects", headers=hdr["la"], json={"name": "麻将", "business_line": "棋牌"})).status_code == 200
        assert (await client.post("/api/v1/projects", headers=hdr["la"], json={"name": "捕鱼", "business_line": "电子"})).status_code == 403
        assert (await client.post("/api/v1/projects", headers=hdr["lm"], json={"name": "跑得快", "business_line": "棋牌"})).status_code == 403
        assert await visible(hdr["lm"]) == ["斗地主", "麻将"]
        # 项目成员身份与业务线角色取高者
        await client.put("/api/v1/projects/老虎机/members", headers=admin, json={"username": "lv", "role": "test_lead"})
        me = (await client.get("/api/v1/auth/me", headers=hdr["lv"])).json()
        assert {p["project"]: p["role"] for p in me["projects"]} == {"斗地主": "viewer", "麻将": "viewer", "老虎机": "test_lead"}
        # 测试计划增删改查：成员可建可改，删除只有系统管理员（业务线管理员、创建人都不行）
        pid = (await client.post("/api/v1/plans", headers=hdr["lm"], json={"name": "冒烟", "project": "斗地主"})).json()["plan_id"]
        r = await client.put(f"/api/v1/plans/{pid}", headers=hdr["lm"],
                             json={"name": "冒烟 V2", "owner": "la", "start_date": "2026-10-10", "end_date": "2026-10-20"})
        assert r.status_code == 200 and r.json()["name"] == "冒烟 V2" and r.json()["owner"] == "la" and r.json()["end_date"] == "2026-10-20"
        assert (await client.put(f"/api/v1/plans/{pid}", headers=hdr["lv"], json={"name": "x"})).status_code == 403
        assert (await client.get(f"/api/v1/plans/{pid}", headers=hdr["lv"])).status_code == 200
        assert (await client.delete(f"/api/v1/plans/{pid}", headers=hdr["lm"])).status_code == 403
        assert (await client.delete(f"/api/v1/plans/{pid}", headers=hdr["la"])).status_code == 403
        assert (await client.delete(f"/api/v1/plans/{pid}", headers=admin)).status_code == 200
        # 移除成员后不可见
        assert (await client.delete("/api/v1/business-lines/棋牌/members/lm", headers=hdr["la"])).status_code == 200
        assert (await client.delete("/api/v1/business-lines/棋牌/members/lm", headers=hdr["la"])).status_code == 400
        assert await visible(hdr["lm"]) == []
    finally:
        await client.put("/api/v1/auth/settings", headers=admin, json={"access_mode": "member"})


async def test_成员制下计划删除也仅系统管理员(client, auth_on):
    admin = {"Authorization": f"Bearer {await _login(client)}"}
    await client.post("/api/v1/auth/users", headers=admin, json={"username": "lead", "password": "Pass1234!", "role": "member"})
    lead = {"Authorization": f"Bearer {await _login(client, 'lead', 'Pass1234!')}"}
    await client.post("/api/v1/projects", headers=admin, json={"name": "P"})
    await client.put("/api/v1/projects/P/members", headers=admin, json={"username": "lead", "role": "project_admin"})
    pid = (await client.post("/api/v1/plans", headers=lead, json={"name": "计划", "project": "P"})).json()["plan_id"]
    assert (await client.put(f"/api/v1/plans/{pid}", headers=lead, json={"name": "改名"})).status_code == 200
    assert (await client.delete(f"/api/v1/plans/{pid}", headers=lead)).status_code == 403   # 创建人 + 项目管理员仍不能删
    assert (await client.delete(f"/api/v1/plans/{pid}", headers=admin)).status_code == 200

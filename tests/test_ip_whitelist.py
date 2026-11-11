"""登录 IP 白名单：开启后只有名单内 IP（单个 / CIDR）能登录；开启时操作人自己的 IP 必须在名单里；被拒记安全日志带 IP。"""

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
    # 用例结束把白名单关掉，不影响其他测试登录
    app.state.auth.set_ip_whitelist(enabled=False, entries=[])


@pytest.fixture
def auth_on():
    settings = get_settings()
    settings.auth_enabled = True
    settings.trusted_proxies = "127.0.0.1"   # 测试客户端直连地址是 127.0.0.1：当它是可信代理，用 X-Forwarded-For 模拟不同来源 IP
    yield
    settings.auth_enabled = False
    settings.trusted_proxies = ""


def _from(ip: str) -> dict:
    return {"X-Forwarded-For": ip}


async def _login(client, username="admin", password="admin123", ip=None):
    return await client.post("/api/v1/auth/login", json={"username": username, "password": password},
                             headers=_from(ip) if ip else {})


async def _admin_headers(client) -> dict:
    resp = await _login(client)
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


async def test_白名单_仅管理员可管_开启前自己IP必须在名单_格式校验(client, auth_on):
    h = await _admin_headers(client)
    await client.post("/api/v1/auth/users", headers=h, json={"username": "wl_member", "password": "member123", "role": "member"})
    mh = {"Authorization": f"Bearer {(await _login(client, 'wl_member', 'member123')).json()['token']}"}
    assert (await client.get("/api/v1/auth/ip-whitelist", headers=mh)).status_code == 403

    cur = (await client.get("/api/v1/auth/ip-whitelist", headers=h)).json()
    assert cur["enabled"] is False and cur["entries"] == [] and cur["client_ip"] == "127.0.0.1"

    # 名单为空不能开启
    r = await client.put("/api/v1/auth/ip-whitelist", headers=h, json={"enabled": True})
    assert r.status_code == 400 and "为空" in r.json()["detail"]
    # 名单里没有自己的 IP 不能开启（防把自己锁在外面）
    r = await client.put("/api/v1/auth/ip-whitelist", headers=h, json={"enabled": True, "entries": [{"ip": "10.8.0.0/24"}]})
    assert r.status_code == 400 and "127.0.0.1" in r.json()["detail"]
    # 格式校验
    r = await client.put("/api/v1/auth/ip-whitelist", headers=h, json={"entries": [{"ip": "10.8.0"}]})
    assert r.status_code == 400 and "格式" in r.json()["detail"]

    # 正常保存：规范化写法、去重、备注与登记人保留
    r = await client.put("/api/v1/auth/ip-whitelist", headers=h, json={
        "enabled": True,
        "entries": [{"ip": "127.0.0.1", "note": "  管理员  本机 "}, {"ip": "10.8.0.5/24"}, {"ip": "127.0.0.1", "note": "重复"}],
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["enabled"] is True
    assert [(e["ip"], e["note"], e["by"]) for e in data["entries"]] == [("127.0.0.1", "管理员 本机", "admin"), ("10.8.0.0/24", "", "admin")]
    assert all(e["at"] for e in data["entries"])
    settings = (await client.get("/api/v1/auth/settings", headers=h)).json()
    assert settings["ip_whitelist_enabled"] is True

    # 只传开关不传名单：名单不变
    r = await client.put("/api/v1/auth/ip-whitelist", headers=h, json={"enabled": False})
    assert r.json()["enabled"] is False and len(r.json()["entries"]) == 2


async def test_白名单开启后_名单外IP登录被拒并记日志_网段命中放行_关闭后恢复(client, auth_on):
    h = await _admin_headers(client)
    r = await client.put("/api/v1/auth/ip-whitelist", headers=h, json={
        "enabled": True, "entries": [{"ip": "127.0.0.1", "note": "本机"}, {"ip": "10.8.0.0/24", "note": "办公网"}],
    })
    assert r.status_code == 200, r.text

    # 名单内：直连与网段命中都能登录；最后登录 IP 记录的是真实来源
    assert (await _login(client)).status_code == 200
    ok = await _login(client, ip="10.8.0.77")
    assert ok.status_code == 200 and ok.json()["user"]["last_login_ip"] == "10.8.0.77"

    # 名单外：口令正确也拒绝，403 且提示加白；口令错误同样 403（不暴露口令对错）
    denied = await _login(client, ip="203.0.113.9")
    assert denied.status_code == 403 and "203.0.113.9" in denied.json()["detail"] and "白名单" in denied.json()["detail"]
    assert (await _login(client, password="wrong", ip="203.0.113.9")).status_code == 403
    # 网段外一位也不行
    assert (await _login(client, ip="10.8.1.1")).status_code == 403

    # 安全日志：登录被拒带 IP；登录成功也带 IP
    logs = (await client.get("/api/v1/audit?kind=auth&days=0&page_size=100", headers=h)).json()["items"]
    denied_logs = [x for x in logs if x["action"] == "登录被拒（IP 未加白）"]
    assert {x["ip"] for x in denied_logs} == {"203.0.113.9", "10.8.1.1"}
    assert all(x["user"] == "admin" and x["status"] == 403 and x["security"] for x in denied_logs)
    assert any(x["action"] == "登录成功" and x["ip"] == "10.8.0.77" for x in logs)

    # 被拒不计入失败锁定：名单内接着登录正常
    for _ in range(6):
        await _login(client, ip="203.0.113.9")
    assert (await _login(client, ip="10.8.0.77")).status_code == 200

    # 关闭后名单外也能登录
    r = await client.put("/api/v1/auth/ip-whitelist", headers=h, json={"enabled": False})
    assert r.status_code == 200
    assert (await _login(client, ip="203.0.113.9")).status_code == 200


def test_ip规则解析与匹配():
    from app.auth import AuthError
    from app.auth.store import ip_matches, parse_ip_rule

    assert parse_ip_rule(" 10.1.2.3 ") == "10.1.2.3"
    assert parse_ip_rule("10.1.2.3/24") == "10.1.2.0/24"
    assert parse_ip_rule("2001:db8::1") == "2001:db8::1"
    for bad in ("", "abc", "10.1.2", "10.1.2.3/40"):
        with pytest.raises(AuthError):
            parse_ip_rule(bad)
    assert ip_matches("10.1.2.3", "10.1.2.3") and ip_matches("10.1.2.200", "10.1.2.0/24")
    assert not ip_matches("10.1.3.1", "10.1.2.0/24") and not ip_matches("", "10.1.2.3") and not ip_matches("testclient", "10.1.2.3")

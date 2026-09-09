"""项目知识库·依赖关系：需求功能依赖 / 用例依赖链路，项目级隔离、成环拦截、链路分层、AI 识别草稿→确认。"""

import json

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


async def _req(client, project, title, headers=None) -> str:
    resp = await client.post("/api/v1/requirements", headers=headers,
                             data={"project": project, "title": title, "text": f"{title}的需求原文"})
    assert resp.status_code == 200, resp.text
    return resp.json()["req_id"]


async def _cases_task(client, project, *cases, headers=None) -> dict[str, str]:
    """建任务并全部通过，返回 case_id -> uid。"""
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(*cases), review_reply(True)])
    resp = await client.post("/api/v1/tasks", headers=headers, data={"text": "登录需求", "project": project})
    assert resp.status_code == 200, resp.text
    tid = resp.json()["task_id"]
    await client.post(f"/api/v1/tasks/{tid}/review", headers=headers,
                      json={"items": [{"case_id": c["case_id"], "action": "approve"} for c in cases]})
    task = (await client.get(f"/api/v1/tasks/{tid}", headers=headers)).json()
    return {c["case_id"]: c["uid"] for c in task["result"]["cases"]}


def _edge(kind, src, dst, relation, note=""):
    return {"kind": kind, "from": src, "to": dst, "relation": relation, "note": note}


async def test_需求功能依赖_人工维护_分层与链路_成环拦截(client):
    await client.post("/api/v1/projects", json={"name": "P"})
    reg = await _req(client, "P", "注册")
    login = await _req(client, "P", "登录")
    bet = await _req(client, "P", "下注")
    settle = await _req(client, "P", "结算")
    for src, dst in ((login, reg), (bet, login), (settle, bet)):
        assert (await client.post("/api/v1/projects/P/dependencies/edges", json=_edge("requirement", src, dst, "depends"))).status_code == 200
    # 关联关系不参与链路
    assert (await client.post("/api/v1/projects/P/dependencies/edges", json=_edge("requirement", settle, reg, "related"))).status_code == 200
    g = (await client.get("/api/v1/projects/P/dependencies", params={"kind": "requirement"})).json()
    assert set(g["nodes"]) == {reg, login, bet, settle} and len(g["edges"]) == 4
    assert g["levels"] == {reg: 0, login: 1, bet: 2, settle: 3}
    assert g["chains"] == [[reg, login, bet, settle]] and g["cycles"] == []
    assert g["relations"]["depends"] == "功能依赖（前置）" and g["can_manage"] is True
    # 成环 / 自依赖 / 重复 / 未知关系 被拒
    r = await client.post("/api/v1/projects/P/dependencies/edges", json=_edge("requirement", reg, settle, "depends"))
    assert r.status_code == 400 and "循环" in r.json()["detail"]
    assert (await client.post("/api/v1/projects/P/dependencies/edges", json=_edge("requirement", reg, reg, "depends"))).status_code == 400
    assert (await client.post("/api/v1/projects/P/dependencies/edges", json=_edge("requirement", login, reg, "depends"))).status_code == 400
    assert (await client.post("/api/v1/projects/P/dependencies/edges", json=_edge("requirement", login, reg, "blocks"))).status_code == 400
    # 删除后链路缩短
    eid = next(e["edge_id"] for e in g["edges"] if e["from"] == settle and e["relation"] == "depends")
    assert (await client.delete(f"/api/v1/projects/P/dependencies/edges/{eid}", params={"kind": "requirement"})).status_code == 200
    g = (await client.get("/api/v1/projects/P/dependencies", params={"kind": "requirement"})).json()
    assert g["chains"] == [[reg, login, bet]] and settle not in g["levels"]
    logs = (await client.get("/api/v1/audit?days=0")).json()["items"]
    assert {"新增依赖关系", "删除依赖关系"} <= {x["action"] for x in logs if x["target"] == "P"}


async def test_项目级隔离_跨项目节点拒绝_非成员不可见(client, auth_on):
    admin = await _login(client)
    await client.post("/api/v1/auth/users", headers=admin, json={"username": "t1", "password": "pass123"})
    await client.post("/api/v1/auth/users", headers=admin, json={"username": "v1", "password": "pass123"})
    for p in ("A", "B"):
        await client.post("/api/v1/projects", headers=admin, json={"name": p})
    await client.put("/api/v1/projects/A/members", headers=admin, json={"username": "t1", "role": "test_lead"})
    await client.put("/api/v1/projects/A/members", headers=admin, json={"username": "v1", "role": "viewer"})
    a1 = await _req(client, "A", "A 登录", headers=admin)
    a2 = await _req(client, "A", "A 下注", headers=admin)
    b1 = await _req(client, "B", "B 登录", headers=admin)
    t1 = await _login(client, "t1", "pass123")
    v1 = await _login(client, "v1", "pass123")
    # 跨项目节点：即使管理员也不能把 B 的需求挂进 A 的图
    r = await client.post("/api/v1/projects/A/dependencies/edges", headers=admin, json=_edge("requirement", a2, b1, "depends"))
    assert r.status_code == 400 and "不属于项目" in r.json()["detail"]
    assert (await client.post("/api/v1/projects/A/dependencies/edges", headers=t1, json=_edge("requirement", a2, a1, "depends"))).status_code == 200
    # 非成员 403；只读成员可看不可改
    assert (await client.get("/api/v1/projects/B/dependencies", headers=t1)).status_code == 403
    assert (await client.get("/api/v1/projects/A/dependencies", headers=v1)).status_code == 200
    assert (await client.get("/api/v1/projects/A/dependencies", headers=v1)).json()["can_manage"] is False
    assert (await client.post("/api/v1/projects/A/dependencies/edges", headers=v1, json=_edge("requirement", a1, a2, "related"))).status_code == 403
    # B 的图里没有 A 的任何边
    gb = (await client.get("/api/v1/projects/B/dependencies", headers=admin)).json()
    assert gb["edges"] == [] and set(gb["nodes"]) == {b1}


async def test_用例依赖链路_按模块圈定_AI识别草稿确认(client):
    await client.post("/api/v1/projects", json={"name": "P"})
    uids = await _cases_task(
        client, "P",
        make_case(case_id="TC-登录-001", title="验证注册新账号成功", module="登录"),
        make_case(case_id="TC-登录-002", title="验证正确账号密码登录成功", module="登录", precondition="已注册账号"),
        make_case(case_id="TC-下注-001", title="验证登录后可下注", module="下注", precondition="已登录"),
    )
    reg, login, bet = uids["TC-登录-001"], uids["TC-登录-002"], uids["TC-下注-001"]
    # 用例识别必须圈定范围
    assert (await client.post("/api/v1/projects/P/dependencies/infer", json={"kind": "case"})).status_code == 400
    app.state.llm = StubLLM([json.dumps({"edges": [
        {"from": login, "to": reg, "relation": "precondition", "reason": "前置条件：已注册账号"},
        {"from": login, "to": "nope", "relation": "precondition", "reason": "编造"},
        {"from": reg, "to": login, "relation": "blocks", "reason": "非法关系"},
    ]}, ensure_ascii=False)])
    r = (await client.post("/api/v1/projects/P/dependencies/infer", json={"kind": "case", "module": "登录"})).json()
    assert len(r["proposed"]) == 1 and len(r["skipped"]) == 2 and r["nodes"] == 2
    sent = app.state.llm.calls[0]["messages"][1]["content"]
    assert "TC-登录-001" in sent and "TC-下注-001" not in sent   # 只送模块内用例
    edge = r["proposed"][0]
    assert edge["status"] == "proposed" and edge["source"] == "ai" and "已注册账号" in edge["reason"]
    # 草稿不参与链路；确认后参与
    g = (await client.get("/api/v1/projects/P/dependencies", params={"kind": "case"})).json()
    assert g["chains"] == [] and g["edges"][0]["status"] == "proposed"
    assert (await client.post(f"/api/v1/projects/P/dependencies/edges/{edge['edge_id']}/confirm", params={"kind": "case"})).status_code == 200
    await client.post("/api/v1/projects/P/dependencies/edges", json=_edge("case", bet, login, "precondition", "先登录"))
    g = (await client.get("/api/v1/projects/P/dependencies", params={"kind": "case"})).json()
    assert g["chains"] == [[reg, login, bet]] and g["levels"][bet] == 2
    assert g["nodes"][bet]["case_id"] == "TC-下注-001" and g["nodes"][bet]["module"] == "下注"
    # 按模块过滤：只保留与该模块相关的节点与边
    g = (await client.get("/api/v1/projects/P/dependencies", params={"kind": "case", "module": "下注"})).json()
    assert set(g["nodes"]) == {bet, login} and len(g["edges"]) == 1
    # 调用日志归属项目
    from app.db import wait_persist
    wait_persist()
    calls = (await client.get("/api/v1/ai/calls", params={"project": "P"})).json()["items"]
    assert any(c["purpose"] == "依赖关系识别" and c["project"] == "P" for c in calls)
    prompts = {p["key"] for p in (await client.get("/api/v1/ai/prompts")).json()["prompts"]}
    assert "dependency_infer" in prompts


async def test_需求依赖启发式建议(client):
    from tests.test_requirements import ANALYSIS_REPLY

    await client.post("/api/v1/projects", json={"name": "P"})
    sms = await _req(client, "P", "短信网关")
    login = await _req(client, "P", "登录")
    app.state.llm = StubLLM([ANALYSIS_REPLY])   # dependencies: ["短信网关"]
    await client.post(f"/api/v1/requirements/{login}/analyze", json={})
    g = (await client.get("/api/v1/projects/P/dependencies", params={"kind": "requirement"})).json()
    assert g["hints"] == [{"from": login, "to": sms, "relation": "depends", "reason": "需求分析·外部依赖：短信网关"}]
    await client.post("/api/v1/projects/P/dependencies/edges", json=_edge("requirement", login, sms, "depends"))
    g = (await client.get("/api/v1/projects/P/dependencies", params={"kind": "requirement"})).json()
    assert g["hints"] == []   # 已建边不再提示


async def test_自动识别_逐模块_输入含原文摘要与模块(client):
    await client.post("/api/v1/projects", json={"name": "P"})
    r1 = await _req(client, "P", "30086115.pdf")   # 迁移来的需求：标题只是文件名
    r2 = await _req(client, "P", "登录")
    await _cases_task(client, "P",
                      make_case(case_id="TC-登录-001", title="验证注册新账号成功", module="登录"),
                      make_case(case_id="TC-登录-002", title="验证正确账号密码登录成功", module="登录", precondition="已注册账号"),
                      make_case(case_id="TC-下注-001", title="验证登录后可下注", module="下注", precondition="已登录"),
                      make_case(case_id="TC-下注-002", title="验证余额不足不能下注", module="下注", precondition="已登录"))
    # 需求图：未自动跑过 → auto_done=False；识别输入含原文摘要
    g = (await client.get("/api/v1/projects/P/dependencies", params={"kind": "requirement"})).json()
    assert g["auto_done"] is False
    app.state.llm = StubLLM([json.dumps({"edges": [{"from": r2, "to": r1, "relation": "depends", "reason": "原文"}]}, ensure_ascii=False)])
    r = (await client.post("/api/v1/projects/P/dependencies/infer", json={"kind": "requirement"})).json()
    sent = app.state.llm.calls[0]["messages"][1]["content"]
    assert "原文摘要：30086115.pdf的需求原文" in sent and len(r["proposed"]) == 1
    g = (await client.get("/api/v1/projects/P/dependencies", params={"kind": "requirement"})).json()
    assert g["auto_done"] is True and g["display_levels"][r2] == 1 and g["levels"] == {}   # 草稿参与布局、不参与链路
    # 用例图「全部模块」：两个模块都未跑过 → 逐模块识别，各一次调用；第一步进入输入
    g = (await client.get("/api/v1/projects/P/dependencies", params={"kind": "case"})).json()
    assert g["auto_done"] is False and g["pending_modules"] == ["下注", "登录"] and g["modules"] == ["下注", "登录"]
    app.state.llm = StubLLM([json.dumps({"edges": []}), json.dumps({"edges": []})])
    r = (await client.post("/api/v1/projects/P/dependencies/infer", json={"kind": "case", "all_modules": True})).json()
    assert r["modules"] == ["下注", "登录"] and r["remaining"] == 0 and len(app.state.llm.calls) == 2
    assert "第一步：输入正确账号密码并提交" in app.state.llm.calls[1]["messages"][1]["content"]
    g = (await client.get("/api/v1/projects/P/dependencies", params={"kind": "case"})).json()
    assert g["auto_done"] is True and g["pending_modules"] == []
    # 再次 all_modules 不重复调用；force 才重跑
    app.state.llm = StubLLM([])
    r = (await client.post("/api/v1/projects/P/dependencies/infer", json={"kind": "case", "all_modules": True})).json()
    assert r["modules"] == []
    app.state.llm = StubLLM([json.dumps({"edges": []}), json.dumps({"edges": []})])
    r = (await client.post("/api/v1/projects/P/dependencies/infer", json={"kind": "case", "all_modules": True, "force": True})).json()
    assert r["modules"] == ["下注", "登录"]


async def test_旧任务字符串测试点不致图接口报错(client):
    await client.post("/api/v1/projects", json={"name": "P"})
    r1 = await _req(client, "P", "老需求")
    await _cases_task(client, "P", make_case())
    # 模拟项目化之前的任务：analysis.test_points 的 points 是纯字符串，且关联到需求
    tid = app.state.tasks.list(limit=10, project="P")[0].task_id
    rec = app.state.tasks.get(tid)
    rec.analysis = {"test_points": [{"module": "登录", "points": ["正常登录", "密码错误"]}, "坏数据"]}
    app.state.tasks.save(rec)
    req = app.state.requirements.get(r1); req["tasks"] = [tid]; app.state.requirements._persist(req)
    g = (await client.get("/api/v1/projects/P/dependencies", params={"kind": "requirement"})).json()
    assert g["nodes"][r1]["modules"] == ["登录"]
    app.state.llm = StubLLM([json.dumps({"edges": []})])
    await client.post("/api/v1/projects/P/dependencies/infer", json={"kind": "requirement"})
    assert "测试点：正常登录；密码错误" in app.state.llm.calls[0]["messages"][1]["content"]

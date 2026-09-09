"""M2 需求中心：需求实体与原文保护、附件解析失败显式记录、AI 分析 11 项、待确认事项确认流、
从需求发起测试设计与追溯链、存量任务迁移。"""

import json

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.main import app
from tests.stubs import ANALYST_REPLY, StubLLM, generator_reply, make_case, review_reply


@pytest.fixture
async def client():
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


ANALYSIS_REPLY = json.dumps({
    "features": ["账号密码登录"], "rules": ["密码错误 5 次锁定 30 分钟"], "preconditions": ["已注册账号"],
    "normal_flows": ["输入账号密码→登录成功"], "exception_flows": ["密码错误提示"], "boundaries": ["5 次"],
    "state_changes": ["正常→锁定"], "permissions": [], "dependencies": ["短信网关"], "risks": ["锁定误伤"],
    "open_questions": ["锁定期间是否允许找回密码？", "锁定次数是否按账号还是按设备统计？"],
}, ensure_ascii=False)


async def _req(client, project="P", title="登录需求", text="用户输入账号密码登录；密码错误 5 次锁定 30 分钟。") -> dict:
    await client.post("/api/v1/projects", json={"name": project})
    resp = await client.post("/api/v1/requirements", data={"project": project, "title": title, "text": text})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_需求实体_原文保护_人工补充(client):
    r = await _req(client)
    assert (r["status"], r["source_type"], r["task_count"]) == ("draft", "manual", 0)
    assert (await client.post("/api/v1/requirements", data={"project": "P", "title": " ", "text": "x"})).status_code == 400
    assert (await client.post("/api/v1/requirements", data={"project": "P", "title": "空", "text": ""})).status_code == 400
    # 人工补充独立于原文；原文接口不可改
    upd = (await client.put(f"/api/v1/requirements/{r['req_id']}", json={"description": "补充：仅 Web 端"})).json()
    assert upd["description"] == "补充：仅 Web 端" and upd["raw_text"] == r["raw_text"]
    listed = (await client.get("/api/v1/requirements", params={"project": "P"})).json()
    assert [x["title"] for x in listed["requirements"]] == ["登录需求"] and "raw_text" not in listed["requirements"][0]
    assert (await client.get("/api/v1/requirements", params={"project": "P", "keyword": "锁定"})).json()["requirements"]
    # 逻辑删除与恢复
    await client.delete(f"/api/v1/requirements/{r['req_id']}")
    assert (await client.get(f"/api/v1/requirements/{r['req_id']}")).status_code == 404
    assert (await client.get("/api/v1/requirements", params={"project": "P"})).json()["requirements"] == []
    assert (await client.post(f"/api/v1/requirements/{r['req_id']}/restore")).status_code == 200


async def _wait_parsed(client, req_id: str) -> dict:
    import asyncio
    for _ in range(200):
        r = (await client.get(f"/api/v1/requirements/{req_id}")).json()
        if not r["parsing"]:
            return r
        await asyncio.sleep(0.02)
    raise AssertionError("附件解析未在预期时间内完成")


async def test_附件解析失败显式记录并可重新解析(client):
    await client.post("/api/v1/projects", json={"name": "P"})
    resp = await client.post("/api/v1/requirements", data={"project": "P", "title": "带附件"},
                             files=[("files", ("需求.txt", "登录需求原文".encode(), "text/plain")),
                                    ("files", ("坏文件.xyz", b"???", "application/octet-stream"))])
    assert resp.status_code == 200, resp.text
    r = resp.json()
    # 上传立即返回：附件处于「解析中」，解析在后台进行；解析中不能发起 AI 分析 / 测试设计
    assert r["source_type"] == "file" and r["parsing"] == 2 and r["parse_failed"] == 0
    assert all(a["parsing"] for a in r["attachments"])
    r = await _wait_parsed(client, r["req_id"])
    assert r["parsing"] == 0 and r["parse_failed"] == 1
    atts = {a["filename"]: a for a in r["attachments"]}
    # 解析中不能发起 AI 分析 / 测试设计（把一个附件置回解析中模拟）
    app.state.requirements.replace_attachment(r["req_id"], atts["需求.txt"]["att_id"], {"parsing": True})
    resp = await client.post(f"/api/v1/requirements/{r['req_id']}/analyze", json={})
    assert resp.status_code == 409 and "解析中" in resp.json()["detail"]
    assert (await client.post(f"/api/v1/requirements/{r['req_id']}/design", json={})).status_code == 409
    app.state.requirements.replace_attachment(r["req_id"], atts["需求.txt"]["att_id"], {"parsing": False})
    assert atts["需求.txt"]["parsed"] and atts["需求.txt"]["chars"] > 0
    assert not atts["坏文件.xyz"]["parsed"] and atts["坏文件.xyz"]["error"]
    # 重新解析（后台）仍失败：原因保留；详情带解析预览
    resp = await client.post(f"/api/v1/requirements/{r['req_id']}/attachments/{atts['坏文件.xyz']['att_id']}/reparse")
    assert resp.status_code == 200 and resp.json()["parsing"] == 1
    r = await _wait_parsed(client, r["req_id"])
    assert r["parse_failed"] == 1 and r["parsing"] == 0
    detail = (await client.get(f"/api/v1/requirements/{r['req_id']}")).json()
    assert next(a for a in detail["attachments"] if a["filename"] == "需求.txt")["preview"] == "登录需求原文"


async def test_AI分析_待确认事项_确认后才能测试设计_追溯(client):
    r = await _req(client)
    rid = r["req_id"]
    app.state.llm = StubLLM([ANALYSIS_REPLY])
    a = (await client.post(f"/api/v1/requirements/{rid}/analyze", json={})).json()
    assert a["status"] == "pending_confirm" and a["open_questions"] == 2
    assert a["analysis"]["rules"] == ["密码错误 5 次锁定 30 分钟"] and set(a["analysis_labels"]) >= {"features", "open_questions"}
    assert a["raw_text"] == r["raw_text"]  # AI 结果不覆盖原文
    # 未确认不得发起测试设计（核心规则 4）
    assert (await client.post(f"/api/v1/requirements/{rid}/design", json={})).status_code == 409
    q1, q2 = [q["q_id"] for q in a["questions"]]
    assert (await client.post(f"/api/v1/requirements/{rid}/questions/{q1}", json={"answer": ""})).status_code == 400
    await client.post(f"/api/v1/requirements/{rid}/questions/{q1}", json={"answer": "允许找回密码"})
    # 人工补充一条待确认事项后仍待确认
    a = (await client.post(f"/api/v1/requirements/{rid}/questions", json={"question": "是否支持记住登录？"})).json()
    assert a["status"] == "pending_confirm" and a["open_questions"] == 2
    q3 = next(q["q_id"] for q in a["questions"] if q["source"] == "manual")
    await client.post(f"/api/v1/requirements/{rid}/questions/{q2}", json={"answer": "按账号"})
    a = (await client.post(f"/api/v1/requirements/{rid}/questions/{q3}", json={"answer": "支持 7 天"})).json()
    assert a["status"] == "confirmed" and a["open_questions"] == 0
    # 再次 AI 分析：已确认的问题保留结论，新问题追加
    app.state.llm = StubLLM([ANALYSIS_REPLY.replace("按设备统计？", "按设备统计？？")])
    a = (await client.post(f"/api/v1/requirements/{rid}/analyze", json={})).json()
    kept = next(q for q in a["questions"] if q["q_id"] == q1)
    assert kept["status"] == "confirmed" and a["open_questions"] == 1
    await client.post(f"/api/v1/requirements/{rid}/questions/{next(q['q_id'] for q in a['questions'] if q['status']=='open')}",
                      json={"answer": "按设备"})

    # 发起测试设计（同步、直接生成）：任务上下文回挂需求，生成文本含确认结论
    case = make_case(point_ids=["TP001"])
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(case), review_reply(True)])
    resp = await client.post(f"/api/v1/requirements/{rid}/design",
                             json={"confirm_points": False, "async_mode": False})
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["task_id"]
    sent = app.state.llm.calls[0]["messages"][-1]["content"]
    assert "允许找回密码" in sent and "仅 Web 端" not in sent
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert (task["context"]["requirement_id"], task["context"]["requirement_title"]) == (rid, "登录需求")
    listed = (await client.get("/api/v1/tasks", params={"project": "P"})).json()["tasks"]
    assert listed[0]["requirement_id"] == rid
    # 追溯：需求 → 任务 → 用例（含来源测试点）→ 覆盖识别
    detail = (await client.get(f"/api/v1/requirements/{rid}")).json()
    assert detail["status"] == "designing" and detail["tasks"] == [task_id]
    t = detail["trace"]
    assert t["totals"]["cases"] == 1 and t["coverage"] == {"has_points": True, "has_cases": True, "in_plan": False, "executed": False}
    rows = (await client.get("/api/v1/cases", params={"project": "P"})).json()["cases"]
    assert rows[0]["requirement_id"] == rid and rows[0]["point_ids"] == ["TP001"]
    # 用例通过并入计划后覆盖识别更新
    await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [{"case_id": case["case_id"], "action": "accept"}]})
    plan = (await client.post("/api/v1/plans", json={"name": "冒烟", "project": "P"})).json()
    await client.post(f"/api/v1/plans/{plan['plan_id']}/cases", json={"task_id": task_id})
    t = (await client.get(f"/api/v1/requirements/{rid}")).json()["trace"]
    assert t["coverage"]["in_plan"] and t["totals"]["cases_approved"] == 1


async def test_存量任务迁移为需求(client):
    from app.requirements import migrate_tasks

    await client.post("/api/v1/projects", json={"name": "P"})
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(make_case()), review_reply(True)])
    task_id = (await client.post("/api/v1/tasks", files={"files": ("玩法.txt", b"# rule", "text/plain")},
                                 data={"project": "P"})).json()["task_id"]
    record = app.state.tasks.get(task_id)
    record.context.pop("requirement_id", None)  # 模拟项目化之前的任务
    app.state.tasks.save(record)
    assert migrate_tasks(app.state.tasks, app.state.requirements) == 1
    assert migrate_tasks(app.state.tasks, app.state.requirements) == 0  # 幂等
    reqs = (await client.get("/api/v1/requirements", params={"project": "P"})).json()["requirements"]
    assert len(reqs) == 1 and reqs[0]["title"] == "玩法.txt" and reqs[0]["source_type"] == "migrated"
    assert reqs[0]["status"] == "designing" and reqs[0]["task_count"] == 1
    assert app.state.tasks.get(task_id).context["requirement_id"] == reqs[0]["req_id"]

"""验收周：完整需求第 23 章「核心业务规则」25 条逐条验收（可执行验收清单，与 docs/验收清单_V1.0.md 对应）。

每个测试对应一条规则，命名 test_R{序号}_规则摘要；全部走 HTTP 接口（LLM 用桩），与线上行为一致。
"""

import json

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.config import get_settings
from app.main import app
from tests.stubs import ANALYST_REPLY, StubLLM, generator_reply, make_case, review_reply
from tests.test_quality_loop import ANALYST_REPLY_V2, GAP_REPLY


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


async def _user(client, admin, username, role="member") -> dict:
    resp = await client.post("/api/v1/auth/users", headers=admin,
                             json={"username": username, "password": "pass123", "role": role})
    assert resp.status_code == 200, resp.text
    return await _login(client, username, "pass123")


async def _project(client, name, members: dict | None = None, headers=None) -> None:
    resp = await client.post("/api/v1/projects", headers=headers, json={"name": name})
    assert resp.status_code == 200, resp.text
    for username, role in (members or {}).items():
        resp = await client.put(f"/api/v1/projects/{name}/members", headers=headers,
                                json={"username": username, "role": role})
        assert resp.status_code == 200, resp.text


async def _ai_task(client, *cases, project="P", headers=None) -> str:
    """AI 生成任务（拆解→生成→评审桩），返回 task_id。"""
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(*cases), review_reply(True)])
    resp = await client.post("/api/v1/tasks", headers=headers, data={"text": "登录需求", "project": project})
    assert resp.status_code == 200, resp.text
    return resp.json()["task_id"]


async def _task(client, task_id) -> dict:
    return (await client.get(f"/api/v1/tasks/{task_id}")).json()


async def _review(client, task_id, *items, expect=200):
    resp = await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": list(items)})
    assert resp.status_code == expect, resp.text
    return resp


async def _versions(client, task_id, uid) -> list[dict]:
    resp = await client.get(f"/api/v1/tasks/{task_id}/versions", params={"kind": "case", "entity_id": uid})
    return resp.json()["versions"]


def _fix_reply(*cases, comment_type="预期不可验证"):
    return json.dumps({"fixes": [{"case_id": c["case_id"], "comment_type": comment_type, "note": "修"} for c in cases],
                       "cases": list(cases), "deleted": []}, ensure_ascii=False)


# ---- 规则 1~3：项目绑定、多项目角色、需求原文 ----


async def test_R01_所有业务数据必须绑定项目(client, auth_on):
    admin = await _login(client)
    await client.post("/api/v1/auth/users", headers=admin, json={"username": "t1", "password": "pass123"})
    await _project(client, "A", {"t1": "tester"}, headers=admin)
    t1 = await _login(client, "t1", "pass123")
    # 普通用户建任务不选项目被拒；需求 / 计划 / 人工用例集都必须落在存在的项目上
    resp = await client.post("/api/v1/tasks", headers=t1, data={"text": "登录需求"})
    assert resp.status_code == 400 and "项目" in resp.json()["detail"]
    assert (await client.post("/api/v1/requirements", headers=t1, data={"title": "x", "text": "y"})).status_code in (400, 422)
    assert (await client.post("/api/v1/plans", headers=t1, json={"name": "冒烟"})).status_code == 422
    assert (await client.post("/api/v1/projects/不存在/manual-task", headers=admin, json={"title": "x"})).status_code == 404
    # 归属项目的数据在列表 / 统计里都带项目字段
    tid = await _ai_task(client, make_case(), project="A", headers=t1)
    listed = (await client.get("/api/v1/tasks", headers=t1, params={"project": "A"})).json()["tasks"]
    assert [t["task_id"] for t in listed] == [tid] and listed[0]["project"] == "A"


async def test_R02_同一用户在不同项目可拥有不同角色(client, auth_on):
    admin = await _login(client)
    lead = await _user(client, admin, "lead")
    await _project(client, "A", {"lead": "test_lead"}, headers=admin)
    await _project(client, "B", {"lead": "viewer"}, headers=admin)
    mine = (await client.get("/api/v1/projects", headers=lead)).json()["projects"]
    assert {p["project"]: p["my_role"] for p in mine} == {"A": "test_lead", "B": "viewer"}
    # A 可写、B 只读
    assert (await client.post("/api/v1/projects/A/manual-task", headers=lead, json={"title": "手工"})).status_code == 200
    assert (await client.post("/api/v1/projects/B/manual-task", headers=lead, json={"title": "手工"})).status_code == 403
    assert (await client.get("/api/v1/tasks", headers=lead, params={"project": "B"})).status_code == 200


async def test_R03_需求原文永久保留_AI结果不覆盖(client):
    from tests.test_requirements import ANALYSIS_REPLY

    await _project(client, "P")
    raw = "用户输入账号密码登录；密码错误 5 次锁定 30 分钟。"
    r = (await client.post("/api/v1/requirements", data={"project": "P", "title": "登录", "text": raw})).json()
    app.state.llm = StubLLM([ANALYSIS_REPLY])
    a = (await client.post(f"/api/v1/requirements/{r['req_id']}/analyze", json={})).json()
    assert a["analysis"]["rules"] and a["raw_text"] == raw
    upd = (await client.put(f"/api/v1/requirements/{r['req_id']}", json={"description": "补充说明", "raw_text": "篡改"})).json()
    assert upd["raw_text"] == raw and upd["description"] == "补充说明"
    # 逻辑删除后原文仍在，可恢复
    await client.delete(f"/api/v1/requirements/{r['req_id']}")
    await client.post(f"/api/v1/requirements/{r['req_id']}/restore")
    assert (await client.get(f"/api/v1/requirements/{r['req_id']}")).json()["raw_text"] == raw


# ---- 规则 4~7：AI 产物默认草稿、待确认项、只有通过的才进入下一步 ----


async def test_R04_AI遇到未知规则必须输出待确认项_不得编造(client):
    from tests.test_requirements import ANALYSIS_REPLY

    await _project(client, "P")
    r = (await client.post("/api/v1/requirements", data={"project": "P", "title": "登录", "text": "登录需求"})).json()
    app.state.llm = StubLLM([ANALYSIS_REPLY])
    a = (await client.post(f"/api/v1/requirements/{r['req_id']}/analyze", json={})).json()
    assert a["status"] == "pending_confirm" and a["open_questions"] == 2
    # 待确认项未回答不能发起测试设计；回答不能为空
    assert (await client.post(f"/api/v1/requirements/{r['req_id']}/design", json={})).status_code == 409
    q1 = a["questions"][0]["q_id"]
    assert (await client.post(f"/api/v1/requirements/{r['req_id']}/questions/{q1}", json={"answer": " "})).status_code == 400
    # 分析 Prompt 明确要求「不确定的写入待确认项」
    from app.prompts import prompt_text
    assert "待确认" in prompt_text("requirement_analysis") or "open_questions" in prompt_text("requirement_analysis")


async def test_R05_R06_测试点默认草稿_只有通过的点才能生成用例(client):
    app.state.llm = StubLLM([ANALYST_REPLY_V2, GAP_REPLY])
    resp = await client.post("/api/v1/tasks", data={"text": "登录需求", "confirm_points": "true"})
    data = resp.json()
    task_id = data["task_id"]
    points = data["test_points"][0]["points"]
    assert data["status"] == "awaiting_confirmation"
    assert all(p["status"] == "pending" and not p["locked"] for p in points)  # 规则 5：默认草稿（待审）
    await client.post(f"/api/v1/tasks/{task_id}/points/review", json={"items": [
        {"tp_id": "TP001", "action": "approve"},
        {"tp_id": "TP002", "action": "reject", "comment": "过大", "reject_types": ["颗粒度过粗"]},
    ]})
    app.state.llm = StubLLM([generator_reply(make_case()), review_reply(True)])
    assert (await client.post(f"/api/v1/tasks/{task_id}/confirm", json={})).status_code == 200
    gen_prompt = app.state.llm.calls[0]["messages"][1]["content"]
    assert "正确账号密码登录成功" in gen_prompt and "连续错误5次账号锁定" not in gen_prompt  # 规则 6


async def test_R07_AI用例默认草稿_评审通过后才能加入计划(client):
    await _project(client, "P")
    task_id = await _ai_task(client, make_case(), make_case(case_id="TC-登录-002", title="验证密码错误提示"))
    task = await _task(client, task_id)
    assert {s["status"] for s in task["case_reviews"].values()} == {"pending"}
    plan = (await client.post("/api/v1/plans", json={"name": "冒烟", "project": "P"})).json()
    r = (await client.post(f"/api/v1/plans/{plan['plan_id']}/cases", json={"task_id": task_id})).json()
    assert r["added"] == 0
    await _review(client, task_id, {"case_id": "TC-登录-001", "action": "approve"})
    r = (await client.post(f"/api/v1/plans/{plan['plan_id']}/cases", json={"task_id": task_id})).json()
    assert r["added"] == 1


# ---- 规则 8~14：驳回、AI 定向修改、锁定、版本与留痕 ----


async def test_R08_驳回必须填写原因_支持明确修改要求(client):
    task_id = await _ai_task(client, make_case())
    resp = await _review(client, task_id, {"case_id": "TC-登录-001", "action": "reject"}, expect=400)
    assert "驳回原因" in resp.json()["detail"]
    await _review(client, task_id, {"case_id": "TC-登录-001", "action": "reject", "comment": "预期空泛",
                                    "reject_types": ["预期不可验证"], "fix_request": "写明提示文案"})
    state = list((await _task(client, task_id))["case_reviews"].values())[0]
    assert (state["status"], state["comment"], state["fix_request"]) == ("rejected", "预期空泛", "写明提示文案")
    # 测试点同样必须带原因
    app.state.llm = StubLLM([ANALYST_REPLY_V2, GAP_REPLY])
    d = (await client.post("/api/v1/tasks", data={"text": "登录需求", "confirm_points": "true"})).json()
    resp = await client.post(f"/api/v1/tasks/{d['task_id']}/points/review",
                             json={"items": [{"tp_id": "TP001", "action": "reject"}]})
    assert resp.status_code == 400


async def test_R09_用例驳回支持字段级与步骤级(client):
    case = make_case(steps=[{"action": "输入账号", "expected": "光标进入密码框"},
                            {"action": "输入密码提交", "expected": "登录成功"}])
    task_id = await _ai_task(client, case)
    await _review(client, task_id, {"case_id": "TC-登录-001", "action": "reject", "comment": "第 2 步预期空泛",
                                    "reject_types": ["预期不可验证"], "fields": ["steps"], "steps": [2]})
    task = await _task(client, task_id)
    state = list(task["case_reviews"].values())[0]
    assert state["fields"] == ["steps"] and state["steps"] == [2]
    assert task["review_log"][-1]["steps"] == [2]
    # 越界步骤 / 非法字段被拒
    await _review(client, task_id, {"case_id": "TC-登录-001", "action": "reject", "comment": "x",
                                    "reject_types": ["其他"], "steps": [9]}, expect=400)
    await _review(client, task_id, {"case_id": "TC-登录-001", "action": "reject", "comment": "x",
                                    "reject_types": ["其他"], "fields": ["不存在"]}, expect=400)


async def test_R10_R11_AI只改被驳回内容_已通过内容锁定不被AI改动(client):
    ok = make_case()
    bad = make_case(case_id="TC-登录-002", title="验证密码错误提示")
    task_id = await _ai_task(client, ok, bad)
    await _review(client, task_id, {"case_id": "TC-登录-001", "action": "approve"},
                  {"case_id": "TC-登录-002", "action": "reject", "comment": "预期空泛", "reject_types": ["预期不可验证"]})
    task = await _task(client, task_id)
    locked = next(s for s in task["case_reviews"].values() if s["status"] == "approved")
    assert locked["locked"] is True  # 规则 11：通过即锁定
    fixed = make_case(case_id="TC-登录-002", title="验证密码错误提示",
                      steps=[{"action": "输入错误密码提交", "expected": "提示「密码错误，还可尝试4次」"}])
    app.state.llm = StubLLM([_fix_reply(fixed)])
    assert (await client.post(f"/api/v1/tasks/{task_id}/cases/fix")).status_code == 200
    sent = app.state.llm.calls[0]["messages"][1]["content"]
    assert "TC-登录-002" in sent and "TC-登录-001" not in sent  # 规则 10：只发送被驳回项
    await client.post(f"/api/v1/tasks/{task_id}/fix/confirm", json={"accept_all": True})
    task = await _task(client, task_id)
    by_id = {c["case_id"]: c for c in task["result"]["cases"]}
    locked_uid = by_id["TC-登录-001"]["uid"]
    assert by_id["TC-登录-001"]["title"] == ok["title"] and by_id["TC-登录-001"]["version"] == 1
    assert "还可尝试4次" in by_id["TC-登录-002"]["steps"][0]["expected"]
    # 规则 11：整体修订也绕开锁定用例（内容、编号、位置都不变）
    app.state.llm = StubLLM([generator_reply(make_case(case_id="TC-登录-002", title="修订后")), review_reply(True)])
    await client.post(f"/api/v1/tasks/{task_id}/revise", json={"instruction": "全部改成更具体的标题"})
    task = await _task(client, task_id)
    kept = next(c for c in task["result"]["cases"] if c["uid"] == locked_uid)
    assert (kept["title"], kept["case_id"], kept["version"]) == (ok["title"], "TC-登录-001", 1)
    assert task["case_reviews"][locked_uid]["locked"] is True


async def test_R12_R13_AI修改不覆盖原数据_新版本带差异_人工确认后才回评审(client):
    task_id = await _ai_task(client, make_case())
    task = await _task(client, task_id)
    uid = task["result"]["cases"][0]["uid"]
    await _review(client, task_id, {"case_id": "TC-登录-001", "action": "reject", "comment": "预期空泛",
                                    "reject_types": ["预期不可验证"]})
    fixed = make_case(steps=[{"action": "输入正确账号密码并提交", "expected": "跳转首页并生成有效登录态"}])
    app.state.llm = StubLLM([_fix_reply(fixed)])
    pending = (await client.post(f"/api/v1/tasks/{task_id}/cases/fix")).json()["pending_fix"]
    assert pending["proposals"][0]["before"]["steps"][0]["expected"] == "跳转首页并展示用户昵称"
    assert "登录态" in pending["proposals"][0]["after"]["steps"][0]["expected"]
    # 规则 12：确认前原数据未动
    task = await _task(client, task_id)
    assert task["result"]["cases"][0]["steps"][0]["expected"] == "跳转首页并展示用户昵称"
    assert list(task["case_reviews"].values())[0]["status"] == "rejected"
    # 规则 13：人工确认后落地为新版本（ai_fix，带差异）并回到待评审
    await client.post(f"/api/v1/tasks/{task_id}/fix/confirm", json={"accept_all": True})
    task = await _task(client, task_id)
    assert task["result"]["cases"][0]["version"] == 2
    assert list(task["case_reviews"].values())[0]["status"] == "pending"
    versions = await _versions(client, task_id, uid)
    assert [v["source"] for v in versions] == ["ai_original", "ai_fix"]
    assert any(c["field"] == "steps" for c in versions[-1]["changes"])
    assert len(task["fix_log"]) == 1


async def test_R14_评审驳回修改重提通过全部留痕(client):
    await _project(client, "P")
    task_id = await _ai_task(client, make_case())
    await _review(client, task_id, {"case_id": "TC-登录-001", "action": "reject", "comment": "预期空泛",
                                    "reject_types": ["预期不可验证"]})
    app.state.llm = StubLLM([_fix_reply(make_case(title="修后标题"))])
    await client.post(f"/api/v1/tasks/{task_id}/cases/fix")
    await client.post(f"/api/v1/tasks/{task_id}/fix/confirm", json={"accept_all": True})
    await _review(client, task_id, {"case_id": "TC-登录-001", "action": "approve"})
    task = await _task(client, task_id)
    assert [e["action"] for e in task["review_log"]] == ["reject", "approve"]
    assert all(e["by"] and e["at"] for e in task["review_log"]) and len(task["fix_log"]) == 1
    logs = (await client.get("/api/v1/audit?days=0")).json()["items"]
    actions = {x["action"] for x in logs if x["target"] == task_id}
    assert "用例评审" in actions and any("修改" in a for a in actions)  # 评审与 AI 修改/确认均入操作日志


# ---- 规则 15~19：计划、快照、执行 ----


async def _approved_task_in_plan(client):
    await _project(client, "P")
    task_id = await _ai_task(client, make_case())
    await _review(client, task_id, {"case_id": "TC-登录-001", "action": "approve"})
    plan = (await client.post("/api/v1/plans", json={"name": "冒烟", "project": "P"})).json()
    await client.post(f"/api/v1/plans/{plan['plan_id']}/cases", json={"task_id": task_id})
    return task_id, plan["plan_id"]


async def test_R15_R16_计划只能使用已通过用例且加入即生成快照(client):
    task_id, pid = await _approved_task_in_plan(client)
    detail = (await client.get(f"/api/v1/plans/{pid}")).json()
    item = detail["items"][0]
    assert item["snapshot"]["title"] == "验证正确账号密码登录成功" and item["version_no"] >= 1
    # 候选列表只列已通过的
    draft_task = await _ai_task(client, make_case(title="验证尚未通过评审的用例"))
    cands = (await client.get(f"/api/v1/plans/{pid}/candidates", params={"task_id": draft_task})).json()
    assert all(c.get("review", "approved") == "approved" for c in cands.get("cases", []))
    r = (await client.post(f"/api/v1/plans/{pid}/cases", json={"task_id": draft_task})).json()
    assert r["added"] == 0


async def test_R17_正式用例修改不影响历史计划快照(client):
    task_id, pid = await _approved_task_in_plan(client)
    task = await _task(client, task_id)
    case = task["result"]["cases"][0]
    await _review(client, task_id, {"case_id": case["case_id"], "action": "unlock"})
    await _review(client, task_id, {"case_id": case["case_id"], "action": "modify", "base_version": case["version"],
                                    "case": {**case, "title": "改动后的标题"}})
    assert (await _task(client, task_id))["result"]["cases"][0]["title"] == "改动后的标题"
    detail = (await client.get(f"/api/v1/plans/{pid}")).json()
    assert detail["items"][0]["snapshot"]["title"] == "验证正确账号密码登录成功"


async def test_R18_每次执行独立保存_重测不覆盖历史(client):
    _, pid = await _approved_task_in_plan(client)
    run = (await client.post(f"/api/v1/plans/{pid}/runs", json={"name": "首轮"})).json()
    rid = run["run"]["run_id"] if "run" in run else run["run_id"]
    item_id = (await client.get(f"/api/v1/plans/{pid}")).json()["items"][0]["item_id"]
    await client.post(f"/api/v1/plans/{pid}/runs/{rid}/results",
                      json={"items": [{"item_id": item_id, "status": "fail", "note": "按钮不存在", "reason": "用例步骤有误"}]})
    r = (await client.post(f"/api/v1/plans/{pid}/runs/{rid}/results",
                           json={"items": [{"item_id": item_id, "status": "pass"}]})).json()
    entry = r["results"][item_id]
    assert entry["status"] == "pass" and [h["status"] for h in entry["history"]] == ["fail"]
    # 新一轮独立于上一轮
    await client.post(f"/api/v1/plans/{pid}/runs/{rid}/finish")
    run2 = (await client.post(f"/api/v1/plans/{pid}/runs", json={"name": "复测"})).json()
    rid2 = run2["run"]["run_id"] if "run" in run2 else run2["run_id"]
    detail = (await client.get(f"/api/v1/plans/{pid}")).json()
    runs = {x["run_id"]: x for x in detail["runs"]}
    assert runs[rid]["results"][item_id]["status"] == "pass" and runs[rid2]["results"] == {}


async def test_R19_FAIL只记录事实与证据_不进入BUG流程(client, tmp_path):
    _, pid = await _approved_task_in_plan(client)
    run = (await client.post(f"/api/v1/plans/{pid}/runs", json={})).json()
    rid = run["run"]["run_id"] if "run" in run else run["run_id"]
    item_id = (await client.get(f"/api/v1/plans/{pid}")).json()["items"][0]["item_id"]
    # 失败必须带说明与失败分类
    assert (await client.post(f"/api/v1/plans/{pid}/runs/{rid}/results",
                              json={"items": [{"item_id": item_id, "status": "fail"}]})).status_code == 400
    r = (await client.post(f"/api/v1/plans/{pid}/runs/{rid}/results",
                           json={"items": [{"item_id": item_id, "status": "fail", "note": "第 1 步按钮不存在",
                                            "reason": "用例步骤有误"}]})).json()
    entry = r["results"][item_id]
    assert entry["note"] == "第 1 步按钮不存在" and entry["reason"] == "用例步骤有误"
    assert not any(k.lower().startswith("bug") for k in entry)  # 无 BUG 字段
    # 证据附件挂在轮次上
    resp = await client.post(f"/api/v1/plans/{pid}/runs/{rid}/attachments", data={"item_id": item_id},
                             files={"file": ("截图.png", b"\x89PNG\r\n", "image/png")})
    assert resp.status_code == 200, resp.text
    # 系统不存在 BUG 相关接口
    assert (await client.get("/api/v1/bugs")).status_code == 404


# ---- 规则 20~25：逻辑删除、并发、AI 隔离与留痕、知识库、全链路可追溯 ----


async def test_R20_核心业务数据默认逻辑删除(client):
    await _project(client, "P")
    task_id = await _ai_task(client, make_case(), make_case(case_id="TC-登录-002", title="验证密码错误提示"))
    await _review(client, task_id, {"case_id": "TC-登录-002", "action": "delete"})
    items = (await client.get(f"/api/v1/tasks/{task_id}/recycle-bin")).json()["items"]
    assert len(items) == 1 and items[0]["deleted_at"] and items[0]["deleted_by"]
    await client.post(f"/api/v1/tasks/{task_id}/recycle-bin/restore", json={"item_id": items[0]["id"]})
    assert len((await _task(client, task_id))["result"]["cases"]) == 2
    # 需求 / 模块也是逻辑删除
    r = (await client.post("/api/v1/requirements", data={"project": "P", "title": "登录", "text": "x"})).json()
    await client.delete(f"/api/v1/requirements/{r['req_id']}")
    assert (await client.post(f"/api/v1/requirements/{r['req_id']}/restore")).status_code == 200
    m = (await client.post("/api/v1/projects/P/modules", json={"name": "登录"})).json()
    mid = m["module_id"] if "module_id" in m else m["module"]["module_id"]
    await client.delete(f"/api/v1/projects/P/modules/{mid}")
    assert (await client.post(f"/api/v1/projects/P/modules/{mid}/restore")).status_code == 200


async def test_R21_多人同时编辑必须做版本冲突检测(client):
    task_id = await _ai_task(client, make_case())
    await _review(client, task_id, {"case_id": "TC-登录-001", "action": "modify", "base_version": 1,
                                    "case": make_case(title="A 的修改")})
    resp = await _review(client, task_id, {"case_id": "TC-登录-001", "action": "modify", "base_version": 1,
                                           "case": make_case(title="B 的覆盖")}, expect=409)
    assert "已被其他用户修改" in resp.json()["detail"]
    assert (await _task(client, task_id))["result"]["cases"][0]["title"] == "A 的修改"
    # 批量维护同样逐行乐观锁
    task = await _task(client, task_id)
    uid = task["result"]["cases"][0]["uid"]
    r = (await client.post(f"/api/v1/tasks/{task_id}/cases/batch",
                           json={"uids": [uid], "action": "set_priority", "value": "P0", "base_versions": {uid: 1}})).json()
    assert r["conflicts"] and not r["applied"]


async def test_R22_AI调用必须绑定项目并隔离上下文(client, auth_on):
    admin = await _login(client)
    t1 = await _user(client, admin, "t1")
    await _project(client, "A", {"t1": "tester"}, headers=admin)
    await _project(client, "B", headers=admin)
    # 借用他项目知识空间被拒
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(make_case()), review_reply(True)])
    resp = await client.post("/api/v1/tasks", headers=t1, data={"text": "登录需求", "project": "A", "knowledge_space": "B"})
    assert resp.status_code == 403
    task_id = await _ai_task(client, make_case(), project="A", headers=t1)
    calls = (await client.get("/api/v1/ai/calls", headers=admin, params={"task_id": task_id})).json()["items"]
    assert calls and all(c["project"] == "A" for c in calls)
    # 非 A 成员看不到 A 的调用记录
    t2 = await _user(client, admin, "t2")
    assert (await client.get("/api/v1/ai/calls", headers=t2, params={"project": "A"})).status_code == 403
    assert (await client.get("/api/v1/ai/calls", headers=t2)).json()["total"] == 0


async def test_R23_AI任务记录模型_Prompt版本_输入输出_发起人(client, auth_on):
    admin = await _login(client)
    await _project(client, "A", headers=admin)
    task_id = await _ai_task(client, make_case(), project="A", headers=admin)
    task = (await client.get(f"/api/v1/tasks/{task_id}", headers=admin)).json()
    assert task["prompt_versions"]["analyst"] == 1 and task["prompt_versions"]["generator"] == 1
    calls = (await client.get("/api/v1/ai/calls", headers=admin, params={"task_id": task_id})).json()["items"]
    gen = next(c for c in calls if c["purpose"] == "用例生成")
    assert gen["model"] and gen["prompt_version"] == 1 and gen["by"] == "admin"
    detail = (await client.get(f"/api/v1/ai/calls/{gen['id']}", headers=admin)).json()
    assert "登录需求" in detail["input_preview"] and detail["output_preview"].startswith("{")
    ai_tasks = (await client.get("/api/v1/ai/tasks", headers=admin)).json()["tasks"]
    assert ai_tasks[0]["task_id"] == task_id and ai_tasks[0]["created_by"] == "admin"


async def test_R24_公共知识可跨项目_项目知识禁止跨项目污染(client):
    from app.knowledge import KnowledgeService, KnowledgeStore
    from app.knowledge.steward import KnowledgeSteward
    from tests.test_knowledge import _VOCAB, StubEmbedder

    app.state.knowledge = KnowledgeService(KnowledgeStore(":memory:", dimensions=len(_VOCAB)), StubEmbedder(),
                                           chunk_max_chars=200)
    try:
        await _project(client, "A")
        await _project(client, "B")
        assert (await client.post("/api/v1/knowledge/docs", data={"category": "test_cases", "text": "x", "level": "project"})).status_code == 400
        await client.post("/api/v1/knowledge/docs", data={"category": "test_cases", "text": "登录 密码 公共用例", "level": "public"})
        await client.post("/api/v1/knowledge/docs", data={"category": "test_cases", "text": "登录 密码 B项目用例", "level": "project", "project": "B"})
        steward = KnowledgeSteward(app.state.knowledge, budget_chars=6000)
        a_text = (await steward.for_analysis("登录 密码", space="A")).render()
        b_text = (await steward.for_analysis("登录 密码", space="B")).render()
        assert "公共用例" in a_text and "B项目用例" not in a_text
        assert "公共用例" in b_text and "B项目用例" in b_text
    finally:
        app.state.knowledge = None


async def test_R25_AI原始_人工修改_AI二次修改_最终通过全部可追溯(client):
    task_id = await _ai_task(client, make_case())
    task = await _task(client, task_id)
    uid = task["result"]["cases"][0]["uid"]
    # 人工修改（v2，定稿）→ 解锁驳回 → AI 二次修改（v3）→ 通过（final）
    await _review(client, task_id, {"case_id": "TC-登录-001", "action": "modify", "base_version": 1,
                                    "case": make_case(title="人工改过的标题"), "feedback": "标题更具体"})
    await _review(client, task_id, {"case_id": "TC-登录-001", "action": "unlock"})
    await _review(client, task_id, {"case_id": "TC-登录-001", "action": "reject", "comment": "预期空泛",
                                    "reject_types": ["预期不可验证"]})
    fixed = make_case(title="人工改过的标题", steps=[{"action": "输入正确账号密码并提交", "expected": "跳转首页并生成有效登录态"}])
    app.state.llm = StubLLM([_fix_reply(fixed)])
    await client.post(f"/api/v1/tasks/{task_id}/cases/fix")
    await client.post(f"/api/v1/tasks/{task_id}/fix/confirm", json={"accept_all": True})
    await _review(client, task_id, {"case_id": "TC-登录-001", "action": "approve"})
    versions = await _versions(client, task_id, uid)
    assert [v["source"] for v in versions] == ["ai_original", "manual", "ai_fix", "final"]
    assert versions[0]["content"]["title"] == "验证正确账号密码登录成功"
    assert versions[1]["content"]["title"] == "人工改过的标题" and versions[1]["reason"] == "标题更具体"
    assert "登录态" in versions[2]["content"]["steps"][0]["expected"]
    assert all(v["created_by"] and v["created_at"] for v in versions)
    # 任意版本可恢复且不覆盖历史
    await client.post(f"/api/v1/tasks/{task_id}/versions/restore",
                      json={"kind": "case", "entity_id": uid, "version_no": versions[0]["version_no"]})
    versions = await _versions(client, task_id, uid)
    assert len(versions) == 5 and versions[-1]["source"] == "manual"

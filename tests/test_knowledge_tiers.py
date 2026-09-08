"""M5-C：知识库三层（公共/项目/模块）与跨项目隔离、AI 质量统计五指标。"""

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.config import get_settings
from app.knowledge import KnowledgeService, KnowledgeStore
from app.main import app
from tests.stubs import ANALYST_REPLY, StubLLM, generator_reply, make_case, review_reply
from tests.test_knowledge import _VOCAB, StubEmbedder


@pytest.fixture
async def client():
    async with LifespanManager(app):
        app.state.knowledge = KnowledgeService(KnowledgeStore(":memory:", dimensions=len(_VOCAB)), StubEmbedder(), chunk_max_chars=200)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
        app.state.knowledge = None


@pytest.fixture
def auth_on():
    settings = get_settings()
    settings.auth_enabled = True
    yield
    settings.auth_enabled = False


async def _login(client, username, password) -> dict:
    resp = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


async def test_三层入库_可见性_检索隔离(client, auth_on):
    admin = await _login(client, "admin", "admin123")
    await client.post("/api/v1/auth/users", headers=admin, json={"username": "lead", "password": "pass123"})
    await client.post("/api/v1/auth/users", headers=admin, json={"username": "t1", "password": "pass123"})
    for p in ("A", "B"):
        await client.post("/api/v1/projects", headers=admin, json={"name": p})
    await client.put("/api/v1/projects/A/members", headers=admin, json={"username": "lead", "role": "test_lead"})
    await client.put("/api/v1/projects/A/members", headers=admin, json={"username": "t1", "role": "tester"})
    lead, t1 = await _login(client, "lead", "pass123"), await _login(client, "t1", "pass123")

    # 公共层仅管理员；项目层需 knowledge.manage（测试人员不可）；模块层需指定模块
    assert (await client.post("/api/v1/knowledge/docs", headers=lead, data={"category": "business_rules", "text": "投注公共规则", "level": "public"})).status_code == 403
    assert (await client.post("/api/v1/knowledge/docs", headers=t1, data={"category": "business_rules", "text": "x", "level": "project", "project": "A"})).status_code == 403
    assert (await client.post("/api/v1/knowledge/docs", headers=lead, data={"category": "business_rules", "text": "x", "level": "module", "project": "A"})).status_code == 400
    assert (await client.post("/api/v1/knowledge/docs", headers=lead, data={"category": "business_rules", "text": "x", "level": "project", "project": "B"})).status_code == 403
    pub = (await client.post("/api/v1/knowledge/docs", headers=admin, data={"category": "business_rules", "text": "投注 公共规则 赔率", "level": "public"})).json()["ingested"][0]
    a_doc = (await client.post("/api/v1/knowledge/docs", headers=lead, data={"category": "business_rules", "text": "投注 A项目规则 桌台", "level": "module", "project": "A", "module": "投注/桌台"})).json()["ingested"][0]
    b_doc = (await client.post("/api/v1/knowledge/docs", headers=admin, data={"category": "business_rules", "text": "投注 B项目规则 荷官", "level": "project", "project": "B"})).json()["ingested"][0]
    assert (pub["level"], pub["space"]) == ("public", "public") and (a_doc["level"], a_doc["module"]) == ("module", "投注/桌台")
    # 台账可见性：t1 见公共 + A
    docs = (await client.get("/api/v1/knowledge/docs", headers=t1)).json()["documents"]
    assert {d["doc_id"] for d in docs} == {pub["doc_id"], a_doc["doc_id"]}
    assert len((await client.get("/api/v1/knowledge/docs", headers=admin)).json()["documents"]) == 3
    # 检索：A 项目范围 = A + 公共，绝不命中 B；无项目上下文的成员只命中公共
    hits = (await client.post("/api/v1/knowledge/search", headers=t1, json={"query": "投注", "space": "A", "top_k": 10})).json()["hits"]
    assert {h["doc_id"] for h in hits} == {pub["doc_id"], a_doc["doc_id"]}
    assert (await client.post("/api/v1/knowledge/search", headers=t1, json={"query": "投注", "space": "B"})).status_code == 403
    hits = (await client.post("/api/v1/knowledge/search", headers=t1, json={"query": "投注", "top_k": 10})).json()["hits"]
    assert {h["doc_id"] for h in hits} == {pub["doc_id"]}
    # 删除权限：项目层由负责人删，公共层仅管理员
    assert (await client.delete(f"/api/v1/knowledge/docs/{pub['doc_id']}", headers=lead)).status_code == 403
    assert (await client.delete(f"/api/v1/knowledge/docs/{b_doc['doc_id']}", headers=lead)).status_code == 403
    assert (await client.delete(f"/api/v1/knowledge/docs/{a_doc['doc_id']}", headers=lead)).status_code == 200


async def test_任务生成只注入本项目与公共知识(client):
    from app.knowledge.steward import KnowledgeSteward

    await client.post("/api/v1/projects", json={"name": "A"})
    await client.post("/api/v1/projects", json={"name": "B"})
    await client.post("/api/v1/knowledge/docs", data={"category": "test_cases", "text": "登录 密码 公共用例", "level": "public"})
    await client.post("/api/v1/knowledge/docs", data={"category": "test_cases", "text": "登录 密码 B项目用例", "level": "project", "project": "B"})
    steward = KnowledgeSteward(app.state.knowledge, budget_chars=6000)
    bundle = await steward.for_analysis("登录 密码", space="A")
    texts = bundle.render()
    assert "公共用例" in texts and "B项目用例" not in texts
    bundle = await steward.for_analysis("登录 密码", space=None)  # 无项目上下文：只用公共层
    assert "B项目用例" not in bundle.render()


async def test_AI质量统计五指标(client):
    await client.post("/api/v1/projects", json={"name": "P"})
    c1, c2, c3 = make_case(), make_case(case_id="TC-登录-002", title="密码错误"), make_case(case_id="TC-登录-003", title="锁定")
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(c1, c2, c3), review_reply(True)])
    resp = await client.post("/api/v1/tasks", data={"text": "登录需求", "project": "P", "confirm_points": "true"})
    task_id = resp.json()["task_id"]
    pts = resp.json()["test_points"][0]["points"]
    # 测试点：一条直接通过，一条驳回后通过
    await client.post(f"/api/v1/tasks/{task_id}/points/review", json={"items": [
        {"tp_id": pts[0]["tp_id"], "action": "approve"},
        {"tp_id": pts[1]["tp_id"], "action": "reject", "comment": "范围过大", "reject_types": ["范围过大"]}]})
    await client.post(f"/api/v1/tasks/{task_id}/points/review", json={"items": [{"tp_id": pts[1]["tp_id"], "action": "approve"}]})
    app.state.llm = StubLLM([generator_reply(c1, c2, c3), review_reply(True)])
    assert (await client.post(f"/api/v1/tasks/{task_id}/confirm")).status_code == 200
    # 用例：c1 直接通过；c2 驳回后通过；c3 人工修改后通过
    await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [
        {"case_id": c1["case_id"], "action": "accept"},
        {"case_id": c2["case_id"], "action": "reject", "comment": "预期不可验证", "reject_types": ["预期不可验证"]}]})
    await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [
        {"case_id": c2["case_id"], "action": "accept"},
        {"case_id": c3["case_id"], "action": "modify", "case": {**c3, "title": "锁定 30 分钟"}, "comment": "补充时长"}]})
    await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [{"case_id": c3["case_id"], "action": "accept"}]})
    q = (await client.get("/api/v1/reports/summary?days=0&project=P")).json()["ai_quality"]
    assert q["point"]["total"] == 2 and q["point"]["approved"] == 2 and q["point"]["direct"] == 1 and q["point"]["rejected"] == 1
    assert q["point"]["adoption_rate"] == 1.0 and q["point"]["direct_pass_rate"] == 0.5 and q["point"]["reject_rate"] == 0.5
    assert q["case"]["total"] == 3 and q["case"]["approved"] == 3 and q["case"]["direct"] == 1
    assert q["case"]["modified"] == 1 and q["case"]["rejected"] == 1 and q["case"]["modify_rate"] == round(1/3, 3)
    assert {x["type"] for x in q["reject_types"]} == {"范围过大", "预期不可验证"}

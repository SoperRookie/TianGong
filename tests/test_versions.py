"""版本历史与恢复（完整需求 10 章）：

AI 原始生成 / 人工修改 / AI 驳回修改 / 评审通过终稿 四类版本入册，
版本链带字段差异，恢复不覆盖历史（追加 manual 新版并回到待评审）。
"""

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


async def _completed_task(client, *cases):
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(*cases), review_reply(True)])
    resp = await client.post("/api/v1/tasks", data={"text": "登录需求"})
    return resp.json()["task_id"]


async def _versions(client, task_id, kind, entity_id):
    resp = await client.get(f"/api/v1/tasks/{task_id}/versions",
                            params={"kind": kind, "entity_id": entity_id})
    assert resp.status_code == 200, resp.text
    return resp.json()["versions"]


async def _case_uid(client, task_id, case_id):
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    return next(c["uid"] for c in task["result"]["cases"] if c["case_id"] == case_id)


async def test_生成即记AI原始版本(client):
    task_id = await _completed_task(client, make_case())
    uid = await _case_uid(client, task_id, "TC-登录-001")
    versions = await _versions(client, task_id, "case", uid)
    assert len(versions) == 1
    assert versions[0]["source"] == "ai_original"
    assert versions[0]["content"]["title"] == "验证正确账号密码登录成功"


async def test_人工修改与评审通过各记一版且带字段差异(client):
    task_id = await _completed_task(client, make_case())
    uid = await _case_uid(client, task_id, "TC-登录-001")
    # 人工修改（定稿即通过）
    resp = await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [
        {"case_id": "TC-登录-001", "action": "modify", "comment": "标题更具体",
         "case": make_case(title="验证有效账号正确密码登录成功")},
    ]})
    assert resp.status_code == 200, resp.text
    versions = await _versions(client, task_id, "case", uid)
    assert [v["source"] for v in versions] == ["ai_original", "manual"]
    assert versions[1]["reason"] == "标题更具体"
    diff_fields = {c["field"] for c in versions[1]["changes"]}
    assert "title" in diff_fields
    # 解锁后通过 → final
    await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [
        {"case_id": "TC-登录-001", "action": "unlock"}]})
    await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [
        {"case_id": "TC-登录-001", "action": "approve"}]})
    versions = await _versions(client, task_id, "case", uid)
    assert versions[-1]["source"] == "final"


async def test_AI定点修改记ai_fix版本(client):
    task_id = await _completed_task(client, make_case())
    uid = await _case_uid(client, task_id, "TC-登录-001")
    await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [
        {"case_id": "TC-登录-001", "action": "reject", "comment": "预期空泛",
         "reject_types": ["预期不可验证"]},
    ]})
    fixed = make_case(steps=[{"action": "输入正确账号密码并提交", "expected": "跳转首页并生成有效登录态"}])
    app.state.llm = StubLLM([json.dumps({
        "fixes": [{"case_id": "TC-登录-001", "comment_type": "预期不可验证", "note": "补预期"}],
        "cases": [fixed], "deleted": [],
    }, ensure_ascii=False)])
    resp = await client.post(f"/api/v1/tasks/{task_id}/cases/fix")
    assert resp.status_code == 200
    versions = await _versions(client, task_id, "case", uid)
    assert versions[-1]["source"] == "ai_fix"
    assert "登录态" in versions[-1]["content"]["steps"][0]["expected"]


async def test_用例恢复历史版本_追加新版并回待评审(client):
    task_id = await _completed_task(client, make_case())
    uid = await _case_uid(client, task_id, "TC-登录-001")
    await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [
        {"case_id": "TC-登录-001", "action": "modify", "comment": "改标题",
         "case": make_case(title="被改过的标题")},
    ]})
    # 恢复到 v1（AI 原始）
    resp = await client.post(f"/api/v1/tasks/{task_id}/versions/restore",
                             json={"kind": "case", "entity_id": uid, "version_no": 1})
    assert resp.status_code == 200, resp.text
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    case = next(c for c in task["result"]["cases"] if c["uid"] == uid)
    assert case["title"] == "验证正确账号密码登录成功"  # 内容回滚
    assert task["case_reviews"][uid]["status"] == "pending"  # 回待评审
    versions = await _versions(client, task_id, "case", uid)
    assert len(versions) == 3  # 历史不覆盖：v1 原始 / v2 人工 / v3 恢复
    assert versions[-1]["reason"] == "恢复自 v1"


async def _confirm_task(client):
    from tests.stubs import StubLLM as _S
    analyst = json.dumps({
        "modules": [{"module": "登录", "points": [
            {"point": "正确账号密码登录成功", "dimension": "正常流程"},
        ]}], "blind_spots": [],
    }, ensure_ascii=False)
    gap = json.dumps({"coverage": {}, "additions": []}, ensure_ascii=False)
    app.state.llm = _S([analyst, gap])
    resp = await client.post("/api/v1/tasks", data={"text": "登录需求", "confirm_points": "true"})
    assert resp.status_code == 200, resp.text
    return resp.json()["task_id"]


async def test_测试点版本链_原始_人工_恢复(client):
    task_id = await _confirm_task(client)
    versions = await _versions(client, task_id, "point", "TP001")
    assert [v["source"] for v in versions] == ["ai_original"]
    # 人工修改
    await client.post(f"/api/v1/tasks/{task_id}/points/review", json={"items": [
        {"tp_id": "TP001", "action": "modify", "point": "有效账号正确密码登录成功"},
    ]})
    versions = await _versions(client, task_id, "point", "TP001")
    assert [v["source"] for v in versions] == ["ai_original", "manual"]
    assert versions[1]["changes"][0]["field"] == "point"
    # 恢复 v1
    resp = await client.post(f"/api/v1/tasks/{task_id}/versions/restore",
                             json={"kind": "point", "entity_id": "TP001", "version_no": 1})
    assert resp.status_code == 200, resp.text
    point = resp.json()["test_points"][0]["points"][0]
    assert point["point"] == "正确账号密码登录成功" and point["status"] == "pending"
    versions = await _versions(client, task_id, "point", "TP001")
    assert len(versions) == 3 and versions[-1]["reason"] == "恢复自 v1"
    # 通过 → final
    await client.post(f"/api/v1/tasks/{task_id}/points/review", json={"items": [
        {"tp_id": "TP001", "action": "approve"}]})
    versions = await _versions(client, task_id, "point", "TP001")
    assert versions[-1]["source"] == "final"


async def test_restore_版本不存在返回404(client):
    task_id = await _completed_task(client, make_case())
    uid = await _case_uid(client, task_id, "TC-登录-001")
    resp = await client.post(f"/api/v1/tasks/{task_id}/versions/restore",
                             json={"kind": "case", "entity_id": uid, "version_no": 99})
    assert resp.status_code == 404

"""多人并发编辑（完整需求 11 章）：乐观锁版本冲突拦截 + 编辑占用提示。"""

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


async def test_用例乐观锁_旧版本保存被拦截(client):
    task_id = await _completed_task(client, make_case())
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    case = task["result"]["cases"][0]
    assert case["version"] == 1
    # A 基于 v1 修改成功 → v2
    resp = await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [
        {"case_id": "TC-登录-001", "action": "modify", "base_version": 1,
         "case": make_case(title="A 修改后的标题")},
    ]})
    assert resp.status_code == 200, resp.text
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert task["result"]["cases"][0]["version"] == 2
    # B 仍基于 v1 保存 → 409 冲突，内容不被覆盖
    resp = await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [
        {"case_id": "TC-登录-001", "action": "modify", "base_version": 1,
         "case": make_case(title="B 的并发覆盖")},
    ]})
    assert resp.status_code == 409
    assert "已被其他用户修改" in resp.json()["detail"]
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert task["result"]["cases"][0]["title"] == "A 修改后的标题"


async def _confirm_task(client):
    import json as _json
    analyst = _json.dumps({"modules": [{"module": "登录", "points": [
        {"point": "正确账号密码登录成功", "dimension": "正常流程"}]}], "blind_spots": []},
        ensure_ascii=False)
    gap = _json.dumps({"coverage": {}, "additions": []}, ensure_ascii=False)
    app.state.llm = StubLLM([analyst, gap])
    resp = await client.post("/api/v1/tasks", data={"text": "登录需求", "confirm_points": "true"})
    return resp.json()["task_id"]


async def test_测试点乐观锁_版本推进与冲突(client):
    task_id = await _confirm_task(client)
    resp = await client.post(f"/api/v1/tasks/{task_id}/points/review", json={"items": [
        {"tp_id": "TP001", "action": "modify", "point": "A 改的测试点", "base_version": 1},
    ]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["test_points"][0]["points"][0]["version"] == 2
    resp = await client.post(f"/api/v1/tasks/{task_id}/points/review", json={"items": [
        {"tp_id": "TP001", "action": "modify", "point": "B 的并发覆盖", "base_version": 1},
    ]})
    assert resp.status_code == 409
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert task["analysis"]["test_points"][0]["points"][0]["point"] == "A 改的测试点"


async def test_编辑占用_登记提示与解除(client):
    task_id = await _completed_task(client, make_case())
    uid = (await client.get(f"/api/v1/tasks/{task_id}")).json()["result"]["cases"][0]["uid"]
    # 登记占用
    resp = await client.post(f"/api/v1/tasks/{task_id}/editing",
                             json={"kind": "case", "entity_id": uid, "action": "start"})
    assert resp.status_code == 200
    assert resp.json()["holder"] is None  # 自己占用成功
    # 任务详情可见占用（"某某正在编辑"）
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert task["editing"] and task["editing"][0]["entity_id"] == uid
    # 释放
    await client.post(f"/api/v1/tasks/{task_id}/editing",
                      json={"kind": "case", "entity_id": uid, "action": "stop"})
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert task["editing"] == []
    # 强制解除（auth 关闭时视为本机管理员）
    await client.post(f"/api/v1/tasks/{task_id}/editing",
                      json={"kind": "case", "entity_id": uid, "action": "start"})
    resp = await client.post(f"/api/v1/tasks/{task_id}/editing",
                             json={"kind": "case", "entity_id": uid, "action": "force_release"})
    assert resp.status_code == 200 and resp.json()["editing"] == []

"""回收站（完整需求 14.4）：逻辑删除 + deleted_by/deleted_at 留痕 + 恢复 + 永久删除。"""

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


async def _bin(client, task_id):
    return (await client.get(f"/api/v1/tasks/{task_id}/recycle-bin")).json()["items"]


async def test_删除用例入回收站并可恢复(client):
    task_id = await _completed_task(
        client, make_case(), make_case(case_id="TC-登录-002", title="验证密码错误提示"))
    resp = await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [
        {"case_id": "TC-登录-002", "action": "delete"},
    ]})
    assert resp.status_code == 200
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert len(task["result"]["cases"]) == 1  # 任务视图已移除
    items = await _bin(client, task_id)
    assert len(items) == 1
    assert "验证密码错误提示" in items[0]["label"]
    assert items[0]["deleted_at"] and items[0]["kind"] == "case"

    # 恢复：放回任务并回到待评审
    resp = await client.post(f"/api/v1/tasks/{task_id}/recycle-bin/restore",
                             json={"item_id": items[0]["id"]})
    assert resp.status_code == 200, resp.text
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    titles = [c["title"] for c in task["result"]["cases"]]
    assert "验证密码错误提示" in titles
    restored = next(c for c in task["result"]["cases"] if c["title"] == "验证密码错误提示")
    assert task["case_reviews"][restored["uid"]]["status"] == "pending"
    assert await _bin(client, task_id) == []  # 回收站条目已移除


async def test_删除测试点入回收站并可恢复(client):
    analyst = json.dumps({"modules": [{"module": "登录", "points": [
        {"point": "正确账号密码登录成功", "dimension": "正常流程"},
        {"point": "连续错误5次账号锁定", "dimension": "异常流程"}]}], "blind_spots": []},
        ensure_ascii=False)
    gap = json.dumps({"coverage": {}, "additions": []}, ensure_ascii=False)
    app.state.llm = StubLLM([analyst, gap])
    resp = await client.post("/api/v1/tasks", data={"text": "登录需求", "confirm_points": "true"})
    task_id = resp.json()["task_id"]

    resp = await client.post(f"/api/v1/tasks/{task_id}/points/review", json={"items": [
        {"tp_id": "TP002", "action": "delete"},
    ]})
    assert resp.status_code == 200
    items = await _bin(client, task_id)
    assert len(items) == 1 and items[0]["entity_id"] == "TP002"

    resp = await client.post(f"/api/v1/tasks/{task_id}/recycle-bin/restore",
                             json={"item_id": items[0]["id"]})
    assert resp.status_code == 200, resp.text
    points = resp.json()["test_points"][0]["points"]
    tp2 = next(p for p in points if p["tp_id"] == "TP002")
    assert tp2["point"] == "连续错误5次账号锁定" and tp2["status"] == "pending"
    assert await _bin(client, task_id) == []


async def test_永久删除后不可恢复(client):
    task_id = await _completed_task(client, make_case())
    await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [
        {"case_id": "TC-登录-001", "action": "delete"},
    ]})
    items = await _bin(client, task_id)
    resp = await client.delete(f"/api/v1/tasks/{task_id}/recycle-bin/{items[0]['id']}")
    assert resp.status_code == 200
    assert await _bin(client, task_id) == []
    resp = await client.post(f"/api/v1/tasks/{task_id}/recycle-bin/restore",
                             json={"item_id": items[0]["id"]})
    assert resp.status_code == 404


async def test_项目回收站聚合(client):
    """任务详情拆分：回收站上移项目工作区，按项目跨任务聚合。"""
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(make_case()), review_reply(True)])
    resp = await client.post("/api/v1/tasks", data={"text": "登录需求", "project": "拆分项目"})
    task_id = resp.json()["task_id"]
    await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [
        {"case_id": "TC-登录-001", "action": "delete"},
    ]})
    items = (await client.get("/api/v1/recycle-bin", params={"project": "拆分项目"})).json()["items"]
    assert len(items) == 1 and items[0]["task_id"] == task_id and items[0]["kind"] == "case"
    # 其他项目视角看不到
    other = (await client.get("/api/v1/recycle-bin", params={"project": "别的项目"})).json()["items"]
    assert other == []

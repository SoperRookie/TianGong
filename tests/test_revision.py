"""M4-W1 测试：多轮修订（F-3-5）、短期会话记忆（F-8-1）、任务持久化与异步执行（F-6-1/2）。"""

import asyncio
import json

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.agents import run_revision
from app.main import app
from app.tasks import TaskRecord, TaskStore
from tests.stubs import ANALYST_REPLY, StubLLM, generator_reply, make_case, review_reply


@pytest.fixture
async def client():
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


# ---- run_revision 编排 ----


async def test_run_revision_incremental_fix_path():
    revised = make_case(title="验证正确账号密码登录成功", priority="P0")
    llm = StubLLM([generator_reply(revised), review_reply(True)])
    result = await run_revision(
        "登录需求",
        cases=[make_case()],
        instruction="把登录用例优先级调整为P0",
        llm=llm,
    )
    assert result.passed and result.cases[0].priority == "P0"
    # 生成 Agent 走定点修正路径：Prompt 含用户修订要求与当前用例全集
    fix_msg = llm.calls[0]["messages"][1]["content"]
    assert "用户修订要求：" in fix_msg and "把登录用例优先级调整为P0" in fix_msg
    assert "验证正确账号密码登录成功" in fix_msg
    # 主控路由留痕
    assert result.trace[0] == {"agent": "主控", "action": "修订路由", "instruction": "把登录用例优先级调整为P0"}


async def test_run_revision_history_injected():
    llm = StubLLM([generator_reply(make_case()), review_reply(True)])
    await run_revision(
        "登录需求",
        cases=[make_case()],
        instruction="补充验证码错误场景",
        llm=llm,
        history=["把登录用例优先级调整为P0"],
    )
    fix_msg = llm.calls[0]["messages"][1]["content"]
    assert "此前已应用的修订" in fix_msg
    assert "把登录用例优先级调整为P0" in fix_msg


# ---- API：修订闭环与短期记忆 ----


async def test_revise_api_roundtrip_and_memory(client):
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(make_case()), review_reply(True)])
    resp = await client.post(
        "/api/v1/tasks", files={"files": ("需求.txt", b"# login", "text/plain")}
    )
    task_id = resp.json()["task_id"]

    # 第一轮修订
    app.state.llm = StubLLM(
        [generator_reply(make_case(priority="P0")), review_reply(True)]
    )
    resp = await client.post(
        f"/api/v1/tasks/{task_id}/revise", json={"instruction": "优先级调整为P0"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision_no"] == 1
    assert resp.json()["downloads"]  # 增量更新后重导出

    # 第二轮修订：短期会话记忆——第一轮指令注入本轮 Prompt
    app.state.llm = StubLLM(
        [generator_reply(make_case(priority="P0"), make_case(case_id="TC-登录-002", title="验证码错误")), review_reply(True)]
    )
    resp = await client.post(
        f"/api/v1/tasks/{task_id}/revise", json={"instruction": "补充验证码错误场景"}
    )
    assert resp.json()["revision_no"] == 2
    fix_msg = app.state.llm.calls[0]["messages"][1]["content"]
    assert "优先级调整为P0" in fix_msg and "补充验证码错误场景" in fix_msg

    record = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert [r["instruction"] for r in record["revisions"]] == ["优先级调整为P0", "补充验证码错误场景"]


async def test_revise_rejects_unfinished_task(client):
    app.state.llm = StubLLM([ANALYST_REPLY])
    resp = await client.post(
        "/api/v1/tasks",
        files={"files": ("需求.txt", b"# login", "text/plain")},
        data={"confirm_points": "true"},
    )
    task_id = resp.json()["task_id"]
    resp = await client.post(f"/api/v1/tasks/{task_id}/revise", json={"instruction": "x"})
    assert resp.status_code == 409


# ---- 任务持久化与异步执行 ----


def test_task_store_persistence_roundtrip(tmp_path):
    store = TaskStore(tmp_path)
    store.save(TaskRecord(task_id="t1", status="completed", sources=["a.txt"]))
    store.save(TaskRecord(task_id="t2", status="running"))
    reloaded = TaskStore(tmp_path)
    assert reloaded.get("t1").sources == ["a.txt"]
    # 重启恢复：进行中任务标记失败并说明原因
    assert reloaded.get("t2").status == "failed"
    assert "重启" in reloaded.get("t2").error


def test_task_store_list_filters(tmp_path):
    store = TaskStore(tmp_path)
    store.save(TaskRecord(task_id="a", status="completed", created_at="2026-08-07T01:00:00+00:00"))
    store.save(TaskRecord(task_id="b", status="failed", created_at="2026-08-07T02:00:00+00:00"))
    assert [r.task_id for r in store.list()] == ["b", "a"]
    assert [r.task_id for r in store.list(status="failed")] == ["b"]


async def test_async_mode_task_lifecycle(client):
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(make_case()), review_reply(True)])
    resp = await client.post(
        "/api/v1/tasks",
        files={"files": ("需求.txt", b"# login", "text/plain")},
        data={"async_mode": "true"},
    )
    assert resp.status_code == 200
    data = resp.json()
    task_id = data["task_id"]
    assert data["status"] == "queued"

    for _ in range(50):  # 轮询直至后台任务完成
        record = (await client.get(f"/api/v1/tasks/{task_id}")).json()
        if record["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.05)
    assert record["status"] == "completed"
    assert record["files"]
    assert len(record["result"]["cases"]) == 1

    resp = await client.get("/api/v1/tasks")
    task_ids = [t["task_id"] for t in resp.json()["tasks"]]
    assert task_id in task_ids


async def test_async_mode_failure_marked(client):
    class BoomLLM:
        async def chat(self, *a, **kw):
            raise RuntimeError("模型全挂")

    app.state.llm = BoomLLM()
    resp = await client.post(
        "/api/v1/tasks",
        files={"files": ("需求.txt", b"# login", "text/plain")},
        data={"async_mode": "true"},
    )
    task_id = resp.json()["task_id"]
    for _ in range(50):
        record = (await client.get(f"/api/v1/tasks/{task_id}")).json()
        if record["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.05)
    assert record["status"] == "failed"
    assert "模型全挂" in record["error"]

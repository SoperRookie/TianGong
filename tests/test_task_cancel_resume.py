"""任务取消 / 继续的健壮性：取消后立刻继续不丢新句柄、孤儿任务可标记失败、执行中有进展摘要。"""
import asyncio

from tests.test_quality_loop import client, app  # noqa: F401
from tests.stubs import StubLLM, ANALYST_REPLY, generator_reply, make_case, review_reply


class HangLLM:
    async def chat(self, *a, **k):
        await asyncio.Event().wait()


async def _awaiting_task(client):
    app.state.llm = StubLLM([ANALYST_REPLY])
    r = await client.post("/api/v1/tasks", data={"text": "登录需求", "confirm_points": "true"})
    tid = r.json()["task_id"]
    assert (await client.get(f"/api/v1/tasks/{tid}")).json()["status"] == "awaiting_confirmation"
    return tid


async def test_取消后继续完成_从已确认测试点续跑(client):
    tid = await _awaiting_task(client)
    app.state.llm = HangLLM()
    assert (await client.post(f"/api/v1/tasks/{tid}/confirm", json={"async_mode": True})).status_code == 200
    await asyncio.sleep(0.05)
    t = (await client.get(f"/api/v1/tasks/{tid}")).json()
    assert t["status"] == "running" and t["progress_detail"]["job_active"] is True
    r = await client.post(f"/api/v1/tasks/{tid}/cancel")
    assert r.status_code == 200 and r.json()["forced"] is False
    t = (await client.get(f"/api/v1/tasks/{tid}")).json()
    assert t["status"] == "failed" and t["resume"]["mode"] == "resume" and t["progress_detail"] is None

    app.state.llm = StubLLM([generator_reply(make_case()), review_reply(True)])
    r = await client.post(f"/api/v1/tasks/{tid}/retry", json={})
    assert r.status_code == 200 and r.json()["mode"] == "resume"
    for _ in range(60):
        await asyncio.sleep(0.05)
        t = (await client.get(f"/api/v1/tasks/{tid}")).json()
        if t["status"] not in ("queued", "running"):
            break
    assert t["status"] == "completed" and len(t["result"]["cases"]) == 1
    assert t["context"]["retry_count"] == 1


async def test_取消后同一拍继续_旧协程收尾不摘新句柄(client):
    tid = await _awaiting_task(client)
    store = app.state.tasks
    app.state.llm = HangLLM()
    assert (await client.post(f"/api/v1/tasks/{tid}/confirm", json={"async_mode": True})).status_code == 200
    await asyncio.sleep(0.05)
    # 取消与重新投入在同一个事件循环拍里完成：旧协程随后才收尾
    assert store.cancel(tid) is True
    gate = asyncio.Event()

    async def _job():
        await gate.wait()

    store.submit(tid, _job)
    await asyncio.sleep(0.05)
    assert store.job_active(tid) is True  # 旧协程的 finally 没有摘掉新句柄
    assert (await client.get(f"/api/v1/tasks/{tid}")).json()["progress_detail"]["job_active"] is True
    # 新句柄仍可取消
    assert (await client.post(f"/api/v1/tasks/{tid}/cancel")).status_code == 200
    assert store.job_active(tid) is False
    gate.set()


async def test_孤儿任务_标记失败后可继续(client):
    tid = await _awaiting_task(client)
    store = app.state.tasks
    store.set_progress(tid, status="running", progress="generating_reviewing")  # 模拟：置 running 后句柄丢失
    t = (await client.get(f"/api/v1/tasks/{tid}")).json()
    assert t["status"] == "running" and t["progress_detail"]["job_active"] is False
    r = await client.post(f"/api/v1/tasks/{tid}/cancel")
    assert r.status_code == 409 and "标记失败" in r.json()["detail"]
    r = await client.post(f"/api/v1/tasks/{tid}/cancel", params={"force": "true"})
    assert r.status_code == 200 and r.json()["forced"] is True
    t = (await client.get(f"/api/v1/tasks/{tid}")).json()
    assert t["status"] == "failed" and "句柄已丢失" in t["error"] and t["resume"]["resumable"] is True
    # 已结束的任务 force 也无效
    assert (await client.post(f"/api/v1/tasks/{tid}/cancel", params={"force": "true"})).status_code == 409


async def test_执行中进展摘要_带检查点(client):
    tid = await _awaiting_task(client)
    store = app.state.tasks
    rec = store.get(tid)
    rec.checkpoint = {"modules": {"登录": {"cases": []}}, "updated_at": "2026-10-27T01:00:00+00:00"}
    store.save(rec)
    store.set_progress(tid, status="running", progress="generating_reviewing")
    pd = (await client.get(f"/api/v1/tasks/{tid}")).json()["progress_detail"]
    assert pd == {"job_active": False, "modules_done": 1, "analysis_done": False, "checkpoint_at": "2026-10-27T01:00:00+00:00"}


async def _completed_with_pending(client):
    """已完成任务 + 一条新通过待生成的测试点（分批审核分批生成的第二批）。"""
    from tests.test_quality_loop import _create_confirm_task

    data = await _create_confirm_task(client)
    tid = data["task_id"]
    pts = [p for m in data["test_points"] for p in m["points"]]
    first, second = pts[0], pts[1]
    await client.post(f"/api/v1/tasks/{tid}/points/review", json={"items": [{"tp_id": first["tp_id"], "action": "approve"}]})
    app.state.llm = StubLLM([generator_reply(make_case(case_id="TC-登录-001", point_ids=[first["tp_id"]])), review_reply(True)])
    assert (await client.post(f"/api/v1/tasks/{tid}/confirm", json={})).status_code == 200
    await client.post(f"/api/v1/tasks/{tid}/points/review", json={"items": [{"tp_id": second["tp_id"], "action": "approve"}]})
    t = (await client.get(f"/api/v1/tasks/{tid}")).json()
    assert t["status"] == "completed" and t["points_pending_generation"] == [second["tp_id"]]
    return tid, second["tp_id"], t["result"]["cases"][0]["uid"]


async def test_增量生成被取消_回到已完成而不是失败重跑(client):
    tid, tp2, uid1 = await _completed_with_pending(client)
    app.state.llm = HangLLM()
    assert (await client.post(f"/api/v1/tasks/{tid}/confirm", json={"async_mode": True})).status_code == 200
    await asyncio.sleep(0.05)
    t = (await client.get(f"/api/v1/tasks/{tid}")).json()
    assert t["status"] == "running" and t["context"]["resume_stage"] == "incremental"
    assert (await client.post(f"/api/v1/tasks/{tid}/cancel")).status_code == 200
    t = (await client.get(f"/api/v1/tasks/{tid}")).json()
    # 不是 failed：现有用例与审核原样，新通过的点仍待生成，继续按钮不出现（resume 不可续）
    assert t["status"] == "completed" and "保留" in t["error"] and t["resume"]["resumable"] is False
    assert [c["uid"] for c in t["result"]["cases"]] == [uid1] and t["points_pending_generation"] == [tp2]
    # 再点一次生成即可并入
    app.state.llm = StubLLM([generator_reply(make_case(case_id="TC-登录-001", title="第二批", point_ids=[tp2])), review_reply(True)])
    assert (await client.post(f"/api/v1/tasks/{tid}/confirm", json={})).status_code == 200
    t = (await client.get(f"/api/v1/tasks/{tid}")).json()
    assert t["status"] == "completed" and len(t["result"]["cases"]) == 2 and t["points_pending_generation"] == [] and not t["error"]


async def test_增量生成模型全链路失败_回到已完成(client):
    from app.llm.client import AllModelsFailedError

    tid, tp2, uid1 = await _completed_with_pending(client)

    class _Dead:
        async def chat(self, *a, **k):
            raise AllModelsFailedError("欠费")

    app.state.llm = _Dead()
    r = await client.post(f"/api/v1/tasks/{tid}/confirm", json={})
    assert r.status_code == 502
    t = (await client.get(f"/api/v1/tasks/{tid}")).json()
    assert t["status"] == "completed" and "欠费" in t["error"] and t["context"]["resume_stage"] == "incremental"
    assert [c["uid"] for c in t["result"]["cases"]] == [uid1] and t["points_pending_generation"] == [tp2]


async def test_旧记录_已有用例的失败任务_继续即恢复已完成(client):
    tid, tp2, uid1 = await _completed_with_pending(client)
    store = app.state.tasks
    rec = store.get(tid)
    rec.status, rec.error = "failed", "任务已被用户取消"  # 修复前增量生成被取消留下的状态
    store.save(rec)
    t = (await client.get(f"/api/v1/tasks/{tid}")).json()
    assert t["resume"] == {"resumable": True, "mode": "restore", "kept_modules": [], "analysis_kept": True, "failed_modules": []}
    r = await client.post(f"/api/v1/tasks/{tid}/retry", json={})
    assert r.status_code == 200 and r.json()["mode"] == "restore" and r.json()["pending_points"] == 1
    t = (await client.get(f"/api/v1/tasks/{tid}")).json()
    assert t["status"] == "completed" and not t["error"] and [c["uid"] for c in t["result"]["cases"]] == [uid1]
    assert t["points_pending_generation"] == [tp2]

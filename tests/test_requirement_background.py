"""AI 分析 / 需求提取的后台模式：请求立即返回，页面轮询状态（同步等几分钟会被网关 / 负载均衡的空闲超时掐断）。"""
import asyncio
import json

from app.main import app
from tests.test_extraction import ANALYSIS_REPLY, StubLLM, client  # noqa: F401


class _SlowLLM(StubLLM):
    """模型调用卡住，直到放行：用来观察「进行中」的状态。"""

    def __init__(self, replies):
        super().__init__(replies)
        self.gate = asyncio.Event()

    async def chat(self, *a, **kw):
        await self.gate.wait()
        return await super().chat(*a, **kw)


async def _wait(client, rid, key, timeout=3.0):
    for _ in range(int(timeout / 0.02)):
        r = (await client.get(f"/api/v1/requirements/{rid}")).json()
        if r[key] != "running":
            return r
        await asyncio.sleep(0.02)
    raise AssertionError(f"{key} 一直是 running")


async def _new_req(client, text="密码错误 5 次锁定 30 分钟"):
    await client.post("/api/v1/projects", json={"name": "P"})
    return (await client.post("/api/v1/requirements", data={"project": "P", "title": "登录", "text": text})).json()


async def test_后台分析_立即返回_进行中不能重复点_完成后落库(client):
    r = await _new_req(client)
    rid = r["req_id"]
    assert r["analysis_status"] == "none"
    llm = app.state.llm = _SlowLLM([ANALYSIS_REPLY])
    resp = await client.post(f"/api/v1/requirements/{rid}/analyze", json={"background": True, "review": False})
    assert resp.status_code == 200
    v = resp.json()
    assert v["analysis_status"] == "running" and not v["analysis"]
    # 进行中再点（后台 / 同步都一样）→ 409，不会跑第二遍
    for body in ({"background": True}, {}):
        again = await client.post(f"/api/v1/requirements/{rid}/analyze", json=body)
        assert again.status_code == 409 and "正在进行中" in again.json()["detail"]
    llm.gate.set()
    v = await _wait(client, rid, "analysis_status")
    assert v["analysis_status"] == "done" and v["analysis"] and v["analysis_meta"]["by"] and not v.get("analysis_error")
    assert len(llm.calls) == 1
    # 完成后可以再分析
    app.state.llm = StubLLM([ANALYSIS_REPLY])
    v = (await client.post(f"/api/v1/requirements/{rid}/analyze", json={"review": False})).json()
    assert v["analysis_status"] == "done" and len(v["analysis_history"]) == 1


async def test_后台分析失败_错误落到需求上_保留旧结果_可重试(client):
    r = await _new_req(client)
    rid = r["req_id"]
    app.state.llm = StubLLM([ANALYSIS_REPLY])
    assert (await client.post(f"/api/v1/requirements/{rid}/analyze", json={"review": False})).json()["analysis"]
    app.state.llm = StubLLM(["这不是 JSON"] * 6)
    v = (await client.post(f"/api/v1/requirements/{rid}/analyze", json={"background": True, "review": False})).json()
    assert v["analysis_status"] == "running"
    v = await _wait(client, rid, "analysis_status")
    assert v["analysis_status"] == "failed" and v["analysis_error"] and v["analysis"]   # 旧结果还在
    # 同步模式失败同样清掉进行中标记
    app.state.llm = StubLLM(["这不是 JSON"] * 6)
    assert (await client.post(f"/api/v1/requirements/{rid}/analyze", json={"review": False})).status_code == 502
    assert (await client.get(f"/api/v1/requirements/{rid}")).json()["analysis_status"] == "failed"
    app.state.llm = StubLLM([ANALYSIS_REPLY])
    v = (await client.post(f"/api/v1/requirements/{rid}/analyze", json={"background": True, "review": False})).json()
    v = await _wait(client, rid, "analysis_status")
    assert v["analysis_status"] == "done" and not v.get("analysis_error")


async def test_后台提取_立即返回_完成与失败都看得到(client):
    r = await _new_req(client)
    rid = r["req_id"]
    llm = app.state.llm = _SlowLLM([json.dumps({"modules": [{"name": "登录", "items": [{"text": "密码错误 5 次锁定 30 分钟"}]}]}, ensure_ascii=False)])
    v = (await client.post(f"/api/v1/requirements/{rid}/extract", json={"background": True})).json()
    assert v["extraction_status"] == "running"
    assert (await client.post(f"/api/v1/requirements/{rid}/extract", json={"background": True})).status_code == 409
    llm.gate.set()
    v = await _wait(client, rid, "extraction_status")
    assert v["extraction_status"] == "current" and v["extraction"]["meta"]["verified"] == 1 and not v.get("extraction_error")
    app.state.llm = StubLLM([json.dumps({"modules": []})])
    v = (await client.post(f"/api/v1/requirements/{rid}/extract", json={"background": True})).json()
    assert v["extraction_status"] == "running"
    v = await _wait(client, rid, "extraction_status")
    assert "没有从材料里提取出" in v["extraction_error"] and v["extraction_status"] == "current"   # 上一版整理稿还在


async def test_服务重启_进行中的分析标记被清掉(client):
    from app.requirements import RequirementStore

    rid = (await _new_req(client))["req_id"]
    app.state.requirements.set_analyzing(rid, True, by="u")
    again = RequirementStore().get(rid)
    assert not again.get("analyzing") and "重启" in again["analysis_error"]

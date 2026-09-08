"""M5-A：Prompt 版本化（全部 Prompt 纳管）、AI 调用日志与任务 Prompt 版本留痕、AI 任务中心与失败重试。"""

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


async def test_prompt_全部纳管_版本_占位符校验_激活回滚(client):
    listed = (await client.get("/api/v1/ai/prompts")).json()["prompts"]
    keys = {p["key"] for p in listed}
    assert keys >= {"analyst", "generator", "reviewer", "fix_instruction", "gap_check", "dup_judge", "point_fix",
                    "case_fix", "requirement_diff", "learning", "requirement_analysis", "point_supplement",
                    "vision_image", "knowledge_cases_block", "rules_block", "memory_block", "knowledge_refs_block"}
    assert all(p["active_version"] == 1 and not p["customized"] for p in listed)
    gen = (await client.get("/api/v1/ai/prompts/generator")).json()
    assert gen["placeholders"] == ["template_spec"] and gen["versions"][0]["status"] == "active"
    # 占位符缺失 / 多余均拒绝
    assert (await client.post("/api/v1/ai/prompts/generator/versions", json={"content": "没有占位符"})).status_code == 400
    assert (await client.post("/api/v1/ai/prompts/generator/versions",
                              json={"content": "{template_spec} {oops}"})).status_code == 400
    # 新建草稿不影响生效；激活后生效；回滚 v1
    r = (await client.post("/api/v1/ai/prompts/analyst/versions", json={"content": "拆解 v2 内容", "note": "试验"})).json()
    assert r["active_version"] == 1 and r["versions"][1]["status"] == "draft"
    r = (await client.post("/api/v1/ai/prompts/analyst/versions/2/activate")).json()
    assert r["active_version"] == 2 and [v["status"] for v in r["versions"]] == ["archived", "active"]
    assert (await client.post("/api/v1/ai/prompts/analyst/versions/2/archive")).status_code == 400  # 生效中不能归档
    from app.prompts import prompt_text
    assert prompt_text("analyst") == "拆解 v2 内容"
    r = (await client.post("/api/v1/ai/prompts/analyst/versions/1/activate")).json()
    assert r["active_version"] == 1 and prompt_text("analyst").startswith("你是一名资深测试分析师")


async def test_任务记录Prompt版本_调用日志归属(client):
    await client.post("/api/v1/projects", json={"name": "P"})
    await client.post("/api/v1/ai/prompts/generator/versions",
                      json={"content": "生成 v2 {template_spec}", "activate": True})
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(make_case()), review_reply(True)])
    resp = await client.post("/api/v1/tasks", data={"text": "登录需求", "project": "P"})
    task_id = resp.json()["task_id"]
    # 生成 Agent 用的是 v2
    assert app.state.llm.calls[1]["messages"][0]["content"].startswith("生成 v2")
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert task["prompt_versions"]["generator"] == 2 and task["prompt_versions"]["analyst"] == 1 \
        and task["prompt_versions"]["reviewer"] == 1
    # 调用日志：3 次调用，归属任务/项目/发起人，含用途与 Prompt 版本
    calls = (await client.get("/api/v1/ai/calls", params={"task_id": task_id})).json()
    assert calls["total"] == 3
    purposes = {c["purpose"] for c in calls["items"]}
    assert purposes == {"需求拆解（测试点）", "用例生成", "用例评审"}
    gen_call = next(c for c in calls["items"] if c["purpose"] == "用例生成")
    assert (gen_call["project"], gen_call["task_id"], gen_call["status"], gen_call["prompt_version"]) == ("P", task_id, "ok", 2)
    detail = (await client.get(f"/api/v1/ai/calls/{gen_call['id']}")).json()
    assert "登录需求" in detail["input_preview"] and detail["output_preview"].startswith("{")
    stats = (await client.get("/api/v1/ai/stats?days=0")).json()
    assert stats["by_model"][0]["calls"] == 3 and {p["purpose"] for p in stats["by_purpose"]} == purposes
    # AI 任务中心
    tasks = (await client.get("/api/v1/ai/tasks")).json()["tasks"]
    assert tasks[0]["task_id"] == task_id and tasks[0]["kind"] == "用例生成" and tasks[0]["prompt_versions"]["generator"] == 2


async def test_失败任务重试与部分成功明示(client):
    from app.llm.client import AllModelsFailedError

    await client.post("/api/v1/projects", json={"name": "P"})

    class FailLLM:
        calls = []

        async def chat(self, messages, model=None, **kw):
            from app.llm.calllog import record_call
            err = AllModelsFailedError("全链路失败")
            record_call(messages, None, error=err, model_name="deepseek-chat", provider="deepseek")
            raise err

    app.state.llm = FailLLM()
    resp = await client.post("/api/v1/tasks", data={"text": "登录需求", "project": "P"})
    assert resp.status_code == 502
    task_id = resp.json().get("task_id") or (await client.get("/api/v1/tasks")).json()["tasks"][0]["task_id"]
    t = (await client.get("/api/v1/ai/tasks")).json()["tasks"][0]
    assert t["status"] == "failed" and t["retryable"] and "全链路失败" in t["error"]
    errs = (await client.get("/api/v1/ai/calls", params={"status": "error"})).json()
    assert errs["total"] == 1 and errs["items"][0]["error"] == "全链路失败"
    # 重试：沿用原上下文，后台执行；评审未收敛 → 部分成功明示
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(make_case()), review_reply(False, [{"case_id": "TC-登录-001", "problem": "x"}]),
                             generator_reply(make_case()), review_reply(False, [{"case_id": "TC-登录-001", "problem": "x"}]),
                             generator_reply(make_case()), review_reply(False, [{"case_id": "TC-登录-001", "problem": "x"}]),
                             generator_reply(make_case()), review_reply(False, [{"case_id": "TC-登录-001", "problem": "x"}])])
    resp = await client.post(f"/api/v1/tasks/{task_id}/retry")
    assert resp.status_code == 200, resp.text
    import asyncio
    for _ in range(50):
        await asyncio.sleep(0.05)
        t = (await client.get(f"/api/v1/tasks/{task_id}")).json()
        if t["status"] in ("completed", "failed"):
            break
    assert t["status"] == "completed" and t["context"]["retry_count"] == 1
    t = next(x for x in (await client.get("/api/v1/ai/tasks")).json()["tasks"] if x["task_id"] == task_id)
    assert t["retry_count"] == 1 and any("评审未收敛" in n or "未解决" in n for n in t["partial"])
    assert (await client.post(f"/api/v1/tasks/{task_id}/retry")).status_code == 409

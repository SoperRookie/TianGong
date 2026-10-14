"""知识自动沉淀与项目画像：任务 / 需求过程产物自动进项目知识库；AI 提炼画像 → 管理员确认 → 全链路注入。"""

import json

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.config import get_settings
from app.knowledge import KnowledgeService, KnowledgeStore
from app.main import app
from tests.stubs import ANALYST_REPLY, StubLLM, generator_reply, make_case, review_reply
from tests.test_knowledge import _VOCAB, StubEmbedder

PROFILE_REPLY = json.dumps({
    "background": "面向会员的登录与密码管理系统",
    "roles": ["会员：可登录、找回密码", "管理员：可锁定账号"],
    "flows": ["登录：输入账号密码 → 校验 → 跳转首页"],
    "glossary": [{"term": "锁定", "meaning": "密码连续错误 5 次后账号 30 分钟内禁止登录"}],
    "rules": ["密码连续错误 5 次锁定账号"],
    "risks": ["锁定时长曾多次变更"],
}, ensure_ascii=False)


@pytest.fixture
async def client():
    settings = get_settings()
    settings.knowledge_auto_ingest = True
    async with LifespanManager(app):
        app.state.knowledge = KnowledgeService(KnowledgeStore(":memory:", dimensions=len(_VOCAB)), StubEmbedder(), chunk_max_chars=200)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
        app.state.knowledge = None
    settings.knowledge_auto_ingest = False
    settings.profile_auto_extract = False


def _docs(space: str):
    return app.state.knowledge.store.list_docs(space)


async def test_任务过程自动沉淀_需求_测试点_用例_删除联动(client):
    await client.post("/api/v1/projects", json={"name": "A"})
    case = make_case()
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(case), review_reply(True)])
    resp = await client.post("/api/v1/tasks", data={"text": "登录需求：密码连续错误 5 次锁定", "project": "A", "confirm_points": "true"})
    task_id = resp.json()["task_id"]
    await app.state.accumulator.drain()
    # 需求原文 → 需求文档库，带任务来源标记
    docs = _docs("A")
    assert [(d.category, d.origin, d.origin_ref) for d in docs] == [("requirement_docs", "task", task_id)]
    assert docs[0].source == f"任务 {task_id} · 需求原文"

    # 审核通过测试点 → 玩法与业务规则库（含设计需求页来源）
    points = resp.json()["test_points"][0]["points"]
    await client.post(f"/api/v1/tasks/{task_id}/points/review", json={"items": [
        {"tp_id": points[0]["tp_id"], "action": "modify", "point": "密码连续错误 5 次锁定", "ref": "登录页 › 密码错误"},
        {"tp_id": points[1]["tp_id"], "action": "reject", "comment": "不需要", "reject_types": ["重复"]},
    ]})
    await app.state.accumulator.drain()
    assert not [d for d in _docs("A") if d.category == "business_rules"]  # 人工修改后仍待审核：未通过不沉淀
    await client.post(f"/api/v1/tasks/{task_id}/points/review", json={"items": [{"tp_id": points[0]["tp_id"], "action": "approve"}]})
    await app.state.accumulator.drain()
    rules = [d for d in _docs("A") if d.category == "business_rules"]
    assert len(rules) == 1 and rules[0].origin_ref == task_id
    chunks = app.state.knowledge.store.iter_chunks(category="business_rules", space="A")
    assert len(chunks) == 1 and "密码连续错误 5 次锁定" in chunks[0].text and "登录页 › 密码错误" in chunks[0].text
    assert chunks[0].text.count("\n- ") == 1  # 驳回的那条不沉淀

    # 确认生成 → 评审通过用例 → 测试用例库；多次评审按来源替换不堆积
    app.state.llm = StubLLM([generator_reply(case), review_reply(True)])
    assert (await client.post(f"/api/v1/tasks/{task_id}/confirm")).status_code == 200
    await app.state.accumulator.drain()
    assert not [d for d in _docs("A") if d.category == "test_cases"]  # 未评审通过不沉淀
    await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [{"case_id": case["case_id"], "action": "accept"}]})
    await app.state.accumulator.drain()
    await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [{"case_id": case["case_id"], "action": "unlock"}]})
    await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [{"case_id": case["case_id"], "action": "accept"}]})
    await app.state.accumulator.drain()
    cases_docs = [d for d in _docs("A") if d.category == "test_cases"]
    assert len(cases_docs) == 1 and cases_docs[0].chunk_count == 1 and cases_docs[0].origin == "task"
    listed = (await client.get("/api/v1/knowledge/docs?space=A")).json()
    assert {d["origin_label"] for d in listed["documents"]} == {"任务沉淀"} and listed["origins"]["task"] == "任务沉淀"

    # 删除任务 → 沉淀的知识随之清理；恢复 → 重新沉淀
    await client.delete(f"/api/v1/tasks/{task_id}")
    assert _docs("A") == []
    await client.post(f"/api/v1/tasks/{task_id}/restore")
    await app.state.accumulator.drain()
    assert {d.category for d in _docs("A")} == {"requirement_docs", "business_rules", "test_cases"}

    # 项目关闭自动沉淀后不再入库
    await client.put("/api/v1/projects/A", json={"knowledge_auto": False})
    assert (await client.get("/api/v1/projects/A")).json()["knowledge_auto"] is False
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(case), review_reply(True)])
    t2 = (await client.post("/api/v1/tasks", data={"text": "登录 需求二", "project": "A"})).json()["task_id"]
    await app.state.accumulator.drain()
    assert not [d for d in _docs("A") if d.origin_ref == t2]


async def test_需求中心沉淀_附件与原文_删除恢复(client):
    await client.post("/api/v1/projects", json={"name": "B"})
    r = await client.post("/api/v1/requirements", data={"project": "B", "title": "登录改版", "text": "登录 密码 规则原文"})
    rid = r.json()["req_id"]
    await app.state.accumulator.drain()
    docs = _docs("B")
    assert len(docs) == 1 and docs[0].origin == "requirement" and docs[0].origin_ref == rid and docs[0].source == "需求 登录改版 · 原文"
    files = {"files": ("附件.txt", "登录 附件里的密码规则".encode("utf-8"), "text/plain")}
    await client.post(f"/api/v1/requirements/{rid}/attachments", files=files)
    for _ in range(20):  # 附件后台解析完成后沉淀
        await app.state.accumulator.drain()
        if len(_docs("B")) == 2:
            break
        import asyncio
        await asyncio.sleep(0.05)
    assert {d.source for d in _docs("B")} == {"需求 登录改版 · 原文", "需求 登录改版 · 附件.txt"}
    await client.delete(f"/api/v1/requirements/{rid}")
    assert _docs("B") == []
    await client.post(f"/api/v1/requirements/{rid}/restore")
    await app.state.accumulator.drain()
    assert len(_docs("B")) == 2


async def test_项目画像_提炼_确认_全链路注入(client):
    await client.post("/api/v1/projects", json={"name": "C0"})
    assert (await client.post("/api/v1/projects/C0/profile/extract")).status_code == 400  # 还没任何素材
    await client.post("/api/v1/projects", json={"name": "C", "description": "会员登录系统"})
    case = make_case()
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(case), review_reply(True)])
    await client.post("/api/v1/tasks", data={"text": "登录 密码错误 5 次锁定", "project": "C"})
    # 画像未生效前：只有项目描述进入拆解上下文（没有画像块）
    first = app.state.llm.calls[0]["messages"][1]["content"]
    assert "项目画像" in first and "会员登录系统" in first and "术语表" not in first

    app.state.llm = StubLLM([PROFILE_REPLY])
    view = (await client.post("/api/v1/projects/C/profile/extract")).json()
    assert view["draft"]["glossary"][0]["term"] == "锁定" and view["draft_meta"]["trigger"] == "manual"
    assert view["has_active"] is False and view["can_confirm"] is True
    material = app.state.llm.calls[0]["messages"][1]["content"]
    assert "会员登录系统" in material and "正常登录" in material  # 素材含项目描述与已确认测试点
    assert (await client.get("/api/v1/projects/C")).json()["profile"] == {"active": False, "version": 0, "draft": True, "confirmed_at": None}

    # 管理员确认（编辑后）→ 生效；草稿清空、版本 +1
    edited = {**view["draft"], "rules": ["密码连续错误 5 次锁定账号", "锁定 30 分钟后自动解锁"]}
    view = (await client.post("/api/v1/projects/C/profile/confirm", json={"profile": edited})).json()
    assert view["has_active"] and view["version"] == 1 and view["draft"] is None and view["confirmed_by"] == "anonymous"
    assert "锁定 30 分钟后自动解锁" in view["rendered"] and view["history"][0]["source"] == "manual"
    assert (await client.post("/api/v1/projects/C/profile/confirm")).status_code == 409  # 没有草稿可确认

    # 生效后：拆解 / 生成 / 评审 / 查漏 / 补充测试点 / 需求分析都带画像
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(case), review_reply(True)])
    await client.post("/api/v1/tasks", data={"text": "登录 密码错误 5 次锁定", "project": "C"})
    for call in app.state.llm.calls:
        text = call["messages"][1]["content"]
        assert "项目画像" in text and "锁定 30 分钟后自动解锁" in text, call["messages"][0]["content"][:30]
    # 画像与项目绑定：其他项目的任务不注入
    await client.post("/api/v1/projects", json={"name": "D"})
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(case), review_reply(True)])
    await client.post("/api/v1/tasks", data={"text": "登录 需求", "project": "D"})
    assert "锁定 30 分钟后自动解锁" not in app.state.llm.calls[0]["messages"][1]["content"]
    # 审计留痕
    actions = [x["action"] for x in (await client.get("/api/v1/audit?limit=50")).json()["items"]]
    assert "提炼项目画像" in actions and "确认项目画像" in actions


async def test_项目画像自动提炼_达到任务阈值后生成草稿(client):
    settings = get_settings()
    settings.profile_auto_extract = True
    settings.profile_refresh_tasks = 2
    await client.post("/api/v1/projects", json={"name": "E"})
    case = make_case()
    # 首个任务完成即自动提炼一版草稿（素材齐全后）；桩 LLM 多给一条画像回复供后台提炼消费
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(case), review_reply(True), PROFILE_REPLY])
    await client.post("/api/v1/tasks", data={"text": "登录 密码 需求一", "project": "E"})
    await app.state.accumulator.drain()
    view = (await client.get("/api/v1/projects/E/profile")).json()
    assert view["draft"] and view["draft_meta"]["trigger"] == "auto" and view["has_active"] is False
    # 有待确认草稿时不重复提炼
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(case), review_reply(True)])
    await client.post("/api/v1/tasks", data={"text": "登录 密码 需求二", "project": "E"})
    await app.state.accumulator.drain()
    assert app.state.llm.replies == []
    # 丢弃草稿后：新增任务数未达阈值不提炼，达到阈值后提炼
    await client.delete("/api/v1/projects/E/profile/draft")
    assert (await client.get("/api/v1/projects/E/profile")).json()["draft"] is None
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(case), review_reply(True), PROFILE_REPLY])
    await client.post("/api/v1/tasks", data={"text": "登录 密码 需求三", "project": "E"})
    await app.state.accumulator.drain()
    assert (await client.get("/api/v1/projects/E/profile")).json()["draft"]
    assert app.state.llm.replies == []


async def test_存量项目回溯沉淀_历史任务与需求补入知识库(client, tmp_path):
    from app.exporters import export_excel
    from app.templates import TestCase

    settings = get_settings()
    # 模拟上线前：自动沉淀关闭时已经产生了历史任务 / 需求（评审通过 + 离线终稿）
    settings.knowledge_auto_ingest = False
    await client.post("/api/v1/projects", json={"name": "F"})
    case = make_case()
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(case), review_reply(True)])
    t1 = (await client.post("/api/v1/tasks", data={"text": "登录 密码 历史需求一", "project": "F"})).json()["task_id"]
    await client.post(f"/api/v1/tasks/{t1}/review", json={"items": [{"case_id": case["case_id"], "action": "accept"}]})
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(case), review_reply(True)])
    t2 = (await client.post("/api/v1/tasks", data={"text": "登录 密码 历史需求二", "project": "F"})).json()["task_id"]
    path = export_excel([TestCase.model_validate(make_case(case_id="TC-登录-009", title="验证断线重连"))], tmp_path / "终稿.xlsx")
    assert (await client.post(f"/api/v1/tasks/{t2}/final", files={"file": ("终稿.xlsx", path.read_bytes(), "application/octet-stream")})).status_code == 200
    await client.post("/api/v1/requirements", data={"project": "F", "title": "历史需求", "text": "登录 密码 历史原文"})
    await app.state.accumulator.drain()
    assert _docs("F") == []  # 关闭期间什么都没沉淀
    settings.knowledge_auto_ingest = True

    view = (await client.get("/api/v1/projects/F/knowledge/backfill")).json()
    assert view["targets"] == {"tasks": 2, "requirements": 1} and view["last"] is None
    view = (await client.post("/api/v1/projects/F/knowledge/backfill")).json()
    assert view["last"]["status"] == "running"
    assert (await client.post("/api/v1/projects/F/knowledge/backfill")).status_code == 409  # 进行中不重复触发
    await app.state.accumulator.drain()
    last = (await client.get("/api/v1/projects/F/knowledge/backfill")).json()["last"]
    assert last["status"] == "done" and last["done"] == last["total"] == 3 and last["errors"] == 0
    sources = sorted(d.source for d in _docs("F"))  # 直接生成的任务没有审核环节，测试点不进规则库
    assert sources == sorted([f"任务 {t1} · 需求原文", f"任务 {t1} · 评审通过用例",
                              f"任务 {t2} · 需求原文", f"任务 {t2} · 离线终稿", "需求 历史需求 · 原文"])
    assert last["docs"] == 5
    final_chunks = app.state.knowledge.store.iter_chunks(category="test_cases", space="F")
    assert any("验证断线重连" in c.text for c in final_chunks)
    # 重复回溯按来源替换，不堆积；旧记录没存终稿路径时按文件名在终稿目录里找
    rec = app.state.tasks.get(t2)
    rec.offline_review.pop("paths")
    app.state.tasks.save(rec)
    await client.post("/api/v1/projects/F/knowledge/backfill")
    await app.state.accumulator.drain()
    assert len(_docs("F")) == 5
    # 全部项目一键回溯（系统管理员）：关闭自动沉淀 / 无历史的项目跳过
    await client.post("/api/v1/projects", json={"name": "G"})
    await client.put("/api/v1/projects/F", json={"knowledge_auto": False})
    assert (await client.post("/api/v1/projects/F/knowledge/backfill")).status_code == 409
    out = (await client.post("/api/v1/knowledge/backfill")).json()
    assert {x["project"]: x["reason"] for x in out["skipped"]}["F"] == "已关闭自动沉淀"
    assert {x["project"]: x["reason"] for x in out["skipped"]}["G"] == "没有历史任务或需求"
    await app.state.accumulator.drain()
    assert "F" in {x["project"] for x in (await client.get("/api/v1/knowledge/backfill")).json()["items"]}
    actions = [x["action"] for x in (await client.get("/api/v1/audit?limit=50")).json()["items"]]
    assert "回溯沉淀历史知识" in actions and "回溯沉淀全部项目" in actions

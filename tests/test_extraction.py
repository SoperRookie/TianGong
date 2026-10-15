"""需求提取（AI 分析前置）：从全部材料逐字摘录归整成整理稿，分析与设计以整理稿为准；自动提取、核验标记、人工修改、过期判定。"""

import asyncio
import json

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.agents.extraction import _merge_modules, extraction_markdown, verify_modules
from app.config import get_settings
from app.main import app
from app.requirements import design_brief, extraction_current, extraction_wanted
from tests.stubs import StubLLM
from tests.test_requirements import ANALYSIS_REPLY, _wait_parsed


@pytest.fixture
async def client():
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
def extract_on(monkeypatch):
    monkeypatch.setenv("TIANGONG_REQUIREMENT_EXTRACT", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.setenv("TIANGONG_REQUIREMENT_EXTRACT", "false")
    get_settings.cache_clear()


NOISY = ("首页 | 活动管理 | 用户管理 | 退出\n活动列表页面：活动状态分为未开始、进行中、已结束。\n"
         "活动名称最长 30 个字符。\n版权所有 © 2026\n首页 | 活动管理 | 用户管理 | 退出\n")
EXTRACT_REPLY = json.dumps({
    "modules": [{"name": "活动后台-活动列表", "items": [
        {"text": "活动状态分为未开始、进行中、已结束", "sources": ["原型.txt › 活动列表"]},
        {"text": "活动名称最长 30 个字符", "sources": ["原型.txt › 活动列表"]},
        {"text": "活动删除后不可恢复", "sources": ["原型.txt"]},     # 材料里没有 → ⚠
    ]}],
    "dropped": ["页面导航", "版权信息"],
}, ensure_ascii=False)


class _R:
    model_name = "stub"


def test_合并去重_逐字核验_整理稿格式():
    a = ({"modules": [{"name": "活动后台-活动列表", "items": [{"text": "活动状态分为未开始、进行中、已结束", "sources": ["a.docx"]}]}],
          "dropped": ["页面导航"]}, _R())
    b = ({"modules": [{"name": "活动后台-活动列表", "items": [
            {"text": "活动状态分为未开始、进行中、已结束", "sources": ["b.html"]},   # 跨片重复：合并来源
            {"text": "活动名称最长 30 个字符", "sources": "b.html"}]},             # sources 给成字符串也兼容
          {"name": "活动后台-数据统计", "items": [{"text": "按天统计参与人数"}]}], "dropped": ["页面导航", "css"]}, _R())
    modules, dropped, _ = _merge_modules([a, b])
    assert [m["name"] for m in modules] == ["活动后台-活动列表", "活动后台-数据统计"]
    assert modules[0]["items"][0]["sources"] == ["a.docx", "b.html"] and len(modules[0]["items"]) == 2
    assert dropped == ["页面导航", "css"]
    stats = verify_modules(modules, NOISY)
    assert stats == {"checked": 3, "verified": 2, "unverified": 1}
    short = [{"name": "m", "items": [{"text": "退出", "sources": []}, {"text": "报名", "sources": []}]}]
    assert verify_modules(short, NOISY) == {"checked": 2, "verified": 1, "unverified": 1}   # 过短条目按子串判
    md = extraction_markdown(modules, dropped)
    assert md.startswith("## 活动后台-活动列表 〔来源：b.html〕\n- 活动状态分为未开始、进行中、已结束 〔来源：a.docx〕\n- 活动名称最长 30 个字符\n")
    assert "- ⚠ 按天统计参与人数" in md and md.endswith("（已剔除：页面导航、css）")


async def test_附件解析完成自动提取_分析与设计以整理稿为准_修改整理稿分析标过期(client, extract_on):
    await client.post("/api/v1/projects", json={"name": "P"})
    app.state.llm = StubLLM([EXTRACT_REPLY, ANALYSIS_REPLY])
    r = (await client.post("/api/v1/requirements", data={"project": "P", "title": "活动后台"},
                           files=[("files", ("原型.txt", NOISY.encode(), "text/plain"))])).json()
    rid = r["req_id"]
    r = await _wait_parsed(client, rid)
    for _ in range(200):   # 解析完成后后台自动提取
        if r["extraction_status"] == "current":
            break
        await asyncio.sleep(0.02)
        r = (await client.get(f"/api/v1/requirements/{rid}")).json()
    assert r["extraction_status"] == "current" and r["extraction_wanted"] is True
    m = r["extraction"]["meta"]
    assert m["auto"] is True and m["modules"] == 1 and m["checked"] == 3 and m["unverified"] == 1 and m["dropped"] == ["页面导航", "版权信息"]
    assert "⚠ 活动删除后不可恢复" in r["extraction"]["text"] and "版权所有" not in r["extraction"]["text"]
    # 提取 Prompt 收到的是全部材料（原文 + 附件文本），最高推理强度
    call = app.state.llm.calls[0]
    assert call["reasoning_effort"] == "medium" and "需求材料" in call["messages"][1]["content"] and "版权所有" in call["messages"][1]["content"]
    # 分析输入 = 整理稿，不再是原始材料
    a = (await client.post(f"/api/v1/requirements/{rid}/analyze", json={})).json()
    user = app.state.llm.calls[1]["messages"][1]["content"]
    assert "【需求整理稿】" in user and "活动状态分为未开始、进行中、已结束" in user and "版权所有" not in user
    assert a["analysis"] and a["analysis_outdated"] is False
    item = app.state.requirements.get(rid)
    assert design_brief(item).startswith("【需求整理稿】") and "首页 | 活动管理" not in design_brief(item)
    # 人工修改整理稿 → 分析标过期；设计上下文用修改后的文本
    r = (await client.put(f"/api/v1/requirements/{rid}/extraction", json={"text": "## 活动后台-活动列表\n- 活动名称最长 30 个字符"})).json()
    assert r["extraction"]["edited"]["by"] and r["analysis_outdated"] is True and r["extraction_status"] == "current"
    assert "人工修改过" in design_brief(app.state.requirements.get(rid))
    assert (await client.put(f"/api/v1/requirements/{rid}/extraction", json={"text": "  "})).status_code == 400
    # 追加附件 → 整理稿过期；分析时先自动重新提取（桩按顺序：提取 → 分析）
    app.state.llm = StubLLM([EXTRACT_REPLY, ANALYSIS_REPLY])
    await client.post(f"/api/v1/requirements/{rid}/attachments",
                      files=[("files", ("补充.txt", "活动最多同时进行 5 个".encode(), "text/plain"))])
    r = await _wait_parsed(client, rid)
    for _ in range(200):
        if r["extraction_status"] == "current":
            break
        await asyncio.sleep(0.02)
        r = (await client.get(f"/api/v1/requirements/{rid}")).json()
    assert r["extraction_status"] == "current" and r["extraction"]["edited"] is None   # 自动重提，人工修改随旧版留档
    assert len(app.state.requirements.get(rid)["extraction_history"]) == 1
    # 删除整理稿 → 退回原始材料
    r = (await client.delete(f"/api/v1/requirements/{rid}/extraction")).json()
    assert r["extraction_status"] == "none" and r["extraction"] is None
    item = app.state.requirements.get(rid)
    assert extraction_wanted(item) and not extraction_current(item) and "首页 | 活动管理" in design_brief(item)


async def test_手动提取_纯原文需求不自动提取_提取失败落错误_疑似非需求文本拦截(client, extract_on):
    await client.post("/api/v1/projects", json={"name": "P"})
    # 纯手填原文：不需要整理稿，分析直接用原文（不消耗提取调用）
    app.state.llm = StubLLM([ANALYSIS_REPLY])
    r = (await client.post("/api/v1/requirements", data={"project": "P", "title": "纯文本", "text": "密码错误 5 次锁定 30 分钟"})).json()
    rid = r["req_id"]
    assert r["extraction_wanted"] is False and r["extraction_status"] == "none"
    a = (await client.post(f"/api/v1/requirements/{rid}/analyze", json={})).json()
    assert a["analysis"] and len(app.state.llm.calls) == 1 and "【需求整理稿】" not in app.state.llm.calls[0]["messages"][1]["content"]
    # 手动提取纯原文也可以
    app.state.llm = StubLLM([json.dumps({"modules": [{"name": "登录", "items": [{"text": "密码错误 5 次锁定 30 分钟"}]}]}, ensure_ascii=False)])
    r = (await client.post(f"/api/v1/requirements/{rid}/extract", json={})).json()
    assert r["extraction_status"] == "current" and r["extraction"]["meta"]["verified"] == 1 and r["extraction"]["meta"]["auto"] is False
    assert r["analysis_outdated"] is True   # 新整理稿生效，之前基于原文的分析过期
    # 模型没提出任何内容 → 502 并落错误，不留半成品
    app.state.llm = StubLLM([json.dumps({"modules": []})])
    resp = await client.post(f"/api/v1/requirements/{rid}/extract", json={})
    assert resp.status_code == 502 and "没有从材料里提取出" in resp.json()["detail"]
    r = (await client.get(f"/api/v1/requirements/{rid}")).json()
    assert r["extraction_status"] == "current" and "没有从材料里提取出" in r["extraction_error"]
    # 附件疑似非需求文本：自动提取跳过，手动提取 409，确认后可继续
    css = "\n".join(f"#u{i} {{\n  position:absolute;\n  left:{i}px;\n  width:100px;\n}}" for i in range(300)).encode()
    app.state.llm = StubLLM([])
    r = (await client.post("/api/v1/requirements", data={"project": "P", "title": "源码"},
                           files=[("files", ("styles.css", css, "text/css"))])).json()
    r = await _wait_parsed(client, r["req_id"]); rid2 = r["req_id"]
    await asyncio.sleep(0.05)
    r = (await client.get(f"/api/v1/requirements/{rid2}")).json()
    assert r["content_warnings"] and r["extraction_status"] == "none" and not app.state.llm.calls
    assert (await client.post(f"/api/v1/requirements/{rid2}/extract", json={})).status_code == 409
    app.state.llm = StubLLM([json.dumps({"modules": [{"name": "样式", "items": [{"text": "position:absolute"}]}]})])
    r = (await client.post(f"/api/v1/requirements/{rid2}/extract", json={"ignore_warnings": True})).json()
    assert r["extraction_status"] == "current"


async def test_开关关闭时不自动提取也不在分析前提取(client):
    await client.post("/api/v1/projects", json={"name": "P"})
    app.state.llm = StubLLM([ANALYSIS_REPLY])
    r = (await client.post("/api/v1/requirements", data={"project": "P", "title": "活动后台"},
                           files=[("files", ("原型.txt", NOISY.encode(), "text/plain"))])).json()
    r = await _wait_parsed(client, r["req_id"]); rid = r["req_id"]
    await asyncio.sleep(0.05)
    r = (await client.get(f"/api/v1/requirements/{rid}")).json()
    assert r["extraction_wanted"] is True and r["extraction_status"] == "none"
    a = (await client.post(f"/api/v1/requirements/{rid}/analyze", json={})).json()
    assert a["analysis"] and len(app.state.llm.calls) == 1 and "版权所有" in app.state.llm.calls[0]["messages"][1]["content"]

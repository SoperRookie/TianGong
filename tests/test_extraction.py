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


def test_分片标题路径_切在章节中间的片带上文件与页面():
    from app.agents.extraction import chunk_contexts
    from app.parsers.chunking import split_text

    page1 = "## 页面：活动详情页\n" + "\n".join(f"规则 {i}：报名费 {i} 元" for i in range(60))
    page2 = "## 页面：活动入口\n### 游戏内入口\n" + "\n".join(f"入口规则 {i}：固定位置不可拖动" for i in range(60))
    materials = "需求原文一句话\n\n【文件：连赢挑战.zip】\n# Axure 原型：连赢挑战.zip\n" + page1 + "\n" + page2
    chunks = split_text(materials, 700)
    assert len(chunks) >= 3
    ctx = chunk_contexts(materials, chunks)
    assert ctx[0] == ""                                   # 第一片从原文开头起，没有上级标题
    mid = [c for c in ctx if c.startswith("连赢挑战.zip › Axure 原型")]
    assert mid and any(c.endswith("页面：活动详情页") for c in mid)   # 切在活动详情页中间的片
    assert any(c.endswith("页面：活动入口 › 游戏内入口") for c in ctx)   # 三级路径
    # 片自身开头就是标题时，路径只到它的上一级
    for c, ch in zip(ctx, chunks):
        if ch.startswith("## 页面：活动入口"):
            assert c == "连赢挑战.zip › Axure 原型：连赢挑战.zip"


async def test_自动提取调用日志带需求归属_提取模型可单独配置(client, extract_on, monkeypatch):
    from app.llm import calllog

    await client.post("/api/v1/projects", json={"name": "P"})
    monkeypatch.setenv("TIANGONG_REQUIREMENT_EXTRACT_MODEL", "stub-extract")
    get_settings.cache_clear()
    app.state.llm = StubLLM([EXTRACT_REPLY])
    r = (await client.post("/api/v1/requirements", data={"project": "P", "title": "活动后台"},
                           files=[("files", ("原型.txt", NOISY.encode(), "text/plain"))])).json()
    rid = r["req_id"]
    r = await _wait_parsed(client, rid)
    for _ in range(200):
        if r["extraction_status"] == "current":
            break
        await asyncio.sleep(0.02)
        r = (await client.get(f"/api/v1/requirements/{rid}")).json()
    assert r["extraction_status"] == "current" and r["extraction"]["meta"]["model"] == "stub-extract"
    assert app.state.llm.calls[0]["model"] == "stub-extract"        # 没指定模型时用配置的提取模型，不跟默认模型降档
    rows = calllog.list_calls(requirement_id=rid)["items"]
    assert rows and rows[0]["project"] == "P" and rows[0]["purpose"] == "需求提取（分析前置）"   # 自动提取也能按需求筛到
    # 手动提取页面选了模型时以页面为准
    app.state.llm = StubLLM([EXTRACT_REPLY])
    r = (await client.post(f"/api/v1/requirements/{rid}/extract", json={"model": "stub-manual"})).json()
    assert r["extraction"]["meta"]["model"] == "stub-manual"
    m = (await client.get("/api/v1/models")).json()
    assert m["requirement_extract_model"] == "stub-extract"
    monkeypatch.delenv("TIANGONG_REQUIREMENT_EXTRACT_MODEL"); get_settings.cache_clear()


def test_结构分层切单元_按文件与页面切_不切在页面中间_小页打包_大页再切():
    from app.agents.extraction import structure_units

    p1 = "## 页面：活动详情页\n" + "\n".join(f"规则 {i}：报名费 {i} 元" for i in range(30))
    p2 = "## 页面：活动入口\n### 游戏内入口\n" + "\n".join(f"入口规则 {i}：固定位置" for i in range(30))
    big = "## 页面：规则说明\n" + "\n".join(f"第 {i} 条规则说明文字很长很长" for i in range(200))
    materials = "需求原文一句话\n\n【文件：连赢挑战.zip】\n# Axure 原型：连赢挑战.zip\n" + p1 + "\n" + p2 + "\n" + big + "\n\n【文件：补充.docx】\n补充说明一句"
    units = structure_units(materials, 1200)
    paths = [u["pages"] for u in units]
    assert paths[0] == ["需求原文"]
    # 同一文件的小页面打包进一次调用，路径带文件与页面
    packed = next(u for u in units if "连赢挑战.zip › Axure 原型：连赢挑战.zip › 页面：活动详情页" in u["pages"])
    assert "连赢挑战.zip › Axure 原型：连赢挑战.zip › 页面：活动入口" in packed["pages"] and "### 游戏内入口" in packed["text"]
    # 超限的页面按段再切，路径带第 k 段；不同文件不混在一次调用里
    segs = [p for u in units for p in u["pages"] if "页面：规则说明（第" in p]
    assert len(segs) >= 2 and all(u["path"] == "补充.docx" for u in units if "补充.docx" in u["pages"])
    assert all(len(u["text"]) <= 1200 for u in units)


def test_未覆盖需求句_剔除外观与样例_整理稿覆盖的不算():
    from app.agents.extraction import uncovered_lines

    materials = ("【文件：原型.zip】\n# 原型\n## 页面：报名\n- 选择次日后，当日将无法再次报名\n- 报名生效日期提供两个选项：今日、次日\n"
                 "- 状态：可见，紫色填充样式\n- 按钮颜色为蓝色且右对齐显示\n- GC1826209540\n- 时间：2026-08-10 15:14:01\n"
                 "（包含：用户昨日连赢数、昨日积分，无对应数据时对应数据项显示 0）\n")
    text = "## 报名-日期\n- 报名生效日期提供两个选项：今日、次日\n- 我的昨日战绩包含用户昨日连赢数、昨日积分"
    missing, stats = uncovered_lines(materials, text)
    lines = [m["line"] for m in missing]
    assert "- 选择次日后，当日将无法再次报名" in lines and all(m["path"] == "原型.zip › 原型 › 页面：报名" for m in missing)
    assert any("无对应数据时对应数据项显示 0" in l for l in lines)       # 被改写丢掉句尾的那句算未覆盖
    assert not any("紫色" in l or "蓝色" in l or "GC1826" in l for l in lines)   # 外观与样例数据不算需求句
    assert stats == {"lines": 3, "covered": 1}


GAP_MATERIAL = ("## 页面：报名\n" + "\n".join(
    f"- 第 {i} 条报名规则：报名费满 {i} 元后当日将无法再次报名并退回到活动详情页面" for i in range(1, 16)))
GAP_FIRST = json.dumps({"modules": [{"name": "报名-规则", "items": [
    {"text": "第 1 条报名规则：报名费满 1 元后当日将无法再次报名并退回到活动详情页面", "sources": ["原型.zip › 报名"]}]}], "dropped": []}, ensure_ascii=False)
GAP_REPLY = json.dumps({"add": [
    {"module": "报名-规则", "text": "- 第 2 条报名规则：报名费满 2 元后当日将无法再次报名并退回到活动详情页面", "sources": ["原型.zip › 报名"]},
    {"module": "报名-新区块", "text": "第 3 条报名规则：报名费满 3 元后当日将无法再次报名并退回到活动详情页面"},
    {"module": "报名-规则", "text": "模型自己编的一句不在材料里的规则内容", "sources": []},
], "skip": [{"text": "第 4 条", "reason": "重复"}]}, ensure_ascii=False)


async def test_自动补漏_未覆盖句送回模型_只认原句_标补漏_覆盖率入meta(client, extract_on):
    await client.post("/api/v1/projects", json={"name": "P"})
    app.state.llm = StubLLM([GAP_FIRST, GAP_REPLY, json.dumps({"add": [], "skip": []})])
    r = (await client.post("/api/v1/requirements", data={"project": "P", "title": "报名"},
                           files=[("files", ("原型.txt", GAP_MATERIAL.encode(), "text/plain"))])).json()
    rid = r["req_id"]
    r = await _wait_parsed(client, rid)
    for _ in range(200):
        if r["extraction_status"] == "current":
            break
        await asyncio.sleep(0.02)
        r = (await client.get(f"/api/v1/requirements/{rid}")).json()
    m = r["extraction"]["meta"]
    assert m["gap"]["rounds"] >= 1 and m["gap"]["added"] == 2 and m["gap"]["skipped"] == 1
    assert m["gap"]["before"]["covered"] == 1 and m["gap"]["after"]["covered"] == 3 and m["gap"]["after"]["lines"] == 15
    text = r["extraction"]["text"]
    assert "第 2 条报名规则" in text and "〔补漏〕" in text and "## 报名-新区块" in text and "模型自己编的" not in text
    gap_call = app.state.llm.calls[1]["messages"][1]["content"]
    assert "未覆盖原句" in gap_call and "第 5 条报名规则" in gap_call and "- 报名-规则" in gap_call
    # 逐条结构：每条有 id、verified；补漏条目 gap=True
    mods = r["extraction"]["modules"]
    items = [it for mod in mods for it in mod["items"]]
    assert len(items) == 3 and all(it["id"] and it["verified"] for it in items) and sum(1 for it in items if it.get("gap")) == 2


async def test_逐条审核_核对修改删除_审核完成_版本历史对比恢复(client, extract_on):
    await client.post("/api/v1/projects", json={"name": "P"})
    app.state.llm = StubLLM([EXTRACT_REPLY])
    r = (await client.post("/api/v1/requirements", data={"project": "P", "title": "活动后台"},
                           files=[("files", ("原型.txt", NOISY.encode(), "text/plain"))])).json()
    rid = r["req_id"]
    r = await _wait_parsed(client, rid)
    for _ in range(200):
        if r["extraction_status"] == "current":
            break
        await asyncio.sleep(0.02)
        r = (await client.get(f"/api/v1/requirements/{rid}")).json()
    items = {it["text"]: it for mod in r["extraction"]["modules"] for it in mod["items"]}
    bad = items["活动删除后不可恢复"]; assert bad["verified"] is False
    # 审核完成前提：⚠ 条目必须处理
    resp = await client.post(f"/api/v1/requirements/{rid}/extraction/review-done")
    assert resp.status_code == 400 and "1 条" in resp.json()["detail"]
    # 核对通过
    r = (await client.post(f"/api/v1/requirements/{rid}/extraction/items", json={"item_id": items["活动状态分为未开始、进行中、已结束"]["id"], "action": "ok"})).json()
    assert r["extraction"]["meta"]["rev"] == 1
    # 修改：改成材料里有的句子 → 重新核验通过，⚠ 消失
    r = (await client.post(f"/api/v1/requirements/{rid}/extraction/items", json={"item_id": bad["id"], "action": "edit", "text": "活动名称最长 30 个字符"})).json()
    it = next(x for mod in r["extraction"]["modules"] for x in mod["items"] if x["id"] == bad["id"])
    assert it["verified"] is True and it["review"]["status"] == "edited" and "⚠" not in r["extraction"]["text"]
    assert r["extraction"]["meta"]["unverified"] == 0 and r["extraction"]["meta"]["rev"] == 2
    # 删除
    r = (await client.post(f"/api/v1/requirements/{rid}/extraction/items", json={"item_id": bad["id"], "action": "delete"})).json()
    assert r["extraction"]["meta"]["checked"] == 2 and "活动名称最长 30 个字符\n- 活动名称" not in r["extraction"]["text"]
    assert (await client.post(f"/api/v1/requirements/{rid}/extraction/items", json={"item_id": bad["id"], "action": "ok"})).status_code == 400
    # 审核完成
    r = (await client.post(f"/api/v1/requirements/{rid}/extraction/review-done")).json()
    assert r["extraction"]["reviewed"]["by"] and r["extraction"]["reviewed"]["rev"] == 3
    # 分析后逐条改动 → 分析过期、审核标记作废
    app.state.llm = StubLLM([ANALYSIS_REPLY])
    a = (await client.post(f"/api/v1/requirements/{rid}/analyze", json={})).json()
    assert a["analysis_outdated"] is False
    r = (await client.post(f"/api/v1/requirements/{rid}/extraction/items", json={"item_id": items["活动状态分为未开始、进行中、已结束"]["id"], "action": "delete"})).json()
    assert r["analysis_outdated"] is True and r["extraction"]["reviewed"] is None
    # 版本历史：重新提取后旧版进历史，可看差异、可恢复
    app.state.llm = StubLLM([EXTRACT_REPLY])
    r = (await client.post(f"/api/v1/requirements/{rid}/extract", json={})).json()
    assert r["extraction_history_count"] == 1
    h = (await client.get(f"/api/v1/requirements/{rid}/extraction/history")).json()
    assert h["current"]["checked"] == 3 and len(h["history"]) == 1 and h["history"][0]["checked"] == 1 and h["history"][0]["index"] == 0
    v = (await client.get(f"/api/v1/requirements/{rid}/extraction/history/0")).json()
    assert "活动名称最长 30 个字符" in v["text"] and any("活动删除后不可恢复" in l for l in v["diff"]["added"])
    assert (await client.get(f"/api/v1/requirements/{rid}/extraction/history/5")).status_code == 404
    r = (await client.post(f"/api/v1/requirements/{rid}/extraction/restore", json={"index": 0})).json()
    assert r["extraction"]["meta"]["checked"] == 1 and r["extraction"]["meta"]["restored_from"] and r["extraction_history_count"] == 2
    assert r["analysis_outdated"] is True
    # 整篇改写后逐条结构失效
    r = (await client.put(f"/api/v1/requirements/{rid}/extraction", json={"text": "## 活动后台\n- 活动名称最长 30 个字符"})).json()
    assert r["extraction"]["modules"] is None
    assert (await client.post(f"/api/v1/requirements/{rid}/extraction/items", json={"item_id": "x", "action": "ok"})).status_code == 400

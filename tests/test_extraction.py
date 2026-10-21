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


# 补漏审核：NOISY 里首尾的导航与版权行第一轮没进整理稿，程序把它们送回模型逐句判定，模型判「不是需求」并给原因
NOISY_GAP = json.dumps({"add": [], "skip": [{"text": "首页 | 活动管理 | 用户管理 | 退出", "reason": "页面导航"},
                                           {"text": "版权所有 © 2026", "reason": "版权信息"},
                                           {"text": "活动列表页面：活动状态分为未开始、进行中、已结束。", "reason": "重复（只差页面名前缀）"}]}, ensure_ascii=False)
NO_GAP = json.dumps({"add": [], "skip": []})


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
    assert md.startswith("## 1. b.html 〔来源：b.html〕\n\n### 1.1 活动后台-活动列表\n- 活动状态分为未开始、进行中、已结束 〔来源：a.docx〕\n- 活动名称最长 30 个字符\n")
    assert "## 2. 活动后台\n\n### 2.1 数据统计\n- ⚠ 按天统计参与人数" in md and md.endswith("（已剔除：页面导航、css）")   # 没来源的模块按名字前半归页


async def test_附件解析完成自动提取_分析与设计以整理稿为准_修改整理稿分析标过期(client, extract_on):
    await client.post("/api/v1/projects", json={"name": "P"})
    app.state.llm = StubLLM([EXTRACT_REPLY, NOISY_GAP, ANALYSIS_REPLY])
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
    # 底稿：材料 5 行去重成 4 句（导航出现两次只记一句）全有去向——1 句整句收录；导航、版权、只差页面名前缀的那句（整理稿命中 71%，不足 90% 不算收录）由模型排除并记原因；待处理 0
    led = r["extraction"]["ledger"]["stats"]
    assert led["total"] == 4 and led["in"] == 1 and led["ex"] == 3 and led["todo"] == 0 and led["ex_by"]["model"] == 3 and led["review"] == 0
    assert "〔整理稿已有其中 71%" in app.state.llm.calls[1]["messages"][1]["content"]
    # 提取 Prompt 收到的是全部材料（原文 + 附件文本），最高推理强度
    call = app.state.llm.calls[0]
    assert call["reasoning_effort"] == "medium" and "需求材料" in call["messages"][1]["content"] and "版权所有" in call["messages"][1]["content"]
    # 分析输入 = 整理稿，不再是原始材料
    a = (await client.post(f"/api/v1/requirements/{rid}/analyze", json={})).json()
    user = app.state.llm.calls[2]["messages"][1]["content"]
    assert "【需求整理稿】" in user and "活动状态分为未开始、进行中、已结束" in user and "版权所有" not in user
    assert a["analysis"] and a["analysis_outdated"] is False
    item = app.state.requirements.get(rid)
    assert design_brief(item).startswith("【需求整理稿】") and "首页 | 活动管理" not in design_brief(item)
    # 人工修改整理稿 → 分析标过期；设计上下文用修改后的文本
    r = (await client.put(f"/api/v1/requirements/{rid}/extraction", json={"text": "## 活动后台-活动列表\n- 活动名称最长 30 个字符"})).json()
    assert r["extraction"]["edited"]["by"] and r["analysis_outdated"] is True and r["extraction_status"] == "current"
    assert "人工修改过" in design_brief(app.state.requirements.get(rid))
    assert (await client.put(f"/api/v1/requirements/{rid}/extraction", json={"text": "  "})).status_code == 400
    # 追加附件 → 整理稿过期；自动重新提取（材料两个文件 → 两次并行摘录 → 补漏两轮；新附件那句模型没判，程序兜底收录）
    app.state.llm = StubLLM([EXTRACT_REPLY, json.dumps({"modules": []}), NOISY_GAP, NO_GAP])
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
    assert "活动最多同时进行 5 个 〔兜底〕" in r["extraction"]["text"] and r["extraction"]["meta"]["gap"]["forced"] == 1
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
    app.state.llm = StubLLM([EXTRACT_REPLY, NOISY_GAP])
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
    assert all(c["model"] == "stub-extract" for c in app.state.llm.calls)   # 没指定模型时提取与补漏都用配置的提取模型，不跟默认模型降档
    rows = calllog.list_calls(requirement_id=rid)["items"]
    assert rows and all(x["project"] == "P" for x in rows) and {x["purpose"] for x in rows} == {"需求提取（分析前置）", "需求提取补漏"}   # 自动提取也能按需求筛到
    # 手动提取页面选了模型时以页面为准
    app.state.llm = StubLLM([EXTRACT_REPLY, NOISY_GAP])
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
    {"uid": "F1-0005", "module": "报名-规则", "text": "模型改写过的第 5 条", "sources": []},        # 带编号但改了写法 → 按原句收
    {"module": "报名-规则", "text": "模型自己编的一句不在材料里的规则内容", "sources": []},         # 没编号也对不上原句 → 不收
], "skip": [{"text": "第 4 条报名规则：报名费满 4 元后当日将无法再次报名并退回到活动详情页面", "reason": "重复"},
            {"uid": "F1-0006", "reason": "样例"}, {"text": "第 7 条", "reason": "对不上原句的概括，不算判定"}]}, ensure_ascii=False)


async def test_自动补漏_未覆盖句送回模型_只认原句_标补漏_覆盖率入meta(client, extract_on):
    await client.post("/api/v1/projects", json={"name": "P"})
    app.state.llm = StubLLM([GAP_FIRST, GAP_REPLY, NO_GAP])
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
    assert m["gap"]["rounds"] == 2 and m["gap"]["added"] == 3 and m["gap"]["skipped"] == 2 and m["gap"]["forced"] == 9
    # 15 句：1 句首轮摘录 + 3 句补漏 + 9 句模型没判定、程序按原句兜底 = 13 句收录；2 句模型排除；待处理 0
    assert m["gap"]["before"]["covered"] == 1 and m["gap"]["after"]["covered"] == 13 and m["gap"]["after"]["lines"] == 15
    led = r["extraction"]["ledger"]["stats"]
    assert led == {"total": 15, "in": 13, "ex": 2, "todo": 0, "review": 2, "review_open": 2, "ex_by": {"model": 2, "auto": 0, "user": 0}}   # 模型排除的两句像规则 → 需复核
    text = r["extraction"]["text"]
    assert "第 2 条报名规则" in text and "〔补漏〕" in text and "### 1.2 新区块" in text and "模型自己编的" not in text   # 模块「报名-新区块」归到「## 1. 报名」下
    assert "模型改写过的" not in text and "- 第 5 条报名规则：报名费满 5 元后当日将无法再次报名并退回到活动详情页面 〔补漏〕" in text
    assert "第 4 条报名规则" not in text and "第 6 条报名规则" not in text and "第 7 条报名规则：报名费满 7 元后当日将无法再次报名并退回到活动详情页面 〔兜底〕" in text
    gap_call = app.state.llm.calls[1]["messages"][1]["content"]
    assert "未覆盖原句" in gap_call and "[F1-0005] 第 5 条报名规则" in gap_call and "- 报名-规则" in gap_call and "每一句都必须出现在 add 或 skip 里" in gap_call
    assert "第 1 条报名规则" not in gap_call.split("未覆盖原句")[1]      # 已收录的不再送
    # 逐条结构：每条有 id、verified；补漏条目 gap=True，兜底条目 forced=True 且记单元编号
    mods = r["extraction"]["modules"]
    items = [it for mod in mods for it in mod["items"]]
    assert len(items) == 13 and all(it["id"] and it["verified"] for it in items) and sum(1 for it in items if it.get("gap")) == 12
    forced = [it for it in items if it.get("forced")]
    assert len(forced) == 9 and all(it["unit"].startswith("F1-") for it in forced) and {mod["name"] for mod in mods} >= {"报名-待归类"}


async def test_逐条审核_核对修改删除_审核完成_版本历史对比恢复(client, extract_on):
    await client.post("/api/v1/projects", json={"name": "P"})
    app.state.llm = StubLLM([EXTRACT_REPLY, NOISY_GAP])
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
    app.state.llm = StubLLM([EXTRACT_REPLY, NOISY_GAP])
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


async def test_补漏调用日志记为需求提取补漏(client, extract_on):
    from app.llm import calllog

    await client.post("/api/v1/projects", json={"name": "P"})
    app.state.llm = StubLLM([GAP_FIRST, GAP_REPLY, NO_GAP])
    r = (await client.post("/api/v1/requirements", data={"project": "P", "title": "报名"},
                           files=[("files", ("原型.txt", GAP_MATERIAL.encode(), "text/plain"))])).json()
    rid = r["req_id"]
    r = await _wait_parsed(client, rid)
    for _ in range(200):
        if r["extraction_status"] == "current":
            break
        await asyncio.sleep(0.02)
        r = (await client.get(f"/api/v1/requirements/{rid}")).json()
    purposes = sorted(x["purpose"] for x in calllog.list_calls(requirement_id=rid)["items"])
    assert purposes == ["需求提取补漏", "需求提取补漏", "需求提取（分析前置）"]


def test_数字运算符条目整句严格核验_条件反转与数值替换标警告():
    """普通规范化会把 ≤ 和 > 都当标点去掉，「≤0 正常结算」改成「>0 正常结算」照样通过；含数字 / 运算符的条目必须整句逐字且运算符一致。"""
    from app.agents.analysis import normalize
    from app.agents.extraction import strict_normalize, verify_modules, verify_text

    mat = ("四、机器人占位规则\n先判断未覆盖基础奖金是否≤0，是则正常结算；否则增加机器人占位。\n初始暂定75%\n导出间隔至少 1 分钟\n"
           "取消第一名的特殊机制，改为与其他排名一致\n报名费计入奖池比例必填，范围 1～100")
    n, st = normalize(mat), strict_normalize(mat)
    ok = ["未覆盖基础奖金是否≤0，是则正常结算", "初始暂定75%", "导出间隔至少1分钟", "报名费计入奖池比例必填，范围 1~100", "改为与其他排名一致"]
    bad = ["未覆盖基础奖金是否>0，是则正常结算", "未覆盖基础奖金是否＞0，是则正常结算", "初始暂定35%", "导出间隔大于 1 分钟", "取消第一次的特殊机制", "范围 1～10"]
    assert all(verify_text(t, n, st) for t in ok) and not any(verify_text(t, n, st) for t in bad)
    assert verify_text("未覆盖基础奖金是否>0，是则正常结算", n)     # 旧规则（不传严格串）抓不住：这正是要堵的洞
    # 不含数字的条目仍允许分段 ≥70% 命中（模型去掉序号 / 拆行）
    assert verify_text("取消第一名的特殊机制，改为与其他排名一致", n, st) and verify_text("先判断未覆盖基础奖金是否≤0，是则正常结算；否则增加机器人占位", n, st)
    mods = [{"name": "机器人", "items": [{"text": "未覆盖基础奖金是否≤0，是则正常结算"}, {"text": "未覆盖基础奖金是否>0，是则正常结算"}, {"text": "初始暂定35%"}]}]
    assert verify_modules(mods, mat) == {"checked": 3, "verified": 1, "unverified": 2}


def test_底稿单元_编号与来源_程序规则只判纯样例():
    from app.agents.ledger import AUTO_SAMPLE, account, build_units

    materials = ("需求原文一句话：密码错误 5 次锁定 30 分钟\n\n【文件：原型.zip】\n# Axure 原型：原型.zip\n## 页面：报名\n"
                 "[u12] 查询功能说明如下：\n[u12] 活动ID\n[u15] 剩余应占位金额 ≤ 0，则不再需要占位\n[u15] 第二段：结余部分计入第二天\n"
                 "38,179.80\nposition:absolute;\nThe user must login before checkout\n[u19] 查询功能说明如下：\n[[图片:1]]\n"
                 "【文档内图片理解 [[图片:1]]】\n# 界面元素清单\n- 底部有 × 按钮和刷新按钮\n## 查询条件区\n- 日期范围控件\n# 图中文字原文\n- 报名后当日不可撤销，次日生效\n【图片理解结束 [[图片:1]]】\n"
                 "## 页面：规则\n[u30] 月榜前三名额外奖励\n")
    units = build_units(materials)
    rows = {u["uid"]: u for u in units}
    # 图片理解块里只有「图中文字原文」段算单元（kind=image，排在原文单元之后）；块内子标题不动路径；同文控件行去重记次数
    assert [u["uid"] for u in units] == ["F0-0001", "u12", "u12.2", "u15", "u15.2", "F1-0005", "F1-0006", "F1-0007", "u30", "F1-0009"]
    assert rows["u12"]["count"] == 2 and rows["u12"]["uids"] == ["u12", "u19"] and rows["u30"]["path"] == "原型.zip › Axure 原型：原型.zip › 页面：规则"
    assert rows["F1-0009"]["kind"] == "image" and rows["F1-0009"]["text"] == "报名后当日不可撤销，次日生效" and rows["u30"]["kind"] == "source"
    assert rows["F1-0009"]["image"] == 1 and rows["F1-0009"]["path"] == "原型.zip › Axure 原型：原型.zip › 页面：报名" and "image" not in rows["u30"]   # 图中文字记是哪一页的第几张图
    assert rows["F0-0001"]["path"] == "需求原文" and rows["u15"]["path"] == "原型.zip › Axure 原型：原型.zip › 页面：报名"
    assert rows["u15"]["text"] == "剩余应占位金额 ≤ 0，则不再需要占位"   # 去掉编号
    assert rows["u12"]["auto"] is None and rows["u12.2"]["auto"] is None and rows["F1-0005"]["auto"] == AUTO_SAMPLE   # 小标题与短标签「活动ID」都交模型判
    assert rows["F1-0006"]["auto"] == AUTO_SAMPLE and rows["F1-0007"]["auto"] is None     # 英文整句交模型判
    assert rows["u15"]["auto"] is None and rows["u30"]["auto"] is None
    acct = account(units, "## 报名\n- 密码错误 5 次锁定 30 分钟\n- 剩余应占位金额 ≤ 0\n- 则不再需要占位",
                   {"u15.2": {"r": "过渡句", "by": "model"}, "u30": {"r": "样例", "by": "model", "checked": {"by": "lisi"}},
                    "F1-0007": {"r": "脱离上下文的片段", "by": "model"}})
    rows = {u["uid"]: u for u in acct["units"]}
    st = {k: v["status"] for k, v in rows.items()}
    assert st["u15"] == "in"                                       # 拆成两条但按段全部命中也算收录
    assert st["F0-0001"] == "todo" and rows["F0-0001"]["partial"] == 0.63   # 只收了后半句（63% < 90%）：待处理，带已覆盖比例提示模型补全
    assert st["u12"] == "todo" and st["u12.2"] == "todo" and st["F1-0005"] == "ex" and st["u15.2"] == "ex" and st["u30"] == "ex"   # 小标题与短标签待模型判
    assert st["F1-0007"] == "ex" and st["F1-0009"] == "todo"
    # 模型排除里像规则的句子标需复核：u15.2（含「第二天」）未看、u30（含数字）已确认；英文句（无汉字）即使理由是「片段」也不标
    assert rows["u15.2"]["review"] is True and "checked" not in rows["u15.2"] and rows["u30"]["review"] is True and rows["u30"]["checked"]["by"] == "lisi"
    assert "review" not in rows["F1-0007"]
    assert acct["stats"] == {"total": 9, "in": 1, "ex": 5, "todo": 3, "review": 2, "review_open": 1, "ex_by": {"model": 3, "auto": 2, "user": 0}}
    assert acct["image"] == {"total": 1, "in": 0, "ex": 0, "todo": 1, "review": 0, "review_open": 0, "ex_by": {"model": 0, "auto": 0, "user": 0}}
    assert rows["u15.2"]["reason"] == "过渡句" and rows["u15.2"]["by"] == "model" and rows["F1-0005"]["by"] == "auto"
    # 流程图半句（无数字无条件词）被模型以「片段」为由排除 → 也要进需复核
    from app.agents.ledger import needs_review
    assert needs_review("覆盖基础奖金成本中", "脱离上下文的片段") and not needs_review("覆盖基础奖金成本中", "外观描述") and not needs_review("是否", "片段")


async def test_底稿核对_每句有去向_人工收录与排除_待处理不能完成审核(client, extract_on):
    await client.post("/api/v1/projects", json={"name": "P"})
    app.state.llm = StubLLM([GAP_FIRST, GAP_REPLY, NO_GAP])
    r = (await client.post("/api/v1/requirements", data={"project": "P", "title": "报名"},
                           files=[("files", ("原型.txt", GAP_MATERIAL.encode(), "text/plain"))])).json()
    rid = r["req_id"]
    r = await _wait_parsed(client, rid)
    for _ in range(200):
        if r["extraction_status"] == "current":
            break
        await asyncio.sleep(0.02)
        r = (await client.get(f"/api/v1/requirements/{rid}")).json()
    assert "decisions" not in r["extraction"]["ledger"]          # 详情只带统计，明细走底稿接口
    # 附件带原型控件索引与自校验结果（Axure 解析写入）：底稿页按编号给每句挂控件类型 / 坐标 / 隐藏态，并列出各包自校验
    att = app.state.requirements.get(rid)["attachments"][0]
    att["controls"] = {"F1-0001": {"cls": "flow_shape", "x": 10, "y": 100, "hidden": True}}
    att["check"] = {"ok": True, "missing": 0, "pages": [{"page": "报名", "missing": 0}]}
    led = (await client.get(f"/api/v1/requirements/{rid}/extraction/ledger")).json()
    assert led["stats"]["todo"] == 0 and len(led["pages"]) == 1 and led["pages"][0]["path"] == "原型.txt › 页面：报名"
    assert led["checks"] == [{"filename": "原型.txt", "ok": True, "missing": 0, "pages": 1}]
    units = {u["uid"]: u for u in led["pages"][0]["units"]}
    assert units["F1-0001"]["ctl"] == {"cls": "flow_shape", "x": 10, "y": 100, "hidden": True} and "ctl" not in units["F1-0002"]
    assert len(units) == 15 and units["F1-0001"]["status"] == "in" and not units["F1-0001"].get("forced")
    u4 = units["F1-0004"]
    assert (u4["path"], u4["status"], u4["reason"], u4["by"], u4["review"], u4["count"], u4["kind"]) == ("原型.txt › 页面：报名", "ex", "重复", "model", True, 1, "source")
    assert u4["text"] == "第 4 条报名规则：报名费满 4 元后当日将无法再次报名并退回到活动详情页面" and "checked" not in u4
    assert units["F1-0007"]["status"] == "in" and units["F1-0007"]["forced"] is True
    # 兜底收录的可以人工排除：条目撤掉、记人工原因
    r = (await client.post(f"/api/v1/requirements/{rid}/extraction/ledger", json={"uid": "F1-0007", "action": "exclude", "reason": "页面提示"})).json()
    assert "第 7 条报名规则" not in r["extraction"]["text"] and r["extraction"]["meta"]["rev"] == 1
    assert r["extraction"]["ledger"]["stats"] == {"total": 15, "in": 12, "ex": 3, "todo": 0, "review": 2, "review_open": 2, "ex_by": {"model": 2, "auto": 0, "user": 1}}
    # 模型排除的可以人工翻回收录：按原句进「页面-人工收录」
    r = (await client.post(f"/api/v1/requirements/{rid}/extraction/ledger", json={"uid": "F1-0004", "action": "include"})).json()
    assert "人工收录\n" in r["extraction"]["text"] and "- 第 4 条报名规则：报名费满 4 元后当日将无法再次报名并退回到活动详情页面" in r["extraction"]["text"]
    it = next(x for m in r["extraction"]["modules"] for x in m["items"] if x.get("unit") == "F1-0004")
    assert it["manual"] is True and it["verified"] is True and it["review"]["status"] == "included" and it["id"]
    assert r["extraction"]["ledger"]["stats"]["in"] == 13 and r["extraction"]["ledger"]["stats"]["ex_by"]["model"] == 1 and r["extraction"]["ledger"]["stats"]["review_open"] == 1
    # 整理稿里本来就逐字有的句子不能排除
    resp = await client.post(f"/api/v1/requirements/{rid}/extraction/ledger", json={"uid": "F1-0001", "action": "exclude"})
    assert resp.status_code == 400 and "不能排除" in resp.json()["detail"]
    assert (await client.post(f"/api/v1/requirements/{rid}/extraction/ledger", json={"uid": "no-such", "action": "include"})).status_code == 400
    # 删掉一条摘录后对应单元回到待处理 → 完成审核被拦；底稿页处理后可完成
    r = (await client.post(f"/api/v1/requirements/{rid}/extraction/items", json={"item_id": it["id"], "action": "delete"})).json()
    assert r["extraction"]["ledger"]["stats"]["todo"] == 1
    resp = await client.post(f"/api/v1/requirements/{rid}/extraction/review-done")
    assert resp.status_code == 400 and "1 句" in resp.json()["detail"]
    led = (await client.get(f"/api/v1/requirements/{rid}/extraction/ledger")).json()
    assert next(u for u in led["pages"][0]["units"] if u["uid"] == "F1-0004")["status"] == "todo"
    r = (await client.post(f"/api/v1/requirements/{rid}/extraction/ledger", json={"uid": "F1-0004", "action": "exclude"})).json()
    assert r["extraction"]["ledger"]["stats"]["todo"] == 0
    # 需复核的模型排除句（F1-0006）没看过也不能完成审核；确认后可以
    resp = await client.post(f"/api/v1/requirements/{rid}/extraction/review-done")
    assert resp.status_code == 400 and "1 句模型排除的需复核句" in resp.json()["detail"]
    r = (await client.post(f"/api/v1/requirements/{rid}/extraction/ledger", json={"uid": "F1-0006", "action": "confirm"})).json()
    assert r["extraction"]["ledger"]["stats"]["review_open"] == 0 and r["extraction"]["meta"]["rev"] == 4   # 确认不改整理稿、不加 rev
    led = (await client.get(f"/api/v1/requirements/{rid}/extraction/ledger")).json()
    assert next(u for u in led["pages"][0]["units"] if u["uid"] == "F1-0006")["checked"]["by"]
    assert (await client.post(f"/api/v1/requirements/{rid}/extraction/ledger", json={"uid": "F1-0001", "action": "confirm"})).status_code == 400
    r = (await client.post(f"/api/v1/requirements/{rid}/extraction/review-done")).json()
    assert r["extraction"]["reviewed"]["by"]
    assert r["analysis_outdated"] is False or r["analysis"] is None
    h = (await client.get(f"/api/v1/requirements/{rid}/extraction/history")).json()
    assert h["current"]["ledger"]["total"] == 15


def test_疑点字段_原句照抄疑点单列_整理稿末尾疑点清单():
    """《Axure 需求提取流程》：需求语句逐字保留只做归类，错别字 / 口径不一致 / 占位符这类疑点单列，不改原文。"""
    out = ({"modules": [{"name": "报名-规则", "items": [
        {"text": "报名费满 4 元后当日将无法再次报名", "sources": ["a.zip › 报名"], "doubt": " 前台写的是 5 元，  前后台口径不一致 "},
        {"text": "活动名称最长 30 个字符", "sources": ["a.zip › 报名"], "doubt": ""}]}], "dropped": []}, None)
    modules, _, _ = _merge_modules([out])
    items = modules[0]["items"]
    assert items[0]["doubt"] == "前台写的是 5 元， 前后台口径不一致" and "doubt" not in items[1]   # 空疑点不带字段
    md = extraction_markdown(modules)
    assert "- 报名费满 4 元后当日将无法再次报名 〔疑点：前台写的是 5 元， 前后台口径不一致〕\n- 活动名称最长 30 个字符\n" in md
    assert md.endswith("## 2. 疑点 / 不一致清单 〔原句已按原文保留在上文，此处只列疑点，供需求澄清与用例设计〕\n\n"
                       "- 报名费满 4 元后当日将无法再次报名 —— 前台写的是 5 元， 前后台口径不一致（报名-规则）")
    assert "疑点" not in extraction_markdown([{"name": "m", "items": [items[1]]}])   # 没有疑点就没有清单
    # 底稿核算：带疑点标记的条目行仍按原句对回单元
    from app.agents.ledger import covered
    assert covered("报名费满 4 元后当日将无法再次报名", md)


def test_底稿收录_整理稿去掉或改写了行首序号也算命中():
    """终验口径（《Axure 需求提取流程》8.3）：原文「1、」整理稿写成「1.」或去掉序号，不算丢。"""
    from app.agents.ledger import covered

    draft = "## 规则\n- 报名费满 4 元后当日将无法再次报名\n- 2. 次日 0 点重置连赢次数\n- 月榜前三名额外奖励"
    assert covered("1、报名费满 4 元后当日将无法再次报名", draft) and covered("注：1、报名费满 4 元后当日将无法再次报名", draft)
    assert covered("2、次日 0 点重置连赢次数", draft) and covered("③ 月榜前三名额外奖励", draft) and covered("三、月榜前三名额外奖励", draft)
    assert not covered("1、报名费满 5 元后当日将无法再次报名", draft)   # 内容不同仍不算
    assert not covered("1、月榜", draft)                                   # 去掉序号后太短的不按子串判


def test_底稿去重与收录按严格口径_运算符与括号算字_表格行按单元格切段():
    """对照《连赢挑战赛_需求提取》复盘：底稿去重曾用去标点的 normalize 当键，「输赢金额=0」「输赢金额<0」被并进「输赢金额>0」，
    「（今日）」并进「今日」，三条判定规则只剩一条且算「已收录」。现在去重键逐字（只去空白），带数字 / 运算符的单元按严格
    规范化比；表格行「A ｜ B」按单元格切段；没有汉字但带区间 / 比较符的格式示例不再由程序判样例；短标签交模型判。"""
    from app.agents.ledger import AUTO_SAMPLE, account, build_units, covered

    materials = ("【文件：原型.zip】\n# Axure 原型：原型.zip\n## 页面：需求说明\n[u39] 本局游戏结果 ｜ 系统计算方式\n"
                 "[u41] 输赢金额>0 ｜ 当前连赢次数“+1”\n[u43] 输赢金额=0 ｜ 当前连赢次数“不变”\n[u45] 输赢金额<0 ｜ 当前连赢次数“清零”\n"
                 "[u48] 该用户当局输赢金额是否>0\n[u54] 该用户当局输赢金额是否=0\n[u341] 今日\n[u1482] （今日）\n"
                 "[u422] 连赢-奖励\n[u1501] 改为直接报名\n[u339] UTC+8 9.21 00:00:00～23:59:59\n[u355] 6/10\n[u64] 3局 ｜ +1分\n[u66] 4局 ｜ +3分\n")
    units = build_units(materials)
    rows = {u["uid"]: u for u in units}
    assert [u["uid"] for u in units] == ["u39", "u41", "u43", "u45", "u48", "u54", "u341", "u1482", "u422", "u1501", "u339", "u355", "u64", "u66"]
    assert rows["u41"]["count"] == 1 and rows["u341"]["count"] == 1   # 不再合并
    assert rows["u422"]["auto"] is None and rows["u1501"]["auto"] is None   # 交易类型名 / 按钮名不由程序排除
    assert rows["u339"]["auto"] is None and rows["u355"]["auto"] == AUTO_SAMPLE   # 带区间符的时间格式示例交模型；纯数字仍是样例
    draft = ("## 需求说明-连赢判断规则\n- 输赢金额>0 ｜ 当前连赢次数“+1”\n- 该用户当局输赢金额是否>0\n- 今日\n"
             "- 输赢金额<0\n- 当前连赢次数“清零”\n- 3局\n- +1分\n")
    acct = {u["uid"]: u for u in account(units, draft)["units"]}
    assert acct["u41"]["status"] == "in" and acct["u48"]["status"] == "in"
    assert acct["u43"]["status"] == "todo" and acct["u54"]["status"] == "todo"   # =0 不能靠 >0 过关
    assert acct["u45"]["status"] == "in" and acct["u64"]["status"] == "in" and acct["u66"]["status"] == "todo"   # 表格行拆成两条：按单元格切段（不设最短字数）都命中也算收录
    assert acct["u341"]["status"] == "in" and acct["u1482"]["status"] == "in"   # 「（今日）」短句整条等于摘录「今日」（只差括号）
    assert covered("输赢金额=0 ｜ 当前连赢次数“不变”", draft) is False and covered("输赢金额>0 ｜ 当前连赢次数“+1”", draft) is True


def test_整理稿层级_页面区块条目_表格还原_子标题与子项_图片文案单列_文头():
    """按《连赢挑战赛_需求提取》的层级成稿：## N. 页面（模块主来源）› ### N.M 区块（去掉「页面-」前缀）› 条目；连续的表格行
    还原成 Markdown 表格（表头第一行、单元格带标记）；以冒号结尾的短句加粗成子标题；「1、」「注：」开头的子项缩进一级；
    只在图片里的文案归最后一节；文头带标题 / 来源 / 校验。底稿核算跳过文头、表格行与单元格都算条目。"""
    from app.agents.extraction import assign_ids, extraction_markdown, extraction_outline, outline_view
    from app.agents.ledger import account, build_units

    src = "连赢挑战.zip › 需求说明"
    modules = [
        {"name": "需求说明-连赢判断规则", "items": [
            {"text": "连赢判断规则如下表所示：", "sources": [src]},
            {"text": "本局游戏结果 ｜ 系统计算方式", "sources": [src]},
            {"text": "输赢金额>0 ｜ 当前连赢次数“+1”", "sources": [src]},
            {"text": "输赢金额=0 ｜ 当前连赢次数“不变”", "sources": [src], "gap": True},
            {"text": "注：", "sources": [src]},
            {"text": "1、输赢金额=派彩金额-投注金额", "sources": [src]},
            {"text": "2、仅影响用户“当前连赢记录”", "sources": [src]},
            {"text": "【活动对象】仅针对正式账号", "sources": [src]},
            {"text": "3局 ｜ +1分", "sources": [src]}]},
        {"name": "pc-活动入口说明-报名确认弹窗", "items": [{"text": "是否确认报名？", "sources": ["连赢挑战.zip › 图片:7"]}]},
        {"name": "pc-活动入口说明-游戏外入口", "items": [{"text": "1、大厅左侧活动入口", "sources": ["连赢挑战.zip › 页面：pc-活动入口说明"]}]},
    ]
    assign_ids(modules)
    outline = extraction_outline(modules)
    assert [s["title"] for s in outline] == ["需求说明", "pc-活动入口说明", "仅出现在图片中的文案"]   # 图片文案永远最后
    assert outline[0]["no"] == "1" and outline[0]["blocks"][0]["no"] == "1.1" and outline[0]["blocks"][0]["name"] == "连赢判断规则"
    rows = outline[0]["blocks"][0]["rows"]
    assert [r["t"] for r in rows] == ["label", "table", "label", "item", "item", "item", "item"]
    assert len(rows[1]["items"]) == 3 and rows[1]["cols"] == 2 and [r.get("lv") for r in rows[3:]] == [1, 1, 0, 0]   # 孤行「3局 ｜ +1分」不成表
    assert outline[1]["blocks"][0]["rows"][0]["lv"] == 0   # 区块第一条就是「1、」不缩进
    view = outline_view(modules)
    assert view[0]["blocks"][0]["rows"][1]["ids"] == [it["id"] for it in modules[0]["items"][1:4]] and view[2]["blocks"][0]["name"] == "pc-活动入口说明-报名确认弹窗"
    md = extraction_markdown(modules, head={"title": "oks", "sources": "`连赢挑战.zip`（Axure 原型导出，共 6 个页面：需求说明 / …）", "check": "底稿 620 句（去重）：收录 421、排除 148、待处理 51；`连赢挑战.zip` 整页字符自校验 0 缺字（6 页）"})
    assert md.startswith("# 「oks」需求整理稿\n\n- 来源：`连赢挑战.zip`（Axure 原型导出，共 6 个页面：需求说明 / …）\n- 提取原则：")
    assert ("## 1. 需求说明 〔来源：连赢挑战.zip › 需求说明〕\n\n### 1.1 连赢判断规则\n**连赢判断规则如下表所示：**\n"
            "| 本局游戏结果 | 系统计算方式 |\n|---|---|\n| 输赢金额>0 | 当前连赢次数“+1” |\n| 输赢金额=0 | 当前连赢次数“不变” 〔补漏〕 |\n"
            "**注：**\n  - 1、输赢金额=派彩金额-投注金额\n  - 2、仅影响用户“当前连赢记录”\n- 【活动对象】仅针对正式账号\n- 3局 ｜ +1分\n") in md
    assert "## 3. 仅出现在图片中的文案\n\n### 3.1 pc-活动入口说明-报名确认弹窗\n- 是否确认报名？ 〔来源：连赢挑战.zip › 图片:7〕" in md
    # 底稿：文头里的字不算收录依据；表格行按整行 / 单元格都能对上；去掉行首序号的子项也对上
    mat = ("【文件：连赢挑战.zip】\n# Axure 原型：连赢挑战.zip\n## 页面：需求说明\n[u41] 输赢金额>0 ｜ 当前连赢次数“+1”\n[u43] 输赢金额=0 ｜ 当前连赢次数“不变”\n"
           "[u45] 输赢金额<0 ｜ 当前连赢次数“清零”\n[u64] 3局\n[u59] 1、输赢金额=派彩金额-投注金额\n[u90] 需求原文\n")
    st = {u["uid"]: u["status"] for u in account(build_units(mat), md)["units"]}
    assert st["u41"] == "in" and st["u43"] == "in" and st["u59"] == "in" and st["u64"] == "in" and st["u45"] == "todo"
    assert st["u90"] == "todo"   # 「需求原文」只在文头出现，不算收录


async def test_交叉核对_跨页面疑点按id追加_疑似草稿整块标记_失败不拦提取(monkeypatch):
    """整理稿是分页面提取的，版本记录写了「取消第一名机制」而月榜正文仍是旧规则这类矛盾单页看不出；画布远处的旧版本（年榜）也要标出来。
    最后整稿过一遍模型：doubts 按 id 追加到条目疑点，drafts 按模块名把整块标〔疑似草稿〕并把理由写进第一条；模型出错只跳过。"""
    from app.agents.extraction import DRAFT_MARK, _crosscheck, apply_crosscheck, assign_ids, extraction_markdown, extraction_outline
    from app.config import get_settings

    modules = [{"name": "月榜排名-榜单列表", "items": [{"text": "第 1 名固定展示在榜单顶部", "sources": ["a.zip › 月榜排名"], "doubt": "原文「固定展示」疑为「固定行」"}]},
               {"name": "连赢活动记录-年榜", "items": [{"text": "晋级年榜 ｜ 字符串 ｜ 系统 ｜ 展示是否晋级年榜", "sources": ["a.zip › 连赢活动记录"]},
                                                 {"text": "年榜统计周期为自然年", "sources": ["a.zip › 连赢活动记录"]}]}]
    assign_ids(modules)
    data = {"doubts": [{"id": modules[0]["items"][0]["id"], "doubt": "版本记录第 4 行写「取消第一名的特殊机制」，此处仍是第 1 名固定行"},
                       {"id": "nope", "doubt": "不存在的 id 忽略"}],
            "drafts": [{"module": "连赢活动记录-年榜", "reason": "正式页面只有日榜、月榜"}, {"module": "没有的模块", "reason": "忽略"}]}
    assert apply_crosscheck(modules, data) == {"doubts": 1, "drafts": 1}
    assert modules[0]["items"][0]["doubt"] == "原文「固定展示」疑为「固定行」；版本记录第 4 行写「取消第一名的特殊机制」，此处仍是第 1 名固定行"
    assert all(it["draft"] for it in modules[1]["items"]) and modules[1]["items"][0]["doubt"] == "疑似遗留草稿：正式页面只有日榜、月榜"
    assert "draft" not in modules[0]["items"][0]
    md = extraction_markdown(modules)
    assert f"### 2.1 年榜 〔疑似遗留草稿〕\n- 晋级年榜 ｜ 字符串 ｜ 系统 ｜ 展示是否晋级年榜 {DRAFT_MARK} 〔疑点：疑似遗留草稿：正式页面只有日榜、月榜〕\n- 年榜统计周期为自然年 {DRAFT_MARK}" in md
    assert "## 4. 疑似遗留草稿 〔" in md and "- 2.1 年榜（连赢活动记录-年榜）" in md   # 疑点清单是第 3 节，草稿清单第 4 节
    assert extraction_outline(modules)[1]["blocks"][0]["draft"] is True and extraction_outline(modules)[0]["blocks"][0]["draft"] is False
    # 开关：默认测试环境关；打开后走模型，模型抛错只跳过
    assert await _crosscheck(modules, StubLLM([]), None, "") is None
    monkeypatch.setenv("TIANGONG_REQUIREMENT_EXTRACT_CROSSCHECK", "true")
    get_settings.cache_clear()
    try:
        class Boom:
            async def chat(self, *a, **k):
                raise RuntimeError("模型超时")
        assert await _crosscheck(modules, Boom(), None, "") is None
        stub = StubLLM([json.dumps({"doubts": [], "drafts": [{"module": "月榜排名-榜单列表", "reason": "测试"}]}, ensure_ascii=False)])
        assert await _crosscheck(modules, stub, None, "") == {"doubts": 0, "drafts": 1} and modules[0]["items"][0]["draft"] is True
    finally:
        monkeypatch.setenv("TIANGONG_REQUIREMENT_EXTRACT_CROSSCHECK", "false")
        get_settings.cache_clear()


def test_合并去重按逐字键_运算符不同是两条_id不撞():
    from app.agents.extraction import _merge_modules, assign_ids

    out = ({"modules": [{"name": "规则", "items": [{"text": "输赢金额>0 ｜ +1"}, {"text": "输赢金额=0 ｜ 不变"}, {"text": "输赢金额 > 0 ｜ +1"}]}]}, None)
    modules, _, _ = _merge_modules([out])
    assert [it["text"] for it in modules[0]["items"]] == ["输赢金额>0 ｜ +1", "输赢金额=0 ｜ 不变"]   # 只有空白差异的才算重复
    assign_ids(modules)
    assert len({it["id"] for it in modules[0]["items"]}) == 2


def test_流程行成稿为代码块_底稿按流程行命中流程框短文字_覆盖率不算流程块():
    from app.agents.extraction import assign_ids, extraction_markdown, extraction_outline, outline_view, uncovered_lines
    from app.agents.ledger import account, build_units

    src = "a.zip › 需求说明"
    modules = [{"name": "需求说明-连赢判断", "items": [{"text": "连赢记录统计判断流程示例：", "sources": [src]},
                                              {"text": "用户单局投注 → 该用户当局输赢金额是否>0", "sources": [src]},
                                              {"text": "该用户当局输赢金额是否>0 —Y→ 当前连赢记录+1", "sources": [src], "gap": True},
                                              {"text": "注：仅影响当前连赢记录", "sources": [src]}]}]
    assign_ids(modules)
    rows = extraction_outline(modules)[0]["blocks"][0]["rows"]
    assert [r["t"] for r in rows] == ["label", "flow", "item"] and len(rows[1]["items"]) == 2
    assert outline_view(modules)[0]["blocks"][0]["rows"][1] == {"t": "flow", "ids": [modules[0]["items"][1]["id"], modules[0]["items"][2]["id"]]}
    md = extraction_markdown(modules)
    assert ("**连赢记录统计判断流程示例：**\n```\n用户单局投注 ──► 该用户当局输赢金额是否>0 ─ Y ─► 当前连赢记录+1\n\n"
            "· 该用户当局输赢金额是否>0 —Y→ 当前连赢记录+1 〔补漏〕\n```\n- 注：仅影响当前连赢记录") in md   # 画成树，带标记的流程行另列在树下
    mat = ("【文件：a.zip】\n# Axure 原型：a.zip\n## 页面：需求说明\n[u47] 用户单局投注\n[u48] 该用户当局输赢金额是否>0\n[u55] 当前连赢记录+1\n[u50] Y\n"
           "【流程图 1：3 个节点 2 条连线】\n用户单局投注 → 该用户当局输赢金额是否>0\n该用户当局输赢金额是否>0 —Y→ 当前连赢记录+1\n【流程图结束】\n[u59] 注：仅影响当前连赢记录\n")
    st = {u["uid"]: u["status"] for u in account(build_units(mat), md)["units"]}
    assert st == {"u47": "in", "u48": "in", "u55": "in", "u50": "in", "u59": "in"}   # 框文字与分支标签「Y」都靠树里拆出的字命中
    missing, cov = uncovered_lines(mat, md)
    assert cov == {"lines": 2, "covered": 2} and missing == []   # 需求句只有判断框与注两行：流程块与 < 8 汉字的行不算


def test_整理稿页面顺序_更新记录与需求说明在前_图片来源写法宽松_底稿不切掉第一节():
    from app.agents.extraction import extraction_outline
    from app.agents.ledger import account, build_units

    mk = lambda name, src: {"name": name, "items": [{"text": f"{name} 的一条内容说明文字", "sources": [src]}]}
    modules = [mk("pc-活动入口说明-游戏外入口", "a.zip › pc-活动入口说明"), mk("pc-活动详情页界面说明-报名成功弹窗", "a.zip › 图片10"),
               mk("更新记录-更新记录表", "a.zip › 更新记录"), mk("移动-活动入口说明-游戏外入口", "a.zip › 移动-活动入口说明"),
               mk("需求说明-需求背景", "a.zip › 需求说明")]
    assert [s["title"] for s in extraction_outline(modules)] == ["更新记录", "需求说明", "pc-活动入口说明", "移动-活动入口说明", "仅出现在图片中的文案"]
    # 没有文头的正文（补漏 / 兜底阶段核算用的）不能把第一节切掉：兜底收进第一节的句子要算收录
    mat = "【文件：a.zip】\n# Axure 原型：a.zip\n## 页面：甲\n[u1] 第一节里的兜底句子\n## 页面：乙\n[u2] 第二节的句子\n"
    draft = "## 1. 甲 〔来源：a.zip › 甲〕\n\n### 1.1 待归类\n- 第一节里的兜底句子 〔兜底〕\n\n## 2. 乙\n\n### 2.1 正文\n- 第二节的句子\n"
    assert {u["uid"]: u["status"] for u in account(build_units(mat), draft)["units"]} == {"u1": "in", "u2": "in"}


def test_疑似草稿只认独立区域_模型别处标的去掉_交叉核对也受限():
    from app.agents.extraction import apply_crosscheck, assign_ids, enforce_draft
    from app.agents.ledger import in_islands, island_keys

    mat = ("【文件：a.zip】\n# Axure 原型：a.zip\n## 页面：连赢活动记录\n[u1] 正式页面说明\n【独立区域 1：主体右侧 2394px，3 控件，疑似遗留草稿】\n"
           "[u11] 晋级年榜 ｜ 字符串 ｜ 系统\n[u12] 年榜统计周期为自然年，按年榜排名\n## 页面：需求说明\n[u20] 活动规则说明正文\n")
    keys = island_keys(mat)
    assert keys == {"晋级年榜|字符串|系统", "年榜统计周期为自然年,按年榜排名"} and in_islands("年榜统计周期为自然年", keys) and not in_islands("正式页面说明", keys)
    modules = [{"name": "需求说明-活动规则说明", "items": [{"text": "活动规则说明正文", "draft": True}]},
               {"name": "连赢活动记录-年榜", "items": [{"text": "晋级年榜 ｜ 字符串 ｜ 系统", "draft": True}, {"text": "年榜统计周期为自然年，按年榜排名"}]}]
    assert enforce_draft(modules, keys) == 1 and "draft" not in modules[0]["items"][0] and modules[1]["items"][0]["draft"] is True
    assign_ids(modules)
    data = {"doubts": [], "drafts": [{"module": "需求说明-活动规则说明", "reason": "与 PC 页重复"}, {"module": "连赢活动记录-年榜", "reason": "年榜未在别处出现"}]}
    assert apply_crosscheck(modules, data, keys) == {"doubts": 0, "drafts": 1}   # 不在独立区域的模块不能标草稿
    assert "draft" not in modules[0]["items"][0] and all(it.get("draft") for it in modules[1]["items"])


def test_流程框文字不与流程行重复_疑点里的条目id换成模块名():
    from app.agents.extraction import apply_crosscheck, assign_ids, dedupe_flow_nodes

    modules = [{"name": "需求说明-连赢判断", "items": [{"text": "用户单局投注"}, {"text": "Y"}, {"text": "用户单局投注 → 该用户当局输赢金额是否>0"},
                                              {"text": "该用户当局输赢金额是否>0 —Y→ 当前连赢记录+1"}, {"text": "当前连赢记录+1"}, {"text": "注：仅影响当前连赢记录"},
                                              {"text": "当局输赢金额"}, {"text": "用户"}]},   # 多行框拆出的一段去掉；短于 4 字的子串不算
               {"name": "月榜排名-榜单列表", "items": [{"text": "第 1 名固定展示"}]}]
    assert dedupe_flow_nodes(modules) == 4
    assert [it["text"] for it in modules[0]["items"]] == ["用户单局投注 → 该用户当局输赢金额是否>0", "该用户当局输赢金额是否>0 —Y→ 当前连赢记录+1", "注：仅影响当前连赢记录", "用户"]
    assign_ids(modules)
    rid = modules[0]["items"][2]["id"]
    apply_crosscheck(modules, {"doubts": [{"id": modules[1]["items"][0]["id"], "doubt": f"与（{rid}）及 deadbeef 矛盾"}], "drafts": []})
    assert modules[1]["items"][0]["doubt"] == "与「需求说明-连赢判断」及 deadbeef 矛盾"   # 已知 id 换模块名，未知的原样


def test_流程行画成分支树_汇合框重复_回路止住_带标记的行另列_底稿按框文字命中():
    from app.agents.extraction import assign_ids, extraction_markdown, flow_ascii
    from app.agents.ledger import draft_index

    j = ["用户单局投注 → 该用户当局输赢金额是否>0", "该用户当局输赢金额是否>0 —N→ 该用户当局输赢金额是否=0",
         "该用户当局输赢金额是否=0 —N→ 当前连赢记录清零", "该用户当局输赢金额是否=0 —Y→ 当前连赢记录不变", "该用户当局输赢金额是否>0 —Y→ 当前连赢记录+1"]
    assert flow_ascii(j) == [
        "用户单局投注 ──► 该用户当局输赢金额是否>0 ──┬─ N ─► 该用户当局输赢金额是否=0 ──┬─ N ─► 当前连赢记录清零",
        "                                                                               └─ Y ─► 当前连赢记录不变",
        "                                            └─ Y ─► 当前连赢记录+1"]
    assert flow_ascii(["甲 → 乙", "乙 → 甲"]) == ["甲 ──► 乙 ──► （回到：甲）"] and flow_ascii(["不是流程行"]) is None
    modules = [{"name": "需求说明-连赢判断", "items": [{"text": j[0]}, {"text": j[1], "gap": True}, {"text": j[2]}, {"text": j[3]}, {"text": j[4]}]}]
    assign_ids(modules)
    md = extraction_markdown(modules)
    assert "```\n用户单局投注 ──► 该用户当局输赢金额是否>0 ──┬─ N ─► " in md and "\n\n· 该用户当局输赢金额是否>0 —N→ 该用户当局输赢金额是否=0 〔补漏〕\n```" in md
    _n, items, _st = draft_index(md)
    assert "当前连赢记录清零" in items and "当前连赢记录+1".replace("+", "") in items or "当前连赢记录1" in items   # 树里拆出的框文字算条目


async def test_逐条修改带疑点的条目后疑点不再显示(client, extract_on):
    await client.post("/api/v1/projects", json={"name": "P"})
    reply = json.dumps({"modules": [{"name": "报名-规则", "items": [
        {"text": "报名费满 4 元后当日将无法再次报名", "sources": ["a.zip › 报名"], "doubt": "前台写的是 5 元"},
        {"text": "活动名称最长 30 个字符", "sources": ["a.zip › 报名"]}]}], "dropped": []}, ensure_ascii=False)
    app.state.llm = StubLLM([reply, NO_GAP])
    r = (await client.post("/api/v1/requirements", data={"project": "P", "title": "报名", "text": "报名费满 4 元后当日将无法再次报名\n活动名称最长 30 个字符"})).json()
    rid = r["req_id"]
    r = (await client.post(f"/api/v1/requirements/{rid}/extract", json={})).json()
    ex = r["extraction"]
    assert "〔疑点：前台写的是 5 元〕" in ex["text"] and "疑点 / 不一致清单" in ex["text"]
    it = ex["modules"][0]["items"][0]
    r = (await client.post(f"/api/v1/requirements/{rid}/extraction/items", json={"item_id": it["id"], "action": "edit", "text": "报名费满 4 元后当日将无法再次报名"})).json()
    ex = r["extraction"]
    assert "〔疑点" not in ex["text"] and "疑点 / 不一致清单" not in ex["text"]   # 人改过的条目疑点算处理过
    assert ex["modules"][0]["items"][0].get("doubt_resolved") is True and "doubt" not in ex["modules"][0]["items"][0]


async def test_附件解析全文接口(client):
    await client.post("/api/v1/projects", json={"name": "P"})
    r = (await client.post("/api/v1/requirements", data={"project": "P", "title": "附件"}, files=[("files", ("规则.txt", "报名费满 4 元后当日将无法再次报名".encode(), "text/plain"))])).json()
    rid = r["req_id"]
    for _ in range(50):
        r = (await client.get(f"/api/v1/requirements/{rid}")).json()
        if all(not a.get("parsing") for a in r["attachments"]):
            break
    att = r["attachments"][0]
    t = (await client.get(f"/api/v1/requirements/{rid}/attachments/{att['att_id']}/text")).json()
    assert t["filename"] == "规则.txt" and t["parsed"] is True and "报名费满 4 元后当日将无法再次报名" in t["text"]
    assert (await client.get(f"/api/v1/requirements/{rid}/attachments/nope/text")).status_code == 404


def test_原文小标题成子标题_区块开头的标题并进区块名_页面间分隔线_底稿去点号编号也能对上():
    """「一、奖池规则」「2.3 查询功能说明」这类原文小标题成加粗子标题不缩进；区块开头就是本区块标题的（「一、活动游戏类型说明」、
    「导出数据」+「2.5 列表的导出数据」）不再单独成行，原文标题更长的用它当区块名；「1、页面说明」后面紧跟「i.」「ii.」子项的也是
    小标题；带 ⚠ / 疑点或冒号结尾的引语不折；页面之间有分隔线。底稿按整理稿正文核算，折进标题的原文仍能对上（「2.1 」点号编号也去掉再比）。"""
    from app.agents.extraction import assign_ids, extraction_markdown, extraction_outline
    from app.agents.ledger import account, build_units

    src = "后台.zip › 需求说明"
    modules = [
        {"name": "需求说明-活动游戏类型说明", "items": [
            {"text": "一、活动游戏类型说明", "sources": [src]},
            {"text": "连赢活动的游戏类型限制为：仅所有百家乐游戏类型及玩法参与", "sources": [src]},
            {"text": "2.3 查询功能说明", "sources": [src]},
            {"text": "更新时间", "sources": [src]}]},
        {"name": "需求说明-导出数据", "items": [
            {"text": "导出数据", "sources": [src]},
            {"text": "2.5 列表的导出数据", "sources": [src]},
            {"text": "i. 点击【导出数据】后，直接在下载中心查看文件；", "sources": [src]},
            {"text": "ii. 前后导出时间间隔最少≥1分钟；", "sources": [src]}]},
        {"name": "需求说明-页面说明", "items": [
            {"text": "1、页面说明", "sources": [src]},
            {"text": "i.展示活动后台“连赢活动”类型活动的相关报名记录", "sources": [src]},
            {"text": "2、功能说明", "sources": [src]},
            {"text": "ii.操作：手动撤销/批量撤销会员的报名", "sources": [src]}]},
        {"name": "报名记录-字段说明", "items": [
            {"text": "2.1 字段说明", "sources": ["后台.zip › 报名记录"], "verified": False},
            {"text": "连赢判断规则如下表所示：", "sources": ["后台.zip › 报名记录"]}]},
    ]
    assign_ids(modules)
    outline = extraction_outline(modules)
    b1, b2, b3 = outline[0]["blocks"]
    assert b1["name"] == "活动游戏类型说明" and b1["rows"][0].get("dup") and [r["t"] for r in b1["rows"]] == ["label", "item", "label", "item"]
    assert b1["rows"][3]["lv"] == 0   # 「2.3 …」是小标题不是子项，后面的条目也不缩进
    assert b2["name"] == "列表的导出数据" and b2["rows"][0].get("dup") and b2["rows"][1].get("dup") and [r["t"] for r in b2["rows"][2:]] == ["item", "item"]
    assert [r["t"] for r in b3["rows"]] == ["label", "item", "label", "item"] and b3["rows"][0].get("dup") and b3["rows"][1]["lv"] == 0
    assert outline[1]["blocks"][0]["name"] == "字段说明" and not outline[1]["blocks"][0]["rows"][0].get("dup")   # ⚠ 的不折
    md = extraction_markdown(modules)
    assert "### 1.1 活动游戏类型说明\n- 连赢活动的游戏类型限制为" in md and "- 一、活动游戏类型说明" not in md and "**2.3 查询功能说明**\n- 更新时间" in md
    assert "### 1.2 列表的导出数据\n- i. 点击" in md and "### 1.3 页面说明\n- i.展示" in md and "**2、功能说明**\n- ii.操作" in md and "\n\n---\n\n## 2. 报名记录" in md
    assert "### 2.1 字段说明\n**⚠ 2.1 字段说明**\n**连赢判断规则如下表所示：**" in md
    materials = "【文件：后台.zip】\n## 页面：需求说明\n[u1] 一、活动游戏类型说明\n[u2] 导出数据\n[u3] 2.5 列表的导出数据\n[u4] 1、页面说明\n[u5] 2.3 查询功能说明\n"
    acct = account(build_units(materials), md)
    assert {u["uid"]: u["status"] for u in acct["units"]} == {"u1": "in", "u2": "in", "u3": "in", "u4": "in", "u5": "in"}

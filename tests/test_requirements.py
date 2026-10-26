"""M2 需求中心：需求实体与原文保护、附件解析失败显式记录、AI 分析 11 项、待确认事项确认流、
从需求发起测试设计与追溯链、存量任务迁移。"""

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


# 新格式：每条 {text, quote}；规则 / 边界 / 状态 / 权限四项的 quote 必须能在原文里找到，否则降级为待确认事项
ANALYSIS_REPLY = json.dumps({
    "features": ["账号密码登录"],
    "rules": [{"text": "密码错误 5 次锁定 30 分钟", "quote": "密码错误 5 次锁定 30 分钟"}],
    "preconditions": ["已注册账号"],
    "normal_flows": ["输入账号密码→登录成功"], "exception_flows": ["密码错误提示"],
    "boundaries": [{"text": "密码错误次数上限 5 次", "quote": "密码错误 5 次"}],
    "state_changes": [{"text": "正常→锁定", "quote": "5 次锁定 30 分钟"}], "permissions": [],
    "dependencies": ["短信网关"], "risks": [{"text": "锁定误伤", "quote": ""}],
    "open_questions": ["锁定期间是否允许找回密码？", "锁定次数是否按账号还是按设备统计？"],
}, ensure_ascii=False)


def texts(items):
    return [x["text"] if isinstance(x, dict) else x for x in items]


async def _req(client, project="P", title="登录需求", text="用户输入账号密码登录；密码错误 5 次锁定 30 分钟。") -> dict:
    await client.post("/api/v1/projects", json={"name": project})
    resp = await client.post("/api/v1/requirements", data={"project": project, "title": title, "text": text})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_需求实体_原文保护_人工补充(client):
    r = await _req(client)
    assert (r["status"], r["source_type"], r["task_count"]) == ("draft", "manual", 0)
    assert (await client.post("/api/v1/requirements", data={"project": "P", "title": " ", "text": "x"})).status_code == 400
    assert (await client.post("/api/v1/requirements", data={"project": "P", "title": "空", "text": ""})).status_code == 400
    # 人工补充独立于原文；原文接口不可改
    upd = (await client.put(f"/api/v1/requirements/{r['req_id']}", json={"description": "补充：仅 Web 端"})).json()
    assert upd["description"] == "补充：仅 Web 端" and upd["raw_text"] == r["raw_text"]
    listed = (await client.get("/api/v1/requirements", params={"project": "P"})).json()
    assert [x["title"] for x in listed["requirements"]] == ["登录需求"] and "raw_text" not in listed["requirements"][0]
    assert (await client.get("/api/v1/requirements", params={"project": "P", "keyword": "锁定"})).json()["requirements"]
    # 逻辑删除与恢复
    await client.delete(f"/api/v1/requirements/{r['req_id']}")
    assert (await client.get(f"/api/v1/requirements/{r['req_id']}")).status_code == 404
    assert (await client.get("/api/v1/requirements", params={"project": "P"})).json()["requirements"] == []
    assert (await client.post(f"/api/v1/requirements/{r['req_id']}/restore")).status_code == 200


async def _wait_parsed(client, req_id: str) -> dict:
    import asyncio
    for _ in range(200):
        r = (await client.get(f"/api/v1/requirements/{req_id}")).json()
        if not r["parsing"]:
            return r
        await asyncio.sleep(0.02)
    raise AssertionError("附件解析未在预期时间内完成")


async def test_附件解析失败显式记录并可重新解析(client):
    await client.post("/api/v1/projects", json={"name": "P"})
    resp = await client.post("/api/v1/requirements", data={"project": "P", "title": "带附件"},
                             files=[("files", ("需求.txt", "登录需求原文".encode(), "text/plain")),
                                    ("files", ("坏文件.xyz", b"\x00\x01\xff\xfe", "application/octet-stream"))])
    assert resp.status_code == 200, resp.text
    r = resp.json()
    # 上传立即返回：附件处于「解析中」，解析在后台进行；解析中不能发起 AI 分析 / 测试设计
    assert r["source_type"] == "file" and r["parsing"] == 2 and r["parse_failed"] == 0
    assert all(a["parsing"] for a in r["attachments"])
    r = await _wait_parsed(client, r["req_id"])
    assert r["parsing"] == 0 and r["parse_failed"] == 1
    atts = {a["filename"]: a for a in r["attachments"]}
    # 解析中不能发起 AI 分析 / 测试设计（把一个附件置回解析中模拟）
    app.state.requirements.replace_attachment(r["req_id"], atts["需求.txt"]["att_id"], {"parsing": True})
    resp = await client.post(f"/api/v1/requirements/{r['req_id']}/analyze", json={})
    assert resp.status_code == 409 and "解析中" in resp.json()["detail"]
    assert (await client.post(f"/api/v1/requirements/{r['req_id']}/design", json={})).status_code == 409
    app.state.requirements.replace_attachment(r["req_id"], atts["需求.txt"]["att_id"], {"parsing": False})
    assert atts["需求.txt"]["parsed"] and atts["需求.txt"]["chars"] > 0
    assert not atts["坏文件.xyz"]["parsed"] and atts["坏文件.xyz"]["error"]
    # 重新解析（后台）仍失败：原因保留；详情带解析预览
    resp = await client.post(f"/api/v1/requirements/{r['req_id']}/attachments/{atts['坏文件.xyz']['att_id']}/reparse")
    assert resp.status_code == 200 and resp.json()["parsing"] == 1
    r = await _wait_parsed(client, r["req_id"])
    assert r["parse_failed"] == 1 and r["parsing"] == 0
    detail = (await client.get(f"/api/v1/requirements/{r['req_id']}")).json()
    assert next(a for a in detail["attachments"] if a["filename"] == "需求.txt")["preview"] == "登录需求原文"


async def test_AI分析_待确认事项_确认后才能测试设计_追溯(client):
    r = await _req(client)
    rid = r["req_id"]
    app.state.llm = StubLLM([ANALYSIS_REPLY])
    a = (await client.post(f"/api/v1/requirements/{rid}/analyze", json={})).json()
    assert a["status"] == "pending_confirm" and a["open_questions"] == 2
    assert texts(a["analysis"]["rules"]) == ["密码错误 5 次锁定 30 分钟"] and set(a["analysis_labels"]) >= {"features", "open_questions"}
    assert a["analysis"]["rules"][0]["verified"] is True and a["analysis_meta"]["verification"]["demoted"] == 0
    assert a["raw_text"] == r["raw_text"]  # AI 结果不覆盖原文
    assert a.get("analysis_history", []) == []
    app.state.llm = StubLLM([ANALYSIS_REPLY])
    a2 = (await client.post(f"/api/v1/requirements/{rid}/analyze", json={})).json()
    assert len(a2["analysis_history"]) == 1 and a2["analysis_history"][0]["analysis"]["rules"] == a["analysis"]["rules"]
    assert a2["analysis_history"][0]["meta"]["at"] == a["analysis_meta"]["at"]
    assert "analysis_history" not in (await client.get("/api/v1/requirements")).json()["requirements"][0]
    assert a2["open_questions"] == 2  # 重新分析：同样的问题不重复追加
    # 未确认不得发起测试设计（核心规则 4）
    assert (await client.post(f"/api/v1/requirements/{rid}/design", json={})).status_code == 409
    q1, q2 = [q["q_id"] for q in a["questions"]]
    assert (await client.post(f"/api/v1/requirements/{rid}/questions/{q1}", json={"answer": ""})).status_code == 400
    await client.post(f"/api/v1/requirements/{rid}/questions/{q1}", json={"answer": "允许找回密码"})
    # 人工补充一条待确认事项后仍待确认
    a = (await client.post(f"/api/v1/requirements/{rid}/questions", json={"question": "是否支持记住登录？"})).json()
    assert a["status"] == "pending_confirm" and a["open_questions"] == 2
    q3 = next(q["q_id"] for q in a["questions"] if q["source"] == "manual")
    await client.post(f"/api/v1/requirements/{rid}/questions/{q2}", json={"answer": "按账号"})
    a = (await client.post(f"/api/v1/requirements/{rid}/questions/{q3}", json={"answer": "支持 7 天"})).json()
    assert a["status"] == "confirmed" and a["open_questions"] == 0
    # 再次 AI 分析：已确认的问题保留结论（换了说法、只差标点的也认出来是同一问题），真正的新问题追加
    app.state.llm = StubLLM([ANALYSIS_REPLY.replace("锁定期间是否允许找回密码？", "锁定期间是否还允许找回密码？")
                             .replace("按设备统计？", "按设备统计？？").replace('"]}', '", "是否记录登录失败日志？"]}')])
    a = (await client.post(f"/api/v1/requirements/{rid}/analyze", json={})).json()
    kept = next(q for q in a["questions"] if q["q_id"] == q1)
    assert kept["status"] == "confirmed" and kept["question"] == "锁定期间是否允许找回密码？" and a["open_questions"] == 1
    assert next(q for q in a["questions"] if q["q_id"] == q2)["status"] == "confirmed"
    assert [q["question"] for q in a["questions"] if q["status"] == "open"] == ["是否记录登录失败日志？"]
    await client.post(f"/api/v1/requirements/{rid}/questions/{next(q['q_id'] for q in a['questions'] if q['status']=='open')}",
                      json={"answer": "按设备"})

    # 发起测试设计（同步、直接生成）：任务上下文回挂需求，生成文本含确认结论
    case = make_case(point_ids=["TP001"])
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(case), review_reply(True)])
    resp = await client.post(f"/api/v1/requirements/{rid}/design",
                             json={"confirm_points": False, "async_mode": False})
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["task_id"]
    sent = app.state.llm.calls[0]["messages"][-1]["content"]
    assert "允许找回密码" in sent and "仅 Web 端" not in sent
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert (task["context"]["requirement_id"], task["context"]["requirement_title"]) == (rid, "登录需求")
    listed = (await client.get("/api/v1/tasks", params={"project": "P"})).json()["tasks"]
    assert listed[0]["requirement_id"] == rid
    # 追溯：需求 → 任务 → 用例（含来源测试点）→ 覆盖识别
    detail = (await client.get(f"/api/v1/requirements/{rid}")).json()
    assert detail["status"] == "designing" and detail["tasks"] == [task_id]
    t = detail["trace"]
    assert t["totals"]["cases"] == 1 and t["coverage"] == {"has_points": True, "has_cases": True, "in_plan": False, "executed": False}
    # 用例中心只收录正式用例：通过前不出现，通过后带需求与来源测试点追溯
    assert (await client.get("/api/v1/cases", params={"project": "P"})).json()["cases"] == []
    await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [{"case_id": case["case_id"], "action": "accept"}]})
    rows = (await client.get("/api/v1/cases", params={"project": "P"})).json()["cases"]
    assert rows[0]["requirement_id"] == rid and rows[0]["point_ids"] == ["TP001"]
    # 用例通过并入计划后覆盖识别更新
    plan = (await client.post("/api/v1/plans", json={"name": "冒烟", "project": "P"})).json()
    await client.post(f"/api/v1/plans/{plan['plan_id']}/cases", json={"task_id": task_id})
    t = (await client.get(f"/api/v1/requirements/{rid}")).json()["trace"]
    assert t["coverage"]["in_plan"] and t["totals"]["cases_approved"] == 1


async def test_存量任务迁移为需求(client):
    from app.requirements import migrate_tasks

    await client.post("/api/v1/projects", json={"name": "P"})
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(make_case()), review_reply(True)])
    task_id = (await client.post("/api/v1/tasks", files={"files": ("玩法.txt", b"# rule", "text/plain")},
                                 data={"project": "P"})).json()["task_id"]
    record = app.state.tasks.get(task_id)
    record.context.pop("requirement_id", None)  # 模拟项目化之前的任务
    app.state.tasks.save(record)
    assert migrate_tasks(app.state.tasks, app.state.requirements) == 1
    assert migrate_tasks(app.state.tasks, app.state.requirements) == 0  # 幂等
    reqs = (await client.get("/api/v1/requirements", params={"project": "P"})).json()["requirements"]
    assert len(reqs) == 1 and reqs[0]["title"] == "rule" and reqs[0]["source_type"] == "migrated"  # 标题取正文一级标题
    assert reqs[0]["status"] == "designing" and reqs[0]["task_count"] == 1
    assert app.state.tasks.get(task_id).context["requirement_id"] == reqs[0]["req_id"]


def test_迁移需求标题从正文推导():
    from app.requirements import derive_title
    raw = "【文件：30086186_735e783b-170726-1503-52.pdf】\n# 0801走地德州-新手进阶\n走地德州仅在4.0新手进阶增加"
    assert derive_title(raw, "30086186_735e783b-170726-1503-52.pdf") == "0801走地德州-新手进阶"
    assert derive_title("用户登录功能：账号密码登录，错误 5 次锁定。\n更多说明", "需求.txt") == "需求.txt"  # 原文无标题：保留文件名
    assert derive_title("登录模块需求说明\n正文…", "") == "登录模块需求说明"   # 纯文本需求：取首行
    assert derive_title("## # 图片类型与整体说明\n…", "a.pdf") == "图片类型与整体说明"
    assert derive_title("", "a.pdf") == "a.pdf"


async def test_存量迁移标题修复幂等(client):
    from app.requirements import repair_migrated_titles
    await client.post("/api/v1/projects", json={"name": "P"})
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(make_case()), review_reply(True)])
    resp = await client.post("/api/v1/tasks", files={"files": ("30086186_xx.pdf", "# 0801走地德州-新手进阶\n规则……".encode(), "text/plain")},
                             data={"project": "P"})
    tid = resp.json()["task_id"]
    from app.requirements import migrate_tasks
    assert migrate_tasks(app.state.tasks, app.state.requirements) == 1   # 启动时的存量迁移
    task = (await client.get(f"/api/v1/tasks/{tid}")).json()
    rid = task["context"]["requirement_id"]
    # 新迁移直接得到正文标题；把标题改回文件名模拟旧数据，修复后恢复并回填任务
    assert task["context"]["requirement_title"] == "0801走地德州-新手进阶"
    item = app.state.requirements.get(rid); item["title"] = "30086186_xx.pdf"; app.state.requirements._persist(item)
    assert repair_migrated_titles(app.state.requirements, app.state.tasks) == 1
    assert repair_migrated_titles(app.state.requirements, app.state.tasks) == 0
    assert (await client.get(f"/api/v1/requirements/{rid}")).json()["title"] == "0801走地德州-新手进阶"
    assert (await client.get(f"/api/v1/tasks/{tid}")).json()["context"]["requirement_title"] == "0801走地德州-新手进阶"


async def test_合并需求_任务附件待确认并入_依赖边改指向(client):
    await client.post("/api/v1/projects", json={"name": "P"})
    await client.post("/api/v1/projects", json={"name": "Q"})
    a = (await client.post("/api/v1/requirements", data={"project": "P", "title": "新手进阶", "text": "原文 A"})).json()
    b = (await client.post("/api/v1/requirements", data={"project": "P", "title": "新手进阶", "text": "原文 B"})).json()
    c = (await client.post("/api/v1/requirements", data={"project": "P", "title": "游戏规则", "text": "原文 C"})).json()
    q = (await client.post("/api/v1/requirements", data={"project": "Q", "title": "别的项目", "text": "x"})).json()
    # b 关联一个任务、一条待确认事项、一条依赖边（b 依赖 c）
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(make_case()), review_reply(True)])
    tid = (await client.post("/api/v1/tasks", data={"text": "登录需求", "project": "P"})).json()["task_id"]
    rec = app.state.tasks.get(tid); rec.context = {**rec.context, "requirement_id": b["req_id"], "requirement_title": b["title"]}; app.state.tasks.save(rec)
    item = app.state.requirements.get(b["req_id"]); item["tasks"] = [tid]; app.state.requirements._persist(item)
    await client.post(f"/api/v1/requirements/{b['req_id']}/questions", json={"question": "限红多少？"})
    await client.post("/api/v1/projects/P/dependencies/edges", json={"kind": "requirement", "from": b["req_id"], "to": c["req_id"], "relation": "depends"})
    # 非法：自身 / 跨项目
    assert (await client.post(f"/api/v1/requirements/{b['req_id']}/merge", json={"into": b["req_id"]})).status_code == 400
    assert (await client.post(f"/api/v1/requirements/{b['req_id']}/merge", json={"into": q["req_id"]})).status_code == 400
    r = (await client.post(f"/api/v1/requirements/{b['req_id']}/merge", json={"into": a["req_id"]})).json()
    assert r["req_id"] == a["req_id"] and r["tasks"] == [tid] and r["raw_text"] == "原文 A" and r["status"] == "designing"
    assert [x["question"] for x in r["questions"]] == ["限红多少？"] and r["merged_from"][0]["req_id"] == b["req_id"]
    task = (await client.get(f"/api/v1/tasks/{tid}")).json()
    assert task["context"]["requirement_id"] == a["req_id"]
    # b 进回收站且标记去向、原文仍在；列表不再出现
    assert (await client.get(f"/api/v1/requirements/{b['req_id']}")).status_code == 404
    gone = app.state.requirements.get(b["req_id"])
    assert gone["deleted_at"] and gone["merged_into"] == a["req_id"] and gone["raw_text"] == "原文 B"
    listed = (await client.get("/api/v1/requirements", params={"project": "P"})).json()["requirements"]
    assert {x["req_id"] for x in listed} == {a["req_id"], c["req_id"]}
    # 依赖边改指向 a
    g = (await client.get("/api/v1/projects/P/dependencies", params={"kind": "requirement"})).json()
    assert [(e["from"], e["to"]) for e in g["edges"]] == [(a["req_id"], c["req_id"])]
    logs = (await client.get("/api/v1/audit?days=0")).json()["items"]
    assert any(x["action"] == "合并需求" and x["target"] == b["req_id"] for x in logs)


# ---- 需求链接抓取：网页 / 图片 / 文档直链下载后走既有解析器 ----


def test_链接提取_去尾标点去重():
    from app.parsers.link import extract_urls

    text = "原型见 https://a.com/p/1.png，接口文档：https://b.com/doc)。重复 https://a.com/p/1.png"
    assert extract_urls(text) == ["https://a.com/p/1.png", "https://b.com/doc"]
    assert extract_urls("") == []


def test_网页正文抽取_去脚本导航保留标题层级():
    from app.parsers.link import html_to_markdown

    raw = """<html><head><title>登录需求 - Wiki</title><style>p{}</style></head><body>
    <nav>首页 &gt; 需求</nav><script>alert(1)</script>
    <article><h1>登录</h1><p>用户输入账号密码登录。</p><h2>规则</h2><ul><li>密码错误 5 次锁定 30 分钟</li></ul></article>
    <footer>版权</footer></body></html>"""
    title, body = html_to_markdown(raw)
    assert title == "登录需求 - Wiki"
    assert "# 登录" in body and "## 规则" in body and "- 密码错误 5 次锁定 30 分钟" in body
    assert "alert" not in body and "首页" not in body and "版权" not in body


async def test_链接抓取_拒绝内网地址(tmp_path, monkeypatch):
    import socket

    from app.parsers.link import LinkFetchError, fetch_link

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("169.254.169.254", 0))])
    with pytest.raises(LinkFetchError, match="内网或本机"):
        await fetch_link("http://metadata.internal/latest", tmp_path, 1024)
    with pytest.raises(LinkFetchError, match="有效的 http"):
        await fetch_link("ftp://x.com/a", tmp_path, 1024)


async def test_需求原文只有链接_后台抓取解析入附件(client, monkeypatch, tmp_path):
    """需求原文只贴了一个图片链接：创建即返回「抓取中」的链接附件，后台下载后按图片走 Vision，正文进入设计输入。"""
    import app.api.routes as routes

    async def fake_fetch(url, save_dir, max_bytes, timeout=30.0):
        if url.endswith(".png"):
            from PIL import Image
            dest = save_dir / "abcd1234_原型图.png"
            Image.new("RGB", (64, 64), "white").save(dest)
            return dest
        if "bad" in url:
            from app.parsers.link import LinkFetchError
            raise LinkFetchError("链接返回 HTTP 404")
        dest = save_dir / "abcd1234_wiki.md"
        dest.write_text(f"来源链接：{url}\n\n# 登录需求\n\n密码错误 5 次锁定 30 分钟", encoding="utf-8")
        return dest

    monkeypatch.setattr(routes, "fetch_link", fake_fetch)
    app.state.llm = StubLLM(["# 登录页原型\n\n账号、密码输入框与登录按钮"] * 3)
    await client.post("/api/v1/projects", json={"name": "P"})
    resp = await client.post("/api/v1/requirements", data={
        "project": "P", "title": "链接需求", "text": "https://wiki.example.com/prototype.png",
        "urls": "https://wiki.example.com/pages/login\nhttps://wiki.example.com/bad",
    })
    assert resp.status_code == 200, resp.text
    r = resp.json()
    assert r["source_type"] == "link" and r["parsing"] == 3 and r["raw_text"].startswith("https://")
    assert all(a["source_url"] and a["parsing"] and not a["stored"] for a in r["attachments"])
    r = await _wait_parsed(client, r["req_id"])
    atts = {a["source_url"]: a for a in r["attachments"]}
    assert atts["https://wiki.example.com/prototype.png"]["parsed"] and "原型" in atts["https://wiki.example.com/prototype.png"]["preview"]
    assert atts["https://wiki.example.com/pages/login"]["parsed"] and atts["https://wiki.example.com/pages/login"]["filename"] == "wiki.md"
    bad = atts["https://wiki.example.com/bad"]
    assert not bad["parsed"] and "404" in bad["error"] and not bad["stored"]
    # 抓取失败的链接可重新抓取（走 reparse 入口）
    resp = await client.post(f"/api/v1/requirements/{r['req_id']}/attachments/{bad['att_id']}/reparse")
    assert resp.status_code == 200 and resp.json()["parsing"] == 1
    r = await _wait_parsed(client, r["req_id"])
    # 设计输入包含链接抓到的正文，并标注来源
    from app.requirements import design_brief
    brief = design_brief(app.state.requirements.get(r["req_id"]))
    assert "【链接：https://wiki.example.com/pages/login】" in brief and "锁定 30 分钟" in brief
    # 追加链接接口：超过 10 个拒绝；文本与链接混合视为 mixed
    many = "\n".join(f"https://x.com/{i}" for i in range(11))
    assert (await client.post(f"/api/v1/requirements/{r['req_id']}/attachments", data={"urls": many})).status_code == 400
    assert (await client.post(f"/api/v1/requirements/{r['req_id']}/attachments", data={})).status_code == 400
    mixed = (await client.post("/api/v1/requirements", data={"project": "P", "title": "混合", "text": "正文 https://wiki.example.com/pages/x"})).json()
    assert mixed["source_type"] == "mixed" and mixed["parsing"] == 1
    await _wait_parsed(client, mixed["req_id"])


async def test_删除需求_关联任务拦截_强制删除后任务保留并标记_可恢复(client):
    r = await _req(client)
    rid = r["req_id"]
    # 未关联任务：直接删除，进回收站；列表默认不显示，include_deleted 可见并可恢复
    assert (await client.delete(f"/api/v1/requirements/{rid}")).status_code == 200
    assert (await client.get(f"/api/v1/requirements/{rid}")).status_code == 404
    listed = (await client.get("/api/v1/requirements?project=P")).json()["requirements"]
    assert all(x["req_id"] != rid for x in listed)
    listed = (await client.get("/api/v1/requirements?project=P&include_deleted=true")).json()["requirements"]
    assert next(x for x in listed if x["req_id"] == rid)["deleted_at"]
    assert (await client.post(f"/api/v1/requirements/{rid}/restore")).status_code == 200
    # 发起测试设计后再删：默认 409，force 才删，任务保留并标记来源已删除
    app.state.llm = StubLLM([ANALYSIS_REPLY])
    await client.post(f"/api/v1/requirements/{rid}/analyze", json={})
    for q in (await client.get(f"/api/v1/requirements/{rid}")).json()["questions"]:
        await client.post(f"/api/v1/requirements/{rid}/questions/{q['q_id']}", json={"answer": "按需求"})
    from tests.stubs import ANALYST_REPLY
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(make_case()), review_reply(True)])
    d = await client.post(f"/api/v1/requirements/{rid}/design", json={"confirm_points": False, "async_mode": False})
    assert d.status_code == 200, d.text
    task_id = d.json()["task_id"]
    resp = await client.delete(f"/api/v1/requirements/{rid}")
    assert resp.status_code == 409 and "1 个测试设计任务" in resp.json()["detail"]
    resp = await client.delete(f"/api/v1/requirements/{rid}?force=true")
    assert resp.status_code == 200 and resp.json()["linked_tasks"] == 1
    t = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert t["context"]["requirement_id"] == rid and t["context"]["requirement_deleted"] is True


async def test_待确认事项分级_必须项阻塞设计_建议项带假设继续(client):
    r = await _req(client)
    rid = r["req_id"]
    reply = json.dumps({
        "features": ["账号密码登录"], "rules": [{"text": "密码错误 5 次锁定 30 分钟", "quote": "密码错误 5 次锁定 30 分钟"}],
        "preconditions": [], "normal_flows": [],
        "exception_flows": [], "boundaries": [], "state_changes": [], "permissions": [], "dependencies": [], "risks": [],
        "open_questions": [
            {"question": "锁定期间是否允许找回密码？", "level": "must", "reason": "决定锁定态预期", "assumption": ""},
            {"question": "锁定提示文案是什么？", "level": "suggest", "reason": "文案细节", "assumption": "按「账号已锁定，请 30 分钟后重试」"},
            "历史字符串格式的问题？",
        ],
    }, ensure_ascii=False)
    app.state.llm = StubLLM([reply])
    a = (await client.post(f"/api/v1/requirements/{rid}/analyze", json={})).json()
    by = {q["question"]: q for q in a["questions"]}
    assert by["锁定期间是否允许找回密码？"]["level"] == "must" and by["锁定提示文案是什么？"]["level"] == "suggest"
    assert by["历史字符串格式的问题？"]["level"] == "must"  # 旧格式默认必须确认
    assert a["status"] == "pending_confirm" and a["open_questions"] == 3 and a["blocking_questions"] == 2
    # 两条必须项未确认：不能设计
    assert (await client.post(f"/api/v1/requirements/{rid}/design", json={})).status_code == 409
    # 把一条必须项降为建议并给假设，另一条确认 → 不再阻塞
    hist = by["历史字符串格式的问题？"]["q_id"]
    r2 = await client.put(f"/api/v1/requirements/{rid}/questions/{hist}/level", json={"level": "suggest", "assumption": "按默认处理"})
    assert r2.status_code == 200 and r2.json()["blocking_questions"] == 1
    assert (await client.put(f"/api/v1/requirements/{rid}/questions/{hist}/level", json={"level": "bad"})).status_code == 400
    must = by["锁定期间是否允许找回密码？"]["q_id"]
    a = (await client.post(f"/api/v1/requirements/{rid}/questions/{must}", json={"answer": "允许找回密码"})).json()
    assert a["status"] == "confirmed" and a["blocking_questions"] == 0 and a["open_questions"] == 2
    # 建议级未确认事项带假设进入设计输入；确认结论也在
    from app.requirements import design_brief
    brief = design_brief(app.state.requirements.get(rid))
    assert "确认：允许找回密码" in brief and "假设：按「账号已锁定，请 30 分钟后重试」" in brief and "假设：按默认处理" in brief
    # 人工补充建议级事项不阻塞；补充必须级则阻塞
    a = (await client.post(f"/api/v1/requirements/{rid}/questions", json={"question": "按钮颜色？", "level": "suggest", "assumption": "沿用主题色"})).json()
    assert a["status"] == "confirmed"
    a = (await client.post(f"/api/v1/requirements/{rid}/questions", json={"question": "并发下注如何处理？"})).json()
    assert a["status"] == "pending_confirm" and a["blocking_questions"] == 1


async def test_多需求合并发起测试设计_追溯与删除标记(client):
    from tests.stubs import ANALYST_REPLY

    a = await _req(client, title="登录需求")
    b = await _req(client, title="注册需求", text="用户填写手机号与验证码完成注册。")
    other = await _req(client, project="Q", title="其他项目需求")
    # 跨项目 / 空选择拒绝
    resp = await client.post("/api/v1/requirements/design", json={"req_ids": [a["req_id"], other["req_id"]]})
    assert resp.status_code == 400 and "同一项目" in resp.json()["detail"]
    assert (await client.post("/api/v1/requirements/design", json={"req_ids": []})).status_code == 400
    # 其中一条有必须确认的事项 → 409 并点名
    app.state.llm = StubLLM([ANALYSIS_REPLY])
    await client.post(f"/api/v1/requirements/{b['req_id']}/analyze", json={})
    resp = await client.post("/api/v1/requirements/design", json={"req_ids": [a["req_id"], b["req_id"]]})
    assert resp.status_code == 409 and "注册需求" in resp.json()["detail"]
    for q in (await client.get(f"/api/v1/requirements/{b['req_id']}")).json()["questions"]:
        await client.post(f"/api/v1/requirements/{b['req_id']}/questions/{q['q_id']}", json={"answer": "按需求"})
    # 合并设计：一个任务、上下文记录全部需求、两条需求都回挂
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(make_case()), review_reply(True)])
    resp = await client.post("/api/v1/requirements/design",
                             json={"req_ids": [a["req_id"], b["req_id"], a["req_id"]], "confirm_points": False, "async_mode": False})
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["task_id"]
    assert resp.json()["requirement_ids"] == [a["req_id"], b["req_id"]]
    t = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    ctx = t["context"]
    assert ctx["requirement_id"] == a["req_id"] and ctx["requirement_ids"] == [a["req_id"], b["req_id"]]
    assert ctx["requirement_title"] == "登录需求 + 注册需求" and ctx["requirement_titles"] == ["登录需求", "注册需求"]
    assert "【需求 1/2：登录需求】" in ctx["requirement"] and "【需求 2/2：注册需求】" in ctx["requirement"]
    assert "手机号与验证码" in ctx["requirement"] and "密码错误 5 次" in ctx["requirement"]
    for rid in (a["req_id"], b["req_id"]):
        r = (await client.get(f"/api/v1/requirements/{rid}")).json()
        assert r["status"] == "designing" and task_id in r["tasks"]
    trace = (await client.get(f"/api/v1/requirements/{b['req_id']}")).json()["trace"]
    assert any(x["task_id"] == task_id for x in trace["tasks"])
    # 删除其中一条：任务仍保留另一条来源；两条都删才算失去来源；恢复后回来
    assert (await client.delete(f"/api/v1/requirements/{a['req_id']}?force=true")).status_code == 200
    ctx = (await client.get(f"/api/v1/tasks/{task_id}")).json()["context"]
    assert ctx["deleted_requirement_ids"] == [a["req_id"]] and ctx["requirement_deleted"] is False
    assert (await client.delete(f"/api/v1/requirements/{b['req_id']}?force=true")).status_code == 200
    ctx = (await client.get(f"/api/v1/tasks/{task_id}")).json()["context"]
    assert ctx["requirement_deleted"] is True
    assert (await client.post(f"/api/v1/requirements/{a['req_id']}/restore")).status_code == 200
    ctx = (await client.get(f"/api/v1/tasks/{task_id}")).json()["context"]
    assert ctx["requirement_deleted"] is False and ctx["deleted_requirement_ids"] == [b["req_id"]]


async def test_盲区转需求待确认事项_多需求任务可选需求(client):
    a = await _req(client, title="登录需求")
    b = await _req(client, title="注册需求", text="用户填写手机号与验证码完成注册。")
    analyst = json.dumps({"modules": [{"module": "登录", "points": ["正常登录"]}],
                          "blind_spots": [{"text": "未说明验证码有效期", "module": "注册-注册-验证码",
                                           "ref": "注册需求 › 1 注册 › 1.2 验证码"}]}, ensure_ascii=False)
    gap = json.dumps({"coverage": {}, "additions": []})
    app.state.llm = StubLLM([analyst, gap])
    resp = await client.post("/api/v1/requirements/design",
                             json={"req_ids": [a["req_id"], b["req_id"]], "confirm_points": True, "async_mode": False})
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["task_id"]
    spot = (await client.get(f"/api/v1/tasks/{task_id}")).json()["blind_spots"][0]
    assert spot["module"] == "注册-注册-验证码" and spot["ref"] == "注册需求 › 1 注册 › 1.2 验证码"
    assert (await client.post(f"/api/v1/tasks/{task_id}/blind-spots/{spot['bs_id']}",
                              json={"action": "to_question", "req_id": "nope"})).status_code == 400
    resp = await client.post(f"/api/v1/tasks/{task_id}/blind-spots/{spot['bs_id']}",
                             json={"action": "to_question", "req_id": b["req_id"], "level": "suggest", "note": "按 5 分钟"})
    assert resp.status_code == 200, resp.text
    tgt = resp.json()["blind_spot"]["target"]
    assert tgt["req_id"] == b["req_id"] and tgt["req_title"] == "注册需求" and tgt["level"] == "suggest"
    r = (await client.get(f"/api/v1/requirements/{b['req_id']}")).json()
    q = next(x for x in r["questions"] if x["q_id"] == tgt["q_id"])
    assert q["question"] == "未说明验证码有效期（出处：注册需求 › 1 注册 › 1.2 验证码）" and q["level"] == "suggest"
    assert q["assumption"] == "按 5 分钟" and q["source"] == "manual"
    # 所属模块按出处定：去掉需求标题与章节号（需求自己的列表里不要「注册-」这层前缀），出处原样留着
    assert q["module"] == "注册-验证码" and q["ref"] == "注册需求 › 1 注册 › 1.2 验证码"
    assert all(x["question"] != "未说明验证码有效期" for x in (await client.get(f"/api/v1/requirements/{a['req_id']}")).json()["questions"])
    # 人工补充可带模块；确认结论进设计上下文时带「问（模块）：」，没模块的仍是「问：」
    a2 = (await client.post(f"/api/v1/requirements/{b['req_id']}/questions",
                            json={"question": "验证码几位？", "module": " 注册-验证码 "})).json()
    q2 = next(x for x in a2["questions"] if x["question"] == "验证码几位？")
    assert q2["module"] == "注册-验证码" and "ref" not in q2
    await client.post(f"/api/v1/requirements/{b['req_id']}/questions/{q2['q_id']}", json={"answer": "6 位"})
    q3 = (await client.post(f"/api/v1/requirements/{b['req_id']}/questions", json={"question": "是否限频？"})).json()["questions"][-1]
    await client.post(f"/api/v1/requirements/{b['req_id']}/questions/{q3['q_id']}", json={"answer": "每分钟 1 次"})
    from app.requirements import design_brief
    brief = design_brief(app.state.requirements.get(b["req_id"]))
    assert "- 问（注册-验证码）：验证码几位？\n  确认：6 位" in brief and "- 问：是否限频？\n  确认：每分钟 1 次" in brief


async def test_存量待确认事项回填模块_按依据_出处_章节号_幂等(client):
    from app.requirements import backfill_question_modules

    text = ("## 1. 需求说明\n活动类型为连赢挑战。\n## 2. 活动运营后台\n### 2.1 活动列表\n活动名称最长 30 个字符，超出不允许保存。\n"
            "## 3. 商户管理后台\n### 3.4 日榜排名\n按天排重，同一会员一天只算一次。")
    r = await _req(client, title="后台", text=text)
    item = app.state.requirements.get(r["req_id"])
    item["questions"] = [   # 老版本数据：没有 module 字段
        {"q_id": "q1", "question": "AI 整理的业务规则「…」找不到依据？", "status": "open", "level": "must", "source": "ai",
         "kind": "unverified", "key": "rules", "text": "同一会员一天只算一次，跨天重新计", "quote": "同一会员一天只算一次，跨天重新计"},
        {"q_id": "q2", "question": "未说明跨天怎么算（出处：后台 › 3 商户管理后台 › 3.4 日榜排名）", "status": "open", "level": "must", "source": "manual"},
        {"q_id": "q3", "question": "2.1 活动名称是否允许重名？", "status": "confirmed", "answer": "允许", "level": "must", "source": "ai"},
        {"q_id": "q4", "question": "整体是否支持多语言？", "status": "open", "level": "suggest", "source": "ai"},
        {"q_id": "q5", "question": "按天排重是否按自然日？", "reason": "影响日榜排名的统计口径", "status": "open", "level": "must", "source": "ai"},
        {"q_id": "q6", "question": "早就算过定不到的", "status": "open", "level": "must", "source": "ai", "module": "", "module_v": 1},
        {"q_id": "q7", "question": "模型给的模块不动", "status": "open", "level": "must", "source": "ai", "module": "登录"},
    ]
    app.state.requirements._persist(item)
    assert backfill_question_modules(app.state.requirements) == 4
    assert app.state.requirements.get(r["req_id"])["questions"][6] == {"q_id": "q7", "question": "模型给的模块不动", "status": "open", "level": "must", "source": "ai", "module": "登录"}
    qs = {q["q_id"]: q for q in app.state.requirements.get(r["req_id"])["questions"]}
    assert qs["q1"]["module"] == "商户管理后台-日榜排名" and qs["q2"]["module"] == "商户管理后台-日榜排名"
    assert qs["q2"]["ref"] == "后台 › 3 商户管理后台 › 3.4 日榜排名"
    assert qs["q3"]["module"] == "活动运营后台-活动列表" and qs["q4"]["module"] == ""
    assert qs["q5"]["module"] == "商户管理后台-日榜排名"   # 没章节号：问题 + reason 点到了章节标题
    assert qs["q6"]["module"] == "" and qs["q6"]["module_v"] == 3   # 旧版本规则算过的，规则升级后再算一次
    assert backfill_question_modules(app.state.requirements) == 0   # 定不到的也写了空串并记版本：第二次不再算
    assert (await client.get(f"/api/v1/requirements/{r['req_id']}")).json()["questions"][0]["module"] == "商户管理后台-日榜排名"


def test_盲区出处转模块名_去需求标题与章节号_子标题已带上级名不重复():
    from app.tasks.blind_spots import ref_to_module

    assert ref_to_module("后台 › 3 商户管理后台 › 3.4 日榜排名 › 3.4.7 查询功能说明", "后台") == "商户管理后台-日榜排名-查询功能说明"
    assert ref_to_module("后台 › 3 商户管理后台 › 3.4 日榜排名", "别的标题") == "商户管理后台-日榜排名"   # 没编号的开头紧跟编号章节：也是需求标题
    assert ref_to_module("活动列表 › 创建功能说明", "后台") == "活动列表-创建功能说明"                   # 模型给的无编号出处：整条都是章节
    assert ref_to_module("前端 › 3 PC › 3.1 PC-活动入口说明", "前端") == "PC-活动入口说明"
    assert ref_to_module("", "x") == "" and ref_to_module("后台", "后台") == ""


async def test_需求列表_筛选与分页(client):
    for i in range(1, 24):
        await _req(client, title=f"需求 {i:02d}", text=f"第 {i} 条需求原文" + ("，含支付流程" if i % 5 == 0 else ""))
    other = await _req(client, title="带附件说明", text="原文")
    await client.put(f"/api/v1/requirements/{other['req_id']}", json={"description": "人工补充：需要验证码有效期"})
    await client.post(f"/api/v1/requirements/{other['req_id']}/questions", json={"question": "锁定时长是多少？"})
    # 不传 page_size：不分页返回全部（内部调用兼容）
    data = (await client.get("/api/v1/requirements", params={"project": "P"})).json()
    assert data["total"] == 24 and len(data["requirements"]) == 24 and data["pages"] == 1
    assert data["status_counts"] == {"draft": 24} and data["creators"] == ["anonymous"] and "manual" in data["source_types"]
    # 分页：每页 20，第 2 页 4 条；越界页收敛；非法页大小 400
    data = (await client.get("/api/v1/requirements", params={"project": "P", "page_size": 20, "page": 2})).json()
    assert (data["total"], data["pages"], data["page"], len(data["requirements"])) == (24, 2, 2, 4)
    assert (await client.get("/api/v1/requirements", params={"project": "P", "page_size": 20, "page": 9})).json()["page"] == 2
    assert (await client.get("/api/v1/requirements", params={"project": "P", "page_size": 7})).status_code == 400
    # 关键词覆盖原文 / 人工补充 / 待确认事项；状态 / 来源 / 创建人筛选；status_counts 不随筛选变化
    assert (await client.get("/api/v1/requirements", params={"project": "P", "keyword": "支付流程"})).json()["total"] == 4
    assert (await client.get("/api/v1/requirements", params={"project": "P", "keyword": "验证码有效期"})).json()["total"] == 1
    assert (await client.get("/api/v1/requirements", params={"project": "P", "keyword": "锁定时长"})).json()["total"] == 1
    r = (await client.get("/api/v1/requirements", params={"project": "P", "status": "analyzed"})).json()
    assert r["total"] == 0 and r["status_counts"] == {"draft": 24}
    assert (await client.get("/api/v1/requirements", params={"project": "P", "source_type": "manual"})).json()["total"] == 24
    assert (await client.get("/api/v1/requirements", params={"project": "P", "source_type": "file"})).json()["total"] == 0
    assert (await client.get("/api/v1/requirements", params={"project": "P", "created_by": "nobody"})).json()["total"] == 0


async def test_同项目再上传同一需求_已确认的待确认事项按相似问题沿用结论_像但数字不同只作参考_人工定了标记撤掉(client):
    """同一份文档重新上传成新需求（或新版本）后，上一条需求里人已经确认过的问题不该再问一遍：新分析出的问题与同项目
    其他需求（含回收站里的）已确认问题相似度 ≥ 0.6 且数字一致 → 直接带确认结论进来、标来源；0.4～0.6 或数字不同只作参考预填。"""
    from app.requirements import question_similarity

    # 线上同一需求里不同问题之间最高 0.57（且那几对本就是一问两提）；改写后的同一问题要在 0.6 以上；短句只认原文相同
    assert question_similarity("锁定期间是否允许找回密码？", "锁定期间是否还允许找回密码？") > 0.8
    assert question_similarity("导出间隔≥1分钟是按操作人、页签、导出类型还是全系统计时；不足1分钟时如何处理？",
                               "新增机器人占位的活动ID是否必填，未输入或活动不存在时分别如何提示和处理？") < 0.4
    assert question_similarity("活动ID是否必填", "活动名称是否必填") == 0 and question_similarity("x", "x") == 1.0

    a = await _req(client, title="登录 v1")
    app.state.llm = StubLLM([ANALYSIS_REPLY])
    a = (await client.post(f"/api/v1/requirements/{a['req_id']}/analyze", json={})).json()
    q1, q2 = [q["q_id"] for q in a["questions"]]
    await client.post(f"/api/v1/requirements/{a['req_id']}/questions/{q1}", json={"answer": "允许找回密码"})
    await client.post(f"/api/v1/requirements/{a['req_id']}/questions/{q2}", json={"answer": "按账号"})
    await client.delete(f"/api/v1/requirements/{a['req_id']}")   # 旧的进了回收站也照样沿用

    b = await _req(client, title="登录 v2")
    reply = ANALYSIS_REPLY.replace("锁定期间是否允许找回密码？", "锁定期间是否还允许找回密码？") \
        .replace("锁定次数是否按账号还是按设备统计？", "锁定次数是否按账号还是按设备统计（上限 5 次）？") \
        .replace('"]}', '", "是否记录登录失败日志？"]}')
    app.state.llm = StubLLM([reply])
    b = (await client.post(f"/api/v1/requirements/{b['req_id']}/analyze", json={})).json()
    by_text = {q["question"]: q for q in b["questions"]}
    got = by_text["锁定期间是否还允许找回密码？"]
    assert got["status"] == "confirmed" and got["answer"] == "允许找回密码" and got["source"] == "ai"
    assert got["inherited"]["req_id"] == a["req_id"] and got["inherited"]["title"] == "登录 v1" and got["inherited"]["score"] > 0.8
    hint = by_text["锁定次数是否按账号还是按设备统计（上限 5 次）？"]      # 像，但多了个数字：不自动沿用，只预填参考
    assert hint["status"] == "open" and "inherited" not in hint and hint["suggested"]["answer"] == "按账号"
    assert hint["suggested"]["question"] == "锁定次数是否按账号还是按设备统计？"
    other = by_text["是否记录登录失败日志？"]
    assert other["status"] == "open" and "suggested" not in other and "inherited" not in other
    assert b["open_questions"] == 2 and b["status"] == "pending_confirm"
    # 人自己确认 / 重新打开后，沿用与参考标记都撤掉
    b = (await client.post(f"/api/v1/requirements/{b['req_id']}/questions/{hint['q_id']}", json={"answer": "按账号，无上限"})).json()
    assert "suggested" not in next(q for q in b["questions"] if q["q_id"] == hint["q_id"])
    b = (await client.post(f"/api/v1/requirements/{b['req_id']}/questions/{got['q_id']}", json={"answer": "", "reopen": True})).json()
    re = next(q for q in b["questions"] if q["q_id"] == got["q_id"])
    assert re["status"] == "open" and "inherited" not in re and re["answer"] == "允许找回密码"


async def test_同名附件再上传先查重_替换旧附件删除后整理稿与分析过期(client):
    """同一份文档再上传多半是新版本或重复导入：新建前 GET /requirements/same-files 查同项目里带同名附件的需求，前端据此问
    「替换到已有需求的附件」还是「仍新建」；替换 = DELETE 旧附件 + 追加新文件（解析中 / 提取中不许删，文件一并删掉）。"""
    await client.post("/api/v1/projects", json={"name": "P"})
    await client.post("/api/v1/projects", json={"name": "Q"})
    r = (await client.post("/api/v1/requirements", data={"project": "P", "title": "连赢 10/23"},
                           files=[("files", ("连赢挑战.txt", "报名费满 4 元后当日将无法再次报名".encode(), "text/plain")),
                                  ("files", ("后台需求.txt", "活动名称最长 30 个字符".encode(), "text/plain"))])).json()
    r = await _wait_parsed(client, r["req_id"]); rid = r["req_id"]
    other = (await client.post("/api/v1/requirements", data={"project": "Q", "title": "别的项目", "text": "x"},
                               files=[("files", ("连赢挑战.txt", b"abc", "text/plain"))])).json()
    await _wait_parsed(client, other["req_id"])
    m = (await client.get("/api/v1/requirements/same-files", params={"project": "P", "filename": ["连赢挑战.txt", "新文件.txt"]})).json()["matches"]
    assert [(x["req_id"], x["filenames"], x["status_label"]) for x in m] == [(rid, ["连赢挑战.txt"], "草稿")]
    assert (await client.get("/api/v1/requirements/same-files", params={"project": "P", "filename": "没有.txt"})).json()["matches"] == []
    assert (await client.get("/api/v1/requirements/same-files", params={"project": "P"})).json()["matches"] == []
    atts = {a["filename"]: a for a in r["attachments"]}
    stored = atts["连赢挑战.txt"]["stored"]
    import os
    assert os.path.exists(stored)
    # 解析中不能删
    app.state.requirements.replace_attachment(rid, atts["连赢挑战.txt"]["att_id"], {"parsing": True})
    assert (await client.delete(f"/api/v1/requirements/{rid}/attachments/{atts['连赢挑战.txt']['att_id']}")).status_code == 409
    app.state.requirements.replace_attachment(rid, atts["连赢挑战.txt"]["att_id"], {"parsing": False})
    assert (await client.delete(f"/api/v1/requirements/{rid}/attachments/no-such")).status_code == 404
    r = (await client.delete(f"/api/v1/requirements/{rid}/attachments/{atts['连赢挑战.txt']['att_id']}")).json()
    assert [a["filename"] for a in r["attachments"]] == ["后台需求.txt"] and not os.path.exists(stored)
    assert (await client.get("/api/v1/requirements/same-files", params={"project": "P", "filename": "连赢挑战.txt"})).json()["matches"] == []
    # 追加新版本文件：同名附件回来了，解析后可再查到
    r = (await client.post(f"/api/v1/requirements/{rid}/attachments",
                           files=[("files", ("连赢挑战.txt", "报名费满 5 元后当日将无法再次报名".encode(), "text/plain"))])).json()
    r = await _wait_parsed(client, rid)
    assert sorted(a["filename"] for a in r["attachments"]) == ["后台需求.txt", "连赢挑战.txt"]
    assert "满 5 元" in next(a for a in r["attachments"] if a["filename"] == "连赢挑战.txt")["preview"]


async def test_内嵌图片识图失败_附件标部分解析并告警_拦分析可忽略_失败说明不进底稿(client, tmp_path):
    """线上 06:38 的 401 让 45 张图识图全失败，附件却照样「已解析」，原文少了 4.5 万字没人知道，整理稿照提、底稿多出 45 句
    「解析失败」说明。改：enrich_images 记 doc.vision={total, done, failed, skipped, error}，有失败的附件标部分解析、
    warning 提示重新解析，与「疑似非需求文本」一样拦自动提取 / 分析（可勾忽略）；失败说明是程序写的，不算底稿单元。"""
    from app.agents.ledger import build_units
    from tests.test_mixed_docs import _mixed_docx

    path = _mixed_docx(tmp_path / "混排.docx")
    await client.post("/api/v1/projects", json={"name": "P"})
    app.state.llm = StubLLM([])   # 识图没有可用回复 → 单图失败不阻塞整体
    r = (await client.post("/api/v1/requirements", data={"project": "P", "title": "带图"},
                           files=[("files", ("混排.docx", path.read_bytes(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))])).json()
    r = await _wait_parsed(client, r["req_id"]); rid = r["req_id"]
    att = r["attachments"][0]
    assert att["parsed"] and att["vision"]["total"] == 1 and att["vision"]["failed"] == 1 and att["vision"]["done"] == 0
    assert "1/1 张图片识图失败" in att["warning"] and "重新解析" in att["warning"]
    assert r["content_warnings"] and "识图失败" in r["content_warnings"][0]["warning"]
    resp = await client.post(f"/api/v1/requirements/{rid}/analyze", json={})
    assert resp.status_code == 409 and "识图失败" in resp.json()["detail"]
    # 失败说明行不是产品原文：底稿不记
    item = app.state.requirements.get(rid)
    assert "解析失败" in item["attachments"][0]["text"]
    assert not [u for u in build_units(item["attachments"][0]["text"]) if "解析失败" in u["text"]]
    assert [u["text"] for u in build_units("（[[图片:3]] 为装饰性小图，已跳过）\n正文一句")] == ["正文一句"]
    # 识图正常：没有告警，vision 统计 done
    app.state.llm = StubLLM(["- 弹窗含充值档位按钮：100/500/1000 元", ANALYSIS_REPLY])
    await client.post(f"/api/v1/requirements/{rid}/attachments/{att['att_id']}/reparse")
    r = await _wait_parsed(client, rid)
    att = r["attachments"][0]
    assert att["vision"] == {"total": 1, "done": 1, "failed": 0, "skipped": 0, "error": None} and att["warning"] is None
    assert r["content_warnings"] == []
    assert (await client.post(f"/api/v1/requirements/{rid}/analyze", json={})).status_code == 200


SUGGEST_REPLY = json.dumps({"suggestions": [
    {"index": 1, "answer": "锁定期间不允许找回密码，提示「账号锁定中」", "basis": "原文", "evidence": "密码错误 5 次锁定 30 分钟",
     "risk": "若允许找回，锁定态下找回密码的用例预期相反", "confidence": "中"},
    {"index": 2, "answer": "按账号统计", "basis": "原文", "evidence": "这一句在原文里并不存在", "risk": "", "confidence": "很高"},
    {"index": 3, "answer": "越界编号", "basis": "行业惯例", "evidence": "", "risk": "", "confidence": "低"},
    {"index": 1, "answer": "同一条的第二个答案", "basis": "行业惯例", "evidence": "", "risk": "", "confidence": "低"},
]}, ensure_ascii=False)


async def test_待确认事项推荐答案_原文引文核验_历史结论入参_人确认才算_澄清记录全程(client):
    """G4：must 级事项只有问题与原因，需求负责人从零想。推荐答案按原文 › 历史结论 › 行业惯例给一个确定答案 + 依据 + 风险，
    basis=原文 的引文程序核验、对不上降为行业惯例并标未核验；写到 suggested_answer 预填，人点确认才算。
    同时每条事项带澄清记录：提出 / 沿用 / AI 推荐 / 调整等级 / 确认（采纳 AI 推荐与否）/ 重新打开。"""
    # 旧需求确认过「按账号」→ 新需求的历史结论来源
    a = await _req(client, title="登录 v1")
    app.state.llm = StubLLM([ANALYSIS_REPLY])
    a = (await client.post(f"/api/v1/requirements/{a['req_id']}/analyze", json={})).json()
    q2 = next(q["q_id"] for q in a["questions"] if q["question"].startswith("锁定次数"))
    await client.post(f"/api/v1/requirements/{a['req_id']}/questions/{q2}", json={"answer": "按账号"})
    b = await _req(client, title="登录 v2")
    rid = b["req_id"]
    reply = ANALYSIS_REPLY.replace("锁定次数是否按账号还是按设备统计？", "锁定次数是否按账号还是按设备统计（上限 5 次）？")
    app.state.llm = StubLLM([reply])
    b = (await client.post(f"/api/v1/requirements/{rid}/analyze", json={})).json()
    assert b["open_questions"] == 2 and all(q["history"][0]["action"] == "提出" and "AI 分析" in q["history"][0]["detail"] for q in b["questions"])
    assert next(q for q in b["questions"] if q["question"].startswith("锁定次数"))["suggested"]["answer"] == "按账号"   # 历史结论参考照旧
    app.state.llm = StubLLM([SUGGEST_REPLY])
    r = await client.post(f"/api/v1/requirements/{rid}/questions/suggest", json={})
    assert r.status_code == 200, r.text
    assert r.json()["suggested"] == 2
    user = app.state.llm.calls[0]["messages"][-1]["content"]
    assert "待确认事项" in user and "历史结论" in user and "按账号" in user and "登录 v1" in user   # 相似的历史结论送进去了
    qs = {q["question"]: q for q in r.json()["requirement"]["questions"]}
    q1 = qs["锁定期间是否允许找回密码？"]
    assert q1["suggested_answer"]["answer"].startswith("锁定期间不允许") and q1["suggested_answer"]["basis"] == "原文"
    assert "unverified" not in q1["suggested_answer"] and q1["suggested_answer"]["confidence"] == "中"
    q2b = qs["锁定次数是否按账号还是按设备统计（上限 5 次）？"]
    assert q2b["suggested_answer"]["basis"] == "行业惯例" and q2b["suggested_answer"]["unverified"] is True   # 说是原文却引不出来
    assert q2b["suggested_answer"]["confidence"] == "中"   # 枚举外的置信度按中
    assert [h["action"] for h in q1["history"]] == ["提出", "AI 推荐答案"] and "引文未核验" in q2b["history"][-1]["detail"]
    # 人按推荐确认：记「采纳 AI 推荐」，推荐标记撤掉；另一条自己写：记「未采纳」
    r = (await client.post(f"/api/v1/requirements/{rid}/questions/{q1['q_id']}", json={"answer": q1["suggested_answer"]["answer"]})).json()
    q1 = next(q for q in r["questions"] if q["q_id"] == q1["q_id"])
    assert q1["status"] == "confirmed" and "suggested_answer" not in q1 and q1["history"][-1]["action"] == "确认" and "采纳 AI 推荐" in q1["history"][-1]["detail"]
    r = (await client.put(f"/api/v1/requirements/{rid}/questions/{q2b['q_id']}/level", json={"level": "suggest", "assumption": "按账号"})).json()
    q2b = next(q for q in r["questions"] if q["q_id"] == q2b["q_id"])
    assert q2b["history"][-1]["action"] == "调整等级" and "必须确认 → 建议确认" in q2b["history"][-1]["detail"]
    r = (await client.post(f"/api/v1/requirements/{rid}/questions/{q2b['q_id']}", json={"answer": "按设备"})).json()
    q2b = next(q for q in r["questions"] if q["q_id"] == q2b["q_id"])
    assert "未采纳 AI 推荐" in q2b["history"][-1]["detail"] and r["open_questions"] == 0
    # 重新打开留痕带原结论；人工补充带「人工补充」
    r = (await client.post(f"/api/v1/requirements/{rid}/questions/{q1['q_id']}", json={"answer": "", "reopen": True})).json()
    q1 = next(q for q in r["questions"] if q["q_id"] == q1["q_id"])
    assert q1["status"] == "open" and q1["history"][-1]["action"] == "重新打开" and "原结论：锁定期间不允许" in q1["history"][-1]["detail"]
    r = (await client.post(f"/api/v1/requirements/{rid}/questions", json={"question": "是否支持记住登录？", "level": "suggest", "assumption": "支持 7 天"})).json()
    manual = next(q for q in r["questions"] if q["source"] == "manual")
    assert manual["history"][0]["action"] == "提出" and "人工补充" in manual["history"][0]["detail"] and "支持 7 天" in manual["history"][0]["detail"]
    # 没有未确认事项时 409
    c = await _req(client, title="空需求")
    assert (await client.post(f"/api/v1/requirements/{c['req_id']}/questions/suggest", json={})).status_code == 409


async def test_AI分析完成后自动推荐答案_开关打开时跑_故障只跳过(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "question_suggest", True)
    r = await _req(client)
    rid = r["req_id"]
    app.state.llm = StubLLM([ANALYSIS_REPLY, SUGGEST_REPLY])
    a = (await client.post(f"/api/v1/requirements/{rid}/analyze", json={})).json()
    assert a["open_questions"] == 2 and sum(1 for q in a["questions"] if q.get("suggested_answer")) == 2
    assert next(q for q in a["questions"] if q["question"].startswith("锁定期间"))["suggested_answer"]["basis"] == "原文"
    # 推荐这一步故障：分析结果照常，没有推荐
    app.state.llm = StubLLM([ANALYSIS_REPLY, "不是 JSON", "还不是 JSON"])
    a = (await client.post(f"/api/v1/requirements/{rid}/analyze", json={})).json()
    assert a["open_questions"] == 2 and all(q["suggested_answer"] for q in a["questions"])   # 上一次的推荐还在（没被清掉）

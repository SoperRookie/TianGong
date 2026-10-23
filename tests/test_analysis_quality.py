"""需求分析准确率防线：原文核验（引用溯源）、独立复核、人工修改留痕、评测集与回归评测、需求编写规范。"""

import asyncio
import json

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.agents.analysis import apply_review, normalize, quote_found, verify_analysis
from app.analysis_eval import score_alignment
from app.config import get_settings
from app.main import app
from tests.stubs import StubLLM
from tests.test_requirements import ANALYSIS_REPLY, texts

RAW = "用户输入账号密码登录；密码错误 5 次锁定 30 分钟。锁定 30 分钟后自动解锁。仅注册会员可登录。"


@pytest.fixture
async def client():
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def _req(client, text=RAW, project="P", title="登录需求"):
    await client.post("/api/v1/projects", json={"name": project})
    r = (await client.post("/api/v1/requirements", data={"project": project, "title": title, "text": text})).json()
    return r["req_id"]


def test_原文核验_逐字命中_分段容错_过短不算依据():
    src = normalize(RAW)
    assert quote_found("密码错误 5 次锁定 30 分钟", src)            # 排版差异（空格 / 标点）不影响
    assert quote_found("密码错误5次锁定30分钟。", src)
    assert quote_found("锁定 30 分钟后自动解锁，并发送通知", src) is False   # 拼接了原文没有的内容：命中占比不足
    assert quote_found("密码错误 3 次锁定 30 分钟", src) is False   # 数字改了
    assert quote_found("5 次", src) is False                       # 过短片段到处能匹配，不算依据
    assert quote_found("", src) is False


def test_原文核验_四个事实项无依据降级为待确认_其余项只打标():
    analysis = {
        "rules": [{"text": "密码连续错 5 次锁定 30 分钟", "quote": "密码错误 5 次锁定 30 分钟"},
                  {"text": "锁定期间禁止找回密码", "quote": "锁定期间禁止找回密码"},   # 原文没有 → 降级
                  "无依据的字符串格式规则"],                                           # 旧格式无 quote → 降级
        "boundaries": [{"text": "5 次", "quote": "密码错误 5 次"}],
        "risks": [{"text": "锁定误伤", "quote": ""}, {"text": "依据对不上的风险", "quote": "完全不在原文里的话"}],
        "features": ["账号密码登录"],
        "open_questions": [{"question": "原有问题？", "level": "must"}],
    }
    out = verify_analysis(analysis, RAW)
    assert texts(out["rules"]) == ["密码连续错 5 次锁定 30 分钟"] and out["rules"][0]["verified"] is True
    assert out["boundaries"][0]["verified"] is True
    assert out["risks"][0]["verified"] is None and out["risks"][1]["verified"] is False   # 不降级，只打标
    assert out["features"][0] == {"text": "账号密码登录", "quote": "", "verified": None}
    assert out["verification"] == {"checked": 4, "verified": 2, "unverified": 2, "demoted": 2}   # 没给 quote 的不计入核对
    qs = out["open_questions"]
    assert [q["question"] for q in qs][0] == "原有问题？"
    demoted = [q for q in qs if q.get("kind") == "unverified"]
    assert [q["text"] for q in demoted] == ["锁定期间禁止找回密码", "无依据的字符串格式规则"]
    assert all(q["level"] == "must" and q["key"] == "rules" for q in demoted)
    assert "对不上" in demoted[0]["reason"] and "未给出" in demoted[1]["reason"]


def test_独立复核_质疑标在条目上_遗漏项核验后补入_无依据补入转待确认():
    analysis = verify_analysis({"rules": [{"text": "密码错误 5 次锁定 15 分钟", "quote": "密码错误 5 次锁定 30 分钟"}],
                                "state_changes": [], "open_questions": []}, RAW)
    data = {"issues": [{"key": "rules", "n": 1, "verdict": "wrong", "fix": "密码错误 5 次锁定 30 分钟", "note": "原文是 30 分钟"},
                       {"key": "rules", "n": 9, "verdict": "wrong"},            # 编号越界：忽略
                       {"key": "features", "n": 1, "verdict": "bogus"}],       # 未知结论：忽略
            "missing": [{"key": "state_changes", "text": "锁定 30 分钟后自动解锁", "quote": "锁定 30 分钟后自动解锁"},
                        {"key": "permissions", "text": "只有 VIP 能登录", "quote": "只有 VIP 能登录"},   # 原文没有
                        {"key": "state_changes", "text": "锁定 30 分钟后自动解锁", "quote": "x"}]}      # 重复：忽略
    out = apply_review(analysis, data, RAW, "reviewer-x")
    assert out["rules"][0]["review"] == {"verdict": "wrong", "fix": "密码错误 5 次锁定 30 分钟", "note": "原文是 30 分钟"}
    assert texts(out["state_changes"]) == ["锁定 30 分钟后自动解锁"] and out["state_changes"][0]["review"]["verdict"] == "added"
    assert out["state_changes"][0]["verified"] is True
    assert [q["kind"] for q in out["open_questions"]] == ["review"] and "VIP" in out["open_questions"][0]["question"]
    assert out["review"] == {"model": "reviewer-x", "wrong": 1, "unsupported": 0, "added": 1, "unverified_added": 1}


def test_评测计分_匹配_理解错_捏造_遗漏():
    golden = {"rules": ["a", "b", "c"], "boundaries": ["x"]}
    predicted = {"rules": ["a", "b'", "d"], "boundaries": []}
    verdict = {"rules": {"matched": [[1, 1]], "wrong": [[2, 2, "数值错"]], "extra": [3], "missing": [3]}}
    total, per_key = score_alignment(golden, predicted, verdict)
    assert per_key["rules"]["wrong_items"] == [["b", "b'", "数值错"]] and per_key["rules"]["extra_items"] == ["d"]
    assert per_key["rules"]["missing_items"] == ["c"]
    assert per_key["boundaries"]["missing"] == 1   # 裁判没提到的分析项：标准答案全算遗漏
    assert (total["golden"], total["predicted"], total["matched"], total["wrong"], total["extra"], total["missing"]) == (4, 3, 1, 1, 1, 2)
    assert total["accuracy"] == 0.2 and total["fabricated_rate"] == 0.3333 and total["wrong_rate"] == 0.5 and total["missing_rate"] == 0.5


REVIEW_REPLY = json.dumps({
    "issues": [{"key": "boundaries", "n": 1, "verdict": "wrong", "fix": "密码错误次数上限 5 次（第 5 次即锁定）", "note": "原文第 5 次即锁定"}],
    "missing": [{"key": "state_changes", "text": "锁定 30 分钟后自动解锁", "quote": "锁定 30 分钟后自动解锁"},
                {"key": "permissions", "text": "仅注册会员可登录", "quote": "仅注册会员可登录"}],
}, ensure_ascii=False)


async def test_AI分析_开启独立复核_质疑未处理不能确认_采纳改正后确认并自动进评测集(client, monkeypatch):
    monkeypatch.setenv("TIANGONG_ANALYSIS_REVIEW", "true")
    get_settings.cache_clear()
    rid = await _req(client)
    app.state.llm = StubLLM([ANALYSIS_REPLY, REVIEW_REPLY])
    a = (await client.post(f"/api/v1/requirements/{rid}/analyze", json={"reviewer_model": None})).json()
    assert len(app.state.llm.calls) == 2 and app.state.llm.calls[0]["reasoning_effort"] == "high"
    assert "需求分析结果" in app.state.llm.calls[1]["messages"][1]["content"]
    assert a["review_pending"] == 1 and a["analysis_meta"]["review"]["wrong"] == 1 and a["analysis_meta"]["review"]["added"] == 2
    assert a["analysis"]["boundaries"][0]["review"]["verdict"] == "wrong"
    assert texts(a["analysis"]["state_changes"]) == ["正常→锁定", "锁定 30 分钟后自动解锁"]
    assert a["analysis"]["permissions"][0]["review"]["verdict"] == "added" and a["analysis"]["permissions"][0]["verified"] is True
    # 复核质疑未处理：不能确认、不能作为标准答案
    assert (await client.post(f"/api/v1/requirements/{rid}/analysis/confirm")).status_code == 409
    assert (await client.post(f"/api/v1/requirements/{rid}/analysis/eval-sample")).status_code == 409
    # 采纳复核改正 → 质疑清零，留痕一条（via review）
    a = (await client.post(f"/api/v1/requirements/{rid}/analysis/items",
                           json={"key": "boundaries", "action": "accept_fix", "index": 0})).json()
    assert a["review_pending"] == 0 and texts(a["analysis"]["boundaries"]) == ["密码错误次数上限 5 次（第 5 次即锁定）"]
    assert a["analysis_edit_count"] == 1 and a["analysis_edits"][0]["via"] == "review" and "review" not in a["analysis"]["boundaries"][0]
    assert a["eval_sample"] is None
    # 确认：有人工修改 → 自动加入评测集（标准答案 = 修改后的结果）
    a = (await client.post(f"/api/v1/requirements/{rid}/analysis/confirm")).json()
    assert a["analysis_confirmed"] and a["analysis_confirmed_stale"] is False and a["eval_sample"]["sample_id"] == f"req-{rid}"
    sample = (await client.get(f"/api/v1/analysis-eval/samples/req-{rid}")).json()
    assert sample["origin"] == "confirm" and sample["golden"]["boundaries"] == ["密码错误次数上限 5 次（第 5 次即锁定）"]
    assert sample["golden"]["state_changes"] == ["正常→锁定", "锁定 30 分钟后自动解锁"] and sample["text"].startswith(RAW)
    # 确认后再修改：已确认的变为旧版本，需重新确认
    a = (await client.post(f"/api/v1/requirements/{rid}/analysis/items",
                           json={"key": "rules", "action": "edit", "index": 0, "text": "密码连续错误 5 次锁定 30 分钟"})).json()
    assert a["analysis_confirmed_stale"] is True and a["analysis"]["rules"][0]["manual"] is True
    # 忽略复核意见的操作不留痕
    monkeypatch.setenv("TIANGONG_ANALYSIS_REVIEW", "false")
    get_settings.cache_clear()


async def test_人工修改分析条目_留痕_进学习语料_采纳降级事项(client):
    rid = await _req(client, text="用户输入账号密码登录；密码错误 5 次锁定 30 分钟。")
    reply = json.loads(ANALYSIS_REPLY)
    reply["rules"].append({"text": "锁定期间禁止找回密码", "quote": "锁定期间禁止找回密码"})   # 原文没有 → 降级
    app.state.llm = StubLLM([json.dumps(reply, ensure_ascii=False)])
    a = (await client.post(f"/api/v1/requirements/{rid}/analyze", json={})).json()
    assert a["analysis_meta"]["verification"]["demoted"] == 1 and texts(a["analysis"]["rules"]) == ["密码错误 5 次锁定 30 分钟"]
    demoted = next(q for q in a["questions"] if q.get("kind") == "unverified")
    assert demoted["key"] == "rules" and demoted["text"] == "锁定期间禁止找回密码" and demoted["level"] == "must"
    # 采纳：回到规则项，问题记为已确认
    a = (await client.post(f"/api/v1/requirements/{rid}/questions/{demoted['q_id']}/adopt")).json()
    assert texts(a["analysis"]["rules"]) == ["密码错误 5 次锁定 30 分钟", "锁定期间禁止找回密码"]
    assert next(q for q in a["questions"] if q["q_id"] == demoted["q_id"])["status"] == "confirmed"
    normal = next(q for q in a["questions"] if q.get("kind") != "unverified")
    assert (await client.post(f"/api/v1/requirements/{rid}/questions/{normal['q_id']}/adopt")).status_code == 400
    # 改 / 删 / 增
    url = f"/api/v1/requirements/{rid}/analysis/items"
    a = (await client.post(url, json={"key": "rules", "action": "edit", "index": 0, "text": "密码连续错误 5 次锁定 30 分钟"})).json()
    a = (await client.post(url, json={"key": "features", "action": "delete", "index": 0})).json()
    a = (await client.post(url, json={"key": "permissions", "action": "add", "text": "仅会员可登录"})).json()
    assert a["analysis"]["features"] == [] and texts(a["analysis"]["permissions"]) == ["仅会员可登录"]
    assert [(e["key"], e["action"]) for e in a["analysis_edits"]] == [("rules", "add"), ("rules", "edit"), ("features", "delete"), ("permissions", "add")]
    assert (await client.post(url, json={"key": "open_questions", "action": "add", "text": "x"})).status_code == 400
    assert (await client.post(url, json={"key": "rules", "action": "edit", "index": 9, "text": "x"})).status_code == 400
    assert (await client.post(url, json={"key": "rules", "action": "add", "text": " "})).status_code == 400
    # 学习语料包含需求分析的人工修改
    from app.learning import collect_samples

    samples = collect_samples(app.state.tasks, project="P", requirements=app.state.requirements)
    kinds = {s["来源"] for s in samples}
    assert {"需求分析人工修改", "需求分析人工删除", "需求分析人工新增"} <= kinds
    edit = next(s for s in samples if s["来源"] == "需求分析人工修改")
    assert edit["分析项"] == "业务规则" and edit["修改后"] == "密码连续错误 5 次锁定 30 分钟"
    # 列表视图不带留痕；详情带
    listed = (await client.get("/api/v1/requirements", params={"project": "P"})).json()["requirements"][0]
    assert "analysis_edits" not in listed and listed["analysis_edit_count"] == 4


JUDGE_REPLY = json.dumps({
    "features": {"matched": [[1, 1]], "wrong": [], "extra": [], "missing": []},
    "rules": {"matched": [], "wrong": [[1, 1, "数值"]], "extra": [], "missing": []},
    "boundaries": {"matched": [[1, 1]], "wrong": [], "extra": [], "missing": []},
    "state_changes": {"matched": [], "wrong": [], "extra": [1], "missing": [1]},
}, ensure_ascii=False)


async def test_评测集_加入_修订_回归评测_计分(client):
    rid = await _req(client)
    app.state.llm = StubLLM([ANALYSIS_REPLY])
    await client.post(f"/api/v1/requirements/{rid}/analyze", json={})
    r = (await client.post(f"/api/v1/requirements/{rid}/analysis/eval-sample")).json()
    assert r["sample"]["sample_id"] == f"req-{rid}" and r["sample"]["golden_count"] == 9 and r["requirement"]["eval_sample"]
    listed = (await client.get("/api/v1/analysis-eval/samples", params={"project": "P"})).json()
    assert [s["title"] for s in listed["samples"]] == ["登录需求"] and listed["running"] is None and "rules" in listed["labels"]
    # 修订标准答案（状态变化改成另一条）
    sid = r["sample"]["sample_id"]
    golden = (await client.get(f"/api/v1/analysis-eval/samples/{sid}")).json()["golden"]
    golden["state_changes"] = ["锁定→正常（30 分钟后自动解锁）"]
    s = (await client.put(f"/api/v1/analysis-eval/samples/{sid}", json={"golden": golden, "notes": "人工修订"})).json()
    assert s["golden"]["state_changes"] == ["锁定→正常（30 分钟后自动解锁）"] and s["notes"] == "人工修订"
    assert (await client.put(f"/api/v1/analysis-eval/samples/{sid}", json={"golden": {k: [] for k in golden}})).status_code == 400
    assert (await client.post("/api/v1/analysis-eval/runs", json={"project": "nope"})).status_code == 400
    # 回归评测：分析（桩）→ 裁判（桩）→ 计分
    app.state.llm = StubLLM([ANALYSIS_REPLY, JUDGE_REPLY])
    run = (await client.post("/api/v1/analysis-eval/runs", json={"project": "P"})).json()
    assert run["status"] == "running" and run["total"] == 1 and run["prompt_versions"]["analysis_judge"] == 1
    for _ in range(100):
        got = (await client.get(f"/api/v1/analysis-eval/runs/{run['run_id']}")).json()
        if got["status"] != "running":
            break
        await asyncio.sleep(0.02)
    assert got["status"] == "done" and got["done"] == 1
    t = got["totals"]
    # 标准答案 9 条，AI 输出 9 条：除人工修订过的状态变化外逐字相同 → 程序先行判定匹配 8 条（裁判对 rules 的「理解错」
    # 被程序判定覆盖：文本完全相同不可能理解错）；状态变化：AI 的「正常→锁定」无对应 → 捏造 1，标准答案那条 → 遗漏 1
    assert (t["golden"], t["predicted"], t["matched"], t["wrong"], t["extra"], t["missing"]) == (9, 9, 8, 0, 1, 1)
    assert t["accuracy"] == 0.8 and t["fabricated_rate"] == round(1 / 9, 4) and got["per_key"]["rules"]["matched"] == 1
    sample = got["samples"][0]
    assert sample["per_key"]["state_changes"]["extra_items"] == ["正常→锁定"]
    assert sample["per_key"]["state_changes"]["missing_items"] == ["锁定→正常（30 分钟后自动解锁）"]
    assert sample["predicted"]["rules"] == ["密码错误 5 次锁定 30 分钟"] and sample["judge_model"] == "stub"
    assert "裁判" not in sample.get("error", "") and got["labels"]["rules"] == "业务规则"
    runs = (await client.get("/api/v1/analysis-eval/runs")).json()["runs"]
    assert runs[0]["run_id"] == run["run_id"] and runs[0]["sample_count"] == 1 and "samples" not in runs[0]
    # 删除样本
    assert (await client.delete(f"/api/v1/analysis-eval/samples/{sid}")).status_code == 200
    assert (await client.get("/api/v1/analysis-eval/samples")).json()["samples"] == []
    assert (await client.get(f"/api/v1/requirements/{rid}")).json()["eval_sample"] is None


async def test_需求编写规范接口(client):
    g = (await client.get("/api/v1/requirements/writing-guide")).json()
    assert g["markdown"].startswith("# 需求文档编写规范") and "R1" in g["markdown"] and g["filename"].endswith(".md")


def test_设计上下文_复核质疑未处理的条目不进_已处理的进():
    from app.requirements import design_brief

    item = {"raw_text": "密码错误 5 次锁定 30 分钟。", "attachments": [], "questions": [],
            "analysis": {"rules": [{"text": "密码错误 5 次锁定 30 分钟", "quote": "密码错误 5 次锁定 30 分钟"},
                                   {"text": "锁定 15 分钟", "quote": "锁定 30 分钟", "review": {"verdict": "wrong", "fix": "锁定 30 分钟", "note": ""}},
                                   {"text": "复核补充的条目", "quote": "", "review": {"verdict": "added", "fix": "", "note": ""}}],
                         "boundaries": [{"text": "无依据的边界", "quote": "", "review": {"verdict": "unsupported", "fix": "", "note": ""}}]}}
    brief = design_brief(item)
    assert "密码错误 5 次锁定 30 分钟" in brief and "复核补充的条目" in brief
    assert "锁定 15 分钟" not in brief and "无依据的边界" not in brief and "边界条件" not in brief


async def test_批量确认_复核质疑未处理的跳过(client, monkeypatch):
    monkeypatch.setenv("TIANGONG_ANALYSIS_REVIEW", "true")
    get_settings.cache_clear()
    rid = await _req(client, title="带质疑")
    app.state.llm = StubLLM([ANALYSIS_REPLY, REVIEW_REPLY])
    a = (await client.post(f"/api/v1/requirements/{rid}/analyze", json={})).json()
    assert a["review_pending"] == 1
    pv = (await client.get("/api/v1/requirements/analysis/batch-confirm", params={"project": "P"})).json()
    assert pv["review_pending"] == 1 and pv["pending"] == 0
    r = (await client.post("/api/v1/requirements/analysis/batch-confirm", json={"project": "P"})).json()
    assert r["confirmed"] == [] and [x["reason"] for x in r["skipped"] if x["req_id"] == rid] == ["复核质疑未处理"]
    assert (await client.get(f"/api/v1/requirements/{rid}")).json()["analysis_confirmed"] is None


def test_评测计分_跨分析项匹配不记遗漏加捏造_复核结论同口径():
    from app.analysis_eval import predicted_after_review

    golden = {"rules": ["密码错误 5 次锁定", "锁定 30 分钟"], "boundaries": []}
    predicted = {"rules": ["锁定 30 分钟"], "boundaries": ["密码错误 5 次锁定账号"]}
    verdict = {"rules": {"matched": [[2, 1], [1, 1, "boundaries"]], "wrong": []}, "boundaries": {"matched": [], "wrong": []}}
    total, per_key = score_alignment(golden, predicted, verdict)
    assert (total["matched"], total["missing"], total["extra"]) == (2, 0, 0) and total["accuracy"] == 1.0
    assert per_key["boundaries"]["extra_items"] == []
    # 跨项 wrong：[g, p, 原因, p 所在项]
    verdict = {"rules": {"matched": [[2, 1]], "wrong": [[1, 1, "次数写错", "boundaries"]]}}
    total, per_key = score_alignment(golden, predicted, verdict)
    assert total["wrong"] == 1 and total["extra"] == 0 and per_key["rules"]["wrong_items"][0][1] == "密码错误 5 次锁定账号"

    analysis = {"rules": [{"text": "锁定 15 分钟", "review": {"verdict": "wrong", "fix": "锁定 30 分钟"}},
                          {"text": "无依据", "review": {"verdict": "unsupported"}},
                          {"text": "正常条目"}]}
    pred, stats = predicted_after_review(analysis)
    assert pred["rules"] == ["锁定 30 分钟", "正常条目"] and stats == {"wrong_fixed": 1, "unsupported_dropped": 1}


async def test_采纳降级事项保留原文依据并核验_驳回复核意见留痕不算新版本(client):
    from app.requirements import _analysis_id

    rid = await _req(client)
    reply = json.loads(ANALYSIS_REPLY)
    reply["rules"].append({"text": "锁定 15 分钟后解锁", "quote": "锁定 15 分钟后解锁"})   # 依据对不上 → 降级为待确认
    app.state.llm = StubLLM([json.dumps(reply, ensure_ascii=False)])
    a = (await client.post(f"/api/v1/requirements/{rid}/analyze", json={})).json()
    q = next(x for x in a["questions"] if x.get("kind") == "unverified" and x.get("quote"))
    r = (await client.post(f"/api/v1/requirements/{rid}/questions/{q['q_id']}/adopt", json={})).json()
    added = [x for x in r["analysis"][q["key"]] if x["text"] == q["text"]]
    assert added and added[0]["quote"] == q["quote"] and added[0]["verified"] is False and added[0]["manual"]
    item = app.state.requirements.get(rid)
    before = _analysis_id(item)
    item["analysis"]["rules"][0]["review"] = {"verdict": "wrong", "fix": "x", "note": "n"}
    app.state.requirements._persist(item)
    r = (await client.post(f"/api/v1/requirements/{rid}/analysis/items", json={"key": "rules", "action": "dismiss_review", "index": 0})).json()
    assert r["review_pending"] == 0
    logs = app.state.requirements.get(rid)["analysis_edits"]
    assert logs[-1]["action"] == "dismiss_review" and logs[-1]["review_verdict"] == "wrong"
    assert _analysis_id(app.state.requirements.get(rid)) == before   # 只留痕，不算内容修改


async def test_附件疑似非需求文本_分析与设计拦截_确认后可继续_分析早于附件提示(client, monkeypatch):
    from tests.test_requirements import _wait_parsed

    await client.post("/api/v1/projects", json={"name": "P"})
    css = "\n".join(f"#u{i} {{\n  position:absolute;\n  left:{i}px;\n  width:100px;\n}}" for i in range(300)).encode()
    r = (await client.post("/api/v1/requirements", data={"project": "P", "title": "原型源码"},
                           files=[("files", ("styles.css", css, "text/css"))])).json()
    r = await _wait_parsed(client, r["req_id"]); rid = r["req_id"]
    assert r["content_warnings"] and "疑似非需求文本" in r["content_warnings"][0]["warning"]
    resp = await client.post(f"/api/v1/requirements/{rid}/analyze", json={})
    assert resp.status_code == 409 and "疑似非需求文本" in resp.json()["detail"]
    assert (await client.post(f"/api/v1/requirements/{rid}/design", json={})).status_code == 409
    app.state.llm = StubLLM([ANALYSIS_REPLY])
    a = (await client.post(f"/api/v1/requirements/{rid}/analyze", json={"ignore_warnings": True})).json()
    assert a["analysis"] and a["analysis_outdated"] is False
    # 分析之后追加附件并解析完成 → 提示分析早于附件
    r = (await client.post(f"/api/v1/requirements/{rid}/attachments",
                           files=[("files", ("补充.txt", "密码错误 5 次锁定 30 分钟".encode(), "text/plain"))])).json()
    r = await _wait_parsed(client, rid)
    assert r["analysis_outdated"] is True


def test_分片直接生成的检查点按片隔离():
    from app.agents.service import _chunk_checkpoint

    saved_analysis, saved_mods = [], []
    ck = {"chunks": {"2": {"test_points": [{"module": "登录", "points": []}]}}, "modules": {"2:登录": {"cases": []}, "1:登录": {"cases": [1]}}}
    out = _chunk_checkpoint(ck, lambda a: saved_analysis.append(a), lambda m, r: saved_mods.append(m), 2)
    assert out["checkpoint"] == {"analysis": {"test_points": [{"module": "登录", "points": []}]}, "modules": {"登录": {"cases": []}}}
    out["on_analysis"]({"test_points": []}); out["on_module_done"]("登录", None)
    assert saved_analysis == [{"test_points": [], "chunk_no": 2}] and saved_mods == ["2:登录"]
    assert _chunk_checkpoint(None, None, None, 1) == {"checkpoint": None, "on_analysis": None, "on_module_done": None}


HEADED = """【需求整理稿】
# 「后台」需求整理稿
## 1. 需求说明
活动类型为连赢挑战。
## 2. 活动运营后台
### 2.1 活动列表
#### 2.1.1 基本配置
活动名称最长 30 个字符，超出不允许保存。
### 2.2 派奖查询
按会员账号查询派奖记录。
## 3. 商户管理后台
### 3.4 日榜排名
#### 3.4.7 查询功能说明
按天排重，同一会员一天只算一次。
"""


def test_待确认事项所属模块_按依据所在章节定_没依据按章节号反查_都没有为空():
    from app.agents.analysis import _merge_outputs, section_module

    assert section_module(HEADED, quote="活动名称最长 30 个字符") == "活动运营后台-活动列表-基本配置"
    assert section_module(HEADED, quote="同一会员一天只算一次") == "商户管理后台-日榜排名-查询功能说明"
    assert section_module(HEADED, quote="完全不在原文里的话") == ""
    assert section_module(HEADED, text="3.4.7 “按天排重”未说明跨天怎么算？") == "商户管理后台-日榜排名-查询功能说明"
    assert section_module(HEADED, text="2.2 派奖查询是否支持导出？") == "活动运营后台-派奖查询"
    assert section_module("没有标题的纯文本", quote="纯文本") == ""
    # 核验降级的按 quote 所在章节定；AI 问题模型给了模块的照用、没给的按章节号反查；复核补充未过核验的同样带模块
    analysis = {"rules": [{"text": "活动名称最长 30 个字符", "quote": "活动名称最长 30 个字符，超出不允许保存"},   # 依据对上 → 保留
                          {"text": "同一会员一天只算一次，跨天重新计", "quote": "同一会员一天只算一次，跨天重新计"}],   # 命中 69% < 70% → 降级，但 ≥ 50% 能定位到章节
                "boundaries": [],
                "open_questions": [{"question": "锁定期间允许找回密码？", "level": "must", "module": "登录"},
                                   {"question": "3.4.7 按天排重是否按活动日历天？", "level": "must"}]}
    out = verify_analysis(analysis, HEADED)
    qs = out["open_questions"]
    assert [q["module"] for q in qs[:2]] == ["登录", "商户管理后台-日榜排名-查询功能说明"]
    assert texts(out["rules"]) == ["活动名称最长 30 个字符"]
    demoted = [q for q in qs if q.get("kind") == "unverified"]
    assert len(demoted) == 1 and demoted[0]["module"] == "商户管理后台-日榜排名-查询功能说明"
    assert section_module(HEADED, quote="完全不在原文里的话，一个字都对不上") == ""   # 最像的章节命中不到一半：不乱标
    out = apply_review(out, {"issues": [], "missing": [{"key": "permissions", "text": "只有运营能改活动名称",
                                                        "quote": "活动名称最长 30 个字符，由运营修改"}]}, HEADED, "r")   # 命中 69%：未过核验转待确认，仍定位到章节
    assert out["open_questions"][-1]["kind"] == "review" and out["open_questions"][-1]["module"] == "活动运营后台-活动列表-基本配置"

    class _R:
        model_name = "m"
    merged, _ = _merge_outputs([({"open_questions": [{"question": "a？", "module": " 活动列表 "}, {"question": "b？"}]}, _R())])
    assert [q["module"] for q in merged["open_questions"]] == ["活动列表", ""]

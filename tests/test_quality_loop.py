"""生成质量核心闭环测试（AI测试用例生成工具核心需求设计）：

测试点实体与审核状态机、过粗检测、查漏补缺（只新增）、重复检查、
驳回定点修改（锁定保护 + Diff）、需求变更最小范围更新、学习候选与规则库。
"""

import json

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.main import app
from app.tasks.points import (
    add_points,
    apply_point_review,
    assign_entities,
    case_duplicate_candidates,
    coarse_warnings,
    confirmable_points,
    duplicate_candidates,
)
from tests.stubs import StubLLM, generator_reply, make_case, review_reply


@pytest.fixture
async def client():
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


def _modules():
    return assign_entities([
        {"module": "登录", "points": [
            {"point": "正确账号密码登录成功", "dimension": "正常流程"},
            {"point": "连续错误5次账号锁定", "dimension": "异常流程"},
        ]},
    ])


ANALYST_REPLY_V2 = json.dumps({
    "modules": [{"module": "登录", "points": [
        {"point": "正确账号密码登录成功", "dimension": "正常流程"},
        {"point": "连续错误5次账号锁定", "dimension": "异常流程"},
    ]}],
    "blind_spots": [],
}, ensure_ascii=False)

GAP_REPLY = json.dumps({
    "coverage": {"正常流程": "已覆盖", "异常流程": "已覆盖", "输入校验": "未覆盖",
                 "边界值": "已覆盖", "状态转换": "不适用", "权限": "不适用", "数据": "已覆盖",
                 "重复操作": "未覆盖", "中断": "不适用", "网络": "不适用"},
    "additions": [{"module": "登录", "point": "密码为空点击登录被拦截", "dimension": "输入校验",
                   "reason": "输入校验未覆盖"}],
}, ensure_ascii=False)


# ---- 测试点实体化与状态机 ----


def test_assign_entities_幂等与tp_id():
    modules = _modules()
    tp_ids = [p["tp_id"] for p in modules[0]["points"]]
    assert tp_ids == ["TP001", "TP002"]
    assert all(p["status"] == "pending" and not p["locked"] for p in modules[0]["points"])
    again = assign_entities(modules)  # 幂等：不重新分配 tp_id、不丢状态
    assert [p["tp_id"] for p in again[0]["points"]] == tp_ids


def test_coarse_detection_过粗测试点提示():
    assert coarse_warnings("验证登录整体功能是否正常")
    assert coarse_warnings("覆盖所有情况")
    assert not coarse_warnings("连续错误5次账号锁定")


def test_point_review_approve_即锁定():
    modules = _modules()
    out = apply_point_review(modules, [{"tp_id": "TP001", "action": "approve"}])
    p = modules[0]["points"][0]
    assert (p["status"], p["locked"]) == ("approved", True)
    assert out["counts"]["approve"] == 1


def test_point_review_reject_须带意见且连续驳回提示():
    modules = _modules()
    with pytest.raises(ValueError):
        apply_point_review(modules, [{"tp_id": "TP001", "action": "reject"}])
    with pytest.raises(ValueError):  # 完整需求 6.4：驳回类型必填
        apply_point_review(modules, [{"tp_id": "TP001", "action": "reject", "comment": "过粗"}])
    apply_point_review(modules, [{"tp_id": "TP001", "action": "reject", "comment": "颗粒度过粗",
                                  "reject_types": ["颗粒度过粗"]}])
    out = apply_point_review(modules, [{"tp_id": "TP001", "action": "reject", "comment": "仍然过粗",
                                        "reject_types": ["颗粒度过粗"]}])
    assert modules[0]["points"][0]["reject_count"] == 2
    assert out["hints"] and "TP001" in out["hints"][0]  # 需求三十五：连续驳回提示人工介入


def test_confirmable_points_只取通过项且驳回项永不进入():
    modules = _modules()
    # 全部未审核：沿用全量
    assert sum(len(e["points"]) for e in confirmable_points(modules)) == 2
    apply_point_review(modules, [
        {"tp_id": "TP001", "action": "approve"},
        {"tp_id": "TP002", "action": "reject", "comment": "重复", "reject_types": ["重复"]},
    ])
    confirmed = confirmable_points(modules)
    assert [p["point"] for e in confirmed for p in e["points"]] == ["正确账号密码登录成功"]


def test_add_points_只新增且生成前查重():
    modules = _modules()
    added = add_points(modules, [
        {"module": "登录", "point": "密码为空点击登录被拦截", "dimension": "输入校验"},
        {"module": "登录", "point": "正确账号密码登录成功"},  # 与已有高度相似：跳过
    ], source="gap")
    assert len(added) == 1 and added[0]["source"] == "gap" and added[0]["status"] == "pending"
    assert len(modules[0]["points"]) == 3  # 存量 2 条不动


def test_duplicate_candidates_文字初筛():
    modules = assign_entities([{"module": "登录", "points": [
        "验证密码为空无法登录", "验证密码为空时登录失败", "连续错误5次账号锁定",
    ]}])
    pairs = duplicate_candidates(modules)
    assert len(pairs) == 1
    assert {pairs[0]["a"], pairs[0]["b"]} == {"TP001", "TP002"}


def test_case_duplicate_candidates_综合字段比对():
    a = make_case()
    b = make_case(case_id="TC-登录-002")  # 同标题同步骤
    c = make_case(case_id="TC-登录-003", title="验证锁定策略",
                  steps=[{"action": "连续输错5次密码", "expected": "账号被锁定"}])
    pairs = case_duplicate_candidates([a, b, c])
    assert len(pairs) == 1 and {pairs[0]["a"], pairs[0]["b"]} == {"TC-登录-001", "TC-登录-002"}


# ---- 定点修改确定性合并 ----


def test_apply_point_fixes_split与越权保护():
    from app.agents.quality import apply_point_fixes

    modules = _modules()
    apply_point_review(modules, [{"tp_id": "TP002", "action": "reject", "comment": "颗粒度过粗",
                                  "reject_types": ["颗粒度过粗"]}])
    data = {"fixes": [
        {"tp_id": "TP002", "comment_type": "颗粒度过粗", "action": "split",
         "split_into": [{"point": "密码错误登录失败", "dimension": "异常流程"},
                        {"point": "连续错误5次账号锁定", "dimension": "异常流程"}]},
        {"tp_id": "TP001", "action": "modify", "point": "越权改动"},  # 非驳回项：忽略
    ]}
    out = apply_point_fixes(modules, data, allowed={"TP002"})
    points = modules[0]["points"]
    assert [p["tp_id"] for p in points] == ["TP001", "TP002-01", "TP002-02"]
    assert points[0]["point"] == "正确账号密码登录成功"  # 越权改动被丢弃
    assert all(p["status"] == "pending" for p in points[1:])  # 局部修改 → 再审核
    assert len(out["diff"]) == 1 and out["diff"][0]["action"] == "split"


def test_merge_case_fix_锁定保护与顺延编号():
    from app.agents.quality import merge_case_fix

    cases = [
        dict(make_case(), uid="u1"),
        dict(make_case(case_id="TC-登录-002", title="验证密码错误提示"), uid="u2"),
    ]
    reviews = {
        "u1": {"status": "approved", "locked": True, "comment": "", "reject_count": 0},
        "u2": {"status": "rejected", "locked": False, "comment": "预期不可验证", "reject_count": 1},
    }
    data = {
        "cases": [
            dict(make_case(), title="越权修改锁定用例"),                      # TC-登录-001 锁定：丢弃
            dict(make_case(case_id="TC-登录-002", title="验证密码错误提示文案")),  # 允许修改
            dict(make_case(case_id="TC-登录-009", title="新增：锁定后正确密码仍拒绝")),  # 新增
        ],
        "deleted": [],
    }
    out = merge_case_fix(cases, data, allowed={"TC-登录-002"}, reviews=reviews)
    result = out["cases"]
    assert result[0]["title"] == "验证正确账号密码登录成功"  # 锁定用例原样
    assert result[1]["title"] == "验证密码错误提示文案"
    assert result[2]["case_id"] == "TC-登录-003"  # 新增顺延编号，不重排存量
    assert reviews[result[1]["uid"]]["status"] == "pending"  # 修改后回到待审核
    actions = {d["action"] for d in out["diff"]}
    assert actions == {"modify", "add"}


# ---- 规则库（需求三十六~三十九）----


def test_rule_store_候选确认与范围过滤(tmp_path):
    from app.learning import RuleStore

    store = RuleStore(tmp_path / "rules.json")
    added = store.add_candidates([
        {"content": "支付场景默认加入快速重复点击", "evidence": "12条中10条人工增加", "occurrences": 10,
         "confidence": "高", "scope_hint": "project"},
        {"content": "支付场景默认加入快速重复点击", "occurrences": 3},  # 重复：去重
    ], project="德州扑克")
    assert len(added) == 1 and added[0].status == "candidate"
    # 候选不生效：active_rules 为空
    assert store.active_rules("德州扑克") == []
    with pytest.raises(ValueError):
        store.confirm(added[0].rule_id, "project", project=None)  # 项目级必须给项目名
    store.confirm(added[0].rule_id, "project", project="德州扑克")
    assert len(store.active_rules("德州扑克")) == 1
    assert store.active_rules("斗地主") == []  # 项目隔离（需求三十九）
    notes, snapshot = store.render("德州扑克")
    assert "快速重复点击" in notes and snapshot[0]["scope"] == "project"


# ---- API：测试点审核工作台 ----


async def _create_confirm_task(client, extra_replies=()):
    app.state.llm = StubLLM([ANALYST_REPLY_V2, GAP_REPLY, *extra_replies])
    resp = await client.post(
        "/api/v1/tasks",
        data={"text": "登录需求：账号密码登录，连续错误5次锁定", "confirm_points": "true"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_confirm_flow_实体化与自动查漏(client):
    data = await _create_confirm_task(client)
    # 拆解 2 条 + 查漏新增 1 条（只新增，存量不动）
    points = data["test_points"][0]["points"]
    assert [p["tp_id"] for p in points] == ["TP001", "TP002", "TP003"]
    assert points[2]["source"] == "gap"
    assert data["coverage"]["输入校验"] == "未覆盖"


async def test_points_review_批量与驳回后定点修改(client):
    data = await _create_confirm_task(client)
    task_id = data["task_id"]
    # 批量通过 + 驳回（需求四十九/五十）
    resp = await client.post(f"/api/v1/tasks/{task_id}/points/review", json={"items": [
        {"tp_id": "TP001", "action": "approve"},
        {"tp_id": "TP003", "action": "approve"},
        {"tp_id": "TP002", "action": "reject", "comment": "测试点过大，需要拆分登录失败和账号锁定",
         "reject_types": ["颗粒度过粗"]},
    ]})
    assert resp.status_code == 200
    assert resp.json()["counts"] == {"approve": 2, "reject": 1, "modify": 0, "delete": 0, "unlock": 0}

    # AI 定点修改：只处理被驳回的 TP002，拆分为两条（需求三十三）
    app.state.llm = StubLLM([json.dumps({"fixes": [
        {"tp_id": "TP002", "comment_type": "颗粒度过粗", "action": "split",
         "split_into": [{"point": "密码错误登录失败", "dimension": "异常流程"},
                        {"point": "连续错误5次账号锁定", "dimension": "异常流程"}]},
    ], "additions": []}, ensure_ascii=False)])
    resp = await client.post(f"/api/v1/tasks/{task_id}/points/fix")
    assert resp.status_code == 200
    fixed = resp.json()
    assert fixed["diff"][0]["comment_type"] == "颗粒度过粗"
    tp_ids = [p["tp_id"] for p in fixed["test_points"][0]["points"]]
    assert "TP002-01" in tp_ids and "TP002-02" in tp_ids

    # 确认生成：只有已通过的点进入正式测试点（TP002-xx 尚未通过）
    app.state.llm = StubLLM([generator_reply(make_case()), review_reply(True)])
    resp = await client.post(f"/api/v1/tasks/{task_id}/confirm", json={})
    assert resp.status_code == 200
    gen_prompt = app.state.llm.calls[0]["messages"][1]["content"]
    assert "正确账号密码登录成功" in gen_prompt
    assert "密码错误登录失败" not in gen_prompt  # 未通过的点不进入生成


async def test_points_add_ai补充只新增(client):
    data = await _create_confirm_task(client)
    task_id = data["task_id"]
    app.state.llm = StubLLM([json.dumps({"additions": [
        {"module": "登录", "point": "网络切换后重新登录保持会话", "dimension": "网络"},
    ]}, ensure_ascii=False)])
    resp = await client.post(f"/api/v1/tasks/{task_id}/points/add",
                             json={"instruction": "补充网络切换场景"})
    assert resp.status_code == 200
    added = resp.json()["added"]
    assert len(added) == 1 and added[0]["source"] == "supplement"


# ---- API：用例驳回定点修改与锁定 ----


async def _create_completed_task(client, *cases):
    from tests.stubs import ANALYST_REPLY

    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(*cases), review_reply(True)])
    resp = await client.post("/api/v1/tasks", data={"text": "登录需求"})
    return resp.json()["task_id"]


async def test_case_reject_then_fix_锁定保护(client):
    task_id = await _create_completed_task(
        client, make_case(), make_case(case_id="TC-登录-002", title="验证密码错误提示"),
    )
    resp = await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [
        {"case_id": "TC-登录-001", "action": "approve"},
        {"case_id": "TC-登录-002", "action": "reject", "comment": "预期结果空泛不可验证",
         "reject_types": ["预期不可验证"]},
    ]})
    assert resp.status_code == 200

    fixed_case = make_case(case_id="TC-登录-002", title="验证密码错误提示",
                           steps=[{"action": "输入错误密码提交", "expected": "提示「密码错误，还可尝试4次」"}])
    app.state.llm = StubLLM([json.dumps({
        "fixes": [{"case_id": "TC-登录-002", "comment_type": "预期不可验证", "note": "补充具体文案"}],
        "cases": [fixed_case], "deleted": [],
    }, ensure_ascii=False)])
    resp = await client.post(f"/api/v1/tasks/{task_id}/cases/fix")
    assert resp.status_code == 200
    data = resp.json()
    assert data["fixes"][0]["comment_type"] == "预期不可验证"
    # 修改仅发送被驳回用例（需求三十一）
    sent = app.state.llm.calls[0]["messages"][1]["content"]
    assert "TC-登录-002" in sent and "TC-登录-001" not in sent

    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    titles = {c["case_id"]: c for c in task["result"]["cases"]}
    assert "还可尝试4次" in titles["TC-登录-002"]["steps"][0]["expected"]
    # 锁定用例原样保留，状态机：修改后的用例回到 pending
    states = list(task["case_reviews"].values())
    assert sorted(s["status"] for s in states) == ["approved", "pending"]


async def test_revise_不动锁定用例(client):
    task_id = await _create_completed_task(
        client, make_case(), make_case(case_id="TC-登录-002", title="验证密码错误提示"),
    )
    await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [
        {"case_id": "TC-登录-001", "action": "approve"},
    ]})
    app.state.llm = StubLLM([
        json.dumps({"cases": [dict(make_case(case_id="TC-登录-001", title="修订后的密码错误提示"))],
                    "deleted": []}, ensure_ascii=False),
        review_reply(True),
    ])
    resp = await client.post(f"/api/v1/tasks/{task_id}/revise",
                             json={"instruction": "密码错误提示补充剩余次数"})
    assert resp.status_code == 200
    # 修订上下文只含未锁定用例
    sent = app.state.llm.calls[0]["messages"][1]["content"]
    assert "验证密码错误提示" in sent and "验证正确账号密码登录成功" not in sent
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    titles = [c["title"] for c in task["result"]["cases"]]
    assert "验证正确账号密码登录成功" in titles  # 锁定用例完好


async def test_confirm_points_后台执行可见可管理(client):
    """先审核测试点 + 后台执行：任务立即落库可见，拆解完成后进入待确认。"""
    import asyncio

    app.state.llm = StubLLM([ANALYST_REPLY_V2, GAP_REPLY])
    resp = await client.post(
        "/api/v1/tasks",
        data={"text": "登录需求", "confirm_points": "true", "async_mode": "true"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"  # 立即返回，任务已可在列表查看
    task_id = body["task_id"]

    for _ in range(100):  # 等待后台拆解 + 查漏完成
        await asyncio.sleep(0.02)
        task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
        if task["status"] == "awaiting_confirmation" and task["progress"] is None:
            break
    assert task["status"] == "awaiting_confirmation"
    points = task["analysis"]["test_points"][0]["points"]
    assert points[0]["tp_id"] == "TP001"  # 实体化完成
    assert task["coverage"]["输入校验"] == "未覆盖"  # 后台查漏已补充


async def test_cancel_运行中的后台任务(client):
    """任务管理：进行中的后台任务可取消，标记失败并留痕。"""
    import asyncio

    class HangLLM:  # 模拟长时间运行的模型调用
        async def chat(self, *args, **kwargs):
            await asyncio.Event().wait()

    app.state.llm = HangLLM()
    resp = await client.post("/api/v1/tasks", data={"text": "登录需求", "async_mode": "true"})
    task_id = resp.json()["task_id"]
    await asyncio.sleep(0.05)  # 等后台任务启动

    resp = await client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert resp.status_code == 200 and resp.json()["canceled"] is True
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert task["status"] == "failed" and "取消" in task["error"]
    # 重复取消：无可取消的执行
    assert (await client.post(f"/api/v1/tasks/{task_id}/cancel")).status_code == 409


async def test_task_list_项目名展示与过滤(client):
    from tests.stubs import ANALYST_REPLY

    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(make_case()), review_reply(True)])
    resp = await client.post("/api/v1/tasks", data={"text": "登录需求", "project": "德州扑克"})
    assert resp.status_code == 200

    data = (await client.get("/api/v1/tasks")).json()
    assert any(t["project"] == "德州扑克" for t in data["tasks"])
    filtered = (await client.get("/api/v1/tasks", params={"project": "德州扑克"})).json()
    assert filtered["tasks"] and all(t["project"] == "德州扑克" for t in filtered["tasks"])
    empty = (await client.get("/api/v1/tasks", params={"project": "不存在的项目"})).json()
    assert empty["tasks"] == []


async def test_项目增删改查与改名联动(client):
    # 新建 + 重名拒绝
    resp = await client.post("/api/v1/projects", json={"name": "增删改查", "description": "CRUD 验证"})
    assert resp.status_code == 200
    assert (await client.post("/api/v1/projects", json={"name": "增删改查"})).status_code == 400

    # 创建任务时选择该项目（自动注册路径同样兼容新名称）
    from tests.stubs import ANALYST_REPLY

    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(make_case()), review_reply(True)])
    resp = await client.post("/api/v1/tasks", data={"text": "登录需求", "project": "增删改查"})
    task_id = resp.json()["task_id"]

    # 列表带描述与统计
    projects = (await client.get("/api/v1/projects")).json()["projects"]
    row = next(p for p in projects if p["project"] == "增删改查")
    assert row["description"] == "CRUD 验证" and row["tasks"] == 1

    # 有任务不可删；改名联动任务归属
    assert (await client.delete("/api/v1/projects/增删改查")).status_code == 400
    resp = await client.put("/api/v1/projects/增删改查",
                            json={"name": "增删改查V2", "description": "已改名"})
    assert resp.status_code == 200
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert task["context"]["project"] == "增删改查V2"
    listed = (await client.get("/api/v1/tasks", params={"project": "增删改查V2"})).json()["tasks"]
    assert any(t["task_id"] == task_id for t in listed)

    # 空项目可删
    await client.post("/api/v1/projects", json={"name": "空项目"})
    assert (await client.delete("/api/v1/projects/空项目")).status_code == 200
    assert (await client.delete("/api/v1/projects/空项目")).status_code == 404


async def test_用例执行轮次与执行记录(client):
    task_id = await _create_completed_task(
        client, make_case(), make_case(case_id="TC-登录-002", title="验证密码错误提示"),
    )
    # 新建轮次
    run = (await client.post(f"/api/v1/tasks/{task_id}/executions", json={"name": "冒烟"})).json()
    assert run["name"] == "冒烟" and run["summary"]["total"] == 2
    # 未结束前不可再开新轮次
    assert (await client.post(f"/api/v1/tasks/{task_id}/executions", json={})).status_code == 409

    rid = run["run_id"]
    # 失败必须填原因；未知状态拒绝
    assert (await client.post(f"/api/v1/tasks/{task_id}/executions/{rid}/results",
            json={"items": [{"case_id": "TC-登录-001", "status": "fail"}]})).status_code == 400
    assert (await client.post(f"/api/v1/tasks/{task_id}/executions/{rid}/results",
            json={"items": [{"case_id": "TC-登录-001", "status": "ok"}]})).status_code == 400
    # 批量记录：1 通过 + 1 失败（带缺陷号）
    resp = await client.post(f"/api/v1/tasks/{task_id}/executions/{rid}/results", json={"items": [
        {"case_id": "TC-登录-001", "status": "pass"},
        {"case_id": "TC-登录-002", "status": "fail", "note": "BUG-1024 提示文案错误"},
    ]})
    assert resp.status_code == 200
    summary = resp.json()["summary"]
    assert summary["pass"] == 1 and summary["fail"] == 1 and summary["pass_rate"] == 0.5

    # 复测覆盖并保留历史
    resp = await client.post(f"/api/v1/tasks/{task_id}/executions/{rid}/results", json={"items": [
        {"case_id": "TC-登录-002", "status": "pass"},
    ]})
    results = resp.json()["results"]
    retested = next(r for r in results.values() if r["case_id"] == "TC-登录-002")
    assert retested["status"] == "pass" and retested["history"][0]["status"] == "fail"
    assert resp.json()["summary"]["pass_rate"] == 1.0

    # 结束轮次后不可继续记录；可开第二轮（回归）
    assert (await client.post(f"/api/v1/tasks/{task_id}/executions/{rid}/finish")).status_code == 200
    assert (await client.post(f"/api/v1/tasks/{task_id}/executions/{rid}/results",
            json={"items": [{"case_id": "TC-登录-001", "status": "pass"}]})).status_code == 409
    run2 = (await client.post(f"/api/v1/tasks/{task_id}/executions", json={})).json()
    assert run2["name"] == "第 2 轮执行"
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert len(task["executions"]) == 2 and task["executions"][0]["finished_at"]


# ---- API：需求变更最小范围更新 ----


async def test_requirement_diff_analyze_and_apply(client):
    task_id = await _create_completed_task(
        client, make_case(), make_case(case_id="TC-登录-002", title="验证错误3次锁定"),
    )
    app.state.llm = StubLLM([json.dumps({
        "changes": [{"type": "规则调整", "description": "锁定次数 3 次改为 5 次",
                     "affected_points": [], "affected_cases": ["TC-登录-002"],
                     "action_hint": "更新锁定边界"}],
        "new_requirements": [], "summary": "锁定次数调整",
    }, ensure_ascii=False)])
    resp = await client.post(f"/api/v1/tasks/{task_id}/requirement-diff",
                             json={"new_requirement": "账号密码登录，连续错误5次锁定"})
    assert resp.status_code == 200
    assert resp.json()["changes"][0]["affected_cases"] == ["TC-登录-002"]

    updated = make_case(case_id="TC-登录-002", title="验证错误5次锁定")
    app.state.llm = StubLLM([json.dumps({
        "fixes": [], "cases": [updated], "deleted": [],
    }, ensure_ascii=False)])
    resp = await client.post(f"/api/v1/tasks/{task_id}/requirement-diff/apply")
    assert resp.status_code == 200
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    titles = [c["title"] for c in task["result"]["cases"]]
    assert "验证错误5次锁定" in titles and "验证正确账号密码登录成功" in titles
    assert task["context"]["requirement"] == "账号密码登录，连续错误5次锁定"  # 基线更新
    # 重复应用被拒绝
    resp = await client.post(f"/api/v1/tasks/{task_id}/requirement-diff/apply")
    assert resp.status_code == 409


# ---- API：学习候选 → 确认 → 注入生成 ----


async def test_learning_loop_候选确认与注入(client):
    task_id = await _create_completed_task(client, make_case())
    # 制造人工修改留痕
    await client.post(f"/api/v1/tasks/{task_id}/review", json={"items": [
        {"case_id": "TC-登录-001", "action": "modify", "feedback": "统一步骤精简",
         "case": make_case(steps=[{"action": "登录并校验", "expected": "进入首页"}])},
    ]})
    app.state.llm = StubLLM([json.dumps({"candidates": [
        {"content": "登录场景步骤保持精简，单条用例不超过3步", "evidence": "人工修改 2 次",
         "occurrences": 2, "confidence": "中", "scope_hint": "team"},
    ]}, ensure_ascii=False)])
    resp = await client.post("/api/v1/learning/analyze", json={})
    assert resp.status_code == 200
    candidates = resp.json()["candidates"]
    assert len(candidates) == 1

    rule_id = candidates[0]["rule_id"]
    resp = await client.post(f"/api/v1/learning/rules/{rule_id}/confirm", json={"scope": "team"})
    assert resp.status_code == 200 and resp.json()["status"] == "active"

    # 新任务生成时注入规则（需求六十一：反哺下一次生成）
    from tests.stubs import ANALYST_REPLY

    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(make_case()), review_reply(True)])
    resp = await client.post("/api/v1/tasks", data={"text": "登录需求"})
    assert resp.status_code == 200
    gen_prompt = app.state.llm.calls[1]["messages"][1]["content"]
    assert "步骤保持精简" in gen_prompt and "团队/项目测试规则" in gen_prompt

"""M4 测试：测试计划实体、用例快照、任务分配、计划执行与附件、旧执行迁移。"""

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


async def _task_with_approved(client, *cases, project="P端", approve=None) -> str:
    """建任务并把指定 case_id（默认全部）评审通过。"""
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(*cases), review_reply(True)])
    resp = await client.post(
        "/api/v1/tasks",
        files={"files": ("需求.txt", b"# login", "text/plain")},
        data={"project": project},
    )
    task_id = resp.json()["task_id"]
    ids = approve if approve is not None else [c["case_id"] for c in cases]
    if ids:
        resp = await client.post(
            f"/api/v1/tasks/{task_id}/review",
            json={"items": [{"case_id": cid, "action": "accept"} for cid in ids]},
        )
        assert resp.status_code == 200, resp.text
    return task_id


async def _plan(client, name="冒烟计划", project="P端", **kw) -> dict:
    resp = await client.post("/api/v1/plans", json={"name": name, "project": project, **kw})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---- 计划实体 ----


async def test_plan_crud_and_status(client):
    resp = await client.post("/api/v1/plans", json={"name": " ", "project": "P端"})
    assert resp.status_code == 400
    plan = await _plan(client, owner="admin", start_date="2026-09-14", end_date="2026-09-18")
    assert plan["status"] == "not_started" and plan["owner"] == "admin"

    pid = plan["plan_id"]
    resp = await client.put(f"/api/v1/plans/{pid}", json={"name": "回归计划", "status": "in_progress"})
    assert resp.json()["name"] == "回归计划" and resp.json()["status_label"] == "进行中"
    assert (await client.put(f"/api/v1/plans/{pid}", json={"status": "闲聊"})).status_code == 400

    listed = (await client.get("/api/v1/plans", params={"project": "P端"})).json()["plans"]
    assert [p["plan_id"] for p in listed] == [pid]
    assert (await client.delete(f"/api/v1/plans/{pid}")).status_code == 200
    assert (await client.get(f"/api/v1/plans/{pid}")).status_code == 404


# ---- 用例快照（只能加入已通过；加入即定格）----


async def test_only_approved_cases_can_join(client):
    task_id = await _task_with_approved(
        client, make_case(), make_case(case_id="TC-登录-002", title="验证密码错误提示"),
        approve=["TC-登录-001"],
    )
    plan = await _plan(client)
    # 候选池只给已通过用例
    cands = (await client.get(
        f"/api/v1/plans/{plan['plan_id']}/candidates", params={"task_id": task_id}
    )).json()["cases"]
    assert [c["case_id"] for c in cands] == ["TC-登录-001"]

    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    uid_pending = next(
        c["uid"] for c in task["result"]["cases"] if c["case_id"] == "TC-登录-002"
    )
    resp = await client.post(
        f"/api/v1/plans/{plan['plan_id']}/cases",
        json={"task_id": task_id, "uids": [uid_pending]},
    )
    assert resp.status_code == 409 and "已通过" in resp.json()["detail"]

    # 筛选全量加入：只落已通过那条；重复加入自动跳过
    resp = await client.post(
        f"/api/v1/plans/{plan['plan_id']}/cases", json={"task_id": task_id}
    )
    assert resp.status_code == 200 and resp.json()["added"] == 1
    resp = await client.post(
        f"/api/v1/plans/{plan['plan_id']}/cases", json={"task_id": task_id}
    )
    assert resp.json()["added"] == 0


async def test_snapshot_frozen_after_case_modified(client):
    task_id = await _task_with_approved(client, make_case())
    plan = await _plan(client)
    await client.post(f"/api/v1/plans/{plan['plan_id']}/cases", json={"task_id": task_id})
    detail = (await client.get(f"/api/v1/plans/{plan['plan_id']}")).json()
    item = detail["items"][0]
    assert item["snapshot"]["title"] == "验证正确账号密码登录成功"
    assert item["version_no"] >= 1  # 快照引用 entity_versions 版本记录（M3 冻结约定）

    # 正式用例继续演进：解锁后人工修改
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    case = task["result"]["cases"][0]
    await client.post(f"/api/v1/tasks/{task_id}/review",
                      json={"items": [{"case_id": case["case_id"], "action": "unlock"}]})
    modified = {**case, "title": "改动后的标题"}
    resp = await client.post(
        f"/api/v1/tasks/{task_id}/review",
        json={"items": [{"case_id": case["case_id"], "action": "modify", "case": modified,
                         "base_version": case["version"], "feedback": "调整标题"}]},
    )
    assert resp.status_code == 200, resp.text

    # 计划内快照不动（12.2 交付验收：改正式用例不影响计划快照）
    detail = (await client.get(f"/api/v1/plans/{plan['plan_id']}")).json()
    assert detail["items"][0]["snapshot"]["title"] == "验证正确账号密码登录成功"


async def test_candidates_filter(client):
    task_id = await _task_with_approved(
        client,
        make_case(),
        make_case(case_id="TC-登录-002", title="验证密码错误提示", priority="P2"),
        make_case(case_id="TC-下单-001", module="下单", title="验证下单成功", keywords="主流程"),
    )
    plan = await _plan(client)
    base = f"/api/v1/plans/{plan['plan_id']}/candidates"
    by_module = (await client.get(base, params={"task_id": task_id, "module": "下单"})).json()
    assert [c["case_id"] for c in by_module["cases"]] == ["TC-下单-001"]
    by_pri = (await client.get(base, params={"task_id": task_id, "priority": "P2"})).json()
    assert [c["case_id"] for c in by_pri["cases"]] == ["TC-登录-002"]
    by_kw = (await client.get(base, params={"task_id": task_id, "keyword": "主流程"})).json()
    assert [c["case_id"] for c in by_kw["cases"]] == ["TC-下单-001"]
    # 不带 task_id：列出本项目下有已通过用例的任务
    tasks = (await client.get(base)).json()["tasks"]
    assert tasks and tasks[0]["task_id"] == task_id and tasks[0]["approved"] == 3


# ---- 任务分配（13 章）----


async def test_assign_by_case_and_module_with_trace(client):
    task_id = await _task_with_approved(
        client, make_case(), make_case(case_id="TC-下单-001", module="下单", title="验证下单成功"),
    )
    plan = await _plan(client)
    pid = plan["plan_id"]
    await client.post(f"/api/v1/plans/{pid}/cases", json={"task_id": task_id})
    detail = (await client.get(f"/api/v1/plans/{pid}")).json()
    login_item = next(i for i in detail["items"] if i["module"] == "登录")

    # 未知执行人拦截
    resp = await client.post(f"/api/v1/plans/{pid}/assign",
                             json={"assignee": "nobody", "item_ids": [login_item["item_id"]]})
    assert resp.status_code == 400

    app.state.auth.add_user("tester", "pw123456")
    # 非项目成员不可被分配（M1 收紧）；加入项目后可分配，候选接口列出成员与模块
    resp = await client.post(f"/api/v1/plans/{pid}/assign",
                             json={"assignee": "tester", "item_ids": [login_item["item_id"]]})
    assert resp.status_code == 400 and "成员" in resp.json()["detail"]
    await client.put("/api/v1/projects/P端/members", json={"username": "tester", "role": "tester"})
    cand = (await client.get(f"/api/v1/plans/{pid}/assignees")).json()
    assert {a["username"] for a in cand["assignees"]} >= {"tester", "admin"}
    assert cand["modules"] == [{"module": "下单", "count": 1}, {"module": "登录", "count": 1}]
    # 按用例分配
    resp = await client.post(f"/api/v1/plans/{pid}/assign",
                             json={"assignee": "tester", "item_ids": [login_item["item_id"]]})
    assert resp.json()["assigned"] == 1
    # 按模块分配
    resp = await client.post(f"/api/v1/plans/{pid}/assign",
                             json={"assignee": "admin", "module": "下单"})
    assert resp.json()["assigned"] == 1
    # 重新分配留痕：原执行人 → 新执行人 + 操作人
    resp = await client.post(f"/api/v1/plans/{pid}/assign",
                             json={"assignee": "admin", "item_ids": [login_item["item_id"]]})
    item = next(i for i in resp.json()["plan"]["items"] if i["item_id"] == login_item["item_id"])
    assert item["assignee"] == "admin"
    assert [(x["prev"], x["assignee"]) for x in item["assign_log"]] == [
        (None, "tester"), ("tester", "admin")]
    assert all(x["by"] for x in item["assign_log"])


async def test_list_plans_by_task(client):
    """概览「关联测试计划」：task_id 过滤只返回引用该任务快照的计划，并给出本任务用例数。"""
    task_id = await _task_with_approved(client, make_case())
    plan = await _plan(client)
    await client.post(f"/api/v1/plans/{plan['plan_id']}/cases", json={"task_id": task_id})
    await _plan(client, name="无关计划")
    plans = (await client.get("/api/v1/plans", params={"task_id": task_id})).json()["plans"]
    assert [p["plan_id"] for p in plans] == [plan["plan_id"]]
    assert plans[0]["task_cases"] == 1
    assert (await client.get("/api/v1/plans", params={"task_id": "没有的任务"})).json()["plans"] == []


async def test_concurrent_assignment_conflict(client):
    """多人同时分配：不同用例互不影响；同一用例他人先行分配时不覆盖、返回冲突。"""
    task_id = await _task_with_approved(
        client, make_case(), make_case(case_id="TC-登录-002", title="验证密码错误提示"),
    )
    plan = await _plan(client)
    pid = plan["plan_id"]
    await client.post(f"/api/v1/plans/{pid}/cases", json={"task_id": task_id})
    items = (await client.get(f"/api/v1/plans/{pid}")).json()["items"]
    i0, i1 = items[0]["item_id"], items[1]["item_id"]
    app.state.auth.add_user("tester", "pw123456")
    await client.put("/api/v1/projects/P端/members", json={"username": "tester", "role": "tester"})

    # 甲乙同时打开页面（都看到两条未分配）；乙先提交：i0 → tester
    resp = await client.post(f"/api/v1/plans/{pid}/assign",
                             json={"assignee": "tester", "item_ids": [i0],
                                   "expected": {i0: None}})
    assert resp.json()["assigned"] == 1 and resp.json()["conflicts"] == []
    # 甲基于旧快照提交 i0+i1 → admin：i0 冲突跳过不覆盖，i1 正常生效
    resp = await client.post(f"/api/v1/plans/{pid}/assign",
                             json={"assignee": "admin", "item_ids": [i0, i1],
                                   "expected": {i0: None, i1: None}})
    data = resp.json()
    assert data["assigned"] == 1
    assert [c["item_id"] for c in data["conflicts"]] == [i0]
    assert data["conflicts"][0]["assignee"] == "tester"
    by_id = {i["item_id"]: i for i in data["plan"]["items"]}
    assert by_id[i0]["assignee"] == "tester" and by_id[i1]["assignee"] == "admin"
    # 甲确认后改派：不带 expected 即明确覆盖，留痕完整
    resp = await client.post(f"/api/v1/plans/{pid}/assign",
                             json={"assignee": "admin", "item_ids": [i0]})
    assert resp.json()["assigned"] == 1
    item = next(i for i in resp.json()["plan"]["items"] if i["item_id"] == i0)
    assert [(x["prev"], x["assignee"]) for x in item["assign_log"]] == [
        (None, "tester"), ("tester", "admin")]


async def test_my_executions(client):
    task_id = await _task_with_approved(client, make_case())
    plan = await _plan(client)
    pid = plan["plan_id"]
    await client.post(f"/api/v1/plans/{pid}/cases", json={"task_id": task_id})
    mine = (await client.get("/api/v1/plans", params={"mine": "true"})).json()["plans"]
    assert mine == []
    app.state.auth.add_user("anonymous", "pw123456")  # 免登态操作人为 anonymous
    await client.post(f"/api/v1/plans/{pid}/assign", json={"assignee": "anonymous"})
    mine = (await client.get("/api/v1/plans", params={"mine": "true"})).json()["plans"]
    assert len(mine) == 1 and mine[0]["my_items"] == 1 and mine[0]["my_pending"] == 1


# ---- 计划执行与附件 ----


async def test_plan_run_flow_and_attachments(client, tmp_path):
    task_id = await _task_with_approved(client, make_case())
    plan = await _plan(client)
    pid = plan["plan_id"]
    # 空计划不能开轮次
    assert (await client.post(f"/api/v1/plans/{pid}/runs", json={})).status_code == 409
    await client.post(f"/api/v1/plans/{pid}/cases", json={"task_id": task_id})
    run = (await client.post(f"/api/v1/plans/{pid}/runs", json={"name": "冒烟"})).json()
    rid = run["run_id"]
    # 未结束不可再开
    assert (await client.post(f"/api/v1/plans/{pid}/runs", json={})).status_code == 409
    # 计划自动进入进行中
    assert (await client.get(f"/api/v1/plans/{pid}")).json()["status"] == "in_progress"

    detail = (await client.get(f"/api/v1/plans/{pid}")).json()
    item_id = detail["items"][0]["item_id"]
    # fail 必须带原因说明与失败分类（直接标失败，无需先驳回）
    resp = await client.post(f"/api/v1/plans/{pid}/runs/{rid}/results",
                             json={"items": [{"item_id": item_id, "status": "fail"}]})
    assert resp.status_code == 400
    resp = await client.post(f"/api/v1/plans/{pid}/runs/{rid}/results",
                             json={"items": [{"item_id": item_id, "status": "fail", "note": "BUG-1"}]})
    assert resp.status_code == 400 and "失败分类" in resp.json()["detail"]
    resp = await client.post(
        f"/api/v1/plans/{pid}/runs/{rid}/results",
        json={"items": [{"item_id": item_id, "status": "fail", "note": "第 1 步按钮不存在",
                         "reason": "用例步骤有误"}]},
    )
    assert resp.json()["summary"]["fail"] == 1
    assert resp.json()["results"][item_id]["reason"] == "用例步骤有误"
    # 用例问题类失败进入学习语料（提示词优化信号）；改判通过后不再入料
    from app.learning.service import collect_samples
    samples = collect_samples(app.state.tasks, plans=app.state.plans)
    exec_samples = [s for s in samples if s["来源"] == "执行失败（用例问题）"]
    assert len(exec_samples) == 1
    assert exec_samples[0]["失败分类"] == "用例步骤有误"
    assert exec_samples[0]["失败原因"] == "第 1 步按钮不存在"
    assert exec_samples[0]["用例"]["标题"] == "验证正确账号密码登录成功"
    # 复测覆盖并留 history
    resp = await client.post(
        f"/api/v1/plans/{pid}/runs/{rid}/results",
        json={"items": [{"item_id": item_id, "status": "pass"}]},
    )
    entry = resp.json()["results"][item_id]
    assert entry["status"] == "pass" and entry["history"][0]["status"] == "fail"

    # 附件不限类型：未知后缀归 other；上传人/时间/关联留痕 + 下载回读
    resp = await client.post(
        f"/api/v1/plans/{pid}/runs/{rid}/attachments",
        files={"file": ("payload.exe", b"MZ", "application/octet-stream")},
    )
    assert resp.status_code == 200 and resp.json()["kind"] == "other"
    resp = await client.post(
        f"/api/v1/plans/{pid}/runs/{rid}/attachments",
        files={"file": ("失败截图.png", b"\x89PNG fake", "image/png")},
        data={"item_id": item_id},
    )
    assert resp.status_code == 200, resp.text
    att = resp.json()
    assert att["kind"] == "image" and att["item_id"] == item_id and att["by"] and att["at"]
    got = await client.get(f"/api/v1/plans/{pid}/attachments/{att['att_id']}")
    assert got.status_code == 200 and got.content == b"\x89PNG fake"
    # 多文件一次上传：全部落盘并关联同一用例
    resp = await client.post(
        f"/api/v1/plans/{pid}/runs/{rid}/attachments",
        files=[("files", ("a.log", b"log-a", "text/plain")), ("files", ("b.mp4", b"\x00mp4", "video/mp4"))],
        data={"item_id": item_id},
    )
    assert resp.status_code == 200, resp.text
    multi = resp.json()
    assert multi["count"] == 2 and [x["filename"] for x in multi["attachments"]] == ["a.log", "b.mp4"]
    assert all(x["item_id"] == item_id for x in multi["attachments"])
    got = await client.get(f"/api/v1/plans/{pid}/attachments/{multi['attachments'][1]['att_id']}")
    assert got.status_code == 200 and got.content == b"\x00mp4"
    assert (await client.post(f"/api/v1/plans/{pid}/runs/{rid}/attachments", data={"item_id": item_id})).status_code == 400

    # 有执行记录的用例不可移出计划；有轮次的计划不可删除
    assert (await client.delete(f"/api/v1/plans/{pid}/cases/{item_id}")).status_code == 409
    assert (await client.delete(f"/api/v1/plans/{pid}")).status_code == 409
    # 结束轮次后可开新一轮
    assert (await client.post(f"/api/v1/plans/{pid}/runs/{rid}/finish")).status_code == 200
    assert (await client.post(f"/api/v1/plans/{pid}/runs/{rid}/finish")).status_code == 409
    assert (await client.post(f"/api/v1/plans/{pid}/runs", json={})).status_code == 200

    # 归档后拒绝加入/分配/执行
    await client.put(f"/api/v1/plans/{pid}", json={"status": "archived"})
    assert (await client.post(f"/api/v1/plans/{pid}/cases",
                              json={"task_id": task_id})).status_code == 409
    assert (await client.post(f"/api/v1/plans/{pid}/runs", json={})).status_code == 409


# ---- 旧任务级执行轮次迁移 ----


async def test_migrate_task_executions(client):
    from app.plans import migrate_task_executions

    task_id = await _task_with_approved(client, make_case(), approve=[])
    store = app.state.tasks
    record = store.get(task_id)
    case = record.result["cases"][0]
    uid = str(case["uid"])
    record.executions = [{
        "run_id": "old00001", "name": "第 1 轮执行", "by": "admin",
        "started_at": "2026-08-20T02:00:00+00:00", "finished_at": "2026-08-20T03:00:00+00:00",
        "results": {uid: {"case_id": case["case_id"], "title": case["title"],
                          "status": "pass", "note": "", "by": "admin",
                          "at": "2026-08-20T02:30:00+00:00", "history": []}},
    }]
    store.save(record)

    migrated = migrate_task_executions(store, app.state.plans)
    assert migrated == 1
    record = store.get(task_id)
    assert record.executions == [] and record.exec_migrated_to
    plan = app.state.plans.get(record.exec_migrated_to)
    assert plan["status"] == "done" and len(plan["items"]) == 1
    item = plan["items"][0]
    assert item["uid"] == uid and item["version_no"] >= 1
    # 结果键由 uid 重映射为 item_id
    assert plan["runs"][0]["results"][item["item_id"]]["status"] == "pass"
    # 再跑一遍幂等
    assert migrate_task_executions(store, app.state.plans) == 0
    # 迁移后任务级执行入口关闭
    resp = await client.post(f"/api/v1/tasks/{task_id}/executions", json={})
    assert resp.status_code == 409 and "迁移" in resp.json()["detail"]


async def test_计划列表查询条件与分页(client):
    for i in range(1, 6):
        await _plan(client, name=f"冒烟计划{i}", project="P端", owner="admin" if i % 2 else "zhang")
    p6 = await _plan(client, name="回归计划", project="P端", owner="zhang")
    await client.put(f"/api/v1/plans/{p6['plan_id']}", json={"status": "in_progress"})

    # 不传 page：返回全部（内部调用兼容），附 total 与负责人候选
    d = (await client.get("/api/v1/plans", params={"project": "P端"})).json()
    assert d["total"] == 6 and len(d["plans"]) == 6 and d["owners"] == ["admin", "zhang"]

    # 分页：每页 4 条 → 2 页；越界页收敛到最后一页
    d = (await client.get("/api/v1/plans", params={"project": "P端", "page": 1, "page_size": 4})).json()
    assert (d["total"], d["pages"], d["page"], len(d["plans"])) == (6, 2, 1, 4)
    d = (await client.get("/api/v1/plans", params={"project": "P端", "page": 9, "page_size": 4})).json()
    assert d["page"] == 2 and len(d["plans"]) == 2

    # 关键词（名称模糊）、状态、负责人
    d = (await client.get("/api/v1/plans", params={"keyword": "回归", "page": 1})).json()
    assert [p["name"] for p in d["plans"]] == ["回归计划"]
    d = (await client.get("/api/v1/plans", params={"status": "in_progress", "page": 1})).json()
    assert d["total"] == 1 and d["plans"][0]["name"] == "回归计划"
    d = (await client.get("/api/v1/plans", params={"owner": "zhang", "page": 1, "page_size": 10})).json()
    assert d["total"] == 3 and all(p["owner"] == "zhang" for p in d["plans"])
    d = (await client.get("/api/v1/plans", params={"owner": "zhang", "keyword": "冒烟", "page": 1})).json()
    assert d["total"] == 2
    assert (await client.get("/api/v1/plans", params={"status": "闲聊"})).status_code == 400


async def test_新建轮次指定执行人(client):
    from app.main import app as _app
    _app.state.auth.add_user("zhang", "zhang123", role="member")
    tid = await _task_with_approved(client, make_case(), make_case(case_id="TC-登录-002", title="第二条"), make_case(case_id="TC-登录-003", title="第三条"))
    plan = await _plan(client)
    pid = plan["plan_id"]
    await client.post(f"/api/v1/plans/{pid}/cases", json={"task_id": tid})
    detail = (await client.get(f"/api/v1/plans/{pid}")).json()
    items = [i["item_id"] for i in detail["items"]]
    assert len(items) == 3
    # 先把第一条分给 zhang（项目成员）
    await client.put("/api/v1/projects/P端/members", json={"username": "zhang", "role": "tester"})
    assert (await client.post(f"/api/v1/plans/{pid}/assign", json={"assignee": "zhang", "item_ids": items[:1]})).status_code == 200

    # 不是项目成员的执行人 → 400；不传 executor 保持原行为（不分配）
    assert (await client.post(f"/api/v1/plans/{pid}/runs", json={"name": "r0", "executor": "nobody"})).status_code == 400
    run0 = (await client.post(f"/api/v1/plans/{pid}/runs", json={"name": "r0"})).json()
    assert run0["executor"] is None and run0["assigned"] == 0
    await client.post(f"/api/v1/plans/{pid}/runs/{run0['run_id']}/finish")

    # 指定 admin 为本轮执行人：未分配的 2 条分给 admin，已分给 zhang 的保持
    run = (await client.post(f"/api/v1/plans/{pid}/runs", json={"name": "r1", "executor": "admin"})).json()
    assert run["executor"] == "admin" and run["assigned"] == 2
    detail = (await client.get(f"/api/v1/plans/{pid}")).json()
    by_item = {i["item_id"]: i for i in detail["items"]}
    assert by_item[items[0]]["assignee"] == "zhang"
    assert all(by_item[x]["assignee"] == "admin" for x in items[1:])
    assert by_item[items[1]]["assign_log"][-1]["assignee"] == "admin"
    await client.post(f"/api/v1/plans/{pid}/runs/{run['run_id']}/finish")

    # reassign_all：已分给他人的也改派，留痕原执行人
    run2 = (await client.post(f"/api/v1/plans/{pid}/runs", json={"name": "r2", "executor": "admin", "reassign_all": True})).json()
    assert run2["assigned"] == 1
    detail = (await client.get(f"/api/v1/plans/{pid}")).json()
    first = next(i for i in detail["items"] if i["item_id"] == items[0])
    assert first["assignee"] == "admin" and first["assign_log"][-1]["prev"] == "zhang"


async def test_计划列表_状态计数_执行进度与关键词筛选(client):
    task_id = await _task_with_approved(client, make_case())
    a = await _plan(client, name="冒烟 A", owner="alice")
    b = await _plan(client, name="回归 B", owner="bob")
    await _plan(client, name="空计划 C")
    await client.post(f"/api/v1/plans/{a['plan_id']}/cases", json={"task_id": task_id})
    await client.post(f"/api/v1/plans/{b['plan_id']}/cases", json={"task_id": task_id})
    run = (await client.post(f"/api/v1/plans/{a['plan_id']}/runs", json={"name": "第一轮冒烟"})).json()
    item_id = (await client.get(f"/api/v1/plans/{a['plan_id']}")).json()["items"][0]["item_id"]
    await client.post(f"/api/v1/plans/{a['plan_id']}/runs/{run['run_id']}/results",
                      json={"items": [{"item_id": item_id, "status": "pass"}]})
    await client.post(f"/api/v1/plans/{a['plan_id']}/runs/{run['run_id']}/finish", json={})
    await client.post(f"/api/v1/plans/{b['plan_id']}/runs", json={"name": "回归第一轮"})
    data = (await client.get("/api/v1/plans", params={"project": "P端", "page": 1, "page_size": 20})).json()
    assert data["total"] == 3 and sum(data["status_counts"].values()) == 3 and data["owners"] == ["alice", "anonymous", "bob"]  # 未填负责人默认创建人
    # 执行进度：未执行 / 执行中 / 已结束；非法值 400；状态筛选不影响 status_counts
    names = lambda r: sorted(p["name"] for p in r["plans"])
    assert names((await client.get("/api/v1/plans", params={"project": "P端", "run_state": "none"})).json()) == ["空计划 C"]
    assert names((await client.get("/api/v1/plans", params={"project": "P端", "run_state": "running"})).json()) == ["回归 B"]
    assert names((await client.get("/api/v1/plans", params={"project": "P端", "run_state": "finished"})).json()) == ["冒烟 A"]
    assert (await client.get("/api/v1/plans", params={"project": "P端", "run_state": "x"})).status_code == 400
    r = (await client.get("/api/v1/plans", params={"project": "P端", "status": "archived"})).json()
    assert r["total"] == 0 and sum(r["status_counts"].values()) == 3
    # 关键词覆盖轮次名与负责人；分页页大小校验
    assert names((await client.get("/api/v1/plans", params={"project": "P端", "keyword": "第一轮冒烟"})).json()) == ["冒烟 A"]
    assert names((await client.get("/api/v1/plans", params={"project": "P端", "keyword": "bob"})).json()) == ["回归 B"]
    assert (await client.get("/api/v1/plans", params={"project": "P端", "page": 5, "page_size": 20})).json()["page"] == 1


async def test_结束轮次_全部执行完计划完成_再开一轮退回进行中(client):
    task_id = await _task_with_approved(client, make_case(), make_case(case_id="TC-登录-002", title="验证错误密码登录失败"))
    plan = await _plan(client)
    pid = plan["plan_id"]
    assert (await client.post(f"/api/v1/plans/{pid}/cases", json={"task_id": task_id})).json()["added"] == 2
    item_ids = [x["item_id"] for x in (await client.get(f"/api/v1/plans/{pid}")).json()["items"]]
    rid = (await client.post(f"/api/v1/plans/{pid}/runs", json={})).json()["run_id"]
    for iid in item_ids:
        await client.post(f"/api/v1/plans/{pid}/runs/{rid}/results", json={"items": [{"item_id": iid, "status": "pass"}]})
    r = (await client.post(f"/api/v1/plans/{pid}/runs/{rid}/finish")).json()
    assert r["plan_status"] == "done"
    assert (await client.get(f"/api/v1/plans/{pid}")).json()["status"] == "done"
    run2 = (await client.post(f"/api/v1/plans/{pid}/runs", json={})).json()
    assert (await client.get(f"/api/v1/plans/{pid}")).json()["status"] == "in_progress"
    r = (await client.post(f"/api/v1/plans/{pid}/runs/{run2['run_id']}/finish")).json()   # 这轮一条没执行 → 仍进行中
    assert r["plan_status"] == "in_progress"


# ---- 执行失败 AI 归因与缺陷单 ----

import json
import json as _json

_PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
TRIAGE_BUG_REPLY = _json.dumps({"triage": {"category": "系统缺陷", "sub": "步骤有误", "failed_step": 9, "confidence": "很高",
                                           "reason": "预期「跳转首页并展示用户昵称」与需求一致，截图提示系统繁忙"},
                                "bug": {"title": "登录提交后提示系统繁忙无法进入首页", "severity": "离谱", "precondition": "已注册账号",
                                        "steps": ["输入正确账号密码并提交", 2], "expected": "跳转首页并展示用户昵称", "actual": "提示「系统繁忙，请稍后再试」", "scope": "登录主流程不通"}},
                               ensure_ascii=False)
TRIAGE_CASE_REPLY = _json.dumps({"triage": {"category": "用例问题", "sub": "预期有误", "failed_step": 1, "confidence": "高", "reason": "原文没有昵称"},
                                 "bug": {"title": "不该有的缺陷单", "severity": "一般", "steps": []}}, ensure_ascii=False)


async def test_执行失败AI归因_截图理解与需求原文入参_分类人采纳才改_缺陷单只在系统缺陷_用例问题采纳后进学习语料(client):
    from app.learning.service import collect_samples

    task_id = await _task_with_approved(client, make_case(point_ids=["TP001"]))
    plan = await _plan(client)
    pid = plan["plan_id"]
    await client.post(f"/api/v1/plans/{pid}/cases", json={"task_id": task_id})
    rid = (await client.post(f"/api/v1/plans/{pid}/runs", json={"name": "冒烟"})).json()["run_id"]
    item_id = (await client.get(f"/api/v1/plans/{pid}")).json()["items"][0]["item_id"]
    assert (await client.post(f"/api/v1/plans/{pid}/runs/{rid}/triage", json={})).status_code == 409   # 没有失败记录
    await client.post(f"/api/v1/plans/{pid}/runs/{rid}/results",
                      json={"items": [{"item_id": item_id, "status": "fail", "note": "点击登录后提示系统繁忙", "reason": "其他"}]})
    r = await client.post(f"/api/v1/plans/{pid}/runs/{rid}/attachments", data={"item_id": item_id},
                          files=[("files", ("失败.png", _PNG, "image/png"))])
    assert r.status_code == 200 and r.json()["kind"] == "image"
    # 截图先走图片理解，再归因；程序兜底：confidence / severity 枚举外、failed_step 超步数、非系统缺陷的 sub、steps 非字符串
    app.state.llm = StubLLM(["页面：登录页；提示文案「系统繁忙，请稍后再试」", TRIAGE_BUG_REPLY])
    r = await client.post(f"/api/v1/plans/{pid}/runs/{rid}/triage", json={})
    assert r.status_code == 200, r.text
    assert r.json()["triaged"] == [item_id] and r.json()["failed"] == []
    t = r.json()["results"][item_id]["triage"]
    assert t["category"] == "系统缺陷" and t["sub"] == "" and t["failed_step"] == 1 and t["confidence"] == "中"
    assert t["suggested_reason"] == "系统缺陷" and t["screenshots"] == 1 and t["model"] == "stub"
    assert t["bug"]["title"] == "登录提交后提示系统繁忙无法进入首页" and t["bug"]["severity"] == "一般" and t["bug"]["steps"] == ["输入正确账号密码并提交", "2"]
    assert app.state.llm.calls[0]["require_vision"] is True
    user = app.state.llm.calls[1]["messages"][-1]["content"]
    assert "系统繁忙，请稍后再试" in user and "验证正确账号密码登录成功" in user and "点击登录后提示系统繁忙" in user
    assert "# login" in user   # 需求原文（直接生成模式的任务测试点没有编号，来源测试点为空）
    # 分类没改，人采纳才改
    rec = (await client.get(f"/api/v1/plans/{pid}")).json()["runs"][0]["results"][item_id]
    assert rec["reason"] == "其他"
    r = await client.post(f"/api/v1/plans/{pid}/runs/{rid}/results/{item_id}/triage/adopt", json={})
    assert r.status_code == 200 and r.json()["result"]["reason"] == "系统缺陷" and r.json()["result"]["triage"]["adopted"]["reason"] == "系统缺陷"
    assert (await client.post(f"/api/v1/plans/{pid}/runs/{rid}/results/{item_id}/triage/adopt", json={"reason": "乱填"})).status_code == 400
    # 用例问题归因：缺陷单丢掉；采纳后进学习语料；指定 item_ids
    app.state.llm = StubLLM(["截图文字", TRIAGE_CASE_REPLY])
    r = (await client.post(f"/api/v1/plans/{pid}/runs/{rid}/triage", json={"item_ids": [item_id]})).json()
    t = r["results"][item_id]["triage"]
    assert t["category"] == "用例问题" and t["sub"] == "预期有误" and t["suggested_reason"] == "用例预期有误" and t["bug"] is None
    assert not [s for s in collect_samples(app.state.tasks, plans=app.state.plans) if s["来源"] == "执行失败（用例问题）"]
    await client.post(f"/api/v1/plans/{pid}/runs/{rid}/results/{item_id}/triage/adopt", json={})
    samples = [s for s in collect_samples(app.state.tasks, plans=app.state.plans) if s["来源"] == "执行失败（用例问题）"]
    assert len(samples) == 1 and samples[0]["失败分类"] == "用例预期有误"
    # 归因故障：截图理解失败只跳过、归因本身失败整条 502（单条）
    app.state.llm = StubLLM(["不是 JSON", "还不是 JSON", "依旧不是"])
    assert (await client.post(f"/api/v1/plans/{pid}/runs/{rid}/triage", json={})).status_code == 502
    # 归因结果不在枚举内：按系统缺陷、置信度低，不丢
    app.state.llm = StubLLM(["截图文字", _json.dumps({"triage": {"category": "不知道", "confidence": "高"}, "bug": None})])
    t = (await client.post(f"/api/v1/plans/{pid}/runs/{rid}/triage", json={})).json()["results"][item_id]["triage"]
    assert t["category"] == "系统缺陷" and t["confidence"] == "低" and t["bug"] is None
    # 轮次结束后不能再采纳
    await client.post(f"/api/v1/plans/{pid}/runs/{rid}/finish")
    assert (await client.post(f"/api/v1/plans/{pid}/runs/{rid}/results/{item_id}/triage/adopt", json={})).status_code == 409


# ---- AI 挑选计划用例 ----


async def test_AI挑选计划用例_只认候选id_冒烟兜底每模块P0_预算按优先级截_变更摘要自动带_草稿勾选后加入(client):
    cases = [make_case(case_id="TC-登录-001", priority="P0", title="登录主流程"),
             make_case(case_id="TC-登录-002", priority="P2", title="密码错误提示"),
             make_case(case_id="TC-下单-001", module="下单", priority="P0", title="下单主流程"),
             make_case(case_id="TC-下单-002", module="下单", priority="P1", title="余额不足下单失败"),
             make_case(case_id="TC-下单-003", module="下单", priority="P3", title="下单页文案")]
    task_id = await _task_with_approved(client, *cases)
    # 另一个计划里 TC-下单-002 执行失败过 → 候选带近期失败标记
    old = await _plan(client, name="上一轮")
    await client.post(f"/api/v1/plans/{old['plan_id']}/cases", json={"task_id": task_id, "module": "下单", "priority": "P1"})
    rid = (await client.post(f"/api/v1/plans/{old['plan_id']}/runs", json={})).json()["run_id"]
    iid = (await client.get(f"/api/v1/plans/{old['plan_id']}")).json()["items"][0]["item_id"]
    await client.post(f"/api/v1/plans/{old['plan_id']}/runs/{rid}/results", json={"items": [{"item_id": iid, "status": "fail", "note": "x", "reason": "系统缺陷"}]})
    # 任务上有需求变更分析 → 变更影响回归时自动带摘要
    rec = app.state.tasks.get(task_id)
    rec.requirement_diff = {"summary": "下单余额校验改为含冻结金额", "changes": [{"type": "规则调整", "description": "余额不足判定含冻结金额", "affected_cases": ["TC-下单-002"]}]}
    app.state.tasks.save(rec)

    plan = await _plan(client)
    pid = plan["plan_id"]
    assert (await client.post(f"/api/v1/plans/{pid}/select", json={"goal": "乱写"})).status_code == 400
    # 冒烟：模型选了登录 P0 + 一条清单外 id + 下单 P1，漏了下单的 P0 → 程序补；按优先级排序；预算 2 截掉 P1
    app.state.llm = StubLLM([json.dumps({"selected": [{"id": "C1", "reason": "登录主流程冒烟"}, {"id": "C99", "reason": "不存在"}, {"id": "C4", "reason": "近期失败"}],
                                         "excluded_summary": "异常与边界留回归", "coverage_note": "「下单」模块未选 P0"}, ensure_ascii=False)])
    r = await client.post(f"/api/v1/plans/{pid}/select", json={"goal": "冒烟", "budget": 2})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["candidates"] == 5 and [c["case_id"] for c in d["selected"]] == ["TC-登录-001", "TC-下单-001"] and d["truncated"] == 1
    assert d["selected"][1]["by"] == "program" and "程序兜底" in d["selected"][1]["reason"] and d["selected"][0]["by"] == "ai"
    assert d["excluded_summary"] == "异常与边界留回归" and "下单" in d["coverage_note"]
    user = app.state.llm.calls[0]["messages"][-1]["content"]
    assert "本轮目标：冒烟" in user and "最多 2 条" in user and '"failed_recent": true' in user and "TC-下单-002" in user and "变更摘要" not in user
    assert (await client.get(f"/api/v1/plans/{pid}")).json()["selection_draft"]["goal"] == "冒烟"   # 草稿留在计划上
    # 变更影响回归：摘要自动带需求变更分析；重点模块与自写摘要透传
    app.state.llm = StubLLM([json.dumps({"selected": [{"id": "C4", "reason": "受影响用例"}], "excluded_summary": "", "coverage_note": ""})])
    d = (await client.post(f"/api/v1/plans/{pid}/select", json={"goal": "变更影响回归", "focus_modules": ["下单"]})).json()
    user = app.state.llm.calls[0]["messages"][-1]["content"]
    assert "下单余额校验改为含冻结金额" in user and "受影响用例 TC-下单-002" in user and "重点模块：下单" in user
    assert [c["case_id"] for c in d["selected"]] == ["TC-下单-002"] and d["selected"][0]["failed_recent"] is True
    # 草稿勾选后按任务加入；加入后不再是候选
    r = (await client.post(f"/api/v1/plans/{pid}/cases", json={"task_id": task_id, "uids": [d["selected"][0]["uid"]]})).json()
    assert r["added"] == 1
    app.state.llm = StubLLM([json.dumps({"selected": []})])
    d = (await client.post(f"/api/v1/plans/{pid}/select", json={"goal": "全量回归"})).json()
    assert d["candidates"] == 4 and d["selected"] == []
    # 全部失败 502；没有候选 409
    app.state.llm = StubLLM(["不是 JSON", "还不是"])
    assert (await client.post(f"/api/v1/plans/{pid}/select", json={"goal": "全量回归"})).status_code == 502
    for c in cases:
        await client.post(f"/api/v1/plans/{pid}/cases", json={"task_id": task_id})
    assert (await client.post(f"/api/v1/plans/{pid}/select", json={"goal": "冒烟"})).status_code == 409

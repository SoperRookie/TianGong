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
    # fail 必须带原因
    resp = await client.post(f"/api/v1/plans/{pid}/runs/{rid}/results",
                             json={"items": [{"item_id": item_id, "status": "fail"}]})
    assert resp.status_code == 400
    resp = await client.post(
        f"/api/v1/plans/{pid}/runs/{rid}/results",
        json={"items": [{"item_id": item_id, "status": "fail", "note": "BUG-1"}]},
    )
    assert resp.json()["summary"]["fail"] == 1
    # 复测覆盖并留 history
    resp = await client.post(
        f"/api/v1/plans/{pid}/runs/{rid}/results",
        json={"items": [{"item_id": item_id, "status": "pass"}]},
    )
    entry = resp.json()["results"][item_id]
    assert entry["status"] == "pass" and entry["history"][0]["status"] == "fail"

    # 附件：类型白名单 + 上传人/时间/关联留痕 + 下载回读
    resp = await client.post(
        f"/api/v1/plans/{pid}/runs/{rid}/attachments",
        files={"file": ("payload.exe", b"MZ", "application/octet-stream")},
    )
    assert resp.status_code == 400
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

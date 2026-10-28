"""M4-W2 测试：在线评审留痕（F-6-6）与离线终稿回传 diff（F-6-8）。"""

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.exporters import export_excel
from app.main import app
from app.tasks.diff import diff_cases
from app.templates import TestCase
from tests.stubs import ANALYST_REPLY, StubLLM, generator_reply, make_case, review_reply


@pytest.fixture
async def client():
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def _create_task(client, *cases) -> str:
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(*cases), review_reply(True)])
    resp = await client.post(
        "/api/v1/tasks", files={"files": ("需求.txt", b"# login", "text/plain")}
    )
    return resp.json()["task_id"]


# ---- diff 计算 ----


def test_diff_cases_added_deleted_modified():
    generated = [
        make_case(),
        make_case(case_id="TC-登录-002", title="验证密码错误提示"),
    ]
    final = [
        make_case(priority="P0"),  # 修改优先级
        make_case(case_id="TC-登录-003", title="验证验证码过期"),  # 新增
    ]
    diff = diff_cases(generated, final)
    assert diff["stats"] == {
        "generated": 2, "final": 2, "added": 1, "deleted": 1,
        "modified": 1, "unchanged": 0, "adoption_rate": 0.0,
    }
    assert diff["added"][0]["title"] == "验证验证码过期"
    assert diff["deleted"][0]["title"] == "验证密码错误提示"
    change = diff["modified"][0]["changes"][0]
    assert (change["field"], change["before"], change["after"]) == ("priority", "P1", "P0")


def test_diff_cases_full_adoption():
    generated = [make_case()]
    diff = diff_cases(generated, generated)
    assert diff["stats"]["adoption_rate"] == 1.0
    assert not diff["added"] and not diff["deleted"] and not diff["modified"]


def test_diff_steps_change_detected():
    origin = make_case()
    edited = make_case(steps=[{"action": "输入正确账号密码并提交", "expected": "跳转首页"}])
    diff = diff_cases([origin], [edited])
    assert diff["modified"][0]["changes"][0]["field"] == "steps"


# ---- 在线评审留痕（F-6-6）----


async def test_online_review_accept_modify_delete(client):
    task_id = await _create_task(
        client,
        make_case(),
        make_case(case_id="TC-登录-002", title="验证密码错误提示"),
        make_case(case_id="TC-登录-003", title="验证锁定策略"),
    )
    modified = make_case(priority="P2", remark="边界场景降级")
    resp = await client.post(
        f"/api/v1/tasks/{task_id}/review",
        json={"items": [
            {"case_id": "TC-登录-001", "action": "modify", "case": modified, "feedback": "优先级偏高"},
            {"case_id": "TC-登录-002", "action": "delete", "feedback": "与需求无关"},
            {"case_id": "TC-登录-003", "action": "accept"},
        ]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["case_count"] == 2
    # accept 兼容为 approve（通过并锁定）；响应含完整状态机计数与连续驳回提示
    assert data["review"] == {
        "approve": 1, "reject": 0, "modify": 1, "delete": 1, "unlock": 0, "submit": 0,
        "log_entries": 3, "hints": [],
    }

    record = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    log = record["review_log"]
    assert log[0]["action"] == "modify" and log[0]["before"]["priority"] == "P1" and log[0]["after"]["priority"] == "P2"
    assert log[1]["action"] == "delete" and log[1]["feedback"] == "与需求无关"
    # 删除后模块内编号重排连续
    ids = [c["case_id"] for c in record["result"]["cases"]]
    assert ids == ["TC-登录-001", "TC-登录-002"]
    # 按终稿重导出
    assert record["files"]


async def test_online_review_rejects_unknown_case(client):
    task_id = await _create_task(client, make_case())
    resp = await client.post(
        f"/api/v1/tasks/{task_id}/review",
        json={"items": [{"case_id": "TC-不存在-999", "action": "accept"}]},
    )
    assert resp.status_code == 400


# ---- 离线终稿回传 diff（F-6-8）----


async def test_offline_final_upload_diff(client, tmp_path):
    task_id = await _create_task(
        client, make_case(), make_case(case_id="TC-登录-002", title="验证密码错误提示")
    )
    # 离线终稿：删 1 条、改 1 条优先级、新增 1 条
    final = [
        TestCase.model_validate(make_case(priority="P0")),
        TestCase.model_validate(make_case(case_id="TC-登录-009", title="验证断线重连")),
    ]
    path = export_excel(final, tmp_path / "终稿.xlsx")
    resp = await client.post(
        f"/api/v1/tasks/{task_id}/final",
        files={"file": ("终稿.xlsx", path.read_bytes(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert resp.status_code == 200, resp.text
    stats = resp.json()["stats"]
    assert stats["added"] == 1 and stats["deleted"] == 1 and stats["modified"] == 1

    record = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert record["offline_review"]["filename"] == "终稿.xlsx"
    assert record["offline_review"]["stats"] == stats


async def test_offline_final_multi_files_merged(client, tmp_path):
    task_id = await _create_task(client, make_case(), make_case(case_id="TC-登录-002", title="验证密码错误"))
    p1 = export_excel([TestCase.model_validate(make_case(priority="P0"))], tmp_path / "登录-1.xlsx")
    p2 = export_excel([TestCase.model_validate(make_case(case_id="TC-登录-009", title="验证断线重连"))], tmp_path / "登录-2.xlsx")
    mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    resp = await client.post(
        f"/api/v1/tasks/{task_id}/final",
        files=[("files", ("登录-1.xlsx", p1.read_bytes(), mime)), ("files", ("登录-2.xlsx", p2.read_bytes(), mime))],
    )
    assert resp.status_code == 200, resp.text
    stats = resp.json()["stats"]
    # 两个文件合并为一份终稿：改 1（优先级）、增 1（断线重连）、删 1（密码错误）
    assert stats["added"] == 1 and stats["deleted"] == 1 and stats["modified"] == 1
    record = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert record["offline_review"]["files"] == ["登录-1.xlsx", "登录-2.xlsx"]
    assert record["offline_review"]["filename"] == "登录-1.xlsx、登录-2.xlsx"
    assert (await client.post(f"/api/v1/tasks/{task_id}/final")).status_code == 400


async def test_offline_final_rejects_bad_file(client):
    task_id = await _create_task(client, make_case())
    resp = await client.post(
        f"/api/v1/tasks/{task_id}/final",
        files={"file": ("bad.csv", "字段A,字段B\n1,2\n".encode("utf-8-sig"), "text/csv")},
    )
    assert resp.status_code == 400


async def test_离线终稿以终稿为准_覆盖线上用例_配对沿用uid_删的进回收站_全部人工定稿(client, tmp_path):
    from app.recycle import list_bin
    from app.versions import list_versions

    c1 = make_case("TC-登录-001", title="验证正确账号密码登录成功")
    c2 = make_case("TC-登录-002", title="验证密码错误提示")
    c3 = make_case("TC-登录-003", title="验证验证码错误")
    task_id = await _create_task(client, c1, c2, c3)
    before = (await client.get(f"/api/v1/tasks/{task_id}")).json()["result"]["cases"]
    uid = {c["case_id"]: c["uid"] for c in before}
    # 终稿：001 标题改了但编号在（按编号配）、002 优先级改 P0 且顺序挪到最前（按标题配）、003 文件里没有（删）、新增一条
    final = [
        TestCase.model_validate(make_case("TC-登录-002", title="验证密码错误提示", priority="P0")),
        TestCase.model_validate(make_case("TC-登录-001", title="验证账号密码正确时登录成功")),
        TestCase.model_validate(make_case("TC-登录-009", title="验证断线重连", precondition="已登录")),
    ]
    path = export_excel(final, tmp_path / "终稿.xlsx")
    mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    resp = await client.post(f"/api/v1/tasks/{task_id}/final", files={"file": ("终稿.xlsx", path.read_bytes(), mime)},
                             data={"apply": "true"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] and body["apply_stats"] == {"total": 3, "modified": 2, "added": 1, "deleted": 1}
    assert body["stats"]["final"] == 3                                   # diff 留痕照旧
    t = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    cases = t["result"]["cases"]
    assert [c["title"] for c in cases] == ["验证密码错误提示", "验证账号密码正确时登录成功", "验证断线重连"]   # 顺序按文件
    assert [c["case_id"] for c in cases] == ["TC-登录-001", "TC-登录-002", "TC-登录-003"]                     # 编号重排
    assert cases[0]["uid"] == uid["TC-登录-002"] and cases[0]["priority"] == "P0" and cases[0]["version"] == 2
    assert cases[1]["uid"] == uid["TC-登录-001"] and cases[1]["version"] == 2
    assert cases[2]["uid"] not in uid.values() and cases[2]["source"] == "import" and cases[2]["precondition"] == "已登录"
    assert all(s["status"] == "approved" and s["locked"] for s in t["case_reviews"].values()) and len(t["case_reviews"]) == 3
    assert t["offline_review"]["applied"] and t["offline_review"]["apply_stats"]["deleted"] == 1
    bin_items = list_bin(task_id)
    assert len(bin_items) == 1 and bin_items[0]["entity_id"] == uid["TC-登录-003"]
    vs = list_versions(task_id, "case", uid["TC-登录-002"])
    assert [v["source"] for v in vs] == ["ai_original", "final"] and vs[-1]["version_no"] == 2
    assert [v["source"] for v in list_versions(task_id, "case", cases[2]["uid"])] == ["import"]
    # 计划能直接选这些用例（已通过）
    r = await client.get(f"/api/v1/tasks/{task_id}")
    assert r.status_code == 200
    # 不勾「以终稿为准」：只留痕，不改用例
    final2 = [TestCase.model_validate(make_case("TC-登录-001", title="验证密码错误提示", priority="P2"))]
    p2 = export_excel(final2, tmp_path / "终稿2.xlsx")
    resp = await client.post(f"/api/v1/tasks/{task_id}/final", files={"file": ("终稿2.xlsx", p2.read_bytes(), mime)})
    assert resp.status_code == 200 and "applied" not in resp.json()
    t2 = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert [c["title"] for c in t2["result"]["cases"]] == [c["title"] for c in cases] and t2["result"]["cases"][0]["priority"] == "P0"
    assert not t2["offline_review"].get("applied")
    # 不合法的终稿（没有步骤）整份不应用
    bad = "用例编号,模块,用例标题,优先级,前置条件,步骤,预期结果\nTC-登录-001,登录,没有步骤的用例,P1,,,\n".encode("utf-8-sig")
    resp = await client.post(f"/api/v1/tasks/{task_id}/final", files={"file": ("bad.csv", bad, "text/csv")}, data={"apply": "true"})
    assert resp.status_code == 400 and "没有步骤的用例" in resp.json()["detail"]
    assert [c["title"] for c in (await client.get(f"/api/v1/tasks/{task_id}")).json()["result"]["cases"]] == [c["title"] for c in cases]

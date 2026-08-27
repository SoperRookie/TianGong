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
        "approve": 1, "reject": 0, "modify": 1, "delete": 1, "unlock": 0,
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


async def test_offline_final_rejects_bad_file(client):
    task_id = await _create_task(client, make_case())
    resp = await client.post(
        f"/api/v1/tasks/{task_id}/final",
        files={"file": ("bad.csv", "字段A,字段B\n1,2\n".encode("utf-8-sig"), "text/csv")},
    )
    assert resp.status_code == 400

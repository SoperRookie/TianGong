"""测试用例页：全库用例聚合、筛选与分页（20/50/100/200，默认 20）。"""

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


async def _task(client, *cases, project="全库") -> str:
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(*cases), review_reply(True)])
    resp = await client.post(
        "/api/v1/tasks",
        files={"files": ("需求.txt", b"# login", "text/plain")},
        data={"project": project},
    )
    return resp.json()["task_id"]


async def test_all_cases_filters_and_meta(client):
    task_id = await _task(
        client,
        make_case(),
        make_case(case_id="TC-登录-002", title="验证密码错误提示", priority="P2"),
        make_case(case_id="TC-下单-001", module="下单", title="验证下单成功", keywords="主流程"),
    )
    await _task(client, make_case(case_id="TC-支付-001", module="支付", title="验证支付成功"),
                project="另一项目")
    await client.post(f"/api/v1/tasks/{task_id}/review",
                      json={"items": [{"case_id": "TC-登录-001", "action": "accept"}]})

    data = (await client.get("/api/v1/cases")).json()
    assert data["total"] == 4 and data["page"] == 1 and data["page_size"] == 20
    assert data["pages"] == 1 and len(data["cases"]) == 4
    assert set(data["modules"]) == {"登录", "下单", "支付"}
    assert {c["project"] for c in data["cases"]} == {"全库", "另一项目"}

    by_project = (await client.get("/api/v1/cases", params={"project": "另一项目"})).json()
    assert [c["case_id"] for c in by_project["cases"]] == ["TC-支付-001"]
    by_module = (await client.get("/api/v1/cases", params={"module": "下单"})).json()
    assert [c["case_id"] for c in by_module["cases"]] == ["TC-下单-001"]
    by_pri = (await client.get("/api/v1/cases", params={"priority": "P2"})).json()
    assert [c["case_id"] for c in by_pri["cases"]] == ["TC-登录-002"]
    approved = (await client.get("/api/v1/cases", params={"review": "approved"})).json()
    assert [c["case_id"] for c in approved["cases"]] == ["TC-登录-001"]
    by_kw = (await client.get("/api/v1/cases", params={"keyword": "主流程"})).json()
    assert [c["case_id"] for c in by_kw["cases"]] == ["TC-下单-001"]


async def test_all_cases_pagination(client):
    cases = [make_case(case_id=f"TC-登录-{i:03d}", title=f"场景 {i}") for i in range(1, 26)]
    await _task(client, *cases)

    # 非法页大小拒绝；合法档位 20/50/100/200
    assert (await client.get("/api/v1/cases", params={"page_size": 30})).status_code == 400
    p1 = (await client.get("/api/v1/cases")).json()
    assert p1["total"] == 25 and p1["pages"] == 2 and len(p1["cases"]) == 20
    p2 = (await client.get("/api/v1/cases", params={"page": 2})).json()
    assert len(p2["cases"]) == 5
    assert p1["cases"][0]["uid"] != p2["cases"][0]["uid"]
    # 越界页收敛到最后一页；页大小 50 一页装下
    over = (await client.get("/api/v1/cases", params={"page": 99})).json()
    assert over["page"] == 2 and len(over["cases"]) == 5
    big = (await client.get("/api/v1/cases", params={"page_size": 50})).json()
    assert big["pages"] == 1 and len(big["cases"]) == 25

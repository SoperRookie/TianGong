"""业务线：一条业务线包含多个项目；同业务线项目共享历史用例复用与知识检索范围。"""

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.main import app


@pytest.fixture
async def client():
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_项目业务线_增改查筛选与同线项目(client):
    assert (await client.post("/api/v1/projects", json={"name": "斗地主", "business_line": "棋牌"})).status_code == 200
    assert (await client.post("/api/v1/projects", json={"name": "麻将", "business_line": " 棋牌 "})).status_code == 200
    assert (await client.post("/api/v1/projects", json={"name": "老虎机", "business_line": "电子"})).status_code == 200
    assert (await client.post("/api/v1/projects", json={"name": "独立项目"})).status_code == 200
    data = (await client.get("/api/v1/projects")).json()
    assert data["business_lines"] == ["棋牌", "电子"]
    by = {p["project"]: p for p in data["projects"]}
    assert by["麻将"]["business_line"] == "棋牌" and by["独立项目"]["business_line"] == ""
    # 按业务线筛选；关键词也能搜业务线
    names = {p["project"] for p in (await client.get("/api/v1/projects?business_line=棋牌")).json()["projects"]}
    assert names == {"斗地主", "麻将"}
    names = {p["project"] for p in (await client.get("/api/v1/projects?keyword=电子")).json()["projects"]}
    assert names == {"老虎机"}
    # 修改业务线；归档项目不进入同线复用范围
    r = await client.put("/api/v1/projects/老虎机", json={"business_line": "棋牌"})
    assert r.status_code == 200 and r.json()["business_line"] == "棋牌"
    pstore = app.state.projects
    assert pstore.siblings("斗地主") == ["老虎机", "麻将"] and pstore.siblings("独立项目") == []
    await client.put("/api/v1/projects/老虎机", json={"status": "archived"})
    assert pstore.siblings("斗地主") == ["麻将"] and pstore.siblings("斗地主", include_archived=True) == ["老虎机", "麻将"]
    # 清空业务线
    await client.put("/api/v1/projects/麻将", json={"business_line": ""})
    assert pstore.siblings("斗地主") == []


async def test_业务线清单与重命名_解散(client):
    for n, l in (("斗地主", "棋牌"), ("麻将", "棋牌"), ("老虎机", "电子")):
        await client.post("/api/v1/projects", json={"name": n, "business_line": l})
    lines = (await client.get("/api/v1/business-lines")).json()["business_lines"]
    assert lines == [{"name": "棋牌", "projects": 2}, {"name": "电子", "projects": 1}]
    r = await client.put("/api/v1/business-lines/棋牌", json={"new_name": "棋牌类"})
    assert r.status_code == 200 and r.json()["projects"] == 2
    by = {p["project"]: p["business_line"] for p in (await client.get("/api/v1/projects")).json()["projects"]}
    assert by["斗地主"] == "棋牌类" and by["麻将"] == "棋牌类" and by["老虎机"] == "电子"
    assert (await client.put("/api/v1/business-lines/不存在", json={"new_name": "x"})).status_code == 400
    # 解散：项目保留，不再归属任何业务线
    assert (await client.put("/api/v1/business-lines/电子", json={"new_name": ""})).json()["projects"] == 1
    assert (await client.get("/api/v1/business-lines")).json()["business_lines"] == [{"name": "棋牌类", "projects": 2}]

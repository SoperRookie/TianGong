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


async def test_业务线实体_增删改查_改名联动项目_删除规则(client):
    # 新建业务线（可先于项目存在）；重复名拒绝；非法字符拒绝
    r = await client.post("/api/v1/business-lines", json={"name": "真人视讯", "code": "LIVE", "description": "现场荷官", "owner": "admin"})
    assert r.status_code == 200 and r.json()["projects"] == 0 and r.json()["code"] == "LIVE"
    assert (await client.post("/api/v1/business-lines", json={"name": "真人视讯"})).status_code == 400
    assert (await client.post("/api/v1/business-lines", json={"name": "a<b"})).status_code == 400
    # 项目归属；项目上直接填的未登记业务线名在清单里自动登记
    for n, l in (("欧洲21点", "真人视讯"), ("欧洲版轮盘", "真人视讯"), ("老虎机", "电子游戏")):
        await client.post("/api/v1/projects", json={"name": n, "business_line": l})
    lines = {l["name"]: l for l in (await client.get("/api/v1/business-lines")).json()["business_lines"]}
    assert lines["真人视讯"]["projects"] == 2 and lines["真人视讯"]["project_names"] == ["欧洲21点", "欧洲版轮盘"]
    assert lines["电子游戏"]["projects"] == 1 and lines["电子游戏"]["code"] == ""
    # 编辑：改名联动项目，描述单独可改
    r = await client.put("/api/v1/business-lines/真人视讯", json={"new_name": "真人游戏", "description": "含 21 点与轮盘"})
    assert r.status_code == 200 and r.json()["moved_projects"] == 2 and r.json()["description"] == "含 21 点与轮盘"
    by = {p["project"]: p["business_line"] for p in (await client.get("/api/v1/projects")).json()["projects"]}
    assert by["欧洲21点"] == "真人游戏" and by["欧洲版轮盘"] == "真人游戏" and by["老虎机"] == "电子游戏"
    assert (await client.put("/api/v1/business-lines/真人游戏", json={"new_name": "电子游戏"})).status_code == 400
    assert (await client.put("/api/v1/business-lines/不存在", json={"description": "x"})).status_code == 404
    # 删除：有项目时拒绝；detach 后项目保留且不再归属；空业务线直接删
    assert (await client.delete("/api/v1/business-lines/真人游戏")).status_code == 400
    r = await client.delete("/api/v1/business-lines/真人游戏?detach=true")
    assert r.status_code == 200 and r.json()["detached_projects"] == 2
    by = {p["project"]: p["business_line"] for p in (await client.get("/api/v1/projects")).json()["projects"]}
    assert by["欧洲21点"] == "" and by["欧洲版轮盘"] == ""
    await client.post("/api/v1/business-lines", json={"name": "空线"})
    assert (await client.delete("/api/v1/business-lines/空线")).status_code == 200
    assert {l["name"] for l in (await client.get("/api/v1/business-lines")).json()["business_lines"]} == {"电子游戏"}
    assert (await client.delete("/api/v1/business-lines/不存在")).status_code == 404

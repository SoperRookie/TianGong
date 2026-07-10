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


async def test_健康检查与模型列表(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    resp = await client.get("/api/v1/models")
    assert resp.status_code == 200
    data = resp.json()
    assert data["default_model"] == "deepseek-chat"
    names = [m["name"] for m in data["models"]]
    assert "deepseek-chat" in names
    for m in data["models"]:
        assert "api_key_env" not in m


async def test_任务端到端_上传文本生成并下载(client):
    app.state.llm = StubLLM([ANALYST_REPLY, generator_reply(make_case()), review_reply(True)])

    resp = await client.post(
        "/api/v1/tasks",
        files={"files": ("需求.txt", "# 登录模块\n支持账号密码登录".encode(), "text/plain")},
        data={"text": "补充：连续错误 5 次锁定账号"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert data["case_count"] == 1
    assert data["passed"] is True

    # 任务查询
    resp = await client.get(f"/api/v1/tasks/{data['task_id']}")
    assert resp.status_code == 200
    assert resp.json()["sources"] == ["需求.txt", "text"]

    # 多格式同步导出下载（F-5-1/2/3/4）
    xlsx = await client.get(data["downloads"]["xlsx"])
    assert xlsx.status_code == 200
    assert xlsx.headers["content-type"].startswith("application/vnd.openxmlformats")
    csv_resp = await client.get(data["downloads"]["csv"])
    assert csv_resp.status_code == 200
    assert csv_resp.content.startswith(b"\xef\xbb\xbf")
    xmind = await client.get(data["downloads"]["xmind"])
    assert xmind.status_code == 200
    assert xmind.content.startswith(b"PK")  # zip 魔数


async def test_解析预览接口(client):
    resp = await client.post(
        "/api/v1/parse",
        files={"files": ("需求.txt", "# 登录模块\n支持账号密码登录".encode(), "text/plain")},
        data={"text": "补充说明"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["documents"]) == 2
    assert data["documents"][0]["source"] == "需求.txt"
    assert data["documents"][0]["sections"][0]["title"] == "登录模块"
    assert "登录模块" in data["merged_text"]
    assert data["estimated_chunks"] == 1


async def test_不支持的文件格式返回400(client):
    resp = await client.post(
        "/api/v1/tasks",
        files={"files": ("需求.exe", b"MZ", "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert "不支持" in resp.json()["detail"]


async def test_超过大小上限返回413(client, monkeypatch):
    from app.config import Settings

    monkeypatch.setattr("app.api.routes.get_settings", lambda: Settings(max_upload_size_mb=0))
    resp = await client.post(
        "/api/v1/tasks",
        files={"files": ("需求.txt", b"some content", "text/plain")},
    )
    assert resp.status_code == 413


async def test_空输入返回400(client):
    resp = await client.post("/api/v1/tasks", data={"text": "  "})
    assert resp.status_code == 400


async def test_模板API_上传识别调整并按模板生成(client):
    from io import BytesIO

    from openpyxl import Workbook, load_workbook

    # 1. 上传模板 → 自动识别（F-4-1）
    wb = Workbook()
    wb.active.append(["用例ID", "所属模块", "用例名称", "优先级", "操作步骤", "预期结果", "测试类型"])
    buf = BytesIO()
    wb.save(buf)
    resp = await client.post(
        "/api/v1/templates",
        files={"file": ("团队模板.xlsx", buf.getvalue())},
        data={"name": "团队模板"},
    )
    assert resp.status_code == 200
    template = resp.json()
    assert {c["name"]: c["maps_to"] for c in template["columns"]}["测试类型"] == "custom"

    # 2. 字段映射调整（F-4-3）：给自定义列补充说明与枚举
    for col in template["columns"]:
        if col["name"] == "测试类型":
            col["description"] = "用例的测试类型"
            col["enum_values"] = ["功能", "边界"]
    resp = await client.put(f"/api/v1/templates/{template['template_id']}", json=template)
    assert resp.status_code == 200

    # 3. 模板库列表与设默认（F-4-4）
    resp = await client.post(f"/api/v1/templates/{template['template_id']}/default")
    assert resp.status_code == 200
    resp = await client.get("/api/v1/templates")
    assert resp.json()["default_id"] == template["template_id"]

    # 4. 按模板生成：导出 Excel 表头与模板 100% 一致（验收 3）
    app.state.llm = StubLLM(
        [
            ANALYST_REPLY,
            generator_reply(make_case(extras={"测试类型": "功能"})),
            review_reply(True),
        ]
    )
    resp = await client.post(
        "/api/v1/tasks",
        data={"text": "登录需求", "template_id": template["template_id"]},
    )
    assert resp.status_code == 200
    xlsx = await client.get(resp.json()["downloads"]["xlsx"])
    ws = load_workbook(BytesIO(xlsx.content)).active
    assert [c.value for c in ws[1]] == ["用例ID", "所属模块", "用例名称", "优先级", "操作步骤", "预期结果", "测试类型"]

    # 清理：恢复内置默认，避免影响其他用例
    await client.post("/api/v1/templates/builtin-default/default")


async def test_指定不存在的模板返回404(client):
    resp = await client.post("/api/v1/tasks", data={"text": "需求", "template_id": "ghost"})
    assert resp.status_code == 404


async def test_指定未注册模型返回400(client):
    # 使用真实 LLMClient：注册表在发起网络调用前即校验模型名
    resp = await client.post(
        "/api/v1/tasks",
        data={"text": "登录需求", "model": "不存在的模型"},
    )
    assert resp.status_code == 400
    assert "未注册" in resp.json()["detail"]

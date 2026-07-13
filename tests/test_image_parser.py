"""图片多模态解析（F-2-3）：桩 Vision 模型验证解析链路。"""

from app.parsers import parse_image
from tests.stubs import StubLLM

_VISION_REPLY = """# 图片类型与整体说明
登录页面原型图

# 界面元素清单
- 账号输入框（placeholder：请输入手机号）
- 密码输入框
- 登录按钮（主按钮，蓝色）

# 交互与流程逻辑
点击登录按钮后校验账号密码，成功跳转首页

# 业务规则与约束
密码错误 5 次锁定账号

# 图中文字原文
欢迎登录 请输入手机号 登录"""


async def test_图片解析走vision路由并还原章节(tmp_path):
    png = tmp_path / "原型图.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfakedata")
    llm = StubLLM([_VISION_REPLY])

    doc = await parse_image(png, llm)

    # 请求侧：要求 Vision 路由，图片以 data url 传入
    call = llm.calls[0]
    assert call["require_vision"] is True
    content = call["messages"][0]["content"]
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")

    # 结果侧：结构化 ParsedDocument
    assert doc.source == "原型图.png"
    assert doc.doc_type == "image"
    titles = [s.title for s in doc.sections if s.title]
    assert "界面元素清单" in titles
    assert "密码错误 5 次锁定账号" in doc.full_text


async def test_混合上传_图片与文本(tmp_path):
    # 经 API 层入口验证图片与文本混合（场景三：Word 主文档 + 截图）
    import httpx
    from asgi_lifespan import LifespanManager

    from app.main import app

    async with LifespanManager(app):
        app.state.llm = StubLLM(
            [
                _VISION_REPLY,  # 图片解析
            ]
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/parse",
                files={"files": ("登录原型.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
                data={"text": "补充：支持第三方微信登录"},
            )
    assert resp.status_code == 200
    data = resp.json()
    assert [d["doc_type"] for d in data["documents"]] == ["image", "txt"]
    assert "界面元素清单" in data["merged_text"]
    assert "微信登录" in data["merged_text"]

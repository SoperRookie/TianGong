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


def _png(size):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, format="PNG")
    return buf.getvalue()


async def test_大图切块逐块理解后拼接():
    """整页原型长截图整体缩到 2000 像素内文字就糊了：改为按块理解，块与块之间有重叠，结果按顺序拼接。"""
    import base64
    import io

    from PIL import Image

    from app.parsers.image import split_tiles, understand_image_bytes

    tiles = split_tiles(_png((1000, 5000)), "image/png")
    assert [pos for _, _, pos in tiles] == [(1, 1), (2, 1), (3, 1)]
    sizes = [Image.open(io.BytesIO(t[0])).size for t in tiles]
    assert sizes[0] == (1000, 2000) and sizes[-1] == (1000, 5000 - 2 * 1880)   # 末块只取剩余高度
    # 相邻块重叠：三块覆盖全高 5000 但总高 > 5000
    assert sum(h for _, h in sizes) > 5000

    llm = StubLLM(["块一：顶部 Banner", "块二：排行榜表格", "块三：底部规则说明"])
    text = await understand_image_bytes(_png((1000, 5000)), "image/png", llm, prompt="提取需求")
    assert len(llm.calls) == 3
    prompts = [c["messages"][0]["content"][1]["text"] for c in llm.calls]
    assert "第 2 块" in prompts[1] and "3 行 × 1 列" in prompts[1] and prompts[0].startswith("提取需求")
    assert text.index("【大图第 1/3 块】") < text.index("块二：排行榜表格") < text.index("【大图第 3/3 块】")
    # 每块都以 data url 上送且不超过单块上限
    for c in llm.calls:
        url = c["messages"][0]["content"][0]["image_url"]["url"]
        w, h = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).size
        assert max(w, h) <= 2000

    # 小图不切块、只调一次
    llm2 = StubLLM(["单块"])
    assert await understand_image_bytes(_png((800, 600)), "image/png", llm2, prompt="提取需求") == "单块"
    assert len(llm2.calls) == 1


def test_块数超限时整体缩小():
    from app.parsers.image import split_tiles

    tiles = split_tiles(_png((9000, 9000)), "image/png")   # 不缩小要 5×5=25 块
    assert 1 < len(tiles) <= 16

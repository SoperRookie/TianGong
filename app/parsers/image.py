"""图片需求解析（F-2-3）：优先多模态模型直接理解，输出结构化需求文本。

原型图/界面截图/流程图 → Vision 模型按固定框架提取 → ParsedDocument。
OCR 兜底（PaddleOCR）待 POC-R5 结论后按需接入。
"""

import base64
import io
from pathlib import Path

from PIL import Image

from app.llm.client import LLMClient
from app.parsers.base import ParsedDocument
from app.parsers.text import TextParser

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")

_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}

# 超大截图降采样上限（最长边像素）：控制视觉 token 数，避免撑爆私有化模型上下文
MAX_IMAGE_SIDE = 2000


def _downscale(raw: bytes, mime: str) -> tuple[bytes, str]:
    """超过上限的图片等比缩小后重编码；小图原样返回。"""
    try:
        with Image.open(io.BytesIO(raw)) as img:
            if max(img.size) <= MAX_IMAGE_SIDE:
                return raw, mime
            img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.LANCZOS)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG", optimize=True)
            return buf.getvalue(), "image/png"
    except Image.DecompressionBombError:
        raise ValueError("图片像素数异常巨大（疑似解压炸弹），已拒绝处理")
    except Exception:
        # 无法用 Pillow 解码（少见格式等）：原样上送，交由模型侧处理
        return raw, mime

VISION_PROMPT = """你是一名资深测试分析师，请仔细观察这张需求相关的图片（可能是界面原型图、页面截图、流程图或架构图），按以下框架提取信息，供后续测试用例设计使用：

# 图片类型与整体说明
（一句话说明这是什么图、描述的是什么功能）

# 界面元素清单
（若为界面图：逐一列出可见的控件——按钮、输入框、下拉框、开关、列表、文案标签等，注明名称与状态；非界面图跳过本节）

# 交互与流程逻辑
（按钮点击后的预期行为、页面跳转关系、流程图的节点与分支走向、判断条件）

# 业务规则与约束
（图中体现的数值规则、必填项、格式限制、状态流转、边界说明）

# 图中文字原文
（完整转录图片中所有可见文字，保持原样）

要求：只描述图中实际存在的内容，不推测图中没有的功能；无法辨认的部分明确标注「无法辨认」。"""


async def understand_image_bytes(
    data: bytes, mime: str, llm: LLMClient, prompt: str | None = None
) -> str:
    """Vision 模型理解图片字节，返回 Markdown 文本（自动路由 Vision 模型，F-1-5）。"""
    if prompt is None:
        from app.prompts import prompt_text

        prompt = prompt_text("vision_image")
    from app.config import get_settings

    payload, mime = _downscale(data, mime)
    limit = get_settings().max_vision_image_mb * 1024 * 1024
    if len(payload) > limit:
        raise ValueError(f"图片 {len(payload) // (1024 * 1024)}MB 超过 Vision 处理上限 {get_settings().max_vision_image_mb}MB")
    data_url = f"data:{mime};base64,{base64.b64encode(payload).decode()}"
    result = await llm.chat(
        [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        require_vision=True,
    )
    return result.content


async def parse_image(path: str | Path, llm: LLMClient) -> ParsedDocument:
    """独立图片文件的解析入口。"""
    path = Path(path)
    mime = _MIME.get(path.suffix.lower(), "image/png")
    content = await understand_image_bytes(path.read_bytes(), mime, llm)
    # 模型输出为 Markdown 结构，复用文本解析器还原章节层级
    doc = TextParser().parse_string(content)
    doc.source = path.name
    doc.doc_type = "image"
    return doc

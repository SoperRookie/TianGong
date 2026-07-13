"""图片需求解析（F-2-3）：优先多模态模型直接理解，输出结构化需求文本。

原型图/界面截图/流程图 → Vision 模型按固定框架提取 → ParsedDocument。
OCR 兜底（PaddleOCR）待 POC-R5 结论后按需接入。
"""

import base64
from pathlib import Path

from app.llm.client import LLMClient
from app.parsers.base import ParsedDocument
from app.parsers.text import TextParser

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")

_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}

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


async def parse_image(path: str | Path, llm: LLMClient) -> ParsedDocument:
    """调用 Vision 模型理解图片（自动路由至 supports_vision 的模型，F-1-5）。"""
    path = Path(path)
    b64 = base64.b64encode(path.read_bytes()).decode()
    data_url = f"data:{_MIME.get(path.suffix.lower(), 'image/png')};base64,{b64}"

    result = await llm.chat(
        [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": VISION_PROMPT},
                ],
            }
        ],
        require_vision=True,
    )
    # 模型输出为 Markdown 结构，复用文本解析器还原章节层级
    doc = TextParser().parse_string(result.content)
    doc.source = path.name
    doc.doc_type = "image"
    return doc

"""混排文档图片理解回填：内嵌图片经 Vision 并行理解，结果替换正文占位符。

小图过滤：短边过小或字节数过小的图片（图标、分隔线、logo）不送 Vision。
单图失败不阻塞整体：占位符替换为失败说明（与多文件部分失败同一原则）。
"""

import asyncio
import io

from PIL import Image

from app.llm.client import LLMClient
from app.parsers.base import ParsedDocument
from app.parsers.image import understand_image_bytes

# 送 Vision 的门槛：短边 ≥100px 且 ≥3KB（低于视为装饰性小图，仅标注跳过）
MIN_IMAGE_SIDE = 100
MIN_IMAGE_BYTES = 3 * 1024

EMBED_PROMPT = """这张图片嵌在一份软件需求文档中。请提取图中与需求相关的信息（界面元素、交互逻辑、
流程分支、数值规则、文字内容），用简洁的 Markdown 输出；只描述图中实际内容，不推测不存在的功能。"""


def _worth_vision(data: bytes) -> bool:
    if len(data) < MIN_IMAGE_BYTES:
        return False
    try:
        with Image.open(io.BytesIO(data)) as img:
            return min(img.size) >= MIN_IMAGE_SIDE
    except Image.DecompressionBombError:
        return False  # 像素炸弹：直接跳过
    except Exception:
        return True  # 无法解码时不武断丢弃，交给 Vision


async def enrich_images(doc: ParsedDocument, llm: LLMClient) -> ParsedDocument:
    """就地回填：doc.sections 中的 [[图片:N]] 占位内容替换为 Vision 理解结果。"""
    if not doc.embedded_images:
        return doc

    async def understand(img) -> tuple[str, str]:
        if not _worth_vision(img.data):
            return img.placeholder, f"（{img.placeholder} 为装饰性小图，已跳过）"
        try:
            content = await understand_image_bytes(img.data, img.mime, llm, prompt=EMBED_PROMPT)
            return img.placeholder, f"【文档内图片理解 {img.placeholder}】\n{content}"
        except Exception as e:  # 单图失败不阻塞整体解析
            return img.placeholder, f"（{img.placeholder} 解析失败：{e}）"

    sem = asyncio.Semaphore(4)  # 文档内图片并发上限，避免一次上传打满 Vision 配额

    async def guarded(img):
        async with sem:
            return await understand(img)

    replacements = dict(await asyncio.gather(*[guarded(img) for img in doc.embedded_images]))
    for section in doc.sections:
        if section.content in replacements:
            section.content = replacements[section.content]
    doc.embedded_images = []
    return doc

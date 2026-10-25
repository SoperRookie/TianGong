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

# 文档内嵌图的提示词：与独立图片同一套框架（Prompt 管理里的「图片理解」），只加一句所处上下文。
# 早先用的「简洁输出」版本会让整页原型截图被概括成几百字摘要，字段、文案、数值大量丢失
def embed_prompt() -> str:
    from app.prompts import prompt_text

    return "这张图片嵌在一份软件需求文档中（Word / PDF / PPT 等的插图，可能是整页原型截图、流程图或表格截图）。\n\n" + prompt_text("vision_image")


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
    stat = {"total": len(doc.embedded_images), "done": 0, "failed": 0, "skipped": 0, "error": None}

    async def understand(img) -> tuple[str, str]:
        if not _worth_vision(img.data):
            stat["skipped"] += 1
            return img.placeholder, f"（{img.placeholder} 为装饰性小图，已跳过）"
        try:
            content = await understand_image_bytes(img.data, img.mime, llm, prompt=embed_prompt())
            stat["done"] += 1
            return img.placeholder, f"【文档内图片理解 {img.placeholder}】\n{content}\n【图片理解结束 {img.placeholder}】"
        except Exception as e:  # 单图失败不阻塞整体解析；失败数记到 doc.vision，附件按「部分解析」显示并拦提取
            stat["failed"] += 1
            stat["error"] = str(e)[:200]
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
    doc.vision = stat
    return doc

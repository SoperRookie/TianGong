"""图片需求解析（F-2-3）：优先多模态模型直接理解，输出结构化需求文本。

原型图/界面截图/流程图 → Vision 模型按固定框架提取 → ParsedDocument。
OCR 兜底（PaddleOCR）待 POC-R5 结论后按需接入。
"""

import asyncio
import base64
import io
from pathlib import Path

from PIL import Image

from app.llm.client import LLMClient
from app.parsers.base import ParsedDocument
from app.parsers.text import TextParser

# Vision 直接接受的格式 + Pillow 可解码后转 PNG 的格式 + 需 LibreOffice 栅格化的矢量格式
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".ico",
                  ".heic", ".heif", ".avif", ".psd", ".svg", ".emf", ".wmf")
_VECTOR_SUFFIXES = (".svg", ".emf", ".wmf")
_NATIVE_MIME = ("image/png", "image/jpeg", "image/webp", "image/gif")

_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
         ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff", ".ico": "image/x-icon",
         ".heic": "image/heic", ".heif": "image/heif", ".avif": "image/avif", ".psd": "image/vnd.adobe.photoshop",
         ".svg": "image/svg+xml", ".emf": "image/emf", ".wmf": "image/wmf"}


def _register_plugins() -> None:
    """HEIC / HEIF / AVIF 需要 Pillow 插件（pillow-heif / pillow-avif-plugin），未安装时静默跳过。"""
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except Exception:
        pass
    try:
        import pillow_avif  # noqa: F401
    except Exception:
        pass


def load_image_bytes(path: str | Path) -> tuple[bytes, str]:
    """读取任意受支持格式的图片，返回 (字节, MIME)：矢量图先栅格化，模型不直接支持的格式转成 PNG。"""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in _VECTOR_SUFFIXES:
        from app.parsers.convert import rasterize_vector

        return rasterize_vector(path), "image/png"
    raw = path.read_bytes()
    mime = _MIME.get(suffix, "image/png")
    if mime in _NATIVE_MIME:
        return raw, mime
    _register_plugins()
    try:
        with Image.open(io.BytesIO(raw)) as img:
            buf = io.BytesIO()
            img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB").save(buf, format="PNG")
            return buf.getvalue(), "image/png"
    except Image.DecompressionBombError:
        raise ValueError("图片像素数异常巨大（疑似解压炸弹），已拒绝处理") from None
    except Exception as e:
        raise ValueError(f"{path.name} 无法解码为图片（{suffix}）：{e}") from None

# 单块图片最长边上限（像素）：控制视觉 token 数，避免撑爆私有化模型上下文
MAX_IMAGE_SIDE = 2000
# 大图切块：整页原型截图 / 长截图往往几千到上万像素，整体缩到 2000 以内文字就糊了，模型只能概括大意。
# 改为按 2000 像素切成带重叠的块逐块理解再拼接；块数超过上限时才整体缩小（相邻块重叠 120 像素，避免切断的控件两边都看不全）
TILE_OVERLAP = 120
MAX_TILES = 16


def _grid(w: int, h: int) -> tuple[int, int]:
    step = MAX_IMAGE_SIDE - TILE_OVERLAP
    nx = max(1, -(-(w - TILE_OVERLAP) // step)) if w > MAX_IMAGE_SIDE else 1
    ny = max(1, -(-(h - TILE_OVERLAP) // step)) if h > MAX_IMAGE_SIDE else 1
    return nx, ny


def split_tiles(raw: bytes, mime: str) -> list[tuple[bytes, str, tuple[int, int]]]:
    """大图切块：返回 [(图片字节, mime, (行, 列))]，按从上到下、从左到右排列；小图原样一块返回。"""
    try:
        with Image.open(io.BytesIO(raw)) as img:
            w, h = img.size
            if max(w, h) <= MAX_IMAGE_SIDE:
                return [(raw, mime, (1, 1))]
            nx, ny = _grid(w, h)
            if nx * ny > MAX_TILES:
                # 块数超限：整体缩小到恰好 MAX_TILES 块以内
                scale = (MAX_TILES / (nx * ny)) ** 0.5
                img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
                w, h = img.size
                nx, ny = _grid(w, h)
            rgb = img.convert("RGB")
            tiles: list[tuple[bytes, str, tuple[int, int]]] = []
            step = MAX_IMAGE_SIDE - TILE_OVERLAP
            for r in range(ny):
                top = min(r * step, max(0, h - MAX_IMAGE_SIDE)) if ny > 1 else 0
                bottom = min(h, top + MAX_IMAGE_SIDE)
                for c in range(nx):
                    left = min(c * step, max(0, w - MAX_IMAGE_SIDE)) if nx > 1 else 0
                    right = min(w, left + MAX_IMAGE_SIDE)
                    buf = io.BytesIO()
                    rgb.crop((left, top, right, bottom)).save(buf, format="PNG", optimize=True)
                    tiles.append((buf.getvalue(), "image/png", (r + 1, c + 1)))
            return tiles
    except Image.DecompressionBombError:
        raise ValueError("图片像素数异常巨大（疑似解压炸弹），已拒绝处理")
    except Exception:
        # 无法用 Pillow 解码（少见格式等）：原样上送，交由模型侧处理
        return [(raw, mime, (1, 1))]


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
    """Vision 模型理解图片字节，返回 Markdown 文本（自动路由 Vision 模型，F-1-5）。超过单块上限的大图切块逐块理解后拼接。"""
    if prompt is None:
        from app.prompts import prompt_text

        prompt = prompt_text("vision_image")
    tiles = split_tiles(data, mime)
    if len(tiles) == 1:
        return await _understand_one(tiles[0][0], tiles[0][1], llm, prompt)
    n = len(tiles)
    rows = max(r for _, _, (r, _) in tiles)
    cols = max(c for _, _, (_, c) in tiles)
    sem = asyncio.Semaphore(4)

    async def one(i: int, payload: bytes, m: str, pos: tuple[int, int]) -> str:
        note = (f"\n\n【说明】这是一张大图按从上到下、从左到右切成 {n} 块（{rows} 行 × {cols} 列）后的第 {i} 块"
                f"（第 {pos[0]} 行第 {pos[1]} 列），相邻块之间有少量重叠。只描述本块内可见的内容，"
                f"被切断的元素照实描述可见部分，不要推测块外内容。")
        async with sem:
            content = await _understand_one(payload, m, llm, prompt + note)
        return f"【大图第 {i}/{n} 块】\n{content}"

    parts = await asyncio.gather(*[one(i, payload, m, pos) for i, (payload, m, pos) in enumerate(tiles, 1)])
    return "\n\n".join(parts)


async def _understand_one(data: bytes, mime: str, llm: LLMClient, prompt: str) -> str:
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
    data, mime = load_image_bytes(path)
    content = await understand_image_bytes(data, mime, llm)
    # 模型输出为 Markdown 结构，复用文本解析器还原章节层级
    doc = TextParser().parse_string(content)
    doc.source = path.name
    doc.doc_type = "image"
    return doc

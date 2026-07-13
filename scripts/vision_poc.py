"""Vision 模型冒烟与 POC-R5 实测：合成原型图 → qwen-vl-local 理解 → 打印结构化结果。

用法：
    .venv/bin/python scripts/vision_poc.py                # 用合成登录页原型图
    .venv/bin/python scripts/vision_poc.py 真实截图.png    # 用真实图片
"""

import asyncio
import sys
import time
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.llm.client import LLMClient
from app.llm.registry import ModelRegistry
from app.parsers import parse_image

OUT_DIR = Path("outputs/vision_poc")


def make_prototype_image() -> Path:
    """用 PyMuPDF 画一张登录页原型图（含控件框线与中文标注）。"""
    doc = fitz.open()
    page = doc.new_page(width=400, height=560)

    def box(x0, y0, x1, y1, text="", fill=None):
        page.draw_rect(fitz.Rect(x0, y0, x1, y1), color=(0.3, 0.3, 0.3), fill=fill, width=1)
        if text:
            page.insert_textbox(
                fitz.Rect(x0, y0 + 8, x1, y1), text, fontsize=12,
                fontname="china-s", align=fitz.TEXT_ALIGN_CENTER,
            )

    page.insert_textbox(fitz.Rect(0, 40, 400, 80), "欢迎登录", fontsize=22,
                        fontname="china-s", align=fitz.TEXT_ALIGN_CENTER)
    box(60, 120, 340, 160, "请输入手机号")
    box(60, 180, 340, 220, "请输入密码")
    box(60, 250, 340, 290, "登 录", fill=(0.85, 0.9, 1))
    page.insert_textbox(fitz.Rect(60, 300, 340, 330), "忘记密码？    注册新账号",
                        fontsize=10, fontname="china-s", align=fitz.TEXT_ALIGN_CENTER)
    page.insert_textbox(fitz.Rect(60, 360, 340, 420),
                        "提示：密码连续错误 5 次，账号锁定 30 分钟",
                        fontsize=9, fontname="china-s")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "登录页原型.png"
    page.get_pixmap(matrix=fitz.Matrix(2, 2)).save(str(path))
    doc.close()
    return path


async def main() -> None:
    registry = ModelRegistry.from_yaml(get_settings().models_config_path)
    llm = LLMClient(registry)
    vision_cfg = registry.resolve_vision()
    print(f"Vision 模型: {vision_cfg.name} ({vision_cfg.model}) @ {vision_cfg.base_url}")

    image = Path(sys.argv[1]) if len(sys.argv) > 1 else make_prototype_image()
    print(f"图片: {image}\n识别中 ...\n")

    start = time.monotonic()
    doc = await parse_image(image, llm)
    elapsed = time.monotonic() - start

    print(f"耗时: {elapsed:.1f}s")
    print("=" * 50)
    print(doc.full_text)


if __name__ == "__main__":
    asyncio.run(main())

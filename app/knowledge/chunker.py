"""知识切片（F-7-3）：面向检索的细粒度切分，与需求分片（F-2-6）策略同源但粒度不同。

需求分片求"语义自洽的子需求"（万字级），知识切片求"检索命中的最小单元"（数百字级）。
复用章节感知的 split_text，另做超短块合并，避免标题行单独成块稀释检索质量。
"""

from app.parsers.chunking import split_text

# 低于此长度的切片并入相邻块：单独的标题/短句不具备检索价值
_MIN_CHUNK_CHARS = 50


def split_knowledge(text: str, max_chars: int = 600) -> list[str]:
    """将知识文档正文切为检索单元，每片不超过 max_chars。"""
    chunks = split_text(text, max_chars)
    merged: list[str] = []
    for chunk in chunks:
        if merged and (len(chunk) < _MIN_CHUNK_CHARS or len(merged[-1]) < _MIN_CHUNK_CHARS):
            merged[-1] = f"{merged[-1]}\n\n{chunk}"
        else:
            merged.append(chunk)
    return [c for c in merged if c.strip()]

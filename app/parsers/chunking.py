"""大文档分片（F-2-6）：按章节边界切分，保证单个分片语义完整。

切分策略：以标题章节为最小单元贪心装包；单章节超限时按段落再切。
每个分片是自洽的子需求，可独立走「拆解→生成→评审」链路后合并。
"""

_PARAGRAPH_SEP = "\n"


def split_text(text: str, max_chars: int) -> list[str]:
    """按章节（Markdown 风格 # 标题行）切分文本为若干分片，每片不超过 max_chars。"""
    if len(text) <= max_chars:
        return [text]

    # 先按标题行切成章节块
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#") and current:
            blocks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))

    # 贪心装包：相邻章节合并到不超过 max_chars
    chunks: list[str] = []
    buffer = ""
    for block in blocks:
        if len(block) > max_chars:  # 单章节超限，按段落再切
            if buffer:
                chunks.append(buffer)
                buffer = ""
            chunks.extend(_split_block(block, max_chars))
            continue
        candidate = f"{buffer}\n\n{block}" if buffer else block
        if len(candidate) > max_chars:
            chunks.append(buffer)
            buffer = block
        else:
            buffer = candidate
    if buffer:
        chunks.append(buffer)
    return [c.strip() for c in chunks if c.strip()]


def _split_block(block: str, max_chars: int) -> list[str]:
    lines = block.split(_PARAGRAPH_SEP)
    parts: list[str] = []
    buffer = ""
    for line in lines:
        candidate = f"{buffer}{_PARAGRAPH_SEP}{line}" if buffer else line
        if len(candidate) > max_chars and buffer:
            parts.append(buffer)
            buffer = line
        else:
            buffer = candidate
    if buffer:
        parts.append(buffer)
    return parts

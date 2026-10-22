"""长需求拆解前的结构化准备：按需求切段、疑点清单不单独成片、模块大纲按整理稿标题生成。

三件事都只认整理稿（`extraction_markdown`）与多需求设计简报（`design_brief` 用「【需求 i/n：标题】」拼接）的既定格式，
拿不到结构时各自回退：没有需求标记就整篇一段，没有编号标题就交给模型出大纲。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.parsers.chunking import split_text

# design_brief 多需求合并时每条需求前的标记行
_REQ_MARK_RE = re.compile(r"^【需求 (\d+)/(\d+)：(.+?)】\s*$")
# 整理稿编号标题：## 1. 页面 〔来源：…〕 / ### 2.1 页面 / #### 2.1.3 区块 〔疑似草稿〕
_HEAD_RE = re.compile(r"^(#{2,4}) (\d+(?:\.\d+)*)\.? (.+?)\s*$")
_ANNOT_RE = re.compile(r"\s*〔[^〕]*〕")
# 整理稿末尾的参考章节：只列疑点 / 草稿，不是正式需求，拆测试点时不单独成片
_APPENDIX_RE = re.compile(r"^## \d+\. (疑点 / 不一致清单|疑似遗留草稿)")
_BRIEF_PART_RE = re.compile(r"^【[^】]+】")   # 简报的其他部分（人工补充说明 / 确认结论 / 分析要点）
_DRAFT_MARK = "疑似草稿"

APPENDIX_NOTE = ("【以下疑点清单 / 疑似遗留草稿只作参考：是整理稿对原文矛盾与可疑区块的记录，不是正式需求，"
                 "不要据此单独拆测试点；原文含糊之处写进 blind_spots】")

# 区块正文达到这个字数才单独成模块，更短的区块归入所在页面的模块：模块数直接决定生成阶段的调用次数
MIN_BLOCK_CHARS = 150
# 标题派生的大纲至少要有这么多模块才采用，否则说明不是整理稿格式，交给模型出大纲
MIN_OUTLINE_MODULES = 3


@dataclass
class Segment:
    """简报里的一条需求：标题（单需求时为 None）与正文。"""

    title: str | None
    text: str


@dataclass
class _Node:
    level: int
    title: str
    draft: bool
    chars: int = 0                      # 自身正文字数（不含子节点）
    children: list["_Node"] = field(default_factory=list)

    def total(self) -> int:
        return self.chars + sum(c.total() for c in self.children)


def requirement_segments(brief: str) -> list[Segment]:
    """按「【需求 i/n：标题】」把多需求简报切回各条需求；没有标记时整篇一段、标题 None。"""
    marks = [(i, m.group(3).strip()) for i, line in enumerate(brief.splitlines()) if (m := _REQ_MARK_RE.match(line))]
    if not marks:
        return [Segment(None, brief)]
    lines = brief.splitlines()
    out: list[Segment] = []
    for k, (i, title) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(lines)
        start = i if k else 0   # 第一条前面若有文字（理论上没有）并入第一条
        out.append(Segment(title, "\n".join(lines[start:end]).strip()))
    return out


def split_appendix(text: str) -> tuple[str, str]:
    """把整理稿末尾的「疑点 / 不一致清单」「疑似遗留草稿」章节从正文里摘出来：返回 (正文, 附录)。
    附录到下一个一级 / 二级标题或简报的下一部分（【…】行）为止；没有这两节时附录为空串、正文原样。"""
    lines = text.splitlines()
    main: list[str] = []
    appendix: list[str] = []
    in_app = False
    for line in lines:
        if _APPENDIX_RE.match(line):
            in_app = True
        elif in_app and (re.match(r"^#{1,2} ", line) or _BRIEF_PART_RE.match(line)):
            in_app = False
        (appendix if in_app else main).append(line)
    if not appendix:
        return text, ""
    return "\n".join(main).strip(), "\n".join(appendix).strip()


def requirement_chunks(brief: str, max_chars: int) -> tuple[list[str], list[str | None]]:
    """拆解用的分片：先按需求切段（片不跨需求），每段再按章节分片；疑点清单 / 遗留草稿不单独成片，
    带说明挂到本需求最后一片末尾。返回 (分片列表, 每片所属需求标题)。整篇不超限时与原来一样只有一片。"""
    if len(brief) <= max_chars:
        return [brief], [None]
    chunks: list[str] = []
    titles: list[str | None] = []
    for seg in requirement_segments(brief):
        main, appendix = split_appendix(seg.text)
        parts = split_text(main, max_chars) if main else []
        if len(parts) > 1 and len(parts[-1]) < max_chars // 8:   # 章节装包剩下的小尾巴并入前片（略超阈值无妨）
            parts[-2] = f"{parts[-2]}\n\n{parts.pop()}"
        if appendix:
            if parts:
                parts[-1] = f"{parts[-1]}\n\n{APPENDIX_NOTE}\n{appendix}"
            else:
                parts = [f"{APPENDIX_NOTE}\n{appendix}"]
        chunks.extend(parts)
        titles.extend([seg.title] * len(parts))
    return chunks, titles


def _parse(text: str) -> list[_Node]:
    """编号标题 → 树（二级为根）。附录章节（疑点清单 / 遗留草稿）整节跳过；标题里的 〔来源〕〔疑似草稿〕 去掉。"""
    roots: list[_Node] = []
    stack: list[_Node] = []
    skipping = False
    for line in text.splitlines():
        m = _HEAD_RE.match(line)
        if m is None:
            if line.startswith("# "):   # 一级标题（整理稿文头）：重置
                stack = []
                skipping = False
            elif stack and not skipping and line.strip() and not line.startswith("---"):
                stack[-1].chars += len(line.strip())
            continue
        level = len(m.group(1))
        if _APPENDIX_RE.match(line):
            skipping = True
            stack = []
            continue
        if skipping and level > 2:
            continue
        skipping = False
        raw = m.group(3)
        node = _Node(level, _ANNOT_RE.sub("", raw).strip(), _DRAFT_MARK in raw)
        while stack and stack[-1].level >= level:
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)
    return roots


def _modules_of_page(page: _Node, blocks: list[_Node], prefix: str) -> list[str]:
    """一个页面出几个模块：正文够长的区块各自成「页面-区块」，其余（页面自身正文 + 短区块）合成「页面」一个模块。"""
    out: list[str] = []
    rest = page.chars
    big: list[str] = []
    for b in blocks:
        if b.draft:
            continue   # 疑似草稿区块不进大纲（用例不要照它写）
        if b.total() >= MIN_BLOCK_CHARS:
            big.append(f"{prefix}{page.title}" if b.title == page.title else f"{prefix}{page.title}-{b.title}")
        else:
            rest += b.total()
    if rest > 0 or not big:
        if rest > 0 or not blocks:
            out.append(f"{prefix}{page.title}")
    out.extend(big)
    return out


def outline_from_headings(text: str, title: str | None = None) -> list[str]:
    """按整理稿的编号标题生成模块大纲，不经模型：
    - 有文件夹的三级结构 `## 文件夹 › ### 页面 › #### 区块`：模块 = 「页面-区块」，短区块并入「页面」；
    - 两级结构 `## 页面 › ### 区块`（需求说明、没文件夹的页面）同样处理；没有子标题的页面自成一个模块；
    - 疑点清单 / 疑似遗留草稿章节与〔疑似草稿〕区块不进大纲；没有正文的空标题不进大纲。
    title 给了就作为每个模块名的前缀（多需求合并设计时区分来源）。不是整理稿格式时返回空列表。"""
    prefix = f"{title}-" if title else ""
    out: list[str] = []
    for top in _parse(text):
        if top.draft:
            continue
        subs = top.children
        is_group = any(c.children for c in subs)
        if is_group:   # 文件夹：每个三级标题是页面
            if top.chars:
                out.append(f"{prefix}{top.title}")
            for page in subs:
                if page.draft:
                    continue
                if page.children:
                    out.extend(_modules_of_page(page, page.children, prefix))
                elif page.chars:
                    out.append(f"{prefix}{page.title}")
        elif subs:   # 页面 › 区块
            out.extend(_modules_of_page(top, subs, prefix))
        elif top.chars:
            out.append(f"{prefix}{top.title}")
    seen: set[str] = set()
    uniq = [m for m in out if not (m in seen or seen.add(m))]
    return uniq if len(uniq) >= MIN_OUTLINE_MODULES else []


def outline_from_brief(brief: str) -> list[str]:
    """整份简报（可能含多条需求）的标题大纲：各需求分别派生，多需求时模块名带需求标题前缀；任一条拿不到就整体放弃。"""
    segs = requirement_segments(brief)
    out: list[str] = []
    for seg in segs:
        mods = outline_from_headings(seg.text, seg.title if len(segs) > 1 else None)
        if not mods:
            return []
        out.extend(mods)
    return out

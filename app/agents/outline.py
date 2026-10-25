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


# ---- 简报里的问答块（确认结论 / 建议级假设）：分片时按模块回到对应片，不再整块落在最后一片 ----

# design_brief 拼在整理稿后面的两段问答：「- 问（模块）：…\n  确认：…」「- 问（模块）：…\n  假设：…」
_QA_BLOCK_HEADS = ("【待确认事项的确认结论", "【未确认的建议级事项")
_QA_ITEM_RE = re.compile(r"^- 问(?:（(.*?)）)?：")
QA_CHUNK_NOTE = "（以下条目涉及本片章节，是人工确认过的正式规则，与整理稿正文同等对待：须拆成测试点并归入所标模块）"


@dataclass
class QABlock:
    head: str                                   # 块的标题行（【…】原样）
    items: list[tuple[str, str]]                # (模块名, 条目全文「- 问（模块）：…\n  确认：…」)


def split_qa_blocks(text: str) -> tuple[str, list[QABlock]]:
    """把简报里的确认结论 / 建议级假设块摘出来：返回 (去掉这两块的文本, 块列表)。
    块从标题行到下一个【…】行或文末；条目以「- 问」行开头、延续到下一个「- 问」行。没有这两块时文本原样。"""
    lines = text.splitlines()
    rest: list[str] = []
    blocks: list[QABlock] = []
    cur: QABlock | None = None
    item: list[str] = []

    def flush_item() -> None:
        if cur is not None and item:
            m = _QA_ITEM_RE.match(item[0])
            cur.items.append(((m.group(1) or "").strip() if m else "", "\n".join(item).rstrip()))
        item.clear()

    for line in lines:
        if line.startswith(_QA_BLOCK_HEADS):
            flush_item()
            cur = QABlock(line.strip(), [])
            blocks.append(cur)
            continue
        if cur is not None and _BRIEF_PART_RE.match(line):
            flush_item()
            cur = None
        if cur is None:
            rest.append(line)
            continue
        if _QA_ITEM_RE.match(line):
            flush_item()
            item.append(line)
        elif item:
            item.append(line)
        elif line.strip():   # 块头与第一条之间的零散说明：留在块头里
            cur.head = f"{cur.head}\n{line}"
    flush_item()
    if not blocks:
        return text, []
    return "\n".join(rest).strip(), [b for b in blocks if b.items]


def heading_modules(text: str) -> dict[str, str]:
    """编号标题行 → 该节的模块名（标题链「文件夹-页面-区块」，与大纲 / 待确认事项的模块名同一套命名）。
    键是标题行原文（去掉首尾空白），用来在分片里认出各节。"""
    out: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    for line in text.splitlines():
        m = _HEAD_RE.match(line)
        if m is None:
            if line.startswith("# "):
                stack = []
            continue
        level = len(m.group(1))
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, _ANNOT_RE.sub("", m.group(3)).strip()))
        out[line.strip()] = module_name_of(tuple(t for _, t in stack))
    return out


def module_name_of(chain: tuple[str, ...]) -> str:
    """标题链 → 模块名：子标题已带上级名的不重复（「PC」下的「PC-活动入口说明」）。与 analysis._module_name 同规则。"""
    out: list[str] = []
    for title in chain:
        if out and (title == out[-1] or title.startswith(f"{out[-1]}-")):
            out[-1] = title
        else:
            out.append(title)
    return "-".join(out)


def match_module(module: str, names: list[str]) -> list[str]:
    """一个模块名在一组章节模块名里对到哪些：完全相同 › 章节名以「-模块名」结尾（模型给的模块常不带文件夹名）›
    模块名比章节更细（「报名记录-撤销」对「商户管理后台-报名记录」，取最长的那个）› 模块名是章节的上级（取全部子节）。
    对不到返回空列表；对到多个由调用方决定（落在同一片就用、分散在几片算定不到）。"""
    module = (module or "").strip()
    if not module:
        return []
    if module in names:
        return [module]
    hit = [n for n in names if n.endswith(f"-{module}")]
    if hit:
        return hit
    parents = [n for n in names if module.startswith(f"{n}-")]
    if parents:
        return [max(parents, key=len)]
    return [n for n in names if n.startswith(f"{module}-")]


def _attach_qa(parts: list[str], full_text: str, blocks: list[QABlock]) -> list[str]:
    """问答块按条目所标模块分到对应片末尾：条目的模块对到哪一片的章节就跟哪一片；没标模块、对不到、
    或对到的章节分散在几片的（整体性问题）放第一片。每片的块保留原块头并加说明。"""
    if not blocks or not parts:
        return parts
    heads = heading_modules(full_text)
    part_names: list[list[str]] = []
    for p in parts:
        part_names.append([heads[l.strip()] for l in p.splitlines() if l.strip() in heads])
    all_names = [n for names in part_names for n in names]
    per_part: list[dict[str, list[str]]] = [{} for _ in parts]   # 片 → {块头: [条目]}
    for block in blocks:
        for module, item in block.items:
            hit = match_module(module, all_names)
            where = {i for i, names in enumerate(part_names) if any(n in names for n in hit)}
            target = where.pop() if len(where) == 1 else 0
            per_part[target].setdefault(block.head, []).append(item)
    out: list[str] = []
    for p, qa in zip(parts, per_part):
        for head, items in qa.items():
            first, _, more = head.partition("\n")
            p = f"{p}\n\n{first}{QA_CHUNK_NOTE}" + (f"\n{more}" if more else "") + "\n" + "\n".join(items)
        out.append(p)
    return out


# ---- 「人工收录」小节：底稿核对时人工收进来的原句，拆解前清掉样例与控件文案、并入所在页面 ----

_MANUAL_TITLE = "人工收录"
MANUAL_NOTE = "**底稿核对时人工补入本页面的需求原句（正式需求，与上文同等对待，须一并拆解）：**"
_MANUAL_SKIP_RE = re.compile(r"^(-{3,}|\|[-| ]+\||\*\*按.*转录[:：]\*\*|（已剔除[:：].*）|-\s*参考示例)\s*$")
_ASCII_RE = re.compile(r"[0-9A-Za-z,.:/%+#¥$\-]")
_PLACEHOLDER_X_RE = re.compile(r"[Xx]{2,}")
_MANUAL_SAMPLE_RATIO = 0.35   # 数字 / 字母占比到这么多的行是样例数据（账号、日期、金额、流水号），不是规则
_MANUAL_MIN_CHARS = 7         # 更短又没有「：」的是控件文案（「请输入」「下一步」「搜索 重置」）


def _is_manual_noise(line: str) -> bool:
    core = line.strip().lstrip("-•*·").strip().strip("*").strip()
    if not core or _MANUAL_SKIP_RE.match(line.strip()):
        return True
    core = core.replace("｜", "|")
    text = "".join(c.strip("|").strip() for c in core.split("|")) if "|" in core else core
    text = re.sub(r"\s+", "", text)
    if not text:
        return True
    if text.startswith(("请输入", "请选择")):
        return True
    if len(text) < _MANUAL_MIN_CHARS and "：" not in text:
        return True
    probe = _PLACEHOLDER_X_RE.sub("", text)
    return bool(probe) and len(_ASCII_RE.findall(probe)) / len(probe) >= _MANUAL_SAMPLE_RATIO


def clean_manual_sections(text: str) -> str:
    """整理稿里的「人工收录」小节（`页面-人工收录`，底稿核对 include 进来的材料原句）：拆解时模型把它整节当噪音跳过
    （线上 10 个这样的小节 0 条测试点），其中夹着真规则（撤销按钮的展示条件、导出文件名、下拉选项）。
    这里去掉纯样例行（账号 / 日期 / 金额占比高）、控件文案（「请输入」「下一步」）、分隔线与转录说明，
    剩下的规则以说明行代替标题留在所在页面末尾（不再是独立章节，大纲里也不再出一个「人工收录」模块）；
    清完一行不剩的整节删掉。只动这些小节，其余原样。"""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    changed = False
    while i < len(lines):
        m = _HEAD_RE.match(lines[i])
        if m is None or _ANNOT_RE.sub("", m.group(3)).strip() != _MANUAL_TITLE:
            out.append(lines[i])
            i += 1
            continue
        changed = True
        j = i + 1
        while j < len(lines) and not (lines[j].startswith("#") or _BRIEF_PART_RE.match(lines[j])):
            j += 1
        kept = [l for l in lines[i + 1:j] if l.strip() and not _is_manual_noise(l)]
        if kept:
            out.extend(["", MANUAL_NOTE, *kept, ""])
        i = j
    return "\n".join(out) if changed else text


def requirement_chunks(brief: str, max_chars: int) -> tuple[list[str], list[str | None]]:
    """拆解用的分片：先按需求切段（片不跨需求），每段再按章节分片；疑点清单 / 遗留草稿不单独成片，
    带说明挂到本需求最后一片末尾；确认结论 / 建议级假设按条目所标模块回到对应片（原来整块落在最后一片，
    那一片的模型按「只拆本片内容」把它们全跳过）。返回 (分片列表, 每片所属需求标题)。整篇不超限时与原来一样只有一片。"""
    if len(brief) <= max_chars:
        return [brief], [None]
    chunks: list[str] = []
    titles: list[str | None] = []
    for seg in requirement_segments(brief):
        main, appendix = split_appendix(seg.text)
        main, qa_blocks = split_qa_blocks(main)
        parts = split_text(main, max_chars) if main else []
        if len(parts) > 1 and len(parts[-1]) < max_chars // 8:   # 章节装包剩下的小尾巴并入前片（略超阈值无妨）
            tail = parts.pop()   # 先 pop 再赋值：写成 parts[-2] = ... parts.pop() 时右边先执行，三片会把第一片覆盖掉
            parts[-1] = f"{parts[-1]}\n\n{tail}"
        if qa_blocks:
            parts = _attach_qa(parts, main, qa_blocks) if parts else ["\n\n".join(f"{b.head}\n" + "\n".join(t for _, t in b.items) for b in qa_blocks)]
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
    - 有文件夹的三级结构 `## 文件夹 › ### 页面 › #### 区块`：模块 = 「文件夹-页面-区块」，短区块并入「文件夹-页面」
      （页面名已带文件夹名的不重复）；
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
        if is_group:   # 文件夹：每个三级标题是页面，模块名带文件夹名（活动运营后台 / 商户管理后台分得开）
            if top.chars:
                out.append(f"{prefix}{top.title}")
            for page in subs:
                if page.draft:
                    continue
                # 页面名本身已带文件夹名（「PC」下的「PC-活动入口说明」）不重复
                pp = prefix if page.title == top.title or page.title.startswith(f"{top.title}-") else f"{prefix}{top.title}-"
                if page.children:
                    out.extend(_modules_of_page(page, page.children, pp))
                elif page.chars:
                    out.append(f"{pp}{page.title}")
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

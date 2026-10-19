"""需求提取底稿（双向核验）：整理稿与材料必须一一对得上，不允许静默丢失。

材料里的每一行文字是一个「单元」：Axure 页面行带控件编号（[u1125]），其余按「文件序号-行号」编号，每个单元记来源路径
「文件 › 页面 › 章节」。整理稿定稿时每个单元必须有去向之一：
- 收录：整理稿里逐字有它（整行是整理稿子串，或按标点切段后几乎全部命中）；
- 排除：写明原因与判定方——模型（补漏审核时逐句判「不是需求」）、程序（小标题 / 纯样例数据 / 不足 8 个汉字的短标签，
  这三类规则简单、可解释，剩下的一律交模型或人判）、人工（底稿核对页点「排除」）；
- 待处理：以上都不是。提取流水线结束前，待处理的单元按原句直接收进整理稿（〔兜底〕），所以 AI 提取完成时待处理为 0；
  之后逐条审核删了条目、整篇改写，都会重新核算，待处理不为 0 不能「完成审核」。

另一方向（整理稿每条都能在材料里逐字找到，否则标 ⚠）由 extraction.verify_modules 负责。
"""

from __future__ import annotations

import re

from app.agents.analysis import normalize

UNIT_ID_RE = re.compile(r"^\[(u\d+)\]\s?")           # Axure 控件编号前缀
_FILE_HEAD = re.compile(r"^【(文件|链接)：(.+?)】\s*$")
_MD_HEAD = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_MARK_LINE = re.compile(r"^【[^】]{1,40}】\s*$")        # 【文档内图片理解 …】【大图第 1/2 块】【说明 / 备注】这类结构标记
_BULLET = re.compile(r"^[\-•·*]\s+")
_CJK = re.compile(r"[一-鿿]")
_HEADING_TAIL = re.compile(r"[:：]\s*$")
_HEADING_WORD = re.compile(r"^[^，。；,.;]{0,24}(示例|说明|参考图例|清单|区域|入口位)[：:]?\s*$")
_WORDS = re.compile(r"[A-Za-z]{2,}")   # 没有汉字的行：英文整句（≥ 3 个单词）交模型判，其余（数字 / 编号 / 代码 / 样式）程序判样例
_LABEL_PUNCT = re.compile(r"[，。；：、,.;:!！?？=＝<>≤≥/|（）()\[\]【】「」]")
UNIT_LEVEL = 2
MIN_CJK = 8

STATUS_IN, STATUS_EX, STATUS_TODO = "in", "ex", "todo"
BY_MODEL, BY_AUTO, BY_USER = "model", "auto", "user"
AUTO_HEADING, AUTO_SAMPLE, AUTO_LABEL = "小标题", "纯样例数据 / 代码（无中文）", "短标签（不足 8 个汉字的字段名 / 按钮名）"


def auto_reason(text: str) -> str | None:
    """程序直接判排除的三类（可解释、可在底稿页翻回来）；其余交模型 / 人判。"""
    s = text.strip()
    cjk = len(_CJK.findall(s))
    if not cjk and len(_WORDS.findall(s)) < 3:
        return AUTO_SAMPLE
    if cjk <= 12 and _HEADING_TAIL.search(s) and not _LABEL_PUNCT.search(s[:-1]):
        return AUTO_HEADING
    if _HEADING_WORD.match(s) and cjk < 16:
        return AUTO_HEADING
    if 0 < cjk < MIN_CJK and not _LABEL_PUNCT.search(s) and not re.search(r"\d", s):
        return AUTO_LABEL
    return None


def page_of(path: str) -> str:
    """来源路径的页面名（最后一级，去掉「页面：」前缀），用作兜底 / 人工收录条目的模块名前半。"""
    last = path.split(" › ")[-1] if path else ""
    return re.sub(r"^页面[:：]\s*", "", last).strip() or "需求材料"


def source_of(path: str) -> str:
    """条目来源只写两级「文件 › 页面」：路径超过两级时取首尾（Axure 包的路径是「文件 › Axure 原型：… › 页面：…」）。"""
    parts = path.split(" › ") if path else []
    return " › ".join(parts[:2] if len(parts) <= 2 else [parts[0], parts[-1]])


def build_units(materials: str) -> list[dict]:
    """材料 → 单元清单 [{uid, path, text, auto}]。标题行、空行、图片占位、结构标记不算单元。"""
    units: list[dict] = []
    stack: dict[int, str] = {}
    file_name, file_no, line_no = "需求原文", 0, 0
    seen_uid: dict[str, int] = {}
    for raw in materials.split("\n"):
        s = raw.strip()
        m = _FILE_HEAD.match(s)
        if m:
            file_name, stack, file_no, line_no = m.group(2).strip(), {}, file_no + 1, 0
            continue
        m = _MD_HEAD.match(s)
        if m:
            lv = len(m.group(1))
            stack[lv] = m.group(2).strip()
            for k in [k for k in stack if k > lv]:
                del stack[k]
            continue
        if not s or _MARK_LINE.match(s) or s.startswith(("[[图片", "```", "|---", ">")):
            continue
        line_no += 1
        m = UNIT_ID_RE.match(s)
        if m:
            base = m.group(1)
            s = s[m.end():].strip()
            n = seen_uid.get(base, 0) + 1
            seen_uid[base] = n
            uid = base if n == 1 else f"{base}.{n}"
        else:
            uid = f"F{file_no}-{line_no:04d}"
        text = _BULLET.sub("", s).strip()
        if not text:
            continue
        path = " › ".join([file_name] + [stack[k] for k in sorted(stack) if k <= UNIT_LEVEL])
        units.append({"uid": uid, "path": path, "text": text, "auto": auto_reason(text)})
    return units


_SEG_RE = re.compile(r"[，。；：！？,.;:!?、()（）「」【】\[\]]+")
_FULL_RATIO = 0.9


_ITEM_LINE = re.compile(r"^-\s+(?:⚠\s+)?(.*?)(?:\s+〔.*)?$")
_SHORT = 4


def draft_index(draft_text: str) -> tuple[str, set[str]]:
    """整理稿 → (规范化全文, 规范化后的条目集合)。条目集合给很短的单元用：「1 名」「序号」这类只有整条等于某条摘录才算收录，
    按子串判会在任何长条目里误中。"""
    items: set[str] = set()
    for line in (draft_text or "").split("\n"):
        m = _ITEM_LINE.match(line.strip())
        if m:
            items.add(normalize(m.group(1)))
    return normalize(draft_text or ""), items


def coverage(text: str, draft_norm: str, items: set[str] | None = None) -> float:
    """单元在整理稿里的覆盖比例：整行是子串为 1（短于 4 字的要整条等于某条摘录）；否则按标点切段后命中字数占比
    （允许整理稿把一行拆成几条、去掉行首序号）。"""
    n = normalize(text)
    if not n:
        return 1.0
    if len(n) < _SHORT:
        return 1.0 if n in (items or set()) else 0.0
    if n in draft_norm:
        return 1.0
    segs = [normalize(x) for x in _SEG_RE.split(text)]
    segs = [x for x in segs if len(x) >= 3]
    if not segs:
        return 0.0
    tot = sum(len(x) for x in segs)
    return sum(len(x) for x in segs if x in draft_norm) / tot


def covered(text: str, draft_text: str) -> bool:
    """命中 ≥ 90% 才算收录；只收了一部分的（多半是模型改写时截掉了句尾条件）当待处理送回模型连缺的部分一起补全。"""
    norm, items = draft_index(draft_text)
    return coverage(text, norm, items) >= _FULL_RATIO


def account(units: list[dict], draft_text: str, decisions: dict | None = None) -> dict:
    """核算每个单元的去向。decisions：{uid: {"r": 原因, "by": model|user}}（模型 / 人工的排除判定）。
    返回 {units: [{uid, path, text, status, reason, by}], stats}。收录优先于排除：整理稿里有的就是收录。"""
    draft_norm, items = draft_index(draft_text)
    decisions = decisions or {}
    out: list[dict] = []
    stats = {"total": 0, "in": 0, "ex": 0, "todo": 0, "ex_by": {BY_MODEL: 0, BY_AUTO: 0, BY_USER: 0}}
    for u in units:
        row = {"uid": u["uid"], "path": u["path"], "text": u["text"]}
        ratio = coverage(u["text"], draft_norm, items)
        if ratio >= _FULL_RATIO:
            row["status"] = STATUS_IN
        elif u["uid"] in decisions:
            d = decisions[u["uid"]]
            row.update(status=STATUS_EX, reason=d.get("r") or "", by=d.get("by") or BY_MODEL)
        elif u.get("auto"):
            row.update(status=STATUS_EX, reason=u["auto"], by=BY_AUTO)
        else:
            row["status"] = STATUS_TODO
            if ratio > 0:
                row["partial"] = round(ratio, 2)   # 整理稿已有一部分：补漏时提示模型连缺的部分一起补
        stats["total"] += 1
        stats[row["status"]] += 1
        if row["status"] == STATUS_EX:
            stats["ex_by"][row["by"]] = stats["ex_by"].get(row["by"], 0) + 1
        out.append(row)
    return {"units": out, "stats": stats}


def match_decisions(units: list[dict], skips: list[dict], by: str = BY_MODEL) -> dict:
    """模型 skip 列表 → 按原句对到单元编号。只认逐字等于（或 ≥ 8 字且互为子串）的，模型自己概括的一句对不上就不算判定。"""
    index = [(u["uid"], normalize(u["text"])) for u in units]
    out: dict = {}
    for sk in skips:
        if not isinstance(sk, dict):
            continue
        t = normalize(str(sk.get("text") or ""))
        if len(t) < 2:
            continue
        reason = str(sk.get("reason") or "").strip() or "模型判定不是需求"
        for uid, n in index:
            if t == n or (len(t) >= 8 and len(n) >= 8 and (t in n or n in t)):
                out[uid] = {"r": reason[:80], "by": by}
    return out


def todo_units(ledger: dict) -> list[dict]:
    return [u for u in ledger["units"] if u["status"] == STATUS_TODO]


def group_by_page(rows: list[dict]) -> list[dict]:
    pages: dict[str, list[dict]] = {}
    for r in rows:
        pages.setdefault(r["path"], []).append(r)
    return [{"path": k, "units": v} for k, v in pages.items()]

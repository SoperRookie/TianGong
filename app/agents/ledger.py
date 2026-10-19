"""需求提取底稿（双向核验）：整理稿与产品原文必须一一对得上，不允许静默丢失。

底稿只记**产品写的原文**：Axure 页面的文字控件行（带控件编号 [u1125]）、文档段落、原型的说明备注。图片理解的结果是我们自己
的中间产物，不是产品需求原文，不进底稿——只有其中「图中文字原文」一段（模型从截图里转录的文字）作为低优先级单元单独记一栏，
不送模型补漏、不兜底、不拦审核。同一句文字在母版 / 面板各状态 / 各页面反复出现只记一个单元（记出现次数与页面）。

每个单元必须有去向之一：
- 收录：整理稿里逐字有它（整行是整理稿子串，或按标点切段后命中 ≥ 90%；短于 4 字的要整条等于某条摘录）；
- 排除：写明原因与判定方——模型（补漏审核时逐句判「不是需求」）、程序（小标题 / 纯样例数据或代码 / 不足 8 个汉字的短标签，
  这三类规则简单可解释，页面按类折叠）、人工（底稿核对页点「排除」）；模型排除里 ≥ 8 个汉字且带数字 / 条件词的标「需复核」，
  人看过点「确认」才算；
- 待处理：以上都不是。提取流水线结束前，待处理的单元按原句直接收进整理稿（〔兜底〕），所以 AI 提取完成时待处理为 0；
  之后逐条审核删了条目、整篇改写，都会重新核算，待处理不为 0 或需复核没看完不能「完成审核」。

另一方向（整理稿每条都能在材料里逐字找到，否则标 ⚠）由 extraction.verify_modules 负责。
"""

from __future__ import annotations

import re

from app.agents.analysis import normalize

UNIT_ID_RE = re.compile(r"^\[(u\d+)\]\s?")           # Axure 控件编号前缀
_FILE_HEAD = re.compile(r"^【(文件|链接)：(.+?)】\s*$")
_MD_HEAD = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_MARK_LINE = re.compile(r"^【[^】]{1,40}】\s*$")        # 【大图第 1/2 块】【说明 / 备注】这类结构标记
IMG_BEGIN = re.compile(r"^【文档内图片理解 \[\[图片:(\d+)\]\]】\s*$")
IMG_END = re.compile(r"^【图片理解结束 \[\[图片:\d+\]\]】\s*$")
_IMG_FILE = re.compile(r"\.(png|jpe?g|gif|webp|bmp|tiff?)$", re.I)
_VISION_SECTIONS = ("图片类型与整体说明", "界面元素清单", "交互与流程逻辑", "业务规则与约束", "图中文字原文")
_IMG_TEXT_HEAD = re.compile(r"^#{1,6}\s*图中文字原文")
_BULLET = re.compile(r"^[\-•·*]\s+")
_PARSER_NOTE = re.compile(r"^共 \d+ 个页面，按站点地图顺序")   # Axure 解析器自己写的说明行，不是产品原文
_CJK = re.compile(r"[一-鿿]")
_HEADING_TAIL = re.compile(r"[:：]\s*$")
_HEADING_WORD = re.compile(r"^[^，。；,.;]{0,24}(示例|说明|参考图例|清单|区域|入口位)[：:]?\s*$")
_WORDS = re.compile(r"[A-Za-z]{2,}")   # 没有汉字的行：英文整句（≥ 3 个单词）交模型判，其余（数字 / 编号 / 代码 / 样式）程序判样例
_LABEL_PUNCT = re.compile(r"[，。；：、,.;:!！?？=＝<>≤≥/|（）()\[\]【】「」]")
_RULE_SIGNAL = re.compile(r"(\d|时|则|不|必须|需|应|若|如|当|按|≥|≤|>|<|=|%|元|次|天|日|月|秒|分钟|后|前|可)")
UNIT_LEVEL = 2
MIN_CJK = 8

KIND_SOURCE, KIND_IMAGE = "source", "image"
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


def needs_review(text: str) -> bool:
    """模型排除里值得人看一眼的：≥ 8 个汉字且带数字 / 条件词（像规则的句子）。样例、外观、过渡句折叠不看。"""
    return len(_CJK.findall(text)) >= MIN_CJK and bool(_RULE_SIGNAL.search(text))


def page_of(path: str) -> str:
    """来源路径的页面名（最后一级，去掉「页面：」前缀），用作兜底 / 人工收录条目的模块名前半。"""
    last = path.split(" › ")[-1] if path else ""
    return re.sub(r"^页面[:：]\s*", "", last).strip() or "需求材料"


def source_of(path: str) -> str:
    """条目来源只写两级「文件 › 页面」：路径超过两级时取首尾（Axure 包的路径是「文件 › Axure 原型：… › 页面：…」）。"""
    parts = path.split(" › ") if path else []
    return " › ".join(parts[:2] if len(parts) <= 2 else [parts[0], parts[-1]])


def _is_vision_heading(lv: int, title: str) -> bool:
    return lv == 1 and any(title.startswith(n) for n in _VISION_SECTIONS)


def build_units(materials: str) -> list[dict]:
    """材料 → 去重后的单元清单 [{uid, uids, path, pages, count, text, kind, auto}]。
    标题行、空行、图片占位、结构标记不算单元；图片理解块里只有「图中文字原文」段的行算（kind=image），其余跳过。"""
    raw_units: list[dict] = []
    stack: dict[int, str] = {}
    file_name, file_no, line_no = "需求原文", 0, 0
    seen_uid: dict[str, int] = {}
    in_img = img_text = img_file = False
    img_no = 0   # 当前图片理解块是第几张图（材料里的 [[图片:N]] 编号；独立图片附件为 0，页面显示文件名）
    for raw in materials.split("\n"):
        s = raw.strip()
        m = _FILE_HEAD.match(s)
        if m:
            file_name, stack, file_no, line_no = m.group(2).strip(), {}, file_no + 1, 0
            img_file = bool(_IMG_FILE.search(file_name))   # 独立图片附件：整个文件都是图片理解结果
            in_img, img_text, img_no = img_file, False, 0
            continue
        m = IMG_BEGIN.match(s)
        if m:
            in_img, img_text, img_no = True, False, int(m.group(1))
            continue
        if IMG_END.match(s):
            in_img, img_text = img_file, False
            continue
        h = _MD_HEAD.match(s)
        if h:
            lv, title = len(h.group(1)), h.group(2).strip()
            if in_img:
                if _is_vision_heading(lv, title):
                    img_text = bool(_IMG_TEXT_HEAD.match(s))
                    continue
                if img_file:
                    continue
                if lv > 1 and not re.match(r"^页面[:：]", title):
                    continue   # 图片理解里的子标题，不动路径
                in_img = img_text = False   # 老材料没有结束标记：不是图片理解自己的节名就说明块已结束
            stack[lv] = title
            for k in [k for k in stack if k > lv]:
                del stack[k]
            continue
        if not s or s.startswith(("[[图片", "```", "|---", ">")) or _PARSER_NOTE.match(s):
            continue
        if _MARK_LINE.match(s):
            if in_img and not img_file and s.startswith("【说明"):
                in_img = img_text = False   # 老材料：Axure 说明备注紧跟在图片理解后面
            continue
        if in_img and not img_text:
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
        unit = {"uid": uid, "path": path, "text": text, "kind": KIND_IMAGE if in_img else KIND_SOURCE}
        if in_img:
            unit["image"] = img_no   # 图中文字：来自哪一页的第几张图，底稿页按「页面 › 图片 N」分组
        raw_units.append(unit)
    # 去重：同一句（规范化后相同）只记一个单元，原文行优先于图中文字
    units: list[dict] = []
    index: dict[str, dict] = {}
    for kind in (KIND_SOURCE, KIND_IMAGE):
        for u in raw_units:
            if u["kind"] != kind:
                continue
            key = normalize(u["text"])
            if not key:
                continue
            hit = index.get(key)
            if hit is not None:
                if hit["kind"] == kind:
                    hit["count"] += 1
                    hit["uids"].append(u["uid"])
                    if u["path"] not in hit["pages"]:
                        hit["pages"].append(u["path"])
                continue
            unit = {**u, "uids": [u["uid"]], "pages": [u["path"]], "count": 1, "auto": auto_reason(u["text"])}
            index[key] = unit
            units.append(unit)
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


def _empty_stats() -> dict:
    return {"total": 0, "in": 0, "ex": 0, "todo": 0, "review": 0, "review_open": 0, "ex_by": {BY_MODEL: 0, BY_AUTO: 0, BY_USER: 0}}


def account(units: list[dict], draft_text: str, decisions: dict | None = None) -> dict:
    """核算每个单元的去向。decisions：{uid: {"r": 原因, "by": model|user, "checked": {...}}}（模型 / 人工的排除判定，
    按单元任一编号对上）。返回 {units: [{uid, uids, path, pages, count, kind, text, status, reason, by, review, checked, partial}],
    stats: 原文单元统计, image: 图中文字单元统计}。收录优先于排除：整理稿里有的就是收录。"""
    draft_norm, items = draft_index(draft_text)
    decisions = decisions or {}
    out: list[dict] = []
    stats, img = _empty_stats(), _empty_stats()
    for u in units:
        row = {k: u[k] for k in ("uid", "uids", "path", "pages", "count", "kind", "text")}
        if "image" in u:
            row["image"] = u["image"]
        ratio = coverage(u["text"], draft_norm, items)
        d = next((decisions[x] for x in u["uids"] if x in decisions), None)
        if ratio >= _FULL_RATIO:
            row["status"] = STATUS_IN
        elif d is not None:
            row.update(status=STATUS_EX, reason=d.get("r") or "", by=d.get("by") or BY_MODEL)
            if d.get("checked"):
                row["checked"] = d["checked"]
        elif u.get("auto"):
            row.update(status=STATUS_EX, reason=u["auto"], by=BY_AUTO)
        else:
            row["status"] = STATUS_TODO
            if ratio > 0:
                row["partial"] = round(ratio, 2)   # 整理稿已有一部分：补漏时提示模型连缺的部分一起补
        if row["status"] == STATUS_EX and row["by"] == BY_MODEL and needs_review(u["text"]):
            row["review"] = True
        st = img if u["kind"] == KIND_IMAGE else stats
        st["total"] += 1
        st[row["status"]] += 1
        if row["status"] == STATUS_EX:
            st["ex_by"][row["by"]] = st["ex_by"].get(row["by"], 0) + 1
        if row.get("review"):
            st["review"] += 1
            if not row.get("checked"):
                st["review_open"] += 1
        out.append(row)
    return {"units": out, "stats": stats, "image": img}


def todo_units(ledger: dict) -> list[dict]:
    """要送模型 / 兜底的：原文单元里的待处理。图中文字单元不送、不兜底。"""
    return [u for u in ledger["units"] if u["status"] == STATUS_TODO and u["kind"] == KIND_SOURCE]


def group_by_page(rows: list[dict]) -> list[dict]:
    pages: dict[str, list[dict]] = {}
    for r in rows:
        pages.setdefault(r["path"], []).append(r)
    return [{"path": k, "units": v} for k, v in pages.items()]

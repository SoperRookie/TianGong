"""测试点实体与审核状态机（生成质量核心需求 · 二~十四 / 四十六~五十五）。

测试点从"拆解产物字符串"升级为带生命周期的一等实体：
    tp_id + 状态（pending/approved/rejected）+ 锁定 + 审核意见 + 驳回计数 + 来源。
- 通过即锁定（APPROVED+LOCKED），退出 AI 修改队列；
- 驳回带审核意见，AI 只定点修改被驳回项；连续驳回 2~3 次提示人工介入；
- 查漏补缺/AI 补充只允许新增（source 标注 gap/supplement），存量不动；
- 过粗检测（确定性规则）与相似度查重（bigram 兜底 + 语义候选）在此实现。
"""

import re
from datetime import datetime, timezone
import uuid

# 测试维度枚举（覆盖矩阵 · 需求八）；覆盖状态：已覆盖/未覆盖/不适用/待确认（需求九）
DIMENSIONS = [
    "正常流程", "异常流程", "输入校验", "边界值", "状态转换",
    "权限", "数据", "重复操作", "中断", "网络",
]
COVERAGE_STATES = ("已覆盖", "未覆盖", "不适用", "待确认")

# 测试点状态机：pending →(通过) approved+locked / →(驳回) rejected →(AI 定点修改) pending
POINT_STATUSES = ("pending", "approved", "rejected")

# 审核意见类型（需求三十二 / 完整需求 6.4）：驳回类型多选，AI 按类型采取不同修改方式
COMMENT_TYPES = [
    "内容错误", "场景遗漏", "颗粒度过粗", "颗粒度过细", "重复", "步骤不可执行",
    "预期不可验证", "优先级错误", "关键词错误", "超出需求", "需求理解错误",
    "范围过大", "范围不足", "描述不清晰", "不符合项目规范", "其他",
]

# 修改范围（完整需求 6.4）：约束 AI 定向修改的处理边界
FIX_SCOPES = ("当前项", "选中项", "只补遗漏", "当前项重生成", "整批重生成")

# 结构化驳回字段（完整需求 6.4）：类型多选必填 + 原因必填 + 修改要求/备注可选 + 范围
REJECT_FIELDS = ("reject_types", "fix_request", "fix_note", "fix_scope")

# 连续驳回提示阈值（需求三十五）：达到即提示检查需求歧义/人工直接修改
REJECT_HINT_THRESHOLD = 2


class ConcurrencyError(ValueError):
    """乐观锁冲突（完整需求 11 章）：保存时版本号已被他人推进。"""
    pass


def check_base_version(item: dict, current: int, label: str) -> None:
    """保存前比对乐观锁版本：客户端提供 base_version 时必须与当前一致。"""
    base = item.get("base_version")
    if base is not None and int(base) != int(current):
        raise ConcurrencyError(
            f"{label} 已被其他用户修改（当前 v{current}，你基于 v{base}），请重新加载后再保存"
        )


def clear_reject_fields(target: dict) -> None:
    """清空结构化驳回字段（通过/修改/AI 修改回待审后调用）。"""
    target["comment"] = ""
    target["reject_types"] = []
    target["fix_request"] = ""
    target["fix_note"] = ""
    target["fix_scope"] = ""


def validate_rejection(item: dict, label: str) -> dict:
    """校验结构化驳回输入（完整需求 6.4：类型必填多选、原因必填），返回规范化字段。

    label 用于报错文案（如「测试点 TP001」「用例 TC-xx-001」）。
    """
    comment = str(item.get("comment", "")).strip()
    if not comment:
        raise PointReviewError(f"驳回{label} 必须填写驳回原因")
    reject_types = [str(t).strip() for t in item.get("reject_types") or [] if str(t).strip()]
    if not reject_types:
        raise PointReviewError(f"驳回{label} 必须选择驳回类型（可多选）")
    unknown = [t for t in reject_types if t not in COMMENT_TYPES]
    if unknown:
        raise PointReviewError(f"驳回{label} 的驳回类型不合法: {'、'.join(unknown)}")
    fix_scope = str(item.get("fix_scope", "")).strip() or "当前项"
    if fix_scope not in FIX_SCOPES:
        raise PointReviewError(f"驳回{label} 的修改范围不合法: {fix_scope}（可用 {'/'.join(FIX_SCOPES)}）")
    return {
        "comment": comment,
        "reject_types": reject_types,
        "fix_request": str(item.get("fix_request", "")).strip(),
        "fix_note": str(item.get("fix_note", "")).strip(),
        "fix_scope": fix_scope,
    }

# 测试点过粗检测（需求五）：出现以下宽泛词即提示可能包含多个独立验证目标
_COARSE_PATTERNS = re.compile(
    "|".join(["功能是否正常", "整体功能", "整个流程", "所有情况", "全部场景", "各种情况",
              "完整流程", "全流程", "各类场景", "是否可用"])
)
COARSE_WARNING = "当前测试点可能包含多个独立验证目标，建议进一步拆分"


def coarse_warnings(point_text: str) -> list[str]:
    """确定性过粗检测：识别宽泛描述词（需求五）。"""
    if _COARSE_PATTERNS.search(point_text):
        return [COARSE_WARNING]
    return []


# 模型输出键名别名（提示词规定 module / points / point / dimension，但不同模型偶发改写）
_MODULE_KEYS = ("module", "module_name", "name", "title", "模块", "模块名", "功能模块")
_POINTS_KEYS = ("points", "test_points", "testPoints", "items", "children", "测试点", "测试点列表")
_POINT_KEYS = ("point", "test_point", "testPoint", "description", "desc", "content", "text", "title", "name",
               "测试点", "描述")
_DIMENSION_KEYS = ("dimension", "dim", "type", "category", "维度", "测试维度")
_REF_KEYS = ("ref", "source_ref", "requirement_ref", "req_ref", "section", "来源需求点", "需求点", "来源")
REF_MAX_CHARS = 120


def point_ref(item: dict) -> str:
    """测试点的来源需求点（章节 / 条目 / 原文摘录），兼容别名；过长截断。"""
    ref = _first(item, _REF_KEYS)
    return str(ref).strip()[:REF_MAX_CHARS] if isinstance(ref, (str, int, float)) else ""


def _first(d: dict, keys: tuple[str, ...]):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def normalize_points(modules: list[dict]) -> list[dict]:
    """归一化拆解结果：兼容模型输出的字符串测试点与对象测试点两种形态，以及常见键名别名。

    输入 [{module, points: ["文本" | {point, dimension}]}]，输出统一为对象形态。
    模块名 / 测试点列表 / 测试点文本 / 维度的键名若与提示词规定不一致（如 name / test_points / description），
    按别名表识别，避免模型小幅改写格式就导致「没有测试点」。
    """
    normalized: list[dict] = []
    for entry in modules or []:
        if not isinstance(entry, dict):
            continue
        points = []
        raw_points = _first(entry, _POINTS_KEYS)
        if isinstance(raw_points, str):
            raw_points = [raw_points]
        for p in raw_points if isinstance(raw_points, list) else []:
            if isinstance(p, str):
                if p.strip():
                    points.append({"point": p.strip(), "dimension": ""})
            elif isinstance(p, dict):
                text = _first(p, _POINT_KEYS)
                if not isinstance(text, str) or not text.strip():
                    continue
                dim = _first(p, _DIMENSION_KEYS)
                item = {"point": text.strip(), "dimension": str(dim).strip() if isinstance(dim, (str, int, float)) else ""}
                if (ref := point_ref(p)):
                    item["ref"] = ref
                # 保留实体字段（已实体化的测试点二次归一化时不丢状态）
                for key in ("tp_id", "status", "locked", "comment", "reject_count", "source", "warnings",
                            "version", *REJECT_FIELDS):
                    if key in p:
                        item[key] = p[key]
                points.append(item)
        module = _first(entry, _MODULE_KEYS)
        normalized.append({"module": str(module).strip() if isinstance(module, (str, int, float)) else "", "points": points})
    return normalized


_MODULES_KEYS = ("modules", "test_points", "testPoints", "module_list", "模块", "测试点", "拆解结果")
_WRAPPER_KEYS = ("result", "data", "output", "analysis", "response")


def extract_modules(data) -> list[dict]:
    """从模型返回的任意结构中找出「模块列表」：
    顶层数组 / modules 及其别名 / 再往下一层 result|data 包装 / 任意一个元素形如模块的数组。
    找不到返回空列表，由调用方报错。"""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    found = _first(data, _MODULES_KEYS)
    if isinstance(found, list):
        return [x for x in found if isinstance(x, dict)]
    if isinstance(found, dict):  # {"模块A": ["点1", ...], "模块B": [...]} 形态
        return [{"module": k, "points": v} for k, v in found.items() if isinstance(v, list)]
    for k in _WRAPPER_KEYS:
        inner = data.get(k)
        if isinstance(inner, (dict, list)):
            got = extract_modules(inner)
            if got:
                return got
    for v in data.values():  # 兜底：任意值是「含模块名与测试点列表」的对象数组
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v) \
                and all(_first(x, _POINTS_KEYS) is not None for x in v):
            return v
    return []


def assign_entities(modules: list[dict], seq_floor: int = 0) -> list[dict]:
    """将归一化测试点升级为实体：补齐 tp_id / status / locked / 审核字段（幂等）。"""
    modules = normalize_points(modules)
    seq = _max_tp_seq(modules, seq_floor)
    for entry in modules:
        for p in entry["points"]:
            if not p.get("tp_id"):
                seq += 1
                p["tp_id"] = f"TP{seq:03d}"
            p.setdefault("status", "pending")
            p.setdefault("locked", False)
            p.setdefault("comment", "")
            p.setdefault("reject_types", [])
            p.setdefault("fix_request", "")
            p.setdefault("fix_note", "")
            p.setdefault("fix_scope", "")
            p.setdefault("reject_count", 0)
            p.setdefault("source", "ai")
            p.setdefault("version", 1)
            p["warnings"] = coarse_warnings(p["point"])
    return modules


def _max_tp_seq(modules: list[dict], floor: int = 0) -> int:
    """当前最大 tp 序号；floor 为任务持久化的历史最大值（删除的编号不复用，避免版本/回收站串号）。"""
    seq = int(floor or 0)
    for entry in modules:
        for p in entry["points"]:
            m = re.match(r"^TP(\d+)", str(p.get("tp_id", "")))
            if m:
                seq = max(seq, int(m.group(1)))
    return seq


def tp_seq_of(analysis: dict | None) -> int:
    return int((analysis or {}).get("tp_seq") or 0)


def bump_tp_seq(analysis: dict) -> None:
    """把当前最大序号持久化到 analysis.tp_seq（每次新增测试点后调用）。"""
    analysis["tp_seq"] = _max_tp_seq(analysis.get("test_points", []), tp_seq_of(analysis))


def iter_points(modules: list[dict]):
    for entry in modules:
        for p in entry["points"]:
            yield entry, p


def iter_point_dicts(modules: list[dict]):
    """只读遍历：兼容实体化之前的字符串测试点（统计/追溯/搜索用，不用于修改）。"""
    for entry in modules or []:
        for p in entry.get("points", []):
            if isinstance(p, dict):
                yield entry, p
            elif isinstance(p, str):
                yield entry, {"point": p, "status": "approved", "source": "ai"}


def find_point(modules: list[dict], tp_id: str) -> tuple[dict, dict] | None:
    for entry, p in iter_points(modules):
        if p.get("tp_id") == tp_id:
            return entry, p
    return None


class PointReviewError(ValueError):
    pass


def apply_point_review(modules: list[dict], items: list[dict]) -> dict:
    """执行测试点审核操作（支持批量，需求四十八/四十九/五十）。

    action: approve（通过并锁定）/ reject（驳回，须带 comment）/ modify（人工直改）/
            delete（删除）/ unlock（解锁，撤销通过）。
    返回统计与连续驳回提示（需求三十五）。
    """
    counts = {"approve": 0, "reject": 0, "modify": 0, "delete": 0, "unlock": 0}
    hints: list[str] = []
    log: list[dict] = []
    for item in items:
        tp_id = str(item.get("tp_id", ""))
        action = str(item.get("action", ""))
        found = find_point(modules, tp_id)
        if found is None:
            raise PointReviewError(f"测试点不存在: {tp_id}")
        entry, point = found
        record = {"tp_id": tp_id, "action": action, "comment": str(item.get("comment", ""))}
        if action == "approve":
            point["status"], point["locked"] = "approved", True
            clear_reject_fields(point)
        elif action == "reject":
            rejection = validate_rejection(item, f"测试点 {tp_id}")
            point["status"], point["locked"] = "rejected", False
            point.update(rejection)
            record.update(rejection)
            point["reject_count"] = int(point.get("reject_count", 0)) + 1
            if point["reject_count"] >= REJECT_HINT_THRESHOLD:
                hints.append(
                    f"{tp_id} 已连续 {point['reject_count']} 次未通过审核，"
                    "建议检查：1) 需求是否存在歧义 2) 是否需要人工直接修改 3) 是否需要补充需求信息"
                )
        elif action == "modify":
            text = str(item.get("point", "")).strip()
            if not text:
                raise PointReviewError(f"修改测试点 {tp_id} 必须提供 point 内容")
            check_base_version(item, point.get("version", 1), f"测试点 {tp_id}")  # 乐观锁（需求 11）
            record["before"], record["after"] = point["point"], text
            point["point"] = text
            point["version"] = int(point.get("version", 1)) + 1
            if item.get("dimension") is not None:
                point["dimension"] = str(item.get("dimension", ""))
            if item.get("ref") is not None:
                point["ref"] = point_ref(item)
            point["status"], point["locked"] = "pending", False
            clear_reject_fields(point)
            point["warnings"] = coarse_warnings(text)
        elif action == "delete":
            record["before"] = dict(point)
            record["module"] = entry["module"]  # 回收站恢复所需上下文
            entry["points"].remove(point)
            if not entry["points"]:  # 模块内最后一个点删掉后不留空模块（审核页 / 导出不出现空分组）
                modules.remove(entry)
        elif action == "unlock":
            point["status"], point["locked"] = "pending", False
        else:
            raise PointReviewError(f"未知审核操作: {action}（可用 approve/reject/modify/delete/unlock）")
        counts[action] += 1
        log.append(record)
    return {"counts": counts, "hints": hints, "log": log}


def rejected_points(modules: list[dict]) -> list[dict]:
    return [dict(p, module=entry["module"]) for entry, p in iter_points(modules) if p.get("status") == "rejected"]


def confirmable_points(modules: list[dict]) -> list[dict]:
    """确认生成时的正式测试点集（需求六十一：通过→锁定→正式测试点）。

    有任何已通过的点 → 只用已通过的；全部未审核 → 沿用全量（兼容整批确认流程）。
    被驳回的点永不进入生成。
    """
    any_approved = any(p.get("status") == "approved" for _, p in iter_points(modules))
    result: list[dict] = []
    for entry in modules:
        wanted = [
            {"point": p["point"], "dimension": p.get("dimension", ""),
             **({"tp_id": p["tp_id"]} if p.get("tp_id") else {}),
             **({"ref": p["ref"]} if p.get("ref") else {})}
            for p in entry["points"]
            if (p.get("status") == "approved" if any_approved else p.get("status") != "rejected")
        ]
        if wanted:
            result.append({"module": entry["module"], "points": wanted})
    return result


AI_SOURCES = ("ai", "gap", "supplement", "ai_fix")


def add_points(modules: list[dict], additions: list[dict], source: str, seq_floor: int = 0,
               skipped: list | None = None, skip_similar: bool = True) -> list[dict]:
    """只允许新增（需求十/五十六）：新测试点以待审核状态追加，存量不动。

    additions: [{module, point, dimension?}]；模块不存在则新建分组。
    skip_similar=False 用于人工测试点对比录入：与 AI 相同的点正是对比要统计的「一致」项，不能跳过。
    """
    seq = _max_tp_seq(modules, seq_floor)
    added: list[dict] = []
    for item in additions:
        module = str(item.get("module", "")).strip() or "未分组"
        text = str(item.get("point", "")).strip()
        if not text:
            continue
        if skip_similar and _has_similar_point(modules, text):  # 生成前查重（需求十四）：高度匹配则跳过
            if skipped is not None:
                skipped.append({"module": module, "point": text, "reason": "与已有测试点高度相似，已跳过"})
            continue
        seq += 1
        point = {
            "tp_id": f"TP{seq:03d}", "point": text,
            "dimension": str(item.get("dimension", "")).strip(),
            **({"ref": ref} if (ref := point_ref(item)) else {}),
            "status": "pending", "locked": False, "comment": "",
            "reject_count": 0, "source": source, "version": 1,
            "warnings": coarse_warnings(text),
        }
        entry = next((e for e in modules if e["module"] == module), None)
        if entry is None:
            entry = {"module": module, "points": []}
            modules.append(entry)
        entry["points"].append(point)
        added.append(dict(point, module=module))
    return added


# ---- 相似度查重（需求十一~十四）：字符 bigram 兜底，语义判定由上层 LLM 复核 ----


def _bigrams(text: str) -> set[str]:
    compact = re.sub(r"[\s\W_]+", "", text)
    return {compact[i: i + 2] for i in range(len(compact) - 1)}


def similarity(a: str, b: str) -> float:
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _has_similar_point(modules: list[dict], text: str, threshold: float = 0.75) -> bool:
    return any(similarity(p["point"], text) >= threshold for _, p in iter_points(modules))


def _jaccard(ga: set[str], gb: set[str]) -> float:
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


DUP_PAIR_STATUS_TEXT = {"open": "待处置", "ignored": "已忽略", "resolved": "已处置"}


def _pair_key(a: str, b: str) -> str:
    return "|".join(sorted((str(a), str(b))))


def dup_pair_entities(record) -> list[dict]:
    """疑似重复测试点对的条目视图：状态 open / ignored（人工判定非重复）/ resolved（其中一条已删除或改写后不再重复）。

    dup_report.points 为最近一次重复检查的结果；resolved 记录人工处置（忽略 / 重开留痕）。
    测试点被删除后自动视为已处置，不需要再手工点；文本已修改的用重新检查刷新。
    """
    report = record.dup_report or {}
    pairs = report.get("points") or []
    reviews = {r["key"]: r for r in (report.get("resolved") or []) if isinstance(r, dict) and r.get("key")}
    modules = (record.analysis or {}).get("test_points") or []
    alive = {p.get("tp_id") for _, p in iter_points(modules)}
    out: list[dict] = []
    for p in pairs:
        key = _pair_key(p.get("a", ""), p.get("b", ""))
        rv = reviews.get(key) or {}
        gone = [x for x in (p.get("a"), p.get("b")) if x not in alive]
        status = "resolved" if gone else (rv.get("status") or "open")
        out.append({**p, "key": key, "status": status, "deleted": gone,
                    "note": rv.get("note", ""), "by": rv.get("by"), "at": rv.get("at")})
    order = {"open": 0, "ignored": 1, "resolved": 2}
    # 同文不同对象（相似度 1.0）是供确认的说明，排在真要处置的「重复 / 待复核」后面
    verdict_order = {"同文不同对象": 1}
    out.sort(key=lambda e: (order.get(e["status"], 9), verdict_order.get(e.get("verdict"), 0), -float(e.get("similarity") or 0)))
    return out


def record_dup_pair_action(record, key: str, action: str, by: str | None, note: str = "") -> dict:
    """忽略 / 重开一对疑似重复（持久化，刷新与重新检查后保留）。"""
    if action not in ("ignore", "reopen"):
        raise ValueError(f"不支持的处置动作: {action}")
    report = record.dup_report or {"points": [], "resolved": []}
    resolved = [r for r in (report.get("resolved") or []) if isinstance(r, dict) and r.get("key") != key]
    if action == "ignore":
        resolved.append({"key": key, "status": "ignored", "note": (note or "").strip(), "by": by,
                         "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    record.dup_report = {**report, "resolved": resolved}
    return next((e for e in dup_pair_entities(record) if e["key"] == key), {"key": key, "status": "open"})


def _point_key(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text or "")


def same_point_text(a: str, b: str) -> bool:
    """两条测试点去掉标点空白后是否同文。"""
    return bool(_point_key(a)) and _point_key(a) == _point_key(b)


def dedupe_module_points(modules: list[dict]) -> int:
    """拆解结果的确定性去重（实体化之前）：同名模块并成一个（模型偶尔把同一模块输出两遍、分片合并也会撞名），
    同一模块里去掉标点空白后同文的测试点只留第一条。返回去掉的条数。不同模块的同文不动：
    日榜排名与月榜排名各自的「商户名称取会员所属商户名称」是两个测试对象。"""
    by_name: dict[str, dict] = {}
    order: list[str] = []
    removed = 0
    for entry in modules:
        name = str(entry.get("module", ""))
        if name not in by_name:
            by_name[name] = {**entry, "points": []}
            order.append(name)
        seen = {_point_key(p.get("point", "") if isinstance(p, dict) else str(p)) for p in by_name[name]["points"]}
        for p in entry.get("points", []):
            key = _point_key(p.get("point", "") if isinstance(p, dict) else str(p))
            if key in seen:
                removed += 1
                continue
            seen.add(key)
            by_name[name]["points"].append(p)
    modules[:] = [by_name[n] for n in order]
    return removed


def module_page(name: str) -> str:
    """单个模块名猜所属页面：「需求-页面-区块」去掉最后一段；两段的「大模块-子模块」取大模块；没有「-」就是自己。
    只看一个名字分不出「后台-日榜排名」（需求-页面）与「登录-密码」（页面-区块），有整组模块名时用 module_pages。"""
    parts = [x for x in str(name or "").split("-") if x]
    return "-".join(parts[:-1]) if len(parts) >= 2 else str(name or "")


def module_pages(names: list[str]) -> dict[str, str]:
    """整组模块名 → 各自所属页面。按模块之间的父子关系定：有子模块（别的模块名以「它-」开头）的自己就是页面；
    否则父模块在组里的归父模块；都没有再按 module_page 去掉最后一段。
    「后台-日榜排名」有子模块「后台-日榜排名-字段说明」，是页面，不会和「后台-月榜排名」一起被当成页面「后台」。"""
    uniq = list(dict.fromkeys(str(n or "") for n in names))
    pool = set(uniq)
    has_child = {n for n in uniq if any(o.startswith(n + "-") for o in uniq if o != n)}
    out: dict[str, str] = {}
    for n in uniq:
        if n in has_child:
            out[n] = n
            continue
        parts = [x for x in n.split("-") if x]
        parent = next(("-".join(parts[:k]) for k in range(len(parts) - 1, 0, -1) if "-".join(parts[:k]) in pool), None)
        out[n] = parent if parent is not None else module_page(n)
    return out


def duplicate_candidates(modules: list[dict], threshold: float = 0.45, cross_page_threshold: float = 0.7) -> list[dict]:
    """测试点两两相似候选：文字层初筛（语句不同但语义相同的靠 LLM 复核补充）。bigram 只算一次。
    同一页面内（module_pages 按模块父子关系定页面）按 threshold；不同页面的两条要文字几乎一样（≥ cross_page_threshold）才算候选——拆细后 20 来字的测试点
    bigram 重合率天然偏高，日榜 / 月榜 / 报名记录各自的字段与查询规则又本来就相同，全放进去会出上千对；
    候选带上两边的模块名，交裁判与人工判断时才知道是不是同一个测试对象。"""
    flat = [dict(p, module=entry["module"]) for entry, p in iter_points(modules)]
    grams = [_bigrams(p["point"]) for p in flat]
    page_of = module_pages([p["module"] for p in flat])
    pages = [page_of[p["module"]] for p in flat]
    pairs: list[dict] = []
    for i in range(len(flat)):
        for j in range(i + 1, len(flat)):
            score = _jaccard(grams[i], grams[j])
            need = threshold if pages[i] == pages[j] else cross_page_threshold
            if score >= need:
                pairs.append({
                    "a": flat[i]["tp_id"], "a_point": flat[i]["point"], "a_module": flat[i]["module"],
                    "b": flat[j]["tp_id"], "b_point": flat[j]["point"], "b_module": flat[j]["module"],
                    "similarity": round(score, 3),
                    "reason": "文字高度相似",
                })
    pairs.sort(key=lambda x: -x["similarity"])
    return pairs


def parse_manual_points(text: str, default_module: str = "") -> list[dict]:
    """人工测试点批量文本 → additions：每行一条，支持「模块|测试点」或「模块：测试点」，编号前缀自动去掉。"""
    out: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-•*·").strip()
        line = re.sub(r"^(?:TP)?\d+[.、:：)）]\s*", "", line, flags=re.I)
        if not line:
            continue
        module, point = default_module, line
        for sep in ("|", "｜"):
            if sep in line:
                module, point = (x.strip() for x in line.split(sep, 1))
                break
        else:
            m = re.match(r"^([^:：]{1,20})[:：](.+)$", line)
            if m and " " not in m.group(1):
                module, point = m.group(1).strip(), m.group(2).strip()
        if point:
            out.append({"module": module or default_module, "point": point})
    return out


def cross_source_candidates(modules: list[dict], threshold: float = 0.3, top: int = 3) -> list[dict]:
    """人工测试点 × AI 测试点的文字相似候选（每个人工点保留最相近的 top 条），供 LLM 语义复核或降级兜底。"""
    flat = [dict(p, module=entry["module"]) for entry, p in iter_points(modules)]
    manual = [p for p in flat if p.get("source") == "manual"]
    ai = [p for p in flat if p.get("source", "ai") in AI_SOURCES]
    ai_grams = [_bigrams(p["point"]) for p in ai]
    pairs: list[dict] = []
    for m in manual:
        gm = _bigrams(m["point"])
        scored = sorted(((_jaccard(gm, g), a) for g, a in zip(ai_grams, ai)), key=lambda x: -x[0])
        for score, a in scored[:top]:
            if score >= threshold:
                pairs.append({"manual": m["tp_id"], "manual_point": m["point"], "ai": a["tp_id"],
                              "ai_point": a["point"], "similarity": round(score, 3)})
    return pairs


def case_duplicate_candidates(cases: list[dict], threshold: float = 0.6) -> list[dict]:
    """用例层重复候选（需求十二）：综合标题+前置+步骤+预期比对，不只看标题。"""
    def _text(c: dict) -> str:
        steps = " ".join(
            f"{s.get('action', '')} {s.get('expected', '')}" for s in c.get("steps") or []
        )
        return f"{c.get('title', '')} {c.get('precondition', '')} {steps}"

    pairs: list[dict] = []
    grams = [_bigrams(_text(c)) for c in cases]
    for i in range(len(cases)):
        for j in range(i + 1, len(cases)):
            score = _jaccard(grams[i], grams[j])
            if score >= threshold:
                pairs.append({
                    "a": str(cases[i].get("case_id")), "a_title": str(cases[i].get("title")),
                    "b": str(cases[j].get("case_id")), "b_title": str(cases[j].get("title")),
                    "similarity": round(score, 3),
                })
    pairs.sort(key=lambda x: -x["similarity"])
    return pairs


def new_uid() -> str:
    return uuid.uuid4().hex[:8]

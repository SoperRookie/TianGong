"""测试点实体与审核状态机（生成质量核心需求 · 二~十四 / 四十六~五十五）。

测试点从"拆解产物字符串"升级为带生命周期的一等实体：
    tp_id + 状态（pending/approved/rejected）+ 锁定 + 审核意见 + 驳回计数 + 来源。
- 通过即锁定（APPROVED+LOCKED），退出 AI 修改队列；
- 驳回带审核意见，AI 只定点修改被驳回项；连续驳回 2~3 次提示人工介入；
- 查漏补缺/AI 补充只允许新增（source 标注 gap/supplement），存量不动；
- 过粗检测（确定性规则）与相似度查重（bigram 兜底 + 语义候选）在此实现。
"""

import re
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


def normalize_points(modules: list[dict]) -> list[dict]:
    """归一化拆解结果：兼容模型输出的字符串测试点与对象测试点两种形态。

    输入 [{module, points: ["文本" | {point, dimension}]}]，输出统一为对象形态。
    """
    normalized: list[dict] = []
    for entry in modules:
        points = []
        for p in entry.get("points", []):
            if isinstance(p, str):
                points.append({"point": p, "dimension": ""})
            elif isinstance(p, dict) and str(p.get("point", "")).strip():
                item = {"point": str(p["point"]).strip(), "dimension": str(p.get("dimension", "")).strip()}
                # 保留实体字段（已实体化的测试点二次归一化时不丢状态）
                for key in ("tp_id", "status", "locked", "comment", "reject_count", "source", "warnings",
                            *REJECT_FIELDS):
                    if key in p:
                        item[key] = p[key]
                points.append(item)
        normalized.append({"module": str(entry.get("module", "")), "points": points})
    return normalized


def assign_entities(modules: list[dict]) -> list[dict]:
    """将归一化测试点升级为实体：补齐 tp_id / status / locked / 审核字段（幂等）。"""
    modules = normalize_points(modules)
    seq = _max_tp_seq(modules)
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
            p["warnings"] = coarse_warnings(p["point"])
    return modules


def _max_tp_seq(modules: list[dict]) -> int:
    seq = 0
    for entry in modules:
        for p in entry["points"]:
            m = re.match(r"^TP(\d+)", str(p.get("tp_id", "")))
            if m:
                seq = max(seq, int(m.group(1)))
    return seq


def iter_points(modules: list[dict]):
    for entry in modules:
        for p in entry["points"]:
            yield entry, p


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
            record["before"], record["after"] = point["point"], text
            point["point"] = text
            if item.get("dimension") is not None:
                point["dimension"] = str(item.get("dimension", ""))
            point["status"], point["locked"] = "pending", False
            clear_reject_fields(point)
            point["warnings"] = coarse_warnings(text)
        elif action == "delete":
            record["before"] = dict(point)
            entry["points"].remove(point)
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
            {"point": p["point"], "dimension": p.get("dimension", "")}
            for p in entry["points"]
            if (p.get("status") == "approved" if any_approved else p.get("status") != "rejected")
        ]
        if wanted:
            result.append({"module": entry["module"], "points": wanted})
    return result


def add_points(modules: list[dict], additions: list[dict], source: str) -> list[dict]:
    """只允许新增（需求十/五十六）：新测试点以待审核状态追加，存量不动。

    additions: [{module, point, dimension?}]；模块不存在则新建分组。
    """
    seq = _max_tp_seq(modules)
    added: list[dict] = []
    for item in additions:
        module = str(item.get("module", "")).strip() or "未分组"
        text = str(item.get("point", "")).strip()
        if not text:
            continue
        if _has_similar_point(modules, text):  # 生成前查重（需求十四）：高度匹配则跳过
            continue
        seq += 1
        point = {
            "tp_id": f"TP{seq:03d}", "point": text,
            "dimension": str(item.get("dimension", "")).strip(),
            "status": "pending", "locked": False, "comment": "",
            "reject_count": 0, "source": source, "warnings": coarse_warnings(text),
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


def duplicate_candidates(modules: list[dict], threshold: float = 0.45) -> list[dict]:
    """测试点两两相似候选：文字层初筛（语句不同但语义相同的靠 LLM 复核补充）。"""
    flat = [dict(p, module=entry["module"]) for entry, p in iter_points(modules)]
    pairs: list[dict] = []
    for i in range(len(flat)):
        for j in range(i + 1, len(flat)):
            score = similarity(flat[i]["point"], flat[j]["point"])
            if score >= threshold:
                pairs.append({
                    "a": flat[i]["tp_id"], "a_point": flat[i]["point"],
                    "b": flat[j]["tp_id"], "b_point": flat[j]["point"],
                    "similarity": round(score, 3),
                    "reason": "文字高度相似",
                })
    pairs.sort(key=lambda x: -x["similarity"])
    return pairs


def case_duplicate_candidates(cases: list[dict], threshold: float = 0.6) -> list[dict]:
    """用例层重复候选（需求十二）：综合标题+前置+步骤+预期比对，不只看标题。"""
    def _text(c: dict) -> str:
        steps = " ".join(
            f"{s.get('action', '')} {s.get('expected', '')}" for s in c.get("steps") or []
        )
        return f"{c.get('title', '')} {c.get('precondition', '')} {steps}"

    pairs: list[dict] = []
    texts = [_text(c) for c in cases]
    for i in range(len(cases)):
        for j in range(i + 1, len(cases)):
            score = similarity(texts[i], texts[j])
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

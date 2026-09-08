"""生成质量闭环 Agent（生成质量核心需求设计）：

- 独立查漏 Agent + 覆盖矩阵（需求七/八/九/十）：与生成分离的第二轮检查，只允许新增；
- 重复测试点语义复核（需求十一~十三）：文字初筛候选交 LLM 复核，人工决策处置；
- 测试点/用例驳回定点修改（需求三十~三十三）：只输入被驳回对象+审核意见，
  锁定资产确定性保护（模型无法越权修改），输出修改前后 Diff（需求三十四）；
- 需求变更差异分析（需求四十~四十五）：识别变化类型与受影响资产，最小范围更新；
- 学习候选提炼（需求三十六~三十八）：从人工修改留痕归纳规则候选，人工确认后生效。
"""

import json
import re

from loguru import logger
from pydantic import ValidationError

from app.agents.graph import _chat_json
from app.agents.prompts import wrap_data
from app.prompts import prompt_text
from app.llm.client import LLMClient
from app.tasks.points import (
    COVERAGE_STATES,
    DIMENSIONS,
    add_points,
    clear_reject_fields,
    coarse_warnings,
    find_point,
    new_uid,
)
from app.templates import CustomTemplate, TestCase, builtin_default_template


def _dump(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1)


def _points_view(modules: list[dict]) -> list[dict]:
    """测试点的精简视图（注入 Prompt）：只带 tp_id / 内容 / 维度，不泄漏审核状态字段。"""
    return [
        {
            "module": e.get("module", ""),
            "points": [
                {"tp_id": p.get("tp_id", ""), "point": p.get("point", ""), "dimension": p.get("dimension", "")}
                for p in e.get("points", [])
            ],
        }
        for e in modules
    ]


# ---- 独立查漏（需求七/八/十）----


async def run_gap_check(
    llm: LLMClient, requirement: str, modules: list[dict], model: str | None
) -> dict:
    """独立查漏 Agent：输出覆盖矩阵 + 新增建议；确定性保证存量测试点不被改动。"""
    data, result = await _chat_json(
        llm,
        [
            {"role": "system", "content": prompt_text("gap_check")},
            {
                "role": "user",
                "content": f"需求内容：\n{wrap_data('需求原文', requirement)}\n\n已有测试点：\n{wrap_data('测试点', _dump(_points_view(modules)))}",
            },
        ],
        model,
    )
    coverage = {}
    raw = data.get("coverage", {}) if isinstance(data.get("coverage"), dict) else {}
    for dim in DIMENSIONS:
        value = str(raw.get(dim, "待确认")).strip()
        coverage[dim] = value if value in COVERAGE_STATES else "待确认"
    additions = [
        a for a in data.get("additions", [])
        if isinstance(a, dict) and str(a.get("point", "")).strip()
    ]
    logger.info(
        "独立查漏完成：未覆盖 {} 项 / 新增建议 {} 条",
        sum(1 for v in coverage.values() if v == "未覆盖"), len(additions),
    )
    return {"coverage": coverage, "additions": additions, "model_name": result.model_name}


# ---- 重复复核（需求十一~十三）----


async def run_dup_judge(
    llm: LLMClient, requirement: str, pairs: list[dict], model: str | None
) -> list[dict]:
    """对文字初筛的疑似重复对做语义复核；LLM 故障时原样返回初筛结果（降级不阻塞）。"""
    if not pairs:
        return []
    try:
        data, _ = await _chat_json(
            llm,
            [
                {"role": "system", "content": prompt_text("dup_judge")},
                {
                    "role": "user",
                    "content": f"需求内容：\n{wrap_data('需求原文', requirement[:2000])}\n\n疑似重复测试点对：\n{wrap_data('测试点对', _dump(pairs))}",
                },
            ],
            model,
        )
    except Exception as e:
        logger.warning("重复复核 LLM 故障，降级为文字初筛结果：{}", e)
        return [dict(p, verdict="待复核", suggestion="LLM 复核失败，请人工判断") for p in pairs]
    judged = {(str(d.get("a")), str(d.get("b"))): d for d in data.get("duplicates", []) if isinstance(d, dict)}
    merged = []
    for p in pairs:
        j = judged.get((p["a"], p["b"])) or judged.get((p["b"], p["a"])) or {}
        merged.append(
            dict(
                p,
                verdict=str(j.get("verdict", "待复核")),
                reason=str(j.get("reason", p.get("reason", ""))),
                suggestion=str(j.get("suggestion", "建议合并或保留其中一条")),
            )
        )
    # 复核为不重复的对不再打扰人工
    return [p for p in merged if p["verdict"] != "不重复"]


# ---- 测试点驳回定点修改（需求三十~三十三）----


async def run_point_fix(
    llm: LLMClient, requirement: str, modules: list[dict], rejected: list[dict], model: str | None
) -> dict:
    """AI 只获取被驳回测试点 + 结构化驳回信息（需求三十一 / 完整需求 7.1）。

    返回修改**提案**（完整需求 7.3 确认流）：不直接应用，逐项接受/拒绝后经
    apply_point_proposals 落地。
    """
    payload = [
        {"tp_id": p["tp_id"], "module": p.get("module", ""), "point": p["point"],
         "dimension": p.get("dimension", ""),
         "驳回类型": p.get("reject_types") or [], "驳回原因": p.get("comment", ""),
         "修改要求": p.get("fix_request", ""), "修改范围": p.get("fix_scope", "") or "当前项"}
        for p in rejected
    ]
    data, result = await _chat_json(
        llm,
        [
            {"role": "system", "content": prompt_text("point_fix")},
            {
                "role": "user",
                "content": f"关联需求：\n{wrap_data('需求原文', requirement)}\n\n被驳回的测试点与审核意见：\n{wrap_data('测试点与审核意见', _dump(payload))}",
            },
        ],
        model,
    )
    proposals = build_point_proposals(modules, data, allowed={p["tp_id"] for p in rejected})
    return {"proposals": proposals, "model_name": result.model_name}


def build_point_proposals(modules: list[dict], data: dict, allowed: set[str]) -> list[dict]:
    """AI 输出 → 修改提案列表（越权项在此过滤；不改动 modules）。"""
    proposals: list[dict] = []
    seq = 0
    for fix in data.get("fixes", []):
        tp_id = str(fix.get("tp_id", ""))
        if tp_id not in allowed:  # 模型越权改动非驳回项：直接丢弃
            logger.warning("测试点定点修改越权改动 {}（非驳回项），已忽略", tp_id)
            continue
        found = find_point(modules, tp_id)
        if found is None:
            continue
        _, point = found
        action = str(fix.get("action", "modify"))
        entry = {
            "proposal_id": f"P{(seq := seq + 1)}", "tp_id": tp_id, "action": action,
            "comment_type": str(fix.get("comment_type", "其他")),
            "comment": point.get("comment", ""), "note": str(fix.get("note", "")),
            "before": point.get("point", ""), "raw": fix,
        }
        if action == "split":
            split_into = [s for s in fix.get("split_into", [])
                          if isinstance(s, dict) and str(s.get("point", "")).strip()]
            if not split_into:
                continue
            entry["after"] = [str(s["point"]).strip() for s in split_into]
        elif action == "delete":
            entry["after"] = None
        else:
            text = str(fix.get("point", "")).strip()
            if not text:
                continue
            entry["after"] = text
        proposals.append(entry)
    for a in data.get("additions", []):
        if not (isinstance(a, dict) and str(a.get("point", "")).strip()):
            continue
        proposals.append({
            "proposal_id": f"P{(seq := seq + 1)}", "tp_id": None, "action": "add",
            "comment_type": "场景遗漏", "note": str(a.get("reason", "")),
            "before": None, "after": str(a["point"]).strip(),
            "module": str(a.get("module", "")).strip(), "raw": a,
        })
    return proposals


def apply_point_proposals(modules: list[dict], accepted: list[dict]) -> dict:
    """确认流落地：只应用被接受的提案（复用 apply_point_fixes 的确定性合并）。"""
    data = {
        "fixes": [p["raw"] for p in accepted if p.get("action") != "add"],
        "additions": [p["raw"] for p in accepted if p.get("action") == "add"],
    }
    return apply_point_fixes(modules, data, allowed={p["tp_id"] for p in accepted if p.get("tp_id")})


def apply_point_fixes(modules: list[dict], data: dict, allowed: set[str]) -> dict:
    """确定性合并测试点修改：只允许改动被驳回项（铁律兜底），产出 Diff（需求三十四）。

    修改后的测试点回到 pending 待再审核（需求六十一：局部修改 → 再审核），保留驳回计数。
    """
    diff: list[dict] = []
    for fix in data.get("fixes", []):
        tp_id = str(fix.get("tp_id", ""))
        if tp_id not in allowed:  # 模型越权改动非驳回项：直接丢弃
            logger.warning("测试点定点修改越权改动 {}（非驳回项），已忽略", tp_id)
            continue
        found = find_point(modules, tp_id)
        if found is None:
            continue
        entry, point = found
        action = str(fix.get("action", "modify"))
        comment_type = str(fix.get("comment_type", "其他"))
        record = {"tp_id": tp_id, "action": action, "comment_type": comment_type,
                  "comment": point.get("comment", ""), "note": str(fix.get("note", "")),
                  "before": point.get("point", "")}
        if action == "split":
            split_into = [
                s for s in fix.get("split_into", [])
                if isinstance(s, dict) and str(s.get("point", "")).strip()
            ]
            if not split_into:
                continue
            idx = entry["points"].index(point)
            entry["points"].remove(point)
            new_points = []
            for i, s in enumerate(split_into):
                new_points.append({
                    "tp_id": f"{tp_id}-{i + 1:02d}",
                    "point": str(s["point"]).strip(),
                    "dimension": str(s.get("dimension", point.get("dimension", ""))).strip(),
                    "status": "pending", "locked": False, "comment": "",
                    "reject_count": point.get("reject_count", 0), "source": "ai", "version": 1,
                    "warnings": coarse_warnings(str(s["point"])),
                })
            entry["points"][idx:idx] = new_points
            record["after"] = [p["point"] for p in new_points]
        elif action == "delete":
            entry["points"].remove(point)
            record["after"] = None
        else:  # modify
            text = str(fix.get("point", "")).strip()
            if not text:
                continue
            point["point"] = text
            point["version"] = int(point.get("version", 1)) + 1
            if str(fix.get("dimension", "")).strip():
                point["dimension"] = str(fix["dimension"]).strip()
            point["status"], point["locked"] = "pending", False
            clear_reject_fields(point)
            point["warnings"] = coarse_warnings(text)
            record["after"] = text
        diff.append(record)
    added = add_points(
        modules,
        [a for a in data.get("additions", []) if isinstance(a, dict)],
        source="ai_fix",
    )
    return {"diff": diff, "added": added}


# ---- 用例驳回定点修改（需求三十一/三十三/五十四）----


async def run_case_fix(
    llm: LLMClient,
    requirement: str,
    cases: list[dict],
    reviews: dict[str, dict],
    template: CustomTemplate | None,
    model: str | None,
) -> dict:
    """AI 只获取被驳回用例 + 结构化驳回信息；锁定用例确定性保护。

    返回修改**提案**（完整需求 9.4 确认流）：不直接应用，逐项接受/拒绝后经
    apply_case_proposals 落地。
    """
    template = template or builtin_default_template()
    rejected = [
        c for c in cases
        if reviews.get(str(c.get("uid") or ""), {}).get("status") == "rejected"
    ]
    if not rejected:
        return {"proposals": [], "invalid": [], "model_name": None}
    payload = [
        dict(_strip_case(c), **_fix_directives(reviews[str(c.get("uid"))]))
        for c in rejected
    ]
    system = prompt_text("case_fix").format(template_spec=template.prompt_spec())
    data, result = await _chat_json(
        llm,
        [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": f"关联需求：\n{wrap_data('需求原文', requirement)}\n\n被驳回的用例与审核意见：\n{wrap_data('用例与审核意见', _dump(payload))}",
            },
        ],
        model,
    )
    allowed = {str(c.get("case_id")) for c in rejected}
    outcome = build_case_proposals(cases, data, allowed, reviews)
    outcome["model_name"] = result.model_name
    return outcome


def build_case_proposals(
    cases: list[dict], data: dict, allowed: set[str], reviews: dict[str, dict]
) -> dict:
    """AI 输出 → 用例修改提案列表（越权过滤 + 字段/步骤定位兜底预先应用；不改动 cases）。"""
    by_id = {str(c.get("case_id")): c for c in cases}
    meta = {str(f.get("case_id", "")): f for f in data.get("fixes", []) if isinstance(f, dict)}
    proposals: list[dict] = []
    invalid: list[dict] = []
    seq = 0
    for raw in data.get("cases", []):
        if not isinstance(raw, dict):
            continue
        try:
            TestCase.model_validate({**raw, "uid": ""})
        except ValidationError as e:
            invalid.append({"case_id": str(raw.get("case_id", "")), "problem": str(e)})
            continue
        cid = str(raw.get("case_id"))
        origin = by_id.get(cid)
        m = meta.get(cid, {})
        if origin is None:  # 新增用例（拆分/场景遗漏）
            seq += 1
            proposals.append({
                "proposal_id": f"P{seq}", "case_id": cid, "uid": None, "action": "add",
                "comment_type": str(m.get("comment_type", "其他")), "note": str(m.get("note", "")),
                "before": None, "after": _strip_case(raw), "changes": [], "raw": raw,
            })
            continue
        if cid not in allowed:  # 越权修改锁定/未驳回用例：丢弃改动
            logger.warning("用例定点修改越权改动 {}（非驳回项），已忽略", cid)
            continue
        uid = str(origin.get("uid") or "")
        review = reviews.get(uid, {})
        restricted = _restrict_case_fix(
            origin, dict(raw), review.get("fields") or [], review.get("steps") or [])
        changes = _case_field_changes(origin, restricted)
        if not changes:
            continue
        seq += 1
        proposals.append({
            "proposal_id": f"P{seq}", "case_id": cid, "uid": uid, "action": "modify",
            "comment_type": str(m.get("comment_type", "其他")), "note": str(m.get("note", "")),
            "before": _strip_case(origin), "after": _strip_case(restricted),
            "changes": changes, "raw": restricted,
        })
    for cid in data.get("deleted", []):
        cid = str(cid)
        if cid not in allowed or cid not in by_id:
            continue
        m = meta.get(cid, {})
        seq += 1
        proposals.append({
            "proposal_id": f"P{seq}", "case_id": cid, "uid": str(by_id[cid].get("uid") or ""),
            "action": "delete", "comment_type": str(m.get("comment_type", "其他")),
            "note": str(m.get("note", "")), "before": _strip_case(by_id[cid]),
            "after": None, "changes": [], "raw": None,
        })
    return {"proposals": proposals, "invalid": invalid}


def apply_case_proposals(
    cases: list[dict], accepted: list[dict], reviews: dict[str, dict]
) -> dict:
    """确认流落地：只应用被接受的提案（复用 merge_case_fix 的确定性合并与编号顺延）。"""
    data = {
        "cases": [p["raw"] for p in accepted if p["action"] in ("modify", "add")],
        "deleted": [p["case_id"] for p in accepted if p["action"] == "delete"],
    }
    allowed = {p["case_id"] for p in accepted if p["action"] in ("modify", "delete")}
    return merge_case_fix(cases, data, allowed, reviews)


def _strip_case(c: dict) -> dict:
    return {k: v for k, v in c.items() if k != "uid"}


def _fix_directives(review: dict) -> dict:
    """结构化驳回信息 → AI 输入（完整需求 9.3：类型/原因/修改要求/指定字段/指定步骤/范围）。"""
    directives = {
        "驳回类型": review.get("reject_types") or [],
        "驳回原因": review.get("comment", ""),
        "修改要求": review.get("fix_request", ""),
        "修改范围": review.get("fix_scope", "") or "当前项",
    }
    if review.get("fields"):
        directives["指定字段"] = review["fields"]
    if review.get("steps"):
        directives["指定步骤"] = review["steps"]
    return directives


def _restrict_case_fix(origin: dict, new: dict, fields: list, step_nos: list) -> dict:
    """字段/步骤最小修改原则的确定性兜底（完整需求 9.4）。

    人工做了字段级/步骤级定位时，AI 对定位之外内容的改动一律还原为原值——
    与锁定保护同理，不依赖模型自觉。
    """
    if not fields and not step_nos:
        return new
    restricted = dict(new)
    for f in ("module", "title", "priority", "precondition", "keywords", "remark"):
        if fields and f not in fields:
            restricted[f] = origin.get(f, "")
    o_steps = [dict(s) for s in origin.get("steps") or []]
    n_steps = [dict(s) for s in new.get("steps") or []]
    if fields and "steps" not in fields and "expected" not in fields and not step_nos:
        restricted["steps"] = o_steps  # 定位不含步骤/预期：步骤整体不允许动
        return restricted
    if len(n_steps) != len(o_steps) and (step_nos or (fields and "steps" not in fields)):
        # 做了步骤级定位（或只允许改预期）时模型却增删了步骤：越权，步骤整体还原
        logger.warning("用例定点修改越权增删步骤（{} → {} 步），已还原", len(o_steps), len(n_steps))
        restricted["steps"] = o_steps
        return restricted
    if len(n_steps) == len(o_steps):
        merged = []
        for i, (o, n) in enumerate(zip(o_steps, n_steps), start=1):
            if step_nos and i not in step_nos:
                merged.append(o)
                continue
            action = n.get("action", "") if (not fields or "steps" in fields) else o.get("action", "")
            expected = n.get("expected", "") if (not fields or "expected" in fields) else o.get("expected", "")
            merged.append({"action": action, "expected": expected})
        restricted["steps"] = merged
    return restricted


_CASE_SEQ_RE = re.compile(r"^(.*?)(\d+)\s*$")


def merge_case_fix(
    cases: list[dict], data: dict, allowed: set[str], reviews: dict[str, dict]
) -> dict:
    """确定性合并用例修改：锁定/未驳回用例不动，编号不重排（审核期编号保持稳定）。

    - 只接受 allowed（被驳回 case_id）范围内的修改与删除，越权改动丢弃；
    - 新增用例按所在模块现有编号顺延；修改后的用例回到 pending 待再审核；
    - 返回字段级 Diff（需求三十四：修改前 vs 修改后）。
    """
    changed: dict[str, dict] = {}
    invalid: list[dict] = []
    for raw in data.get("cases", []):
        if not isinstance(raw, dict):
            continue
        try:
            TestCase.model_validate({**raw, "uid": ""})
        except ValidationError as e:
            invalid.append({"case_id": str(raw.get("case_id", "")), "problem": str(e)})
            continue
        changed[str(raw.get("case_id"))] = raw
    deleted = {str(x) for x in data.get("deleted", [])} & allowed

    diff: list[dict] = []
    result: list[dict] = []
    for case in cases:
        cid = str(case.get("case_id"))
        uid = str(case.get("uid") or "")
        if cid in deleted:
            diff.append({"case_id": cid, "action": "delete", "before": _strip_case(case)})
            reviews.pop(uid, None)
            continue
        if cid in changed and cid in allowed:
            review = reviews.get(uid, {})
            new = _restrict_case_fix(
                case, dict(changed.pop(cid)),
                review.get("fields") or [], review.get("steps") or [],
            )
            new["uid"] = uid or new_uid()
            new["version"] = int(case.get("version", 1)) + 1  # 乐观锁版本推进（需求 11）
            diff.append({
                "case_id": cid, "action": "modify",
                "changes": _case_field_changes(case, new),
            })
            reviews[new["uid"]] = {"status": "pending", "comment": "",
                                   "reject_count": reviews.get(uid, {}).get("reject_count", 0),
                                   "locked": False}
            result.append(new)
            continue
        if cid in changed:  # 越权修改锁定/未驳回用例：丢弃改动，保留原样
            logger.warning("用例定点修改越权改动 {}（非驳回项），已忽略", cid)
            changed.pop(cid)
        result.append(case)

    # 剩余为新增用例：编号按所在模块顺延，不重排存量
    counters: dict[str, int] = {}
    prefixes: dict[str, str] = {}
    for case in result:
        m = _CASE_SEQ_RE.match(str(case.get("case_id", "")))
        module = str(case.get("module", ""))
        if m:
            counters[module] = max(counters.get(module, 0), int(m.group(2)))
            prefixes.setdefault(module, m.group(1))
    for raw in changed.values():
        new = dict(raw)
        module = str(new.get("module", ""))
        counters[module] = counters.get(module, 0) + 1
        prefix = prefixes.get(module, f"TC-{module}-")
        new["case_id"] = f"{prefix}{counters[module]:03d}"
        new["uid"] = new_uid()
        result.append(new)
        diff.append({"case_id": new["case_id"], "action": "add", "after": _strip_case(new)})

    return {"cases": result, "diff": diff, "invalid": invalid}


def _case_field_changes(before: dict, after: dict) -> list[dict]:
    changes: list[dict] = []
    for field in ("module", "title", "priority", "precondition", "keywords", "remark"):
        b, a = str(before.get(field, "") or ""), str(after.get(field, "") or "")
        if b != a:
            changes.append({"field": field, "before": b, "after": a})
    fmt = lambda c: "\n".join(  # noqa: E731
        f"{s.get('action', '')} => {s.get('expected', '')}" for s in c.get("steps") or []
    )
    if fmt(before) != fmt(after):
        changes.append({"field": "steps", "before": fmt(before), "after": fmt(after)})
    return changes


# ---- 需求变更差异分析（需求四十~四十五）----


async def run_requirement_diff(
    llm: LLMClient,
    old_requirement: str,
    new_requirement: str,
    modules: list[dict],
    cases: list[dict],
    model: str | None,
) -> dict:
    case_index = [
        {"case_id": str(c.get("case_id")), "title": str(c.get("title"))} for c in cases
    ]
    data, result = await _chat_json(
        llm,
        [
            {"role": "system", "content": prompt_text("requirement_diff")},
            {
                "role": "user",
                "content": (
                    f"旧版需求：\n{wrap_data('旧版需求', old_requirement)}\n\n新版需求：\n{wrap_data('新版需求', new_requirement)}\n\n"
                    f"现有测试点：\n{_dump(_points_view(modules))}\n\n"
                    f"现有用例索引：\n{_dump(case_index)}"
                ),
            },
        ],
        model,
    )
    known_points = {p.get("tp_id") for e in modules for p in e.get("points", [])}
    known_cases = {str(c.get("case_id")) for c in cases}
    changes = []
    for ch in data.get("changes", []):
        if not isinstance(ch, dict):
            continue
        changes.append({
            "type": str(ch.get("type", "修改")),
            "description": str(ch.get("description", "")),
            "affected_points": [t for t in ch.get("affected_points", []) if t in known_points],
            "affected_cases": [c for c in ch.get("affected_cases", []) if str(c) in known_cases],
            "action_hint": str(ch.get("action_hint", "")),
        })
    return {
        "changes": changes,
        "new_requirements": [str(x) for x in data.get("new_requirements", [])],
        "summary": str(data.get("summary", "")),
        "model_name": result.model_name,
    }


# ---- 学习候选提炼（需求三十六~三十八）----


async def run_learning_analysis(
    llm: LLMClient, samples: list[dict], model: str | None
) -> list[dict]:
    """从人工修改留痕提炼规则候选；样本不足或无模式时返回空列表。"""
    if not samples:
        return []
    data, _ = await _chat_json(
        llm,
        [
            {"role": "system", "content": prompt_text("learning")},
            {"role": "user", "content": f"人工修改留痕样本（共 {len(samples)} 条）：\n{wrap_data('修改留痕', _dump(samples))}"},
        ],
        model,
    )
    candidates = []
    for c in data.get("candidates", []):
        if not isinstance(c, dict) or not str(c.get("content", "")).strip():
            continue
        candidates.append({
            "content": str(c["content"]).strip(),
            "evidence": str(c.get("evidence", "")),
            "occurrences": int(c.get("occurrences", 0) or 0),
            "confidence": str(c.get("confidence", "中")),
            "scope_hint": str(c.get("scope_hint", "project")),
        })
    return candidates

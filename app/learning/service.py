"""学习语料收集（需求三十六）：从任务留痕中提取「AI 生成 → 人工修改」样本。

学习来源优先级（PRD 锁定）：审核通过后的人工修改（在线评审 review_log、
离线终稿 diff、定点修改 Diff）；用户修订指令作为补充信号；
计划执行中标记为「用例问题」的失败（步骤有误/预期有误）作为提示词优化信号；
需求分析结果的人工修改（改 / 删 / 增）作为 AI 理解需求偏差的信号。
"""

from app.tasks.store import TaskStore

# 单次分析的样本上限：控制 Prompt 规模，优先取最近任务
_MAX_SAMPLES = 80


def collect_samples(
    store: TaskStore, project: str | None = None, limit: int = 30, plans=None, requirements=None
) -> list[dict]:
    samples: list[dict] = []
    if plans is not None:
        samples.extend(_exec_failure_samples(plans, project))
    if requirements is not None:
        samples.extend(_analysis_edit_samples(requirements, project))
    for record in store.list(limit=limit):
        ctx = record.context or {}
        if project and ctx.get("project") != project:
            continue
        task_project = ctx.get("project") or ""
        # 在线评审留痕：人工修改（含修改前后对照）与删除
        for entry in record.review_log:
            if entry.get("action") == "modify" and entry.get("before") and entry.get("after"):
                samples.append({
                    "来源": "在线评审修改", "项目": task_project,
                    "修改前": _case_brief(entry["before"]), "修改后": _case_brief(entry["after"]),
                    "反馈": entry.get("feedback", ""),
                })
            elif entry.get("action") == "delete" and entry.get("before"):
                samples.append({
                    "来源": "在线评审删除", "项目": task_project,
                    "被删用例": _case_brief(entry["before"]), "反馈": entry.get("feedback", ""),
                })
        # 离线终稿 diff：人工定稿相对生成结果的修改
        offline = record.offline_review or {}
        for item in offline.get("modified", []):
            samples.append({
                "来源": "离线终稿修改", "项目": task_project,
                "用例": item.get("title", ""),
                "字段变更": [
                    f"{c.get('field')}: {str(c.get('before', ''))[:80]} → {str(c.get('after', ''))[:80]}"
                    for c in item.get("changes", [])
                ],
            })
        for item in offline.get("added", []):
            samples.append({
                "来源": "离线终稿人工新增", "项目": task_project,
                "新增用例": _case_brief(item.get("case", {})),
            })
        # 修订指令：用户反复提出的要求
        for rev in record.revisions:
            samples.append({
                "来源": "用户修订指令", "项目": task_project, "指令": rev.get("instruction", ""),
            })
        if len(samples) >= _MAX_SAMPLES:
            break
    return samples[:_MAX_SAMPLES]


def _analysis_edit_samples(requirements, project: str | None) -> list[dict]:
    """需求分析结果的人工修改留痕（改文本 / 删除 / 新增）：AI 对需求理解的系统性偏差（术语、数值口径、
    该拆不该拆、哪些推断不算规则）由此归纳；新近需求优先。"""
    from app.agents.prompts import REQUIREMENT_ANALYSIS_LABELS

    samples: list[dict] = []
    for item in requirements.list(project=project):
        for log in item.get("analysis_edits") or []:
            label = REQUIREMENT_ANALYSIS_LABELS.get(log.get("key"), log.get("key"))
            if log.get("action") == "edit" and log.get("before") and log.get("after"):
                samples.append({"来源": "需求分析人工修改", "项目": item.get("project", ""), "分析项": label,
                                "修改前": str(log["before"])[:160], "修改后": str(log["after"])[:160]})
            elif log.get("action") == "delete" and log.get("before"):
                samples.append({"来源": "需求分析人工删除", "项目": item.get("project", ""), "分析项": label,
                                "被删条目": str(log["before"])[:160]})
            elif log.get("action") == "add" and log.get("after"):
                samples.append({"来源": "需求分析人工新增", "项目": item.get("project", ""), "分析项": label,
                                "新增条目": str(log["after"])[:160]})
            if len(samples) >= _MAX_SAMPLES // 2:
                return samples
    return samples


def _exec_failure_samples(plans, project: str | None) -> list[dict]:
    """计划执行失败且分类为「用例问题」的样本：用例快照 + 失败分类 + 原因说明。

    这类失败说明 AI 生成的用例本身不可执行/预期错误，是提示词优化的直接信号；
    系统缺陷/环境/数据类失败与生成质量无关，不入语料。
    """
    from app.plans import CASE_PROBLEM_REASONS

    samples: list[dict] = []
    for plan in plans.list(project=project):
        by_item = {i["item_id"]: i for i in plan["items"]}
        for run in plan["runs"]:
            for item_id, result in run.get("results", {}).items():
                if result.get("status") != "fail":
                    continue
                if result.get("reason") not in CASE_PROBLEM_REASONS:
                    continue
                item = by_item.get(item_id)
                samples.append({
                    "来源": "执行失败（用例问题）", "项目": plan.get("project", ""),
                    "用例": _case_brief(item["snapshot"]) if item else result.get("title", ""),
                    "失败分类": result.get("reason", ""),
                    "失败原因": result.get("note", ""),
                })
    return samples


def _case_brief(case: dict) -> dict:
    steps = case.get("steps") or []
    return {
        "标题": case.get("title", ""),
        "优先级": case.get("priority", ""),
        "前置": str(case.get("precondition", ""))[:60],
        "步骤数": len(steps),
        "首步": (steps[0].get("action", "") if steps else "")[:60],
        "关键词": case.get("keywords", ""),
    }

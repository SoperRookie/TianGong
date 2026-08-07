"""三角色最小编排（M1）：拆解 → 生成 → 评审 → 修正回环（≤3 轮）→ 输出。

有向图预定义（LangGraph StateGraph），非自由对话；
生成与评审强制分离，评审不合格项带具体问题定点打回。
"""

import json
import re

from langgraph.graph import END, StateGraph
from pydantic import ValidationError

from app.agents.json_utils import LLMOutputError, extract_json
from app.agents.prompts import (
    ANALYST_SYSTEM,
    FIX_INSTRUCTION,
    GENERATOR_SYSTEM,
    KNOWLEDGE_CASES_BLOCK,
    KNOWLEDGE_REFS_BLOCK,
    MEMORY_BLOCK,
    REVIEWER_SYSTEM,
)
from app.agents.state import MAX_REVIEW_ROUNDS, OrchestrationState
from app.llm.client import LLMClient
from app.templates import CustomTemplate, TestCase, builtin_default_template


def _dump(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1)


# 输出被 max_tokens 截断或格式异常时的节点级重试次数
_JSON_RETRIES = 1


async def _chat_json(llm: LLMClient, messages: list[dict], model: str | None) -> tuple[dict, "object"]:
    """调用 LLM 并解析 JSON；解析失败自动重试（输出截断/格式异常兜底）。"""
    last_error: Exception | None = None
    for _ in range(1 + _JSON_RETRIES):
        result = await llm.chat(messages, model=model)
        try:
            return extract_json(result.content), result
        except LLMOutputError as e:
            last_error = e
    raise last_error


_CASE_SEQ_RE = re.compile(r"(\d+)\s*$")


def rule_check(cases: list[dict], template: CustomTemplate | None = None) -> list[dict]:
    """规则校验（评审 Agent 的确定性部分）：模板合规（F-4-5）+ 编号唯一 + 模块内编号连续。"""
    issues: list[dict] = []
    seen_ids: set[str] = set()
    module_seqs: dict[str, list[int]] = {}
    for i, raw in enumerate(cases):
        case_id = str(raw.get("case_id", f"<第{i + 1}条>"))
        try:
            TestCase.model_validate(raw)
        except ValidationError as e:
            problems = "; ".join(err["msg"] for err in e.errors())
            issues.append({"case_id": case_id, "problem": f"模板校验不通过: {problems}"})
        if template is not None:
            issues.extend({"case_id": case_id, "problem": p} for p in _template_check(raw, template))
        if case_id in seen_ids:
            issues.append({"case_id": case_id, "problem": "用例编号重复"})
        seen_ids.add(case_id)
        m = _CASE_SEQ_RE.search(case_id)
        if m:
            module_seqs.setdefault(str(raw.get("module", "")), []).append(int(m.group(1)))
    for module, seqs in module_seqs.items():
        expected = list(range(1, len(seqs) + 1))
        if sorted(seqs) != expected:
            issues.append(
                {
                    "case_id": f"<模块:{module}>",
                    "problem": f"模块「{module}」用例编号不连续（应为 001-{len(seqs):03d}，实际序号 {sorted(seqs)}），请重新整理编号",
                }
            )
    return issues


def _template_check(raw: dict, template: CustomTemplate) -> list[str]:
    """自定义模板约束（F-4-5）：优先级枚举、必填自定义列、自定义列取值枚举。"""
    problems: list[str] = []
    priority = str(raw.get("priority", "")).upper()
    allowed = template.priority_enum()
    if priority and priority not in allowed:
        problems.append(f"优先级 {priority} 不在模板允许范围 {allowed}")
    extras = raw.get("extras") or {}
    for col in template.custom_columns():
        value = str(extras.get(col.name, "") or "").strip()
        if col.required and not value:
            problems.append(f"模板必填字段「{col.name}」缺失，请写入 extras[\"{col.name}\"]")
        if value and col.enum_values and value not in col.enum_values:
            problems.append(f"字段「{col.name}」取值 {value} 不在枚举 {col.enum_values} 中")
    return problems


async def analyze_requirement(
    llm: LLMClient, requirement: str, model: str | None, knowledge_cases: str | None = None
) -> dict:
    """需求分析 Agent：测试点拆解 + 盲区识别（拆解确认流程 F-3-3 亦单独调用）。

    knowledge_cases：测试用例库检索结果，拆解阶段注入做覆盖度查漏（PRD 检索时机约束）。
    """
    user = requirement
    if knowledge_cases:
        user += KNOWLEDGE_CASES_BLOCK.format(knowledge=knowledge_cases)
    data, result = await _chat_json(
        llm,
        [
            {"role": "system", "content": ANALYST_SYSTEM},
            {"role": "user", "content": user},
        ],
        model,
    )
    return {
        "test_points": data.get("modules", []),
        "blind_spots": data.get("blind_spots", []),
        "model_name": result.model_name,
    }


def build_graph(
    llm: LLMClient,
    template: CustomTemplate | None = None,
    knowledge_refs: str | None = None,
    knowledge_cases: str | None = None,
    memory_notes: str | None = None,
):
    """knowledge_refs：需求文档/规则库知识，生成前注入生成 Agent；
    knowledge_cases：历史用例，只注入拆解与评审 Agent（PRD 上下文隔离约束）；
    memory_notes：用户偏好与项目记忆（F-8-7），独立预算注入生成 Agent。"""
    template = template or builtin_default_template()
    async def analyze(state: OrchestrationState) -> dict:
        analysis = await analyze_requirement(
            llm, state["requirement"], state.get("model"), knowledge_cases=knowledge_cases
        )
        trace = state.get("trace", []) + [{"agent": "需求分析", "model": analysis["model_name"]}]
        return {
            "test_points": analysis["test_points"],
            "blind_spots": analysis["blind_spots"],
            "trace": trace,
        }

    async def generate(state: OrchestrationState) -> dict:
        system = GENERATOR_SYSTEM.format(template_spec=template.prompt_spec())
        if state.get("issues"):
            # 定点修正：携带评审问题与当前用例全集
            user = FIX_INSTRUCTION.format(
                issues=_dump(state["issues"]), cases=_dump(state["cases"])
            )
            action = "定点修正"
        else:
            user = (
                f"需求内容：\n{state['requirement']}\n\n"
                f"测试点拆解结果：\n{_dump(state['test_points'])}\n\n"
                "请为上述全部测试点生成详细测试用例。"
            )
            if knowledge_refs:
                user += KNOWLEDGE_REFS_BLOCK.format(knowledge=knowledge_refs)
            action = "全量生成"
        if memory_notes:
            user += MEMORY_BLOCK.format(memories=memory_notes)
        data, result = await _chat_json(
            llm,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            state.get("model"),
        )
        trace = state.get("trace", []) + [{"agent": "用例生成", "action": action, "model": result.model_name}]
        return {"cases": data.get("cases", []), "trace": trace}

    async def review(state: OrchestrationState) -> dict:
        issues = rule_check(state["cases"], template)
        current_round = state.get("review_rounds", 0) + 1
        prior = ""
        if state.get("issues"):
            prior = f"\n\n上一轮评审问题（本轮重点核对是否已修复）：\n{_dump(state['issues'])}"
        if knowledge_cases:
            # 历史用例注入评审 Agent 辅助覆盖度把关（PRD：用例库进评审与拆解，不进生成）
            prior += KNOWLEDGE_CASES_BLOCK.format(knowledge=knowledge_cases)
        data, result = await _chat_json(
            llm,
            [
                {"role": "system", "content": REVIEWER_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"本次为第 {current_round} 轮评审。\n\n"
                        f"需求内容：\n{state['requirement']}\n\n"
                        f"待评审用例：\n{_dump(state['cases'])}{prior}"
                    ),
                },
            ],
            state.get("reviewer_model") or state.get("model"),
        )
        missing = data.get("missing", [])
        if not data.get("passed", False):
            issues.extend(data.get("issues", []))
            # 遗漏场景转为可修正问题，让生成 Agent 补用例
            issues.extend({"case_id": "(新增)", "problem": f"补充遗漏场景: {m}"} for m in missing)
        rounds = state.get("review_rounds", 0) + 1
        passed = not issues
        trace = state.get("trace", []) + [
            {"agent": "评审", "model": result.model_name, "round": rounds, "passed": passed}
        ]
        update: dict = {
            "issues": issues,
            "missing": missing,
            "suggestions": state.get("suggestions", []) + data.get("suggestions", []),
            "passed": passed,
            "review_rounds": rounds,
            "trace": trace,
        }
        if not passed and rounds >= MAX_REVIEW_ROUNDS:
            # 回环超限强制出稿，未解决项显式标注供人工重点关注
            update["unresolved"] = issues
        return update

    def decide(state: OrchestrationState) -> str:
        if state["passed"] or state["review_rounds"] >= MAX_REVIEW_ROUNDS:
            return "done"
        return "fix"

    graph = StateGraph(OrchestrationState)
    graph.add_node("analyze", analyze)
    graph.add_node("generate", generate)
    graph.add_node("review", review)
    # 已确认测试点的任务（拆解确认流程 F-3-3）直接从生成开始，不重复拆解
    graph.set_conditional_entry_point(
        lambda s: "generate" if s.get("test_points") else "analyze",
        {"analyze": "analyze", "generate": "generate"},
    )
    graph.add_edge("analyze", "generate")
    graph.add_edge("generate", "review")
    graph.add_conditional_edges("review", decide, {"fix": "generate", "done": END})
    return graph.compile()

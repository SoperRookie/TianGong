"""三角色最小编排（M1）：拆解 → 生成 → 评审 → 修正回环（≤3 轮）→ 输出。

有向图预定义（LangGraph StateGraph），非自由对话；
生成与评审强制分离，评审不合格项带具体问题定点打回。
"""

import json

from langgraph.graph import END, StateGraph
from pydantic import ValidationError

from app.agents.json_utils import extract_json
from app.agents.prompts import ANALYST_SYSTEM, FIX_INSTRUCTION, GENERATOR_SYSTEM, REVIEWER_SYSTEM
from app.agents.state import MAX_REVIEW_ROUNDS, OrchestrationState
from app.llm.client import LLMClient
from app.templates import DEFAULT_TEMPLATE, TestCase


def _dump(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1)


def rule_check(cases: list[dict]) -> list[dict]:
    """规则校验（评审 Agent 的确定性部分）：模板合规 + 编号唯一。"""
    issues: list[dict] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(cases):
        case_id = str(raw.get("case_id", f"<第{i + 1}条>"))
        try:
            TestCase.model_validate(raw)
        except ValidationError as e:
            problems = "; ".join(err["msg"] for err in e.errors())
            issues.append({"case_id": case_id, "problem": f"模板校验不通过: {problems}"})
        if case_id in seen_ids:
            issues.append({"case_id": case_id, "problem": "用例编号重复"})
        seen_ids.add(case_id)
    return issues


def build_graph(llm: LLMClient):
    async def analyze(state: OrchestrationState) -> dict:
        result = await llm.chat(
            [
                {"role": "system", "content": ANALYST_SYSTEM},
                {"role": "user", "content": state["requirement"]},
            ],
            model=state.get("model"),
        )
        data = extract_json(result.content)
        trace = state.get("trace", []) + [{"agent": "需求分析", "model": result.model_name}]
        return {
            "test_points": data.get("modules", []),
            "blind_spots": data.get("blind_spots", []),
            "trace": trace,
        }

    async def generate(state: OrchestrationState) -> dict:
        system = GENERATOR_SYSTEM.format(template_spec=DEFAULT_TEMPLATE.prompt_spec())
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
            action = "全量生成"
        result = await llm.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=state.get("model"),
        )
        data = extract_json(result.content)
        trace = state.get("trace", []) + [{"agent": "用例生成", "action": action, "model": result.model_name}]
        return {"cases": data.get("cases", []), "trace": trace}

    async def review(state: OrchestrationState) -> dict:
        issues = rule_check(state["cases"])
        result = await llm.chat(
            [
                {"role": "system", "content": REVIEWER_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"需求内容：\n{state['requirement']}\n\n"
                        f"待评审用例：\n{_dump(state['cases'])}"
                    ),
                },
            ],
            model=state.get("reviewer_model") or state.get("model"),
        )
        data = extract_json(result.content)
        if not data.get("passed", False):
            issues.extend(data.get("issues", []))
        rounds = state.get("review_rounds", 0) + 1
        passed = not issues
        trace = state.get("trace", []) + [
            {"agent": "评审", "model": result.model_name, "round": rounds, "passed": passed}
        ]
        update: dict = {
            "issues": issues,
            "missing": data.get("missing", []),
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
    graph.set_entry_point("analyze")
    graph.add_edge("analyze", "generate")
    graph.add_edge("generate", "review")
    graph.add_conditional_edges("review", decide, {"fix": "generate", "done": END})
    return graph.compile()

"""三角色最小编排（M1）：拆解 → 生成 → 评审 → 修正回环（≤3 轮）→ 输出。

有向图预定义（LangGraph StateGraph），非自由对话；
生成与评审强制分离，评审不合格项带具体问题定点打回。
"""

import json
import re

from langgraph.graph import END, StateGraph
from pydantic import ValidationError

from app.agents.json_utils import LLMOutputError, extract_json
from app.agents.prompts import ANALYST_SYSTEM, FIX_INSTRUCTION, GENERATOR_SYSTEM, REVIEWER_SYSTEM
from app.agents.state import MAX_REVIEW_ROUNDS, OrchestrationState
from app.llm.client import LLMClient
from app.templates import DEFAULT_TEMPLATE, TestCase


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


def rule_check(cases: list[dict]) -> list[dict]:
    """规则校验（评审 Agent 的确定性部分）：模板合规 + 编号唯一 + 模块内编号连续。"""
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


def build_graph(llm: LLMClient):
    async def analyze(state: OrchestrationState) -> dict:
        data, result = await _chat_json(
            llm,
            [
                {"role": "system", "content": ANALYST_SYSTEM},
                {"role": "user", "content": state["requirement"]},
            ],
            state.get("model"),
        )
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
        data, result = await _chat_json(
            llm,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            state.get("model"),
        )
        trace = state.get("trace", []) + [{"agent": "用例生成", "action": action, "model": result.model_name}]
        return {"cases": data.get("cases", []), "trace": trace}

    async def review(state: OrchestrationState) -> dict:
        issues = rule_check(state["cases"])
        current_round = state.get("review_rounds", 0) + 1
        prior = ""
        if state.get("issues"):
            prior = f"\n\n上一轮评审问题（本轮重点核对是否已修复）：\n{_dump(state['issues'])}"
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
    graph.set_entry_point("analyze")
    graph.add_edge("analyze", "generate")
    graph.add_edge("generate", "review")
    graph.add_conditional_edges("review", decide, {"fix": "generate", "done": END})
    return graph.compile()

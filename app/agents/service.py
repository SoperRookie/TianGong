"""任务编排入口（主控 Agent 的 M1 最小实现：调度有向图并汇总结果）。"""

from pydantic import BaseModel, Field

from app.agents.graph import build_graph
from app.agents.state import MAX_REVIEW_ROUNDS
from app.llm.client import LLMClient
from app.templates import TestCase


class GenerationResult(BaseModel):
    cases: list[TestCase]
    passed: bool
    review_rounds: int
    unresolved: list[dict] = Field(default_factory=list, description="超限强制出稿时的未解决问题")
    blind_spots: list[str] = Field(default_factory=list, description="疑似需求盲区")
    missing: list[str] = Field(default_factory=list, description="评审提示的遗漏场景")
    test_points: list[dict] = Field(default_factory=list)
    trace: list[dict] = Field(default_factory=list, description="Agent 调用链路")


async def run_generation(
    requirement: str,
    llm: LLMClient,
    model: str | None = None,
    reviewer_model: str | None = None,
) -> GenerationResult:
    """执行「拆解 → 生成 → 评审（≤3 轮回环）」全流程。

    model / reviewer_model 为任务级模型选择；reviewer_model 不传时评审与生成同模型。
    """
    graph = build_graph(llm)
    final = await graph.ainvoke(
        {
            "requirement": requirement,
            "model": model,
            "reviewer_model": reviewer_model,
            "review_rounds": 0,
            "trace": [],
        },
        {"recursion_limit": 10 + MAX_REVIEW_ROUNDS * 10},
    )
    return GenerationResult(
        cases=[TestCase.model_validate(c) for c in final["cases"]] if final.get("passed") else _lenient_cases(final),
        passed=final.get("passed", False),
        review_rounds=final.get("review_rounds", 0),
        unresolved=final.get("unresolved", []),
        blind_spots=final.get("blind_spots", []),
        missing=final.get("missing", []),
        test_points=final.get("test_points", []),
        trace=final.get("trace", []),
    )


def _lenient_cases(final: dict) -> list[TestCase]:
    """强制出稿路径：跳过校验不通过的用例，保留可用部分（未解决问题已另行标注）。"""
    cases: list[TestCase] = []
    for raw in final.get("cases", []):
        try:
            cases.append(TestCase.model_validate(raw))
        except Exception:
            continue
    return cases

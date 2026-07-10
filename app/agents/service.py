"""任务编排入口（主控 Agent 的 M1/M2 实现：调度有向图并汇总结果）。

大文档分片（F-2-6）：超长需求按章节切分，各分片**并行**走完整链路后合并——
合并时去重（模块+标题）、模块内重编号；分片部分失败不阻塞整体交付（PRD 异常流程）。
"""

import asyncio
import re

from pydantic import BaseModel, Field

from app.agents.graph import build_graph
from app.agents.state import MAX_REVIEW_ROUNDS
from app.config import get_settings
from app.llm.client import LLMClient
from app.parsers.chunking import split_text
from app.templates import CustomTemplate, TestCase


class GenerationResult(BaseModel):
    cases: list[TestCase]
    passed: bool
    review_rounds: int
    unresolved: list[dict] = Field(default_factory=list, description="超限强制出稿时的未解决问题")
    blind_spots: list[str] = Field(default_factory=list, description="疑似需求盲区")
    missing: list[str] = Field(default_factory=list, description="评审提示的遗漏场景")
    suggestions: list[str] = Field(default_factory=list, description="评审的非阻断优化建议")
    test_points: list[dict] = Field(default_factory=list)
    trace: list[dict] = Field(default_factory=list, description="Agent 调用链路")
    chunks: int = Field(default=1, description="分片数（F-2-6），1 表示未分片")


async def run_generation(
    requirement: str,
    llm: LLMClient,
    model: str | None = None,
    reviewer_model: str | None = None,
    template: CustomTemplate | None = None,
    chunk_max_chars: int | None = None,
) -> GenerationResult:
    """执行「拆解 → 生成 → 评审（≤3 轮回环）」全流程。

    model / reviewer_model 为任务级模型选择；reviewer_model 不传时评审与生成同模型。
    template 为自定义用例模板，缺省用内置默认模板（F-4-2）。
    超长需求自动分片并行处理（F-2-6）。
    """
    chunk_max_chars = chunk_max_chars or get_settings().chunk_max_chars
    chunks = split_text(requirement, chunk_max_chars)
    if len(chunks) == 1:
        return await _run_single(requirement, llm, model, reviewer_model, template)

    outcomes = await asyncio.gather(
        *[_run_single(chunk, llm, model, reviewer_model, template) for chunk in chunks],
        return_exceptions=True,
    )
    return _merge(outcomes)


async def _run_single(
    requirement: str,
    llm: LLMClient,
    model: str | None,
    reviewer_model: str | None,
    template: CustomTemplate | None,
) -> GenerationResult:
    graph = build_graph(llm, template)
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
        suggestions=final.get("suggestions", []),
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


_CASE_ID_PREFIX_RE = re.compile(r"^(.*?)(\d+)\s*$")


def _merge(outcomes: list) -> GenerationResult:
    """合并分片结果：用例去重（模块+标题）、模块内重编号、失败分片显式标注。"""
    merged = GenerationResult(cases=[], passed=True, review_rounds=0, chunks=len(outcomes))
    seen_cases: set[tuple[str, str]] = set()
    seen_notes: dict[str, set[str]] = {"blind_spots": set(), "missing": set(), "suggestions": set()}
    module_points: dict[str, list[str]] = {}

    for i, outcome in enumerate(outcomes, 1):
        if isinstance(outcome, BaseException):
            # 分片失败不阻塞整体交付，显式标注缺失范围（PRD 异常流程）
            merged.passed = False
            merged.unresolved.append({"case_id": f"<分片{i}>", "problem": f"分片处理失败: {outcome}"})
            continue
        merged.passed = merged.passed and outcome.passed
        merged.review_rounds = max(merged.review_rounds, outcome.review_rounds)
        merged.unresolved.extend(outcome.unresolved)
        merged.trace.extend({"chunk": i, **entry} for entry in outcome.trace)
        for field, seen in seen_notes.items():
            for note in getattr(outcome, field):
                if note not in seen:
                    seen.add(note)
                    getattr(merged, field).append(note)
        for tp in outcome.test_points:
            module_points.setdefault(str(tp.get("module", "")), []).extend(tp.get("points", []))
        for case in outcome.cases:
            key = (case.module, case.title.strip())
            if key in seen_cases:
                continue
            seen_cases.add(key)
            merged.cases.append(case)

    merged.test_points = [{"module": m, "points": pts} for m, pts in module_points.items()]
    _renumber(merged.cases)
    return merged


def _renumber(cases: list[TestCase]) -> None:
    """模块内重编号：合并后保证各模块 case_id 从 001 连续（沿用原编号前缀风格）。"""
    counters: dict[str, int] = {}
    for case in cases:
        counters[case.module] = counters.get(case.module, 0) + 1
        m = _CASE_ID_PREFIX_RE.match(case.case_id)
        prefix = m.group(1) if m else f"TC-{case.module}-"
        case.case_id = f"{prefix}{counters[case.module]:03d}"

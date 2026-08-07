"""任务编排入口（主控 Agent 的 M1/M2 实现：调度有向图并汇总结果）。

大文档分片（F-2-6）：超长需求按章节切分，各分片**并行**走完整链路后合并——
合并时去重（模块+标题）、模块内重编号；分片部分失败不阻塞整体交付（PRD 异常流程）。
"""

import asyncio
import re

from pydantic import BaseModel, Field

from app.agents.graph import analyze_requirement, build_graph
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


class AnalysisResult(BaseModel):
    test_points: list[dict]
    blind_spots: list[str] = Field(default_factory=list)
    trace: list[dict] = Field(default_factory=list)


async def run_analysis(
    requirement: str,
    llm: LLMClient,
    model: str | None = None,
    chunk_max_chars: int | None = None,
    knowledge_cases: str | None = None,
) -> AnalysisResult:
    """仅执行需求分析（拆解确认流程 F-3-3 第一阶段）；超长需求分片并行拆解后合并。

    knowledge_cases：历史用例知识（知识管家在拆解阶段注入，覆盖度查漏）。
    """
    chunk_max_chars = chunk_max_chars or get_settings().chunk_max_chars
    chunks = split_text(requirement, chunk_max_chars)
    analyses = await asyncio.gather(
        *[analyze_requirement(llm, chunk, model, knowledge_cases=knowledge_cases) for chunk in chunks]
    )
    module_points: dict[str, list[str]] = {}
    blind_spots: list[str] = []
    trace: list[dict] = []
    for i, a in enumerate(analyses, 1):
        for tp in a["test_points"]:
            module_points.setdefault(str(tp.get("module", "")), []).extend(tp.get("points", []))
        blind_spots.extend(b for b in a["blind_spots"] if b not in blind_spots)
        trace.append({"chunk": i, "agent": "需求分析", "model": a["model_name"]})
    return AnalysisResult(
        test_points=[{"module": m, "points": pts} for m, pts in module_points.items()],
        blind_spots=blind_spots,
        trace=trace,
    )


async def run_generation(
    requirement: str,
    llm: LLMClient,
    model: str | None = None,
    reviewer_model: str | None = None,
    template: CustomTemplate | None = None,
    chunk_max_chars: int | None = None,
    test_points: list[dict] | None = None,
    knowledge_refs: str | None = None,
    knowledge_cases: str | None = None,
) -> GenerationResult:
    """执行「拆解 → 生成 → 评审（≤3 轮回环）」全流程。

    model / reviewer_model 为任务级模型选择；reviewer_model 不传时评审与生成同模型。
    template 为自定义用例模板，缺省用内置默认模板（F-4-2）。
    超长需求自动分片并行处理（F-2-6）。
    test_points 传入已确认的拆解结果（F-3-3）：跳过需求分析，多模块时按模块并行生成。
    knowledge_refs / knowledge_cases：知识管家产出的分类知识（F-7-6 差异化注入时机）。
    """
    kw = {"knowledge_refs": knowledge_refs, "knowledge_cases": knowledge_cases}
    if test_points:
        return await _run_from_points(
            requirement, llm, model, reviewer_model, template, test_points, **kw
        )

    chunk_max_chars = chunk_max_chars or get_settings().chunk_max_chars
    chunks = split_text(requirement, chunk_max_chars)
    if len(chunks) == 1:
        return await _run_single(requirement, llm, model, reviewer_model, template, **kw)

    outcomes = await asyncio.gather(
        *[_run_single(chunk, llm, model, reviewer_model, template, **kw) for chunk in chunks],
        return_exceptions=True,
    )
    return _merge(outcomes)


async def _run_from_points(
    requirement: str,
    llm: LLMClient,
    model: str | None,
    reviewer_model: str | None,
    template: CustomTemplate | None,
    test_points: list[dict],
    knowledge_refs: str | None = None,
    knowledge_cases: str | None = None,
) -> GenerationResult:
    """从已确认测试点继续：单模块直接生成；多模块按模块并行多实例（PRD 4.1a 并行加速）。"""
    kw = {"knowledge_refs": knowledge_refs, "knowledge_cases": knowledge_cases}
    if len(test_points) <= 1:
        return await _run_single(
            requirement, llm, model, reviewer_model, template, test_points=test_points, **kw
        )
    outcomes = await asyncio.gather(
        *[
            _run_single(requirement, llm, model, reviewer_model, template, test_points=[tp], **kw)
            for tp in test_points
        ],
        return_exceptions=True,
    )
    merged = _merge(outcomes)
    merged.chunks = 1  # 并行维度是模块而非文档分片
    return merged


async def _run_single(
    requirement: str,
    llm: LLMClient,
    model: str | None,
    reviewer_model: str | None,
    template: CustomTemplate | None,
    test_points: list[dict] | None = None,
    knowledge_refs: str | None = None,
    knowledge_cases: str | None = None,
) -> GenerationResult:
    graph = build_graph(llm, template, knowledge_refs=knowledge_refs, knowledge_cases=knowledge_cases)
    initial: dict = {
        "requirement": requirement,
        "model": model,
        "reviewer_model": reviewer_model,
        "review_rounds": 0,
        "trace": [],
    }
    if test_points:
        initial["test_points"] = test_points  # 已确认拆解：图从生成节点开始
    final = await graph.ainvoke(initial, {"recursion_limit": 10 + MAX_REVIEW_ROUNDS * 10})
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


async def run_revision(
    requirement: str,
    cases: list[dict],
    instruction: str,
    llm: LLMClient,
    model: str | None = None,
    reviewer_model: str | None = None,
    template: CustomTemplate | None = None,
    test_points: list[dict] | None = None,
    history: list[str] | None = None,
    knowledge_cases: str | None = None,
) -> GenerationResult:
    """多轮修订（F-3-5）：用户修订要求作为定点修正问题进入「生成→评审」回环。

    增量更新：生成 Agent 走定点修正路径，只改受影响用例，其余原样保留。
    history：本任务此前已应用的修订指令（短期会话记忆 F-8-1），注入保持多轮一致性。
    """
    problem = f"用户修订要求：{instruction}"
    if history:
        applied = "；".join(history)
        problem += f"\n（此前已应用的修订，保持其效果不被本次修订破坏：{applied}）"
    graph = build_graph(llm, template, knowledge_cases=knowledge_cases)
    initial: dict = {
        "requirement": requirement,
        "model": model,
        "reviewer_model": reviewer_model,
        "review_rounds": 0,
        "trace": [{"agent": "主控", "action": "修订路由", "instruction": instruction}],
        # 提供 test_points 使图从生成节点进入；issues 使生成节点走定点修正路径
        "test_points": test_points or [{"module": "(修订)", "points": [instruction]}],
        "cases": cases,
        "issues": [{"case_id": "(用户修订)", "problem": problem}],
    }
    final = await graph.ainvoke(initial, {"recursion_limit": 10 + MAX_REVIEW_ROUNDS * 10})
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

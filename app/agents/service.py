"""任务编排入口（主控 Agent 的 M1/M2 实现：调度有向图并汇总结果）。

大文档分片（F-2-6）：超长需求按章节切分，各分片**并行**走完整链路后合并——
合并时去重（模块+标题）、模块内重编号；分片部分失败不阻塞整体交付（PRD 异常流程）。
"""

import asyncio
import re

from loguru import logger
from pydantic import BaseModel, Field

from app.agents.graph import analyze_requirement, build_graph
from app.agents.state import MAX_REVIEW_ROUNDS
from app.agents.prompts import wrap_data
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
    failed_modules: list[str] = Field(default_factory=list, description="生成失败的模块（可断点续跑补全）")


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


async def run_requirement_analysis(
    text: str, llm: LLMClient, model: str | None = None
) -> dict:
    """需求中心 AI 需求分析（完整需求 5.5）：11 项结构化输出。超长需求分片后逐项合并去重。"""
    from app.agents.graph import _chat_json
    from app.agents.prompts import REQUIREMENT_ANALYSIS_KEYS
    from app.prompts import prompt_text

    chunks = split_text(text, get_settings().chunk_max_chars)
    outputs = await asyncio.gather(*[
        _chat_json(llm, [{"role": "system", "content": prompt_text("requirement_analysis")},
                         {"role": "user", "content": f"需求原文：\n{wrap_data('需求原文', chunk)}"}], model)
        for chunk in chunks
    ])
    merged: dict = {k: [] for k in REQUIREMENT_ANALYSIS_KEYS}
    model_name = ""
    for data, result in outputs:
        model_name = result.model_name
        for key in REQUIREMENT_ANALYSIS_KEYS:
            for item in data.get(key) or []:
                item = str(item).strip()
                if item and item not in merged[key]:
                    merged[key].append(item)
    merged["model_name"] = model_name
    merged["chunks"] = len(chunks)
    return merged


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
    memory_notes: str | None = None,
    rule_notes: str | None = None,
    on_analyzed=None,
    checkpoint: dict | None = None,
    on_module_done=None,
    on_analysis=None,
) -> GenerationResult:
    """执行「拆解 → 生成 → 评审（≤3 轮回环）」全流程。

    断点续跑：checkpoint = {"analysis": {...}, "modules": {模块名: GenerationResult}}——已拆解 / 已生成的模块直接复用；
    on_analysis(analysis) / on_module_done(module, result) 在各阶段完成时回调，调用方据此持久化检查点。

    model / reviewer_model 为任务级模型选择；reviewer_model 不传时评审与生成同模型。
    template 为自定义用例模板，缺省用内置默认模板（F-4-2）。
    超长需求自动分片并行处理（F-2-6）。
    test_points 传入已确认的拆解结果（F-3-3）：跳过需求分析，多模块时按模块并行生成。
    knowledge_refs / knowledge_cases：知识管家产出的分类知识（F-7-6 差异化注入时机）。
    memory_notes：用户偏好与项目记忆（F-8-7），独立预算注入生成 Agent。
    on_analyzed：拆解完成回调（任务进度上报用，F-6-2）。
    """
    kw = {"knowledge_refs": knowledge_refs, "knowledge_cases": knowledge_cases,
          "memory_notes": memory_notes, "rule_notes": rule_notes}
    ck = {"checkpoint": checkpoint, "on_module_done": on_module_done}
    if test_points:
        return await _run_from_points(
            requirement, llm, model, reviewer_model, template, test_points, **kw, **ck
        )

    chunk_max_chars = chunk_max_chars or get_settings().chunk_max_chars
    chunks = split_text(requirement, chunk_max_chars)
    if len(chunks) == 1:
        return await _analyze_then_generate(
            requirement, llm, model, reviewer_model, template, on_analyzed=on_analyzed,
            on_analysis=on_analysis, **kw, **ck
        )

    logger.info("需求 {} 字超过分片阈值，切分为 {} 片并行处理", len(requirement), len(chunks))
    outcomes = await asyncio.gather(
        *[
            _analyze_then_generate(chunk, llm, model, reviewer_model, template, on_analyzed=on_analyzed, **kw)
            for chunk in chunks
        ],
        return_exceptions=True,
    )
    return _merge(outcomes)


async def _analyze_then_generate(
    requirement: str,
    llm: LLMClient,
    model: str | None,
    reviewer_model: str | None,
    template: CustomTemplate | None,
    knowledge_refs: str | None = None,
    knowledge_cases: str | None = None,
    memory_notes: str | None = None,
    rule_notes: str | None = None,
    on_analyzed=None,
    checkpoint: dict | None = None,
    on_module_done=None,
    on_analysis=None,
) -> GenerationResult:
    """先拆解，再按模块并行生成（PRD 4.1a 生成 Agent 多实例）。

    单次生成调用只输出一个模块的用例，避免大需求下输出超过模型 max_tokens 被截断
    （多数模型单次输出上限 8K～16K，全模块一次性输出必然超限）。
    检查点里已有拆解结果时跳过拆解（断点续跑）。
    """
    saved = (checkpoint or {}).get("analysis")
    if saved and saved.get("test_points"):
        analysis = {"test_points": saved["test_points"], "blind_spots": saved.get("blind_spots", []),
                    "model_name": saved.get("model_name", "")}
        logger.info("断点续跑：复用已保存的拆解结果（{} 个模块）", len(analysis["test_points"]))
    else:
        analysis = await analyze_requirement(llm, requirement, model, knowledge_cases=knowledge_cases)
        if on_analysis:
            on_analysis({"test_points": analysis["test_points"], "blind_spots": analysis["blind_spots"],
                         "model_name": analysis.get("model_name", "")})
    if on_analyzed:
        on_analyzed()
    result = await _run_from_points(
        requirement, llm, model, reviewer_model, template, analysis["test_points"],
        knowledge_refs=knowledge_refs, knowledge_cases=knowledge_cases,
        memory_notes=memory_notes, rule_notes=rule_notes, checkpoint=checkpoint, on_module_done=on_module_done,
    )
    for spot in analysis["blind_spots"]:
        if spot not in result.blind_spots:
            result.blind_spots.insert(0, spot)
    result.trace = [{"agent": "需求分析", "model": analysis["model_name"]}] + result.trace
    return result


async def _run_from_points(
    requirement: str,
    llm: LLMClient,
    model: str | None,
    reviewer_model: str | None,
    template: CustomTemplate | None,
    test_points: list[dict],
    knowledge_refs: str | None = None,
    knowledge_cases: str | None = None,
    memory_notes: str | None = None,
    rule_notes: str | None = None,
    checkpoint: dict | None = None,
    on_module_done=None,
) -> GenerationResult:
    """从已确认测试点继续：单模块直接生成；多模块按模块并行多实例（PRD 4.1a 并行加速）。

    断点续跑：checkpoint["modules"] 里已完成的模块直接复用结果，只生成缺失模块；
    每个模块成功后回调 on_module_done(模块名, 结果) 供调用方落盘检查点。
    """
    kw = {"knowledge_refs": knowledge_refs, "knowledge_cases": knowledge_cases,
          "memory_notes": memory_notes, "rule_notes": rule_notes}
    done = (checkpoint or {}).get("modules") or {}
    if not test_points:  # 拆解为空的兜底：回退图内拆解
        return await _run_single(requirement, llm, model, reviewer_model, template, **kw)

    async def _module(tp: dict) -> GenerationResult:
        name = str(tp.get("module", ""))
        if name in done:
            logger.info("断点续跑：模块「{}」复用已生成结果", name)
            return GenerationResult.model_validate(done[name])
        result = await _run_single(requirement, llm, model, reviewer_model, template, test_points=[tp], **kw)
        if on_module_done:
            try:
                on_module_done(name, result)
            except Exception as e:  # 检查点落盘失败不影响生成
                logger.warning("模块「{}」检查点保存失败：{}", name, e)
        return result

    if len(test_points) == 1:
        return await _module(test_points[0])
    logger.info("按 {} 个模块并行生成：{}（复用 {} 个）", len(test_points),
                [str(tp.get("module", "")) for tp in test_points], sum(1 for tp in test_points if str(tp.get("module", "")) in done))
    outcomes = await asyncio.gather(*[_module(tp) for tp in test_points], return_exceptions=True)
    merged = _merge(outcomes, modules=[str(tp.get("module", "")) for tp in test_points], points=test_points)
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
    memory_notes: str | None = None,
    rule_notes: str | None = None,
) -> GenerationResult:
    graph = build_graph(llm, template, knowledge_refs=knowledge_refs,
                        knowledge_cases=knowledge_cases, memory_notes=memory_notes,
                        rule_notes=rule_notes)
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
        cases=[TestCase.model_validate(c, context={"require_expected": True}) for c in final["cases"]] if final.get("passed") else _lenient_cases(final),
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
    memory_notes: str | None = None,
    rule_notes: str | None = None,
) -> GenerationResult:
    """多轮修订（F-3-5）：用户修订要求作为定点修正问题进入「生成→评审」回环。

    增量更新：生成 Agent 走定点修正路径，只改受影响用例，其余原样保留。
    history：本任务此前已应用的修订指令（短期会话记忆 F-8-1），注入保持多轮一致性。
    memory_notes：用户偏好与项目记忆（F-8-7）。
    """
    problem = f"用户修订要求：{wrap_data('修订要求', instruction)}"
    if history:
        applied = "；".join(history)
        problem += f"\n（此前已应用的修订，保持其效果不被本次修订破坏：{applied}）"
    graph = build_graph(llm, template, knowledge_cases=knowledge_cases,
                        memory_notes=memory_notes, rule_notes=rule_notes)
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
        cases=[TestCase.model_validate(c, context={"require_expected": True}) for c in final["cases"]] if final.get("passed") else _lenient_cases(final),
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
            cases.append(TestCase.model_validate(raw, context={"require_expected": True}))
        except Exception:
            continue
    return cases


_CASE_ID_PREFIX_RE = re.compile(r"^(.*?)(\d+)\s*$")


def _merge(outcomes: list, modules: list[str] | None = None, points: list[dict] | None = None) -> GenerationResult:
    """合并分片 / 模块结果：用例去重（模块+标题）、模块内重编号、失败分片显式标注。

    modules 给出时按模块并行：失败项记入 failed_modules，供「继续完成」只补这些模块。
    """
    merged = GenerationResult(cases=[], passed=True, review_rounds=0, chunks=len(outcomes))
    if outcomes and all(isinstance(o, BaseException) for o in outcomes):
        # 全部分片 / 模块都失败（典型：模型欠费）：没有任何产出，按任务失败处理并保留检查点供续跑
        raise outcomes[0]
    seen_cases: set[tuple[str, str]] = set()
    seen_notes: dict[str, set[str]] = {"blind_spots": set(), "missing": set(), "suggestions": set()}
    module_points: dict[str, list[str]] = {}

    from app.agents.json_utils import LLMOutputError
    from app.llm.client import AllModelsFailedError
    from app.llm.schemas import MissingAPIKeyError

    for i, outcome in enumerate(outcomes, 1):
        if isinstance(outcome, BaseException):
            if isinstance(outcome, (asyncio.CancelledError, TypeError, KeyError, AttributeError, IndexError, NameError)):
                raise outcome  # 取消 / 编程错误：不能伪装成"分片失败"以 completed 出稿
            # 分片失败不阻塞整体交付，显式标注缺失范围（PRD 异常流程）
            merged.passed = False
            if modules is not None:
                name = modules[i - 1]
                merged.failed_modules.append(name)
                if points:  # 失败模块的测试点也要留在结果里，补全时据此重新生成
                    module_points.setdefault(name, []).extend(points[i - 1].get("points", []))
                merged.unresolved.append({"case_id": f"<模块:{name}>", "module": name,
                                          "problem": f"模块「{name}」生成失败（可点「继续完成」补全）: {outcome}"})
                logger.error("模块「{}」生成失败（不阻塞整体交付）：{}", name, outcome)
            else:
                merged.unresolved.append({"case_id": f"<分片{i}>", "problem": f"分片处理失败: {outcome}"})
                logger.error("分片 {} 处理失败（不阻塞整体交付）：{}", i, outcome)
            continue
        merged.failed_modules.extend(m for m in outcome.failed_modules if m not in merged.failed_modules)
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
    """模块内重编号：与 graph.renumber_case_ids 同一规则（前缀由模块派生、全局唯一），作用于 TestCase 对象。"""
    from app.agents.graph import renumber_case_ids

    dicts = [{"case_id": c.case_id, "module": c.module} for c in cases]
    renumber_case_ids(dicts)
    for case, d in zip(cases, dicts):
        case.case_id, case.module = d["case_id"], d["module"]

"""任务编排入口（主控 Agent 的 M1/M2 实现：调度有向图并汇总结果）。

大文档分片（F-2-6）：超长需求按章节切分，各分片**并行**走完整链路后合并——
合并时去重（模块+标题）、模块内重编号；分片部分失败不阻塞整体交付（PRD 异常流程）。
"""

import asyncio
import re

from loguru import logger
from pydantic import BaseModel, Field

from app.agents.graph import analyze_requirement, apply_extensions, build_graph, extend_points, outline_requirement
from app.agents.state import MAX_REVIEW_ROUNDS
from app.agents.prompts import wrap_data
from app.config import get_settings
from app.llm.client import LLMClient
from app.agents.outline import clean_manual_sections, outline_from_brief, requirement_chunks
from app.templates import CustomTemplate, TestCase


async def gather_or_cancel(coros):
    """并行执行，任一失败立即取消其余协程后再抛出。

    asyncio.gather 默认不取消兄弟协程：一片拆解失败后任务已标记失败，其余分片却仍在后台继续调用模型
    （前端已提示失败、后台还在消耗 token）。这里改为失败即取消，等取消完成后再向上抛。
    """
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        pending = [t for t in tasks if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        raise


# 全文大纲一次送模型的上限：超过则改用各分片开头拼接（私有化小上下文模型也能出大纲）
OUTLINE_MAX_CHARS = 120_000


def _outline_source(requirement: str, chunks: list[str]) -> str:
    if len(requirement) <= OUTLINE_MAX_CHARS:
        return requirement
    head = max(1500, OUTLINE_MAX_CHARS // max(len(chunks), 1))
    return "\n\n……\n\n".join(c[:head] for c in chunks)


async def _outline_for(llm: LLMClient, requirement: str, chunks: list[str], model: str | None) -> list[str]:
    """长需求：先出全文模块大纲，再分片拆解并注入大纲，避免各片各自命名、漏掉跨片的模块；大纲失败不阻塞。
    需求是整理稿（带编号标题）时大纲直接按页面 / 区块标题生成，粗细与模型无关（gpt-6-sol 给 6 万字只列 52 个模块，
    拆解跟着变粗）；多需求合并时模块名带需求标题前缀。不是整理稿格式才让模型出大纲。"""
    if len(chunks) <= 1:
        return []
    heads = outline_from_brief(requirement)
    if heads:
        logger.info("模块大纲按整理稿标题生成：{} 个模块，不调模型", len(heads))
        return heads
    try:
        return await outline_requirement(llm, _outline_source(requirement, chunks), model)
    except Exception as e:
        logger.warning("需求大纲生成失败，分片拆解不注入大纲：{}", e)
        return []


class GenerationResult(BaseModel):
    cases: list[TestCase]
    passed: bool
    review_rounds: int
    unresolved: list[dict] = Field(default_factory=list, description="超限强制出稿时的未解决问题")
    blind_spots: list[str] = Field(default_factory=list, description="疑似需求盲区")
    blind_spot_refs: dict[str, dict] = Field(default_factory=dict, description="盲区出处 {盲区文本: {module, ref}}")
    resolved_blind_spots: list[dict] = Field(default_factory=list, description="分片盲区复核：已被需求其他分片说明而剔除的盲区 [{text, evidence, chunk}]")
    missing: list[str] = Field(default_factory=list, description="评审提示的遗漏场景")
    suggestions: list[str] = Field(default_factory=list, description="评审的非阻断优化建议")
    test_points: list[dict] = Field(default_factory=list)
    trace: list[dict] = Field(default_factory=list, description="Agent 调用链路")
    chunks: int = Field(default=1, description="分片数（F-2-6），1 表示未分片")
    failed_modules: list[str] = Field(default_factory=list, description="生成失败的模块（可断点续跑补全）")


class AnalysisResult(BaseModel):
    test_points: list[dict]
    blind_spots: list[str] = Field(default_factory=list)
    blind_spot_refs: dict[str, dict] = Field(default_factory=dict, description="盲区出处 {盲区文本: {module, ref}}")
    resolved_blind_spots: list[dict] = Field(default_factory=list, description="分片盲区复核剔除项（含原文依据）")
    trace: list[dict] = Field(default_factory=list)
    e2e: dict | None = Field(default=None, description="端到端场景摘要：{scenarios: [{name, terminals, flow, points}], points, model_name} 或 {error}")


async def _verify_chunk_blind_spots(
    llm: LLMClient, chunks: list[str], blind_spots: list[str], model: str | None
) -> tuple[list[str], list[dict], list[dict]]:
    """分片模式下复核盲区：返回 (保留的盲区, 剔除项, 追加的 trace)。单片不复核（模型已看过全文）。"""
    if len(chunks) <= 1 or not blind_spots:
        return blind_spots, [], []
    from app.agents.quality import run_blind_spot_verify

    resolved = await run_blind_spot_verify(llm, chunks, blind_spots, model)
    gone = {r["text"] for r in resolved}
    kept = [b for b in blind_spots if b not in gone]
    trace = [{"agent": "盲区复核", "checked": len(blind_spots), "removed": len(resolved)}]
    return kept, resolved, trace


def _extend_enabled(extend: bool | None) -> bool:
    return get_settings().point_extension if extend is None else bool(extend)


async def _e2e_step(llm: LLMClient, requirement: str, model: str | None, e2e: bool | None,
                    section_terminals: dict[str, str] | None) -> tuple[str, list[dict], dict | None]:
    """拆解前的端到端场景（配置 point_e2e / 参数 e2e）：通读全文理出跨端主线。返回 (注入各分片的参考块, 场景点模块, 摘要)；
    故障只丢这一步，拆解照常。"""
    if not (get_settings().point_e2e if e2e is None else bool(e2e)):
        return "", [], None
    from app.agents.quality import e2e_reference_block, run_e2e_scenarios

    try:
        r = await run_e2e_scenarios(llm, requirement, model, section_terminals)
    except Exception as e:  # 端到端是参考与补充：故障不影响明文拆解
        logger.warning("端到端场景故障，跳过（拆解不带端到端参考）：{}", e)
        return "", [], {"error": str(e)}
    modules: dict[str, list[dict]] = {}
    for a in r["additions"]:
        modules.setdefault(a["module"], []).append({"point": a["point"], "dimension": a["dimension"], "ref": a["ref"], "basis": a["basis"],
                                                    "source": "e2e", "terminal": a["terminal"], "scenario": a["scenario"]})
    summary = {"scenarios": r["scenarios"], "points": len(r["additions"]), "model_name": r["model_name"]}
    return e2e_reference_block(r["scenarios"]), [{"module": m, "points": pts} for m, pts in modules.items()], summary


async def _analyze_and_extend(
    llm: LLMClient, requirement: str, model: str | None, extend: bool | None, knowledge_cases: str | None = None, **kw,
) -> dict:
    """拆解明文测试点，再按资深测试经验补需求没写到的场景（经验延伸，`point_extension` 开关 / 参数 extend 控制）。

    延伸是独立的第二次调用：明文拆解只认需求原文、宁多勿漏，延伸只补需求之外、条数受限、每条带依据，两边职责分开
    模型才不会把「需求里有」和「经验上该测」混成一团。延伸点以 source=derived 追加进对应模块；延伸故障只丢延伸，
    明文测试点照常返回。analysis["trace"] 记两步各自的模型。
    """
    analysis = await analyze_requirement(llm, requirement, model, knowledge_cases=knowledge_cases, **kw)
    trace = [{"agent": "需求分析", "model": analysis["model_name"], **({"empty": True} if analysis.get("empty") else {})}]
    if _extend_enabled(extend) and analysis["test_points"]:
        try:
            ext = await extend_points(llm, requirement, analysis["test_points"], model,
                                      knowledge_cases=knowledge_cases, req_title=kw.get("req_title"))
            added = apply_extensions(analysis["test_points"], ext["additions"])
            analysis["extension"] = {"added": added, "skipped": ext["skipped"], "model_name": ext["model_name"]}
            trace.append({"agent": "经验延伸", "model": ext["model_name"], "added": added, "skipped": len(ext["skipped"])})
        except Exception as e:  # 延伸是锦上添花：故障不影响明文测试点
            logger.warning("经验延伸故障，跳过（明文测试点不受影响）：{}", e)
            analysis["extension"] = {"error": str(e)}
            trace.append({"agent": "经验延伸", "error": str(e)})
    analysis["trace"] = trace
    return analysis


async def run_analysis(
    requirement: str,
    llm: LLMClient,
    model: str | None = None,
    chunk_max_chars: int | None = None,
    knowledge_cases: str | None = None,
    extend: bool | None = None,
    e2e: bool | None = None,
    section_terminals: dict[str, str] | None = None,
) -> AnalysisResult:
    """仅执行需求分析（拆解确认流程 F-3-3 第一阶段）；超长需求分片并行拆解后合并。

    knowledge_cases：历史用例知识（知识管家在拆解阶段注入，覆盖度查漏）。
    extend：拆解后是否再做经验延伸（None 取配置 point_extension）；分片模式各片拆完各自延伸。
    e2e：拆解前是否先理端到端场景（None 取配置 point_e2e）：跨端主线注入各片拆解作参考，场景点以「端到端」模块追加；
    section_terminals：整理稿章节 → 端（需求提取时判定），给端到端场景当提示。
    """
    chunk_max_chars = chunk_max_chars or get_settings().chunk_max_chars
    requirement = clean_manual_sections(requirement)   # 「人工收录」小节去样例留规则、并入所在页面（模型会整节跳过）
    chunks, titles = requirement_chunks(requirement, chunk_max_chars)
    e2e_ref, e2e_modules, e2e_summary = await _e2e_step(llm, requirement, model, e2e, section_terminals)
    e2e_trace = ([{"agent": "端到端场景", **({"error": e2e_summary["error"]} if e2e_summary.get("error") else
                                           {"model": e2e_summary.get("model_name"), "scenarios": len(e2e_summary.get("scenarios") or []),
                                            "points": e2e_summary.get("points", 0)})}] if e2e_summary else [])
    if len(chunks) == 1:
        a = await _analyze_and_extend(llm, requirement, model, extend, knowledge_cases=knowledge_cases, e2e_ref=e2e_ref or None)
        return AnalysisResult(test_points=a["test_points"] + e2e_modules, blind_spots=a["blind_spots"],
                              blind_spot_refs=a.get("blind_spot_refs") or {},
                              trace=e2e_trace + a["trace"], e2e=e2e_summary)
    outline = await _outline_for(llm, requirement, chunks, model)
    analyses = await gather_or_cancel(
        [_analyze_and_extend(llm, chunk, model, extend, knowledge_cases=knowledge_cases, outline=outline,
                             chunk_no=i, chunk_total=len(chunks), allow_empty=len(chunks) > 1, req_title=title, e2e_ref=e2e_ref or None)
         for i, (chunk, title) in enumerate(zip(chunks, titles), 1)]
    )
    if all(not a["test_points"] for a in analyses):
        from app.agents.json_utils import LLMOutputError

        raise LLMOutputError(f"需求拆解未输出任何测试点（{len(chunks)} 个分片均为空，请检查需求内容是否可读）")
    module_points: dict[str, list[str]] = {}
    blind_spots: list[str] = []
    blind_spot_refs: dict[str, dict] = {}
    trace: list[dict] = []
    for i, a in enumerate(analyses, 1):
        for tp in a["test_points"]:
            module_points.setdefault(str(tp.get("module", "")), []).extend(tp.get("points", []))
        blind_spots.extend(b for b in a["blind_spots"] if b not in blind_spots)
        blind_spot_refs.update(a.get("blind_spot_refs") or {})
        trace.extend({"chunk": i, **t} for t in a["trace"])
    blind_spots, resolved, extra = await _verify_chunk_blind_spots(llm, chunks, blind_spots, model)
    from app.tasks.points import dedupe_module_points

    test_points = [{"module": m, "points": pts} for m, pts in module_points.items()]
    removed = dedupe_module_points(test_points)
    if removed:
        logger.info("分片合并后同模块同文测试点去重 {} 条", removed)
    return AnalysisResult(
        test_points=test_points + e2e_modules,
        blind_spots=blind_spots,
        blind_spot_refs=blind_spot_refs,
        resolved_blind_spots=resolved,
        trace=e2e_trace + trace + extra,
        e2e=e2e_summary,
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
    memory_notes: str | None = None,
    rule_notes: str | None = None,
    on_analyzed=None,
    checkpoint: dict | None = None,
    on_module_done=None,
    on_analysis=None,
    extend: bool | None = None,
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
    extend：直接生成模式下拆解后是否做经验延伸（None 取配置 point_extension）；已确认测试点的路径不再延伸。
    """
    kw = {"knowledge_refs": knowledge_refs, "knowledge_cases": knowledge_cases,
          "memory_notes": memory_notes, "rule_notes": rule_notes}
    ck = {"checkpoint": checkpoint, "on_module_done": on_module_done}
    ag = {"extend": extend}
    if test_points:
        return await _run_from_points(
            requirement, llm, model, reviewer_model, template, test_points, **kw, **ck
        )

    chunk_max_chars = chunk_max_chars or get_settings().chunk_max_chars
    requirement = clean_manual_sections(requirement)
    chunks, titles = requirement_chunks(requirement, chunk_max_chars)
    if len(chunks) == 1:
        return await _analyze_then_generate(
            requirement, llm, model, reviewer_model, template, on_analyzed=on_analyzed,
            on_analysis=on_analysis, **kw, **ck, **ag
        )

    logger.info("需求 {} 字超过分片阈值，切分为 {} 片并行处理", len(requirement), len(chunks))
    outline = await _outline_for(llm, requirement, chunks, model)
    outcomes = await asyncio.gather(
        *[
            _analyze_then_generate(chunk, llm, model, reviewer_model, template, on_analyzed=on_analyzed,
                                   outline=outline, chunk_no=i, chunk_total=len(chunks), req_title=title, **kw, **ag,
                                   **_chunk_checkpoint(checkpoint, on_analysis, on_module_done, i))
            for i, (chunk, title) in enumerate(zip(chunks, titles), 1)
        ],
        return_exceptions=True,
    )
    merged = _merge(outcomes)
    if not merged.cases and not merged.unresolved:
        from app.agents.json_utils import LLMOutputError

        raise LLMOutputError(f"需求拆解未输出任何测试点（{len(chunks)} 个分片均为空，请检查需求内容是否可读）")
    merged.blind_spots, merged.resolved_blind_spots, extra = await _verify_chunk_blind_spots(
        llm, chunks, merged.blind_spots, model)
    merged.trace.extend(extra)
    return merged


def _chunk_checkpoint(checkpoint: dict | None, on_analysis, on_module_done, chunk_no: int) -> dict:
    """分片直接生成的断点续跑：检查点按片隔离——拆解结果存在 chunks[片号]，模块结果键名加「片号:」前缀，
    避免不同片里同名模块互相冒充；回调同样带上片号。"""
    prefix = f"{chunk_no}:"
    ck = checkpoint or {}
    mods = {k[len(prefix):]: v for k, v in (ck.get("modules") or {}).items() if k.startswith(prefix)}
    sub = {"analysis": (ck.get("chunks") or {}).get(str(chunk_no)), "modules": mods} if ck else None

    def _on_analysis(analysis: dict) -> None:
        on_analysis({**analysis, "chunk_no": chunk_no})

    def _on_module_done(module: str, result) -> None:
        on_module_done(prefix + module, result)

    return {"checkpoint": sub, "on_analysis": _on_analysis if on_analysis else None,
            "on_module_done": _on_module_done if on_module_done else None}


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
    outline: list[str] | None = None,
    chunk_no: int = 0,
    chunk_total: int = 0,
    req_title: str | None = None,
    extend: bool | None = None,
) -> GenerationResult:
    """先拆解，再按模块并行生成（PRD 4.1a 生成 Agent 多实例）。

    单次生成调用只输出一个模块的用例，避免大需求下输出超过模型 max_tokens 被截断
    （多数模型单次输出上限 8K～16K，全模块一次性输出必然超限）。
    检查点里已有拆解结果时跳过拆解（断点续跑）。
    """
    saved = (checkpoint or {}).get("analysis")
    if saved and saved.get("test_points"):
        analysis = {"test_points": saved["test_points"], "blind_spots": saved.get("blind_spots", []),
                    "blind_spot_refs": saved.get("blind_spot_refs") or {}, "model_name": saved.get("model_name", "")}
        logger.info("断点续跑：复用已保存的拆解结果（{} 个模块）", len(analysis["test_points"]))
    else:
        if chunk_total > 1:
            analysis = await _analyze_and_extend(llm, requirement, model, extend, knowledge_cases=knowledge_cases,
                                                 outline=outline, chunk_no=chunk_no, chunk_total=chunk_total,
                                                 allow_empty=True, req_title=req_title)
        else:
            analysis = await _analyze_and_extend(llm, requirement, model, extend, knowledge_cases=knowledge_cases)
        if not analysis["test_points"]:  # 分片不含可测内容：本片无产出，不进生成
            return GenerationResult(cases=[], passed=True, review_rounds=0, blind_spots=analysis["blind_spots"],
                                    trace=[{"agent": "需求分析", "model": analysis["model_name"], "empty": True}])
        if on_analysis:
            on_analysis({"test_points": analysis["test_points"], "blind_spots": analysis["blind_spots"],
                         "blind_spot_refs": analysis.get("blind_spot_refs") or {}, "model_name": analysis.get("model_name", "")})
    if on_analyzed:
        on_analyzed()
    result = await _run_from_points(
        requirement, llm, model, reviewer_model, template, analysis["test_points"],
        knowledge_refs=knowledge_refs, knowledge_cases=knowledge_cases,
        memory_notes=memory_notes, rule_notes=rule_notes, checkpoint=checkpoint, on_module_done=on_module_done,
    )
    for spot in reversed(analysis["blind_spots"]):  # 拆解盲区放最前，且保持拆解时的顺序
        if spot not in result.blind_spots:
            result.blind_spots.insert(0, spot)
    result.blind_spot_refs = {**(analysis.get("blind_spot_refs") or {}), **result.blind_spot_refs}
    result.trace = (analysis.get("trace") or [{"agent": "需求分析", "model": analysis["model_name"]}]) + result.trace
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
    texts = _module_requirements(requirement, test_points)

    async def _module(tp: dict) -> GenerationResult:
        name = str(tp.get("module", ""))
        if name in done:
            logger.info("断点续跑：模块「{}」复用已生成结果", name)
            return GenerationResult.model_validate(done[name])
        text = texts.get(name) or requirement
        result = await _run_single(text, llm, model, reviewer_model, template, test_points=[tp],
                                   requirement_scoped=len(text) < len(requirement), **kw)
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


def _module_requirements(requirement: str, test_points: list[dict]) -> dict[str, str]:
    """每个模块生成 / 评审时带的需求原文：配置 generation_module_scope 开着就只带该模块所在章节（与查漏同一套 `_module_texts`
    定位：整篇不足一批、模块名与 ref 都对不上、跨章节的仍是整篇），关着或只有一个模块、定位出错都返回空（调用方用整篇）。"""
    if not get_settings().generation_module_scope or len(test_points) <= 1:
        return {}
    try:
        from app.agents.quality import _module_texts

        texts = _module_texts(requirement, test_points)
    except Exception as e:   # 定位失败不影响生成
        logger.warning("按模块定位需求章节失败，生成带整份需求：{}", e)
        return {}
    scoped = [n for n, t in texts.items() if len(t) < len(requirement)]
    if scoped:
        avg = sum(len(texts[n]) for n in scoped) // len(scoped)
        logger.info("生成 / 评审按模块只带所在章节：{}/{} 个模块定位到章节（平均 {} 字，整篇 {} 字），其余带整篇",
                    len(scoped), len(test_points), avg, len(requirement))
    else:
        logger.info("生成 / 评审带整篇需求（{} 字）：整篇不足一批或模块定位不到章节", len(requirement))
    return texts


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
    requirement_scoped: bool = False,
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
    if requirement_scoped:
        initial["requirement_scope"] = "module"   # 只带本模块所在章节：Prompt 里注明，模型不把别处规则当遗漏
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
        merged.blind_spot_refs.update(outcome.blind_spot_refs)
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


from app.agents.analysis import run_requirement_analysis  # noqa: E402  需求中心分析流水线（原文核验 + 独立复核）

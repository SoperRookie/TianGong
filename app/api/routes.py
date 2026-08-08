"""API 路由。

POST /api/v1/tasks：上传需求（文件/文本）→ 解析 → 三角色编排生成 → 导出 Excel/CSV。
M1 为同步执行；M4 接入 Celery 异步队列与任务进度。
"""

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from loguru import logger

from pydantic import BaseModel

from app.agents import run_analysis, run_generation
from app.agents.json_utils import LLMOutputError
from app.config import get_settings
from app.exporters import export_csv, export_excel, export_xmind
from app.llm.client import AllModelsFailedError
from app.llm.registry import NoVisionModelError, UnknownModelError
from app.llm.schemas import MissingAPIKeyError
from app.parsers import (
    IMAGE_SUFFIXES,
    ScannedPDFError,
    UnsupportedFormatError,
    enrich_images,
    parse_file,
    parse_image,
    parse_text,
)
from app.tasks import TaskRecord
from app.templates import CustomTemplate, TemplateParseError, recognize_template

router = APIRouter()


def _knowledge_service(app):
    """知识库服务惰性初始化：首次访问时构建并缓存到 app.state（避免无关链路加载向量库）。"""
    if getattr(app.state, "knowledge", None) is None:
        from app.knowledge import KnowledgeService, KnowledgeStore
        from app.llm.embeddings import EmbeddingClient, EmbeddingRegistry

        settings = get_settings()
        registry = EmbeddingRegistry.from_yaml(settings.models_config_path)
        embedder = EmbeddingClient(registry)
        store = KnowledgeStore(
            settings.knowledge_dir, dimensions=registry.get().dimensions
        )
        app.state.knowledge = KnowledgeService(
            store, embedder, chunk_max_chars=settings.knowledge_chunk_chars
        )
    return app.state.knowledge


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/api/v1/models")
async def list_models(request: Request) -> dict:
    """模型清单（不含密钥信息），供前端任务创建时选择（F-1-4）。"""
    registry = request.app.state.registry
    return {"default_model": registry.default_model, "models": registry.list_public()}


async def _read_upload(upload: UploadFile, task_dir: Path, max_bytes: int) -> Path:
    """文件限制校验（F-2-8）：大小上限 + 落盘供解析。格式白名单由 parse_file 校验。"""
    content = await upload.read()
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"文件 {upload.filename} 超过大小上限 {max_bytes // (1024 * 1024)}MB",
        )
    dest = task_dir / "uploads" / Path(upload.filename or "unnamed").name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    return dest


async def _parse_inputs(
    files: list[UploadFile], text: str, save_dir: Path, max_bytes: int, llm
) -> list:
    """解析多文件 + 粘贴文本（F-2-5 混合上传），返回 ParsedDocument 列表。

    图片文件走 Vision 模型多模态理解（F-2-3），其余格式走本地解析器。
    """
    docs = []
    try:
        for upload in files:
            saved = await _read_upload(upload, save_dir, max_bytes)
            if saved.suffix.lower() in IMAGE_SUFFIXES:
                docs.append(await parse_image(saved, llm))
            else:
                # 图文混排：文档内嵌图片经 Vision 理解后回填原位置
                docs.append(await enrich_images(parse_file(saved), llm))
        if text.strip():
            docs.append(parse_text(text))
    except (UnsupportedFormatError, ScannedPDFError, NoVisionModelError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not docs:
        raise HTTPException(status_code=400, detail="请上传需求文件或粘贴需求文本")
    return docs


def _merge_docs(docs: list) -> str:
    parts = []
    for doc in docs:
        if doc.source == "text":
            parts.append(doc.full_text)
        else:
            parts.append(f"【文件：{doc.source}】\n{doc.full_text}")
    return "\n\n".join(parts)


@router.post("/api/v1/parse")
async def preview_parse(
    request: Request,
    files: list[UploadFile] = File(default=[]),
    text: str = Form(default=""),
) -> dict:
    """解析结果预览（F-2-7）：返回结构化解析结果供确认修正，修正后以 text 提交创建任务。"""
    from app.parsers.chunking import split_text

    settings = get_settings()
    preview_dir = request.app.state.tasks.output_dir / "_previews"
    docs = await _parse_inputs(
        files, text, preview_dir, settings.max_upload_size_mb * 1024 * 1024, request.app.state.llm
    )
    merged = _merge_docs(docs)
    return {
        "documents": [
            {
                "source": doc.source,
                "doc_type": doc.doc_type,
                "sections": [s.model_dump() for s in doc.sections],
                "tables": doc.tables,
                "full_text": doc.full_text,
            }
            for doc in docs
        ],
        "merged_text": merged,
        "total_chars": len(merged),
        "estimated_chunks": len(split_text(merged, settings.chunk_max_chars)),
    }


@router.post("/api/v1/templates")
async def upload_template(
    request: Request,
    file: UploadFile = File(...),
    name: str | None = Form(default=None),
) -> dict:
    """上传模板并自动识别字段结构（F-4-1）；返回识别草稿供确认调整（F-4-3）。"""
    settings = get_settings()
    content = await file.read()
    if len(content) > settings.max_upload_size_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail="模板文件超过大小上限")
    tmp = request.app.state.tasks.output_dir / "_template_uploads" / Path(file.filename or "t").name
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(content)
    try:
        template = recognize_template(tmp, name=name)
    except TemplateParseError as e:
        raise HTTPException(status_code=400, detail=str(e))
    request.app.state.templates.save(template)
    return template.model_dump()


@router.get("/api/v1/templates")
async def list_templates(request: Request) -> dict:
    store = request.app.state.templates
    return {"default_id": store.default_id, "templates": store.list()}


@router.get("/api/v1/templates/{template_id}")
async def get_template(request: Request, template_id: str) -> dict:
    template = request.app.state.templates.get(template_id)
    if template is None:
        raise HTTPException(status_code=404, detail=f"模板不存在: {template_id}")
    return template.model_dump()


@router.put("/api/v1/templates/{template_id}")
async def update_template(request: Request, template_id: str, body: CustomTemplate) -> dict:
    """字段映射确认与调整（F-4-3）：整体覆盖模板定义。"""
    store = request.app.state.templates
    if store.get(template_id) is None:
        raise HTTPException(status_code=404, detail=f"模板不存在: {template_id}")
    body.template_id = template_id
    return store.save(body).model_dump()


@router.delete("/api/v1/templates/{template_id}")
async def delete_template(request: Request, template_id: str) -> dict:
    try:
        existed = request.app.state.templates.delete(template_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not existed:
        raise HTTPException(status_code=404, detail=f"模板不存在: {template_id}")
    return {"deleted": template_id}


@router.post("/api/v1/templates/{template_id}/default")
async def set_default_template(request: Request, template_id: str) -> dict:
    try:
        request.app.state.templates.set_default(template_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e.args[0]))
    return {"default_id": template_id}


async def _gather_knowledge(
    app, requirement: str, stages: tuple[str, ...], space: str | None
) -> tuple[dict, list[dict]]:
    """知识管家编排（F-7-6）：按阶段检索三大知识库并按 5:3:2 配额装填。

    知识链路任何故障均降级为无知识注入，不阻塞生成主链路（容错优先）。
    返回 ({"cases": 历史用例文本, "refs": 需求/规则文本}, 知识快照)。
    """
    from app.knowledge.steward import KnowledgeSteward

    out: dict = {"cases": None, "refs": None}
    snapshot: list[dict] = []
    try:
        service = _knowledge_service(app)
        if not service.store.list_docs():
            return out, snapshot
        steward = KnowledgeSteward(service, budget_chars=get_settings().knowledge_budget_chars)
        query = requirement[:1500]
        if "analysis" in stages:
            bundle = await steward.for_analysis(query, space=space)
            if not bundle.empty:
                out["cases"] = bundle.render()
                snapshot.extend(bundle.snapshot)
        if "generation" in stages:
            bundle = await steward.for_generation(query, space=space)
            if not bundle.empty:
                out["refs"] = bundle.render()
                snapshot.extend(bundle.snapshot)
        if snapshot:
            logger.info("知识注入：{} 个切片，共 {} 字", len(snapshot), sum(x["chars"] for x in snapshot))
    except Exception as e:
        logger.warning("知识检索故障，降级为无知识注入：{}", e)
        return {"cases": None, "refs": None}, snapshot
    return out, snapshot


def _gather_memories(app, requirement: str, project: str | None) -> tuple[str | None, list[dict]]:
    """记忆检索注入（F-8-7）：用户偏好 + 当前项目记忆，独立预算，故障降级不阻塞主链路。"""
    try:
        notes, snapshot = app.state.memory.retrieve(
            requirement[:1500], project=project, budget_chars=get_settings().memory_budget_chars
        )
        if snapshot:
            logger.info("记忆注入：{} 条（项目={}）", len(snapshot), project or "-")
        return notes, snapshot
    except Exception as e:
        logger.warning("记忆检索故障，降级为无记忆注入：{}", e)
        return None, []


# ---- 长期记忆（F-8-2/3/6）----


class MemoryBody(BaseModel):
    content: str
    scope: str = "user"
    project: str | None = None


class MemoryUpdateBody(BaseModel):
    content: str


@router.get("/api/v1/memories")
async def list_memories(
    request: Request, scope: str | None = None, project: str | None = None
) -> dict:
    """记忆可见（F-8-6）：列出全部记忆；defaults 为使用习惯沉淀的默认模板/模型（F-8-2）。"""
    store = request.app.state.memory
    return {
        "memories": [e.model_dump() for e in store.list(scope, project)],
        "defaults": store.defaults(),
    }


@router.post("/api/v1/memories")
async def create_memory(request: Request, body: MemoryBody) -> dict:
    """手工维护记忆：用户偏好（scope=user）或项目记忆（scope=project + 项目名）。"""
    try:
        entry = request.app.state.memory.add(body.content, scope=body.scope, project=body.project)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return entry.model_dump()


@router.put("/api/v1/memories/{memory_id}")
async def update_memory(request: Request, memory_id: str, body: MemoryUpdateBody) -> dict:
    """记忆纠错（F-8-6）：直接改写记忆内容。"""
    try:
        entry = request.app.state.memory.update(memory_id, body.content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if entry is None:
        raise HTTPException(status_code=404, detail=f"记忆不存在: {memory_id}")
    return entry.model_dump()


@router.delete("/api/v1/memories/{memory_id}")
async def delete_memory(request: Request, memory_id: str) -> dict:
    if not request.app.state.memory.delete(memory_id):
        raise HTTPException(status_code=404, detail=f"记忆不存在: {memory_id}")
    return {"deleted": memory_id}


@router.delete("/api/v1/memories")
async def clear_memories(
    request: Request, scope: str | None = None, project: str | None = None
) -> dict:
    """一键清空（F-8-6），可按维度/项目过滤。"""
    return {"cleared": request.app.state.memory.clear(scope, project)}


# ---- 知识库（F-7-1/3/4）----


@router.get("/api/v1/knowledge/categories")
async def list_categories() -> dict:
    """三大知识分类枚举，供上传时选择。"""
    from app.knowledge import CATEGORIES

    return {"categories": CATEGORIES}


@router.post("/api/v1/knowledge/docs")
async def ingest_knowledge(
    request: Request,
    category: str = Form(...),
    space: str = Form(default="default"),
    files: list[UploadFile] = File(default=[]),
    text: str = Form(default=""),
    source: str = Form(default="text"),
) -> dict:
    """知识文档入库：解析 → 切片 → 向量化 → 入库；支持文件与粘贴文本。"""
    import openai as _openai

    from app.knowledge import InvalidCategoryError

    settings = get_settings()
    service = _knowledge_service(request.app)
    save_dir = request.app.state.tasks.output_dir / "_knowledge_uploads"
    docs = []
    try:
        for upload in files:
            saved = await _read_upload(upload, save_dir, settings.max_upload_size_mb * 1024 * 1024)
            docs.append(await service.ingest_file(saved, category=category, space=space))
        if text.strip():
            docs.append(await service.ingest_text(text, source=source, category=category, space=space))
    except (InvalidCategoryError, UnsupportedFormatError, ScannedPDFError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except (MissingAPIKeyError, _openai.APIConnectionError, _openai.APIStatusError) as e:
        raise HTTPException(status_code=502, detail=f"Embedding 服务调用失败: {e}")
    if not docs:
        raise HTTPException(status_code=400, detail="请上传知识文件或提供文本内容")
    return {"ingested": [d.model_dump() for d in docs]}


@router.post("/api/v1/knowledge/cases")
async def ingest_history_cases(
    request: Request,
    files: list[UploadFile] = File(...),
    space: str = Form(default="default"),
) -> dict:
    """历史用例入库（F-7-2）：Excel/CSV/XMind 存量用例导入测试用例库。"""
    import openai as _openai

    from app.knowledge.importers import CaseImportError

    settings = get_settings()
    service = _knowledge_service(request.app)
    save_dir = request.app.state.tasks.output_dir / "_knowledge_uploads"
    docs = []
    try:
        for upload in files:
            saved = await _read_upload(upload, save_dir, settings.max_upload_size_mb * 1024 * 1024)
            docs.append(await service.ingest_cases(saved, space=space))
    except CaseImportError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except (MissingAPIKeyError, _openai.APIConnectionError, _openai.APIStatusError) as e:
        raise HTTPException(status_code=502, detail=f"Embedding 服务调用失败: {e}")
    return {"ingested": [d.model_dump() for d in docs]}


@router.get("/api/v1/knowledge/docs")
async def list_knowledge_docs(
    request: Request, space: str | None = None, category: str | None = None
) -> dict:
    service = _knowledge_service(request.app)
    return {"documents": [d.model_dump() for d in service.store.list_docs(space, category)]}


@router.delete("/api/v1/knowledge/docs/{doc_id}")
async def delete_knowledge_doc(request: Request, doc_id: str) -> dict:
    service = _knowledge_service(request.app)
    if not service.store.delete_doc(doc_id):
        raise HTTPException(status_code=404, detail=f"知识文档不存在: {doc_id}")
    return {"deleted": doc_id}


class KnowledgeSearchBody(BaseModel):
    query: str
    top_k: int = 5
    category: str | None = None
    space: str | None = None


@router.post("/api/v1/knowledge/search")
async def search_knowledge(request: Request, body: KnowledgeSearchBody) -> dict:
    """语义检索：按分类/知识空间过滤，返回相似度排序的切片。"""
    import openai as _openai

    from app.knowledge import InvalidCategoryError

    service = _knowledge_service(request.app)
    try:
        hits = await service.search(
            body.query, top_k=body.top_k, category=body.category, space=body.space
        )
    except InvalidCategoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except (MissingAPIKeyError, _openai.APIConnectionError, _openai.APIStatusError) as e:
        raise HTTPException(status_code=502, detail=f"Embedding 服务调用失败: {e}")
    return {"hits": [h.model_dump() for h in hits]}


@router.post("/api/v1/tasks")
async def create_task(
    request: Request,
    files: list[UploadFile] = File(default=[]),
    text: str = Form(default=""),
    model: str | None = Form(default=None),
    reviewer_model: str | None = Form(default=None),
    template_id: str | None = Form(default=None),
    confirm_points: bool = Form(default=False),
    knowledge_space: str | None = Form(default=None),
    project: str | None = Form(default=None),
    async_mode: bool = Form(default=False),
) -> dict:
    settings = get_settings()
    store = request.app.state.tasks
    template = request.app.state.templates.get(template_id)
    if template is None:
        raise HTTPException(status_code=404, detail=f"模板不存在: {template_id}")
    task_id, task_dir = store.new_task_dir()

    # 1. 解析输入（文件 + 粘贴文本可混合 F-2-5；图片走 Vision F-2-3）
    docs = await _parse_inputs(
        files, text, task_dir, settings.max_upload_size_mb * 1024 * 1024, request.app.state.llm
    )
    sources = [doc.source for doc in docs]
    requirement = _merge_docs(docs)
    logger.info(
        "任务 {} 创建：来源={} 共 {} 字（项目={} 模板={} 确认拆解={} 异步={}）",
        task_id, sources, len(requirement), project or "-", template.template_id, confirm_points, async_mode,
    )

    # 1.5 拆解确认流程（F-3-3）：只做需求分析，等待用户确认测试点
    if confirm_points:
        # 拆解阶段注入历史用例做覆盖度查漏（知识管家 F-7-6，检索时机约束）
        knowledge, snapshot = await _gather_knowledge(
            request.app, requirement, ("analysis",), knowledge_space
        )
        try:
            analysis = await run_analysis(
                requirement,
                llm=request.app.state.llm,
                model=model,
                knowledge_cases=knowledge["cases"],
            )
        except (UnknownModelError,) as e:
            raise HTTPException(status_code=400, detail=str(e))
        except (MissingAPIKeyError, AllModelsFailedError) as e:
            raise HTTPException(status_code=502, detail=str(e))
        record = TaskRecord(
            task_id=task_id,
            status="awaiting_confirmation",
            sources=sources,
            analysis=analysis.model_dump(),
            knowledge=snapshot,
            context={
                "requirement": requirement,
                "model": model,
                "reviewer_model": reviewer_model,
                "template_id": template.template_id,
                "knowledge_space": knowledge_space,
                "project": project,
            },
        )
        store.save(record)
        return {
            "task_id": task_id,
            "status": record.status,
            "test_points": analysis.test_points,
            "blind_spots": analysis.blind_spots,
            "confirm_url": f"/api/v1/tasks/{task_id}/confirm",
        }

    # 2. 编排生成（超长需求自动分片并行，F-2-6）；知识管家按时机注入（F-7-6）
    task_context = {
        "requirement": requirement,
        "model": model,
        "reviewer_model": reviewer_model,
        "template_id": template.template_id,
        "knowledge_space": knowledge_space,
        "project": project,
    }
    # 使用习惯沉淀（F-8-2）：常用模板/模型达到阈值后固化为默认偏好
    memory_store = request.app.state.memory
    memory_store.record_usage("template", template.template_id)
    if model:
        memory_store.record_usage("model", model)

    async def _generate() -> dict:
        knowledge, snapshot = await _gather_knowledge(
            request.app, requirement, ("analysis", "generation"), knowledge_space
        )
        # 记忆检索注入（F-8-7）：独立预算，不占知识库配额
        memory_notes, memory_snapshot = _gather_memories(request.app, requirement, project)
        store.set_progress(task_id, progress="analyzing")
        result = await run_generation(
            requirement,
            llm=request.app.state.llm,
            model=model,
            reviewer_model=reviewer_model,
            template=template,
            knowledge_refs=knowledge["refs"],
            knowledge_cases=knowledge["cases"],
            memory_notes=memory_notes,
            on_analyzed=lambda: store.set_progress(task_id, progress="generating_reviewing"),
        )
        store.set_progress(task_id, progress="exporting")
        return _finalize_task(
            store, task_id, task_dir, sources, result, template,
            knowledge=snapshot, context=task_context, memories=memory_snapshot,
        )

    # 异步模式（F-6-1/2）：立即返回 task_id，后台执行，GET /tasks/{id} 轮询进度
    if async_mode:
        store.save(TaskRecord(task_id=task_id, status="queued", sources=sources, context=task_context))
        store.submit(task_id, _generate)
        return {"task_id": task_id, "status": "queued", "poll_url": f"/api/v1/tasks/{task_id}"}

    try:
        return await _generate()
    except UnknownModelError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except (MissingAPIKeyError, LLMOutputError) as e:
        raise HTTPException(status_code=502, detail=str(e))
    except AllModelsFailedError as e:
        record = TaskRecord(task_id=task_id, status="failed", sources=sources, error=str(e))
        store.save(record)
        raise HTTPException(status_code=502, detail=str(e))


def _finalize_task(
    store, task_id: str, task_dir: Path, sources: list[str], result, template,
    knowledge: list[dict] | None = None, context: dict | None = None,
    memories: list[dict] | None = None,
) -> dict:
    """导出多格式产物（F-5-1/2/3/4）并落库，返回任务响应。

    复用既有记录（异步任务/修订任务），保留 created_at 与修订历史等留痕。
    """
    files_map: dict[str, str] = {}
    if result.cases:
        files_map["xlsx"] = str(export_excel(result.cases, task_dir / "测试用例.xlsx", template))
        files_map["csv"] = str(export_csv(result.cases, task_dir / "测试用例.csv", template))
        root_title = Path(sources[0]).stem if sources and sources[0] != "text" else "测试用例"
        files_map["xmind"] = str(
            export_xmind(result.cases, task_dir / "测试用例.xmind", root_title=root_title)
        )

    logger.info("任务 {} 导出完成：{} 条用例，格式={}", task_id, len(result.cases), list(files_map))
    record = store.get(task_id) or TaskRecord(task_id=task_id)
    record.status = "completed"
    record.progress = None
    record.error = None
    record.sources = sources
    record.result = result.model_dump()
    record.files = files_map
    if knowledge is not None:
        record.knowledge = knowledge
    if context is not None:
        record.context = context
    if memories is not None:
        record.memories = memories
    store.save(record)
    return {
        "task_id": task_id,
        "status": record.status,
        "case_count": len(result.cases),
        "passed": result.passed,
        "review_rounds": result.review_rounds,
        "chunks": result.chunks,
        "unresolved": result.unresolved,
        "blind_spots": result.blind_spots,
        "downloads": {fmt: f"/api/v1/tasks/{task_id}/files/{fmt}" for fmt in files_map},
    }


class ConfirmBody(BaseModel):
    test_points: list[dict] | None = None  # 缺省沿用拆解草稿；传入则以用户修改后的为准


@router.post("/api/v1/tasks/{task_id}/confirm")
async def confirm_task(request: Request, task_id: str, body: ConfirmBody | None = None) -> dict:
    """确认（或修改后确认）测试点，继续生成（F-3-3 第二阶段）。多模块按模块并行生成。"""
    store = request.app.state.tasks
    record = store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if record.status != "awaiting_confirmation":
        raise HTTPException(status_code=409, detail=f"任务状态为 {record.status}，无待确认的拆解结果")

    ctx = record.context or {}
    test_points = (body.test_points if body and body.test_points else None) or (
        record.analysis or {}
    ).get("test_points", [])
    if not test_points:
        raise HTTPException(status_code=400, detail="测试点为空，无法生成")
    template = request.app.state.templates.get(ctx.get("template_id"))

    # 生成前注入需求/规则库；历史用例注入评审 Agent（知识管家 F-7-6）
    knowledge, snapshot = await _gather_knowledge(
        request.app, ctx.get("requirement", ""), ("analysis", "generation"), ctx.get("knowledge_space")
    )
    memory_notes, memory_snapshot = _gather_memories(
        request.app, ctx.get("requirement", ""), ctx.get("project")
    )
    try:
        result = await run_generation(
            ctx.get("requirement", ""),
            llm=request.app.state.llm,
            model=ctx.get("model"),
            reviewer_model=ctx.get("reviewer_model"),
            template=template,
            test_points=test_points,
            knowledge_refs=knowledge["refs"],
            knowledge_cases=knowledge["cases"],
            memory_notes=memory_notes,
        )
    except (MissingAPIKeyError, LLMOutputError) as e:
        raise HTTPException(status_code=502, detail=str(e))
    except AllModelsFailedError as e:
        record.status = "failed"
        record.error = str(e)
        store.save(record)
        raise HTTPException(status_code=502, detail=str(e))

    task_dir = store.output_dir / task_id
    response = _finalize_task(
        store, task_id, task_dir, record.sources, result, template,
        knowledge=record.knowledge + snapshot,  # 拆解阶段 + 生成阶段的知识快照合并留痕
        memories=memory_snapshot,
    )
    # 保留拆解阶段留痕
    saved = store.get(task_id)
    saved.analysis = record.analysis
    store.save(saved)
    return response


class ReviseBody(BaseModel):
    instruction: str
    model: str | None = None           # 缺省沿用任务创建时的模型
    reviewer_model: str | None = None


@router.post("/api/v1/tasks/{task_id}/revise")
async def revise_task(request: Request, task_id: str, body: ReviseBody) -> dict:
    """多轮对话修订（F-3-5）：修订要求走「定点修正→评审」回环，增量更新并重导出。

    短期会话记忆（F-8-1）：本任务此前的修订指令随 Prompt 注入，保持多轮一致性。
    """
    from app.agents import run_revision

    store = request.app.state.tasks
    record = store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if record.status != "completed" or not (record.result or {}).get("cases"):
        raise HTTPException(
            status_code=409, detail=f"任务状态为 {record.status}，无可修订的用例结果"
        )
    if not body.instruction.strip():
        raise HTTPException(status_code=400, detail="修订要求不能为空")
    logger.info("任务 {} 发起修订（第 {} 轮）：{}", task_id, len(record.revisions) + 1, body.instruction)

    ctx = record.context or {}
    template = request.app.state.templates.get(ctx.get("template_id"))
    # 评审 Agent 仍可参考历史用例（知识管家时机约束）；知识故障降级不阻塞修订
    knowledge, snapshot = await _gather_knowledge(
        request.app, ctx.get("requirement", ""), ("analysis",), ctx.get("knowledge_space")
    )
    memory_notes, memory_snapshot = _gather_memories(
        request.app, ctx.get("requirement", ""), ctx.get("project")
    )
    # 使用习惯沉淀（F-8-2）：跨任务重复的修订指令固化为偏好，后续生成主动满足
    request.app.state.memory.record_usage("revision", body.instruction)
    try:
        result = await run_revision(
            ctx.get("requirement", ""),
            cases=record.result["cases"],
            instruction=body.instruction,
            llm=request.app.state.llm,
            model=body.model or ctx.get("model"),
            reviewer_model=body.reviewer_model or ctx.get("reviewer_model"),
            template=template,
            test_points=(record.result or {}).get("test_points") or None,
            history=[r["instruction"] for r in record.revisions],
            knowledge_cases=knowledge["cases"],
            memory_notes=memory_notes,
        )
    except UnknownModelError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except (MissingAPIKeyError, AllModelsFailedError, LLMOutputError) as e:
        raise HTTPException(status_code=502, detail=str(e))

    task_dir = store.output_dir / task_id
    response = _finalize_task(
        store, task_id, task_dir, record.sources, result, template,
        knowledge=record.knowledge + snapshot, memories=memory_snapshot,
    )
    saved = store.get(task_id)
    saved.analysis = record.analysis
    saved.revisions = record.revisions + [
        {
            "instruction": body.instruction,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "passed": result.passed,
            "review_rounds": result.review_rounds,
            "case_count": len(result.cases),
        }
    ]
    store.save(saved)
    response["revision_no"] = len(saved.revisions)
    return response


class ReviewItem(BaseModel):
    case_id: str
    action: str  # accept / modify / delete
    case: dict | None = None  # modify 时提交修改后的完整用例
    feedback: str = ""  # 一键反馈：问题类型或意见（学习语料）


class ReviewBody(BaseModel):
    items: list[ReviewItem]


@router.post("/api/v1/tasks/{task_id}/review")
async def review_task(request: Request, task_id: str, body: ReviewBody) -> dict:
    """在线评审留痕（F-6-6）：逐条采纳/修改/删除 + 一键反馈，按终稿重导出。

    留痕（含修改前后对照与反馈）是学习 Agent 归因与 Prompt 优化的核心语料。
    """
    from app.agents import GenerationResult
    from app.agents.service import _renumber
    from app.templates import TestCase

    store = request.app.state.tasks
    record = store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if record.status != "completed" or not (record.result or {}).get("cases"):
        raise HTTPException(status_code=409, detail=f"任务状态为 {record.status}，无可评审的用例结果")

    cases: list[dict] = list(record.result["cases"])
    by_id = {str(c.get("case_id")): c for c in cases}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    counts = {"accept": 0, "modify": 0, "delete": 0}
    for item in body.items:
        origin = by_id.get(item.case_id)
        if origin is None:
            raise HTTPException(status_code=400, detail=f"用例不存在: {item.case_id}")
        entry: dict = {"case_id": item.case_id, "action": item.action, "feedback": item.feedback, "at": now}
        if item.action == "accept":
            pass
        elif item.action == "delete":
            entry["before"] = origin
            cases.remove(origin)
        elif item.action == "modify":
            if not item.case:
                raise HTTPException(status_code=400, detail=f"修改操作需提交 case 字段: {item.case_id}")
            try:
                updated = TestCase.model_validate(item.case).model_dump()
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"修改后的用例不合法: {e}")
            entry["before"], entry["after"] = origin, updated
            cases[cases.index(origin)] = updated
            by_id[item.case_id] = updated
        else:
            raise HTTPException(status_code=400, detail=f"未知评审操作: {item.action}（可用 accept/modify/delete）")
        counts[item.action] += 1
        record.review_log.append(entry)

    logger.info("任务 {} 在线评审：采纳 {} / 修改 {} / 删除 {}", task_id, counts["accept"], counts["modify"], counts["delete"])
    # 删除后重排各模块编号，按终稿重导出
    result = GenerationResult.model_validate({**record.result, "cases": cases})
    _renumber(result.cases)
    task_dir = store.output_dir / task_id
    response = _finalize_task(store, task_id, task_dir, record.sources, result,
                              request.app.state.templates.get((record.context or {}).get("template_id")))
    response["review"] = {**counts, "log_entries": len(record.review_log)}
    return response


@router.post("/api/v1/tasks/{task_id}/final")
async def upload_final_cases(
    request: Request, task_id: str, file: UploadFile = File(...)
) -> dict:
    """离线评审终稿回传（F-6-8）：上传人工定稿文件，与生成结果做字段级 diff 留痕。

    与在线评审（F-6-6）构成评审闭环双通道，diff 是离线通道的学习语料入口。
    """
    from app.knowledge.importers import CaseImportError, parse_cases_file
    from app.tasks.diff import diff_cases

    settings = get_settings()
    store = request.app.state.tasks
    record = store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if not (record.result or {}).get("cases"):
        raise HTTPException(status_code=409, detail="任务无生成结果，无法对比终稿")

    saved = await _read_upload(
        file, store.output_dir / task_id / "final", settings.max_upload_size_mb * 1024 * 1024
    )
    try:
        final_cases = parse_cases_file(saved)
    except CaseImportError as e:
        raise HTTPException(status_code=400, detail=str(e))

    diff = diff_cases(record.result["cases"], final_cases)
    logger.info("任务 {} 终稿回传 diff：{}", task_id, diff["stats"])
    record.offline_review = {
        "filename": file.filename,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **diff,
    }
    store.save(record)
    return {"task_id": task_id, "stats": diff["stats"], "added": diff["added"],
            "deleted": diff["deleted"], "modified": diff["modified"]}


@router.get("/api/v1/tasks")
async def list_tasks(request: Request, status: str | None = None, limit: int = 50) -> dict:
    """任务列表（F-6-1）：倒序返回任务概要，供任务管理界面轮询。"""
    records = request.app.state.tasks.list(status=status, limit=limit)
    return {
        "tasks": [
            {
                "task_id": r.task_id,
                "status": r.status,
                "progress": r.progress,
                "created_at": r.created_at,
                "sources": r.sources,
                "case_count": len((r.result or {}).get("cases", [])),
                "revision_count": len(r.revisions),
                "error": r.error,
            }
            for r in records
        ]
    }


@router.get("/api/v1/tasks/{task_id}")
async def get_task(request: Request, task_id: str) -> dict:
    record = request.app.state.tasks.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return record.model_dump()


_MEDIA_TYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
    "xmind": "application/vnd.xmind.workbook",
}


@router.get("/api/v1/tasks/{task_id}/files/{fmt}")
async def download_file(request: Request, task_id: str, fmt: str) -> FileResponse:
    record = request.app.state.tasks.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    path = record.files.get(fmt)
    if path is None or not Path(path).exists():
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 无 {fmt} 产物")
    return FileResponse(path, media_type=_MEDIA_TYPES.get(fmt), filename=Path(path).name)

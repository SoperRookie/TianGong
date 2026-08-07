"""API 路由。

POST /api/v1/tasks：上传需求（文件/文本）→ 解析 → 三角色编排生成 → 导出 Excel/CSV。
M1 为同步执行；M4 接入 Celery 异步队列与任务进度。
"""

from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from pydantic import BaseModel

from app.agents import run_analysis, run_generation
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


def _knowledge_service(request: Request):
    """知识库服务惰性初始化：首次访问时构建并缓存到 app.state（避免无关链路加载向量库）。"""
    if getattr(request.app.state, "knowledge", None) is None:
        from app.knowledge import KnowledgeService, KnowledgeStore
        from app.llm.embeddings import EmbeddingClient, EmbeddingRegistry

        settings = get_settings()
        registry = EmbeddingRegistry.from_yaml(settings.models_config_path)
        embedder = EmbeddingClient(registry)
        store = KnowledgeStore(
            settings.knowledge_dir, dimensions=registry.get().dimensions
        )
        request.app.state.knowledge = KnowledgeService(
            store, embedder, chunk_max_chars=settings.knowledge_chunk_chars
        )
    return request.app.state.knowledge


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
    request: Request, requirement: str, stages: tuple[str, ...], space: str | None
) -> tuple[dict, list[dict]]:
    """知识管家编排（F-7-6）：按阶段检索三大知识库并按 5:3:2 配额装填。

    知识链路任何故障均降级为无知识注入，不阻塞生成主链路（容错优先）。
    返回 ({"cases": 历史用例文本, "refs": 需求/规则文本}, 知识快照)。
    """
    from app.knowledge.steward import KnowledgeSteward

    out: dict = {"cases": None, "refs": None}
    snapshot: list[dict] = []
    try:
        service = _knowledge_service(request)
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
    except Exception:
        return {"cases": None, "refs": None}, snapshot
    return out, snapshot


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
    service = _knowledge_service(request)
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
    service = _knowledge_service(request)
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
    service = _knowledge_service(request)
    return {"documents": [d.model_dump() for d in service.store.list_docs(space, category)]}


@router.delete("/api/v1/knowledge/docs/{doc_id}")
async def delete_knowledge_doc(request: Request, doc_id: str) -> dict:
    service = _knowledge_service(request)
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

    service = _knowledge_service(request)
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

    # 1.5 拆解确认流程（F-3-3）：只做需求分析，等待用户确认测试点
    if confirm_points:
        # 拆解阶段注入历史用例做覆盖度查漏（知识管家 F-7-6，检索时机约束）
        knowledge, snapshot = await _gather_knowledge(
            request, requirement, ("analysis",), knowledge_space
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
    knowledge, snapshot = await _gather_knowledge(
        request, requirement, ("analysis", "generation"), knowledge_space
    )
    try:
        result = await run_generation(
            requirement,
            llm=request.app.state.llm,
            model=model,
            reviewer_model=reviewer_model,
            template=template,
            knowledge_refs=knowledge["refs"],
            knowledge_cases=knowledge["cases"],
        )
    except UnknownModelError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except MissingAPIKeyError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except AllModelsFailedError as e:
        record = TaskRecord(task_id=task_id, status="failed", sources=sources, error=str(e))
        store.save(record)
        raise HTTPException(status_code=502, detail=str(e))

    return _finalize_task(store, task_id, task_dir, sources, result, template, knowledge=snapshot)


def _finalize_task(
    store, task_id: str, task_dir: Path, sources: list[str], result, template,
    knowledge: list[dict] | None = None,
) -> dict:
    """导出多格式产物（F-5-1/2/3/4）并落库，返回任务响应。"""
    files_map: dict[str, str] = {}
    if result.cases:
        files_map["xlsx"] = str(export_excel(result.cases, task_dir / "测试用例.xlsx", template))
        files_map["csv"] = str(export_csv(result.cases, task_dir / "测试用例.csv", template))
        root_title = Path(sources[0]).stem if sources and sources[0] != "text" else "测试用例"
        files_map["xmind"] = str(
            export_xmind(result.cases, task_dir / "测试用例.xmind", root_title=root_title)
        )

    record = TaskRecord(
        task_id=task_id,
        status="completed",
        sources=sources,
        result=result.model_dump(),
        files=files_map,
        knowledge=knowledge or [],
    )
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
        request, ctx.get("requirement", ""), ("analysis", "generation"), ctx.get("knowledge_space")
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
        )
    except MissingAPIKeyError as e:
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
    )
    # 保留拆解阶段留痕
    saved = store.get(task_id)
    saved.analysis = record.analysis
    store.save(saved)
    return response


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

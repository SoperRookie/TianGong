"""API 路由。

POST /api/v1/tasks：上传需求（文件/文本）→ 解析 → 三角色编排生成 → 导出 Excel/CSV。
M1 为同步执行；M4 接入 Celery 异步队列与任务进度。
"""

from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app.agents import run_generation
from app.config import get_settings
from app.exporters import export_csv, export_excel
from app.llm.client import AllModelsFailedError
from app.llm.registry import UnknownModelError
from app.llm.schemas import MissingAPIKeyError
from app.parsers import ScannedPDFError, UnsupportedFormatError, parse_file, parse_text
from app.tasks import TaskRecord

router = APIRouter()


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


@router.post("/api/v1/tasks")
async def create_task(
    request: Request,
    files: list[UploadFile] = File(default=[]),
    text: str = Form(default=""),
    model: str | None = Form(default=None),
    reviewer_model: str | None = Form(default=None),
) -> dict:
    settings = get_settings()
    store = request.app.state.tasks
    task_id, task_dir = store.new_task_dir()

    # 1. 解析输入（文件 + 粘贴文本可混合）
    parts: list[str] = []
    sources: list[str] = []
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    try:
        for upload in files:
            saved = await _read_upload(upload, task_dir, max_bytes)
            doc = parse_file(saved)
            parts.append(f"【文件：{doc.source}】\n{doc.full_text}")
            sources.append(doc.source)
        if text.strip():
            parts.append(parse_text(text).full_text)
            sources.append("text")
    except (UnsupportedFormatError, ScannedPDFError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not parts:
        raise HTTPException(status_code=400, detail="请上传需求文件或粘贴需求文本")

    # 2. 编排生成
    try:
        result = await run_generation(
            "\n\n".join(parts),
            llm=request.app.state.llm,
            model=model,
            reviewer_model=reviewer_model,
        )
    except UnknownModelError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except MissingAPIKeyError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except AllModelsFailedError as e:
        record = TaskRecord(task_id=task_id, status="failed", sources=sources, error=str(e))
        store.save(record)
        raise HTTPException(status_code=502, detail=str(e))

    # 3. 导出（F-5-2/3），双格式内容一致
    files_map: dict[str, str] = {}
    if result.cases:
        files_map["xlsx"] = str(export_excel(result.cases, task_dir / "测试用例.xlsx"))
        files_map["csv"] = str(export_csv(result.cases, task_dir / "测试用例.csv"))

    record = TaskRecord(
        task_id=task_id,
        status="completed",
        sources=sources,
        result=result.model_dump(),
        files=files_map,
    )
    store.save(record)
    return {
        "task_id": task_id,
        "status": record.status,
        "case_count": len(result.cases),
        "passed": result.passed,
        "review_rounds": result.review_rounds,
        "unresolved": result.unresolved,
        "blind_spots": result.blind_spots,
        "downloads": {fmt: f"/api/v1/tasks/{task_id}/files/{fmt}" for fmt in files_map},
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

"""FastAPI 应用入口。启动：uvicorn app.main:app --reload；Web 界面访问 http://localhost:8000/"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger

from app.api.routes import router
from app.auth import AuthStore
from app.config import BASE_DIR, get_settings
from app.llm.client import LLMClient
from app.llm.registry import ModelRegistry
from app.learning import RuleStore
from app.logging_setup import setup_logging
from app.memory import MemoryStore
from app.tasks import TaskStore
from app.templates import TemplateStore

setup_logging(get_settings().log_level, get_settings().log_dir)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    registry = ModelRegistry.from_yaml(settings.models_config_path)
    app.state.registry = registry
    app.state.llm = LLMClient(registry)
    app.state.tasks = TaskStore(output_dir=settings.outputs_dir)
    app.state.templates = TemplateStore(storage_path=settings.data_dir / "templates.json")
    app.state.memory = MemoryStore(
        storage_path=settings.data_dir / "memory.json",
        pref_threshold=settings.memory_pref_threshold,
        revision_threshold=settings.memory_revision_threshold,
    )
    app.state.rules = RuleStore(storage_path=settings.data_dir / "rules.json")
    from app.projects import ModuleStore, ProjectStore, UserPrefStore, VersionStore

    app.state.projects = ProjectStore(storage_path=settings.data_dir / "projects.json")
    app.state.user_prefs = UserPrefStore()
    app.state.versions = VersionStore()
    app.state.modules = ModuleStore()
    from app.plans import PlanStore, migrate_task_executions

    app.state.plans = PlanStore()
    migrated = migrate_task_executions(app.state.tasks, app.state.plans)
    if migrated:
        logger.info("M4 执行迁移：{} 个任务的历史执行轮次已搬入测试计划", migrated)
    from app.requirements import RequirementStore, migrate_tasks

    app.state.requirements = RequirementStore()
    migrated = migrate_tasks(app.state.tasks, app.state.requirements)
    if migrated:
        logger.info("M2 需求迁移：{} 个历史任务已建为需求实体并回填关联", migrated)
    from app.prompts import PromptStore, set_current

    app.state.prompts = PromptStore()
    set_current(app.state.prompts)
    app.state.auth = AuthStore(
        storage_path=settings.data_dir / "auth.json",
        session_ttl_hours=settings.session_ttl_hours,
    )
    app.state.auth.ensure_admin(settings.admin_username, settings.admin_password)
    logger.info(
        "服务启动：默认模型={} 可用模型={} 输出目录={} 日志目录={}",
        registry.default_model,
        [m["name"] for m in registry.list_public()],
        settings.outputs_dir,
        settings.log_dir,
    )
    yield
    set_current(None)
    logger.info("服务关闭")


app = FastAPI(title="TestCase Agent", version="0.1.0", lifespan=lifespan)
app.include_router(router)

# 前端第三方库本地托管（kity / kityminder-core，MeterSphere 同款脑图内核）：内网部署无外部依赖
from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount("/vendor", StaticFiles(directory=BASE_DIR / "app" / "web" / "vendor"), name="vendor")

# 无需登录即可访问：登录接口、健康检查（Web 首页为静态壳，登录态由前端接口驱动）
_PUBLIC_API_PATHS = {"/api/v1/auth/login", "/health"}


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """登录鉴权：/api/v1/* 需携带有效 Bearer Token（文件下载支持 ?token= 查询参数）。

    同时充当操作日志采集点（完整需求 17 章）：全部写接口按路由映射为中文动作落 audit_log。
    """
    import time

    from app.audit import describe, record

    path = request.url.path
    if (
        get_settings().auth_enabled
        and path.startswith("/api/v1")
        and path not in _PUBLIC_API_PATHS
    ):
        header = request.headers.get("Authorization", "")
        token = header.removeprefix("Bearer ").strip() or request.query_params.get("token")
        user = request.app.state.auth.verify(token)
        if user is None:
            return JSONResponse({"detail": "未登录、会话已过期或账号已禁用，请重新登录"}, status_code=401)
        request.state.user = user
    described = describe(request.method, path)
    started = time.monotonic()
    response = await call_next(request)
    if described is not None:
        kind, action, target = described
        user = getattr(request.state, "user", None)
        record(
            user=(user or {}).get("username") if user else ("anonymous" if not get_settings().auth_enabled else None),
            ip=_client_ip(request), ua=request.headers.get("User-Agent", ""),
            kind=kind, action=action, target=target,
            project=getattr(request.state, "audit_project", None),
            method=request.method, path=path, status=response.status_code,
            detail=getattr(request.state, "audit_detail", ""),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    return response


@app.get("/", include_in_schema=False)
async def web_index() -> FileResponse:
    """Web 界面（M4-W2）：单页静态实现，后续可平移 Vue3 工程化前端。"""
    return FileResponse(BASE_DIR / "app" / "web" / "index.html")

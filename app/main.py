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
        max_failures=settings.login_max_failures,
        lockout_minutes=settings.login_lockout_minutes,
    )
    app.state.auth.ensure_admin(settings.admin_username, settings.admin_password)
    _acquire_instance_lock(settings)
    _purge_logs(settings)
    logger.info(
        "服务启动：默认模型={} 可用模型={} 输出目录={} 日志目录={}",
        registry.default_model,
        [m["name"] for m in registry.list_public()],
        settings.outputs_dir,
        settings.log_dir,
    )
    yield
    from app.db import flush_persist

    flush_persist()
    set_current(None)
    _release_instance_lock()
    logger.info("服务关闭")


_lock_handle = None


def _acquire_instance_lock(settings) -> None:
    """单实例守卫：用户/项目/任务等为进程内内存态 + 整表回写，多 worker 会互相覆盖，启动即拦截。"""
    global _lock_handle
    if not settings.single_instance_lock:
        return
    import fcntl

    settings.outputs_dir.mkdir(parents=True, exist_ok=True)
    handle = open(settings.outputs_dir / ".instance.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError(
            "检测到另一个天工实例正在使用同一数据目录。当前架构只支持单进程（uvicorn 不要加 --workers），"
            "如需多实例请先迁移到按键读写的存储层。"
        )
    handle.write(str(__import__("os").getpid()))
    handle.flush()
    _lock_handle = handle


def _release_instance_lock() -> None:
    global _lock_handle
    if _lock_handle is not None:
        try:
            _lock_handle.close()
        except OSError:
            pass
        _lock_handle = None


def _purge_logs(settings) -> None:
    if settings.log_retention_days <= 0:
        return
    from datetime import datetime, timedelta, timezone

    from app.audit import purge_before as purge_audit
    from app.llm.calllog import purge_before as purge_calls

    cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.log_retention_days)).isoformat(timespec="seconds")
    try:
        n1, n2 = purge_calls(cutoff), purge_audit(cutoff)
        if n1 or n2:
            logger.info("日志留存清理：AI 调用日志 {} 条，操作日志 {} 条（早于 {} 天）", n1, n2, settings.log_retention_days)
    except Exception as e:  # pragma: no cover
        logger.warning("日志清理失败：{}", e)


_settings_boot = get_settings()
app = FastAPI(
    title="TestCase Agent", version="0.1.0", lifespan=lifespan,
    docs_url="/docs" if _settings_boot.expose_docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _settings_boot.expose_docs else None,
)
app.include_router(router)

# 前端第三方库本地托管（kity / kityminder-core，MeterSphere 同款脑图内核）：内网部署无外部依赖
from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount("/vendor", StaticFiles(directory=BASE_DIR / "app" / "web" / "vendor"), name="vendor")
app.mount("/brand", StaticFiles(directory=BASE_DIR / "app" / "web" / "brand"), name="brand")  # 夜枭标志与表情

# 无需登录即可访问：登录接口、健康检查（Web 首页为静态壳，登录态由前端接口驱动）
_PUBLIC_API_PATHS = {"/api/v1/auth/login", "/health"}


def _client_ip(request: Request) -> str:
    """客户端 IP：只有来自可信代理的请求才采信 X-Forwarded-For，否则一律用直连地址（防审计 IP 伪造）。"""
    direct = request.client.host if request.client else ""
    trusted = {p.strip() for p in get_settings().trusted_proxies.split(",") if p.strip()}
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd and direct in trusted:
        hops = [h.strip() for h in fwd.split(",") if h.strip()]
        for hop in reversed(hops):  # 从右往左跳过可信代理，取第一个非代理地址
            if hop not in trusted:
                return hop
    return direct


# 允许用 ?token= 鉴权的下载类路径（浏览器直接打开链接无法带 Authorization 头）
_QUERY_TOKEN_PATHS = ("/files/", "/attachments/")
# 强制改密期间仍允许访问的接口
_MUST_CHANGE_ALLOW = {"/api/v1/auth/password", "/api/v1/auth/me", "/api/v1/auth/logout"}
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'"
    ),
}


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
        token = header.removeprefix("Bearer ").strip()
        if not token and any(seg in path for seg in _QUERY_TOKEN_PATHS):
            token = request.query_params.get("token")
        user = request.app.state.auth.verify(token)
        if user is None:
            return JSONResponse({"detail": "未登录、会话已过期或账号已禁用，请重新登录"}, status_code=401)
        if user.get("must_change_password") and path not in _MUST_CHANGE_ALLOW:
            return JSONResponse({"detail": "初始口令须先修改后才能使用系统", "must_change_password": True}, status_code=403)
        request.state.user = user
    described = describe(request.method, path)
    started = time.monotonic()
    response = await call_next(request)
    for k, v in _SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
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

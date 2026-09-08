"""API 路由。

POST /api/v1/tasks：上传需求（文件/文本）→ 解析 → 三角色编排生成 → 导出 Excel/CSV。
M1 为同步执行；M4 接入 Celery 异步队列与任务进度。
"""

import re
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


# ---- 登录认证与用户管理 ----


def _current_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if user is None:  # auth_enabled=false（测试/内网免登）时兜底为管理员语义
        return {"username": "anonymous", "role": "admin"}
    return user


def _operator(request: Request) -> str:
    """当前操作人（登录用户名）：任务归属与审核留痕精确到人。"""
    return _current_user(request)["username"]


def _require_admin(request: Request) -> dict:
    user = _current_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="该操作需要管理员权限")
    return user


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


# ---- 项目级权限与数据隔离（完整需求 3.3 / 3.4 / 3.5）----
#
# 每个业务接口按 (所属项目, 动作) 校验：系统管理员视同任一项目的项目管理员；
# 其他用户必须是该项目成员且项目角色具备该动作；未加入项目 → 403，杜绝跨项目 ID 访问。
# 历史遗留的「未指定项目」任务不属于任何项目：仅系统管理员可见可操作（M2 需求实体化后迁移归属）。


def _is_admin(request: Request) -> bool:
    return _current_user(request)["role"] == "admin"


def _project_role(request: Request, project: str | None) -> str | None:
    if _is_admin(request):
        return "project_admin"
    return request.app.state.projects.role_of(project, _operator(request))


def _require_project(request: Request, project: str | None, action: str) -> dict | None:
    """校验当前用户对项目的动作权限；返回项目实体（未指定项目返回 None）。"""
    from app.permissions import is_write_action, role_allows
    from app.reports import UNASSIGNED

    if not project or project == UNASSIGNED:
        if not _is_admin(request):
            raise HTTPException(status_code=403, detail="该数据未归属任何项目，仅系统管理员可访问")
        return None
    entity = request.app.state.projects.get(project)
    if entity is None:
        if _is_admin(request):
            return None  # 管理员访问尚未注册的历史项目名：按遗留数据放行
        raise HTTPException(status_code=403, detail="无权访问该项目")
    role = _project_role(request, project)
    if role is None:
        raise HTTPException(status_code=403, detail="无权访问该项目")
    if not role_allows(role, action):
        raise HTTPException(status_code=403, detail=f"当前项目角色「{_role_label(role)}」无此权限")
    if entity["status"] == "archived" and is_write_action(action):
        raise HTTPException(status_code=409, detail="项目已归档，恢复后才能修改")
    return entity


def _role_label(role: str) -> str:
    from app.permissions import PROJECT_ROLES

    return PROJECT_ROLES.get(role, role)


def _visible_projects(request: Request) -> set[str] | None:
    """当前用户可见的项目集合；None 表示不限（系统管理员）。"""
    if _is_admin(request):
        return None
    return {p["project"] for p in request.app.state.projects.projects_of(_operator(request))}


def _record_visible(request: Request, project: str | None) -> bool:
    from app.reports import UNASSIGNED

    visible = _visible_projects(request)
    if visible is None:
        return True
    if not project or project == UNASSIGNED:
        return False
    return project in visible


def _task(request: Request, task_id: str, action: str) -> TaskRecord:
    """取任务并校验其所属项目权限（404 优先于 403，避免探测存在性；同项目内按动作细分）。"""
    record = request.app.state.tasks.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    _require_project(request, (record.context or {}).get("project"), action)
    return record


def _plan(request: Request, plan_id: str, action: str) -> dict:
    plan = request.app.state.plans.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"测试计划不存在: {plan_id}")
    _require_project(request, plan.get("project"), action)
    return plan


class LoginBody(BaseModel):
    username: str
    password: str
    otp: str | None = None  # 两步验证动态码（绑定用户必填，二段式提交）


@router.post("/api/v1/auth/login")
async def auth_login(request: Request, body: LoginBody) -> dict:
    from app.auth import AuthError
    from app.auth.store import OtpRequired

    try:
        token, user = request.app.state.auth.login(
            body.username, body.password, otp=body.otp, ip=_client_ip(request)
        )
    except OtpRequired:
        # 口令正确但需动态码：不签发会话，前端展示验证码输入后重新提交
        return {"otp_required": True}
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    logger.info("用户 {} 登录成功（IP {}）", user["username"], user.get("last_login_ip") or "-")
    return {"token": token, "user": _with_memberships(request, user)}


# ---- 安全设置（系统级开关）----


@router.get("/api/v1/auth/settings")
async def auth_settings(request: Request) -> dict:
    """安全设置：两步验证功能总开关状态（登录用户可读，用于界面展隐）。"""
    return {"totp_enabled": request.app.state.auth.totp_policy()}


class AuthSettingsBody(BaseModel):
    totp_enabled: bool


@router.put("/api/v1/auth/settings")
async def update_auth_settings(request: Request, body: AuthSettingsBody) -> dict:
    """更新安全设置（管理员）：关闭两步验证后全平台登录不再校验动态码，也不可新绑定；
    已绑定用户的密钥保留，重新开启后继续生效。"""
    _require_admin(request)
    request.app.state.auth.set_totp_policy(body.totp_enabled)
    logger.info("两步验证功能总开关：{}", "开启" if body.totp_enabled else "关闭")
    return {"totp_enabled": request.app.state.auth.totp_policy()}


# ---- 两步验证（TOTP：Google Authenticator / 海月盾等标准验证器）----


@router.post("/api/v1/auth/totp/setup")
async def totp_setup(request: Request) -> dict:
    """开始绑定：返回密钥、otpauth URI 与二维码 SVG（本地生成），扫码后回填动态码确认。"""
    from app.auth import AuthError
    from app.auth.totp import otpauth_uri, qr_svg

    user = _current_user(request)
    try:
        secret = request.app.state.auth.totp_setup(user["username"])
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    uri = otpauth_uri(secret, user["username"])
    return {"secret": secret, "otpauth_uri": uri, "qr_svg": qr_svg(uri)}


class TotpCodeBody(BaseModel):
    code: str


@router.post("/api/v1/auth/totp/enable")
async def totp_enable(request: Request, body: TotpCodeBody) -> dict:
    from app.auth import AuthError

    user = _current_user(request)
    try:
        request.app.state.auth.totp_enable(user["username"], body.code)
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info("用户 {} 已绑定两步验证", user["username"])
    return {"ok": True, "message": "两步验证已启用，下次登录需输入动态验证码"}


class TotpDisableBody(BaseModel):
    password: str
    code: str


@router.post("/api/v1/auth/totp/disable")
async def totp_disable(request: Request, body: TotpDisableBody) -> dict:
    from app.auth import AuthError

    user = _current_user(request)
    try:
        request.app.state.auth.totp_disable(user["username"], body.password, body.code)
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info("用户 {} 已解绑两步验证", user["username"])
    return {"ok": True}


@router.post("/api/v1/auth/logout")
async def auth_logout(request: Request) -> dict:
    header = request.headers.get("Authorization", "")
    request.app.state.auth.logout(header.removeprefix("Bearer ").strip())
    return {"ok": True}


def _with_memberships(request: Request, user: dict) -> dict:
    """用户信息附加：所属项目与项目角色（3.4）、收藏/最近访问（3.2）。"""
    projects = request.app.state.projects.projects_of(user["username"]) if user["role"] != "admin" \
        else [{"project": p["name"], "role": "project_admin", "status": p["status"]}
              for p in request.app.state.projects.list()]
    return {**user, "projects": projects, "prefs": request.app.state.user_prefs.get(user["username"])}


@router.get("/api/v1/auth/me")
async def auth_me(request: Request) -> dict:
    return _with_memberships(request, _current_user(request))


class ProfileBody(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    avatar: str | None = None  # 内联 data URL 或图片地址（≤150KB）


@router.put("/api/v1/auth/me")
async def auth_update_me(request: Request, body: ProfileBody) -> dict:
    """用户自助维护资料（3.1）。免登模式下无真实账号，直接返回。"""
    from app.auth import AuthError

    user = _current_user(request)
    if not request.app.state.auth.exists(user["username"]):
        return _with_memberships(request, user)
    try:
        updated = request.app.state.auth.update_profile(
            user["username"], **body.model_dump(exclude_none=True)
        )
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _with_memberships(request, updated)


class PasswordBody(BaseModel):
    old_password: str
    new_password: str


@router.post("/api/v1/auth/password")
async def auth_change_password(request: Request, body: PasswordBody) -> dict:
    from app.auth import AuthError

    user = _current_user(request)
    try:
        request.app.state.auth.change_password(user["username"], body.old_password, body.new_password)
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "message": "密码已修改，请重新登录"}


class UserBody(BaseModel):
    username: str
    password: str
    role: str = "member"
    name: str = ""
    email: str = ""
    phone: str = ""


@router.get("/api/v1/auth/users")
async def auth_list_users(request: Request) -> dict:
    """用户列表（管理员）：含资料/状态/最后登录，并附所属项目与项目角色（3.1「查看所属项目」）。"""
    _require_admin(request)
    pstore = request.app.state.projects
    users = request.app.state.auth.list_users()
    for u in users:
        u["projects"] = pstore.projects_of(u["username"])
    return {"users": users}


@router.post("/api/v1/auth/users")
async def auth_add_user(request: Request, body: UserBody) -> dict:
    from app.auth import AuthError

    _require_admin(request)
    try:
        return request.app.state.auth.add_user(
            body.username, body.password, body.role,
            name=body.name, email=body.email, phone=body.phone,
        )
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))


class UserUpdateBody(BaseModel):
    role: str | None = None          # 修改角色（admin/member）
    new_password: str | None = None  # 重置密码（无需原密码，重置后该用户需重新登录）
    reset_totp: bool = False         # 重置两步验证（手机丢失等场景解绑，用户可重新绑定）
    status: str | None = None        # active / disabled（禁用即会话失效）
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    avatar: str | None = None


@router.put("/api/v1/auth/users/{username}")
async def auth_update_user(request: Request, username: str, body: UserUpdateBody) -> dict:
    """管理员管理用户（重置密码 / 修改角色 / 重置两步验证）。"""
    from app.auth import AuthError

    operator = _require_admin(request)
    fields = body.model_dump(exclude_none=True)
    fields.pop("reset_totp", None)
    if not fields and not body.reset_totp:
        raise HTTPException(status_code=400, detail="请提供要修改的角色、资料、状态、新密码或重置两步验证")
    try:
        user = request.app.state.auth.admin_update(
            username, reset_totp=body.reset_totp, operator=operator["username"], **fields
        )
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info("管理员更新用户 {}：角色={} 状态={} 重置密码={} 重置两步验证={} 资料字段={}",
                username, body.role or "-", body.status or "-", bool(body.new_password),
                body.reset_totp, [k for k in fields if k in ("name", "email", "phone", "avatar")])
    user["projects"] = request.app.state.projects.projects_of(username)
    return user


@router.delete("/api/v1/auth/users/{username}")
async def auth_delete_user(request: Request, username: str) -> dict:
    from app.auth import AuthError

    operator = _require_admin(request)
    try:
        request.app.state.auth.delete_user(username, operator=operator["username"])
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    request.app.state.projects.remove_user_everywhere(username)
    return {"deleted": username}


# ---- 模型接入配置（F-1-x 管理界面）----


@router.get("/api/v1/models")
async def list_models(request: Request) -> dict:
    """模型清单（不含密钥信息），供前端任务创建时选择（F-1-4）。"""
    registry = request.app.state.registry
    return {"default_model": registry.default_model, "models": registry.list_public()}


@router.get("/api/v1/models/config")
async def get_models_config(request: Request) -> dict:
    """完整模型接入配置（管理员）：含 base_url / 密钥环境变量名 / 降级链路，不含密钥值。"""
    _require_admin(request)
    registry = request.app.state.registry
    import os

    return {
        "default_model": registry.default_model,
        "max_retries": registry.max_retries,
        "models": [
            {**m.model_dump(), "api_key_set": bool(not m.api_key_env or os.environ.get(m.api_key_env))}
            for m in registry.all()
        ],
    }


class ModelsConfigBody(BaseModel):
    default_model: str
    max_retries: int = 1
    models: list[dict]


@router.put("/api/v1/models/config")
async def update_models_config(request: Request, body: ModelsConfigBody) -> dict:
    """更新模型接入配置（管理员）：整体校验 → 写回 models.yaml → 热重载注册表与客户端。

    密钥仍走环境变量（api_key_env 只存变量名），配置文件不落任何密钥值。
    """
    import yaml

    from app.llm.client import LLMClient
    from app.llm.registry import ModelRegistry
    from app.llm.schemas import ModelConfig

    _require_admin(request)
    try:
        models = [ModelConfig.model_validate(m) for m in body.models]
        registry = ModelRegistry(
            default_model=body.default_model, models=models, max_retries=max(0, body.max_retries)
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"模型配置不合法: {e}")

    # 保留 embeddings 段原样写回（Embedding 选型已定型，不在此界面管理）
    path = get_settings().models_config_path
    existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data = {
        "default_model": body.default_model,
        "max_retries": registry.max_retries,
        "models": [m.model_dump() for m in models],
    }
    for key in ("default_embedding", "embeddings"):
        if key in existing:
            data[key] = existing[key]
    header = (
        "# LLM 模型配置（F-1-1 ~ F-1-5）——本文件由平台「模型配置」界面管理，手工注释不会保留。\n"
        "# 密钥不写入本文件：api_key_env 为密钥所在环境变量名，请在部署环境/.env 中配置。\n"
    )
    path.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    request.app.state.registry = registry
    request.app.state.llm = LLMClient(registry)
    logger.info("模型配置已更新并热重载：默认={} 共 {} 个模型", registry.default_model, len(models))
    return {"default_model": registry.default_model, "models": registry.list_public()}


class ModelTestBody(BaseModel):
    name: str


@router.post("/api/v1/models/test")
async def test_model(request: Request, body: ModelTestBody) -> dict:
    """连通性测试（管理员）：向指定模型发送一次最小请求，返回耗时与结果。"""
    _require_admin(request)
    try:
        request.app.state.registry.get(body.name)
    except UnknownModelError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        result = await request.app.state.llm.chat(
            [{"role": "user", "content": "ping，请只回复 pong"}],
            model=body.name, max_tokens=8,
        )
        return {"ok": True, "model": result.model_name, "provider": result.provider,
                "elapsed_ms": result.elapsed_ms, "reply": result.content[:50]}
    except Exception as e:
        return {"ok": False, "model": body.name, "error": str(e)}


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
    except AllModelsFailedError as e:
        raise HTTPException(status_code=502, detail=f"图片解析失败（Vision 模型不可用）：{e}")
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


def _gather_rules(app, project: str | None) -> tuple[str | None, list[dict]]:
    """规则注入（需求三十九）：已确认生效的团队/项目规则，独立预算，故障降级不阻塞。"""
    try:
        notes, snapshot = app.state.rules.render(
            project=project, budget_chars=get_settings().rules_budget_chars
        )
        if snapshot:
            logger.info("规则注入：{} 条（项目={}）", len(snapshot), project or "-")
        return notes, snapshot
    except Exception as e:
        logger.warning("规则检索故障，降级为无规则注入：{}", e)
        return None, []


async def _reuse_hints(app, requirement: str, space: str | None) -> list[dict]:
    """历史用例复用提示（需求二十九）：向量检索测试用例库，高相似即提示复用。"""
    try:
        service = _knowledge_service(app)
        if not service.store.list_docs(space, "test_cases"):
            return []
        hits = await service.search(
            requirement[:1500], top_k=3, category="test_cases", space=space, mode="vector"
        )
        threshold = get_settings().reuse_hint_score
        return [
            {"source": h.source, "text": h.text, "score": round(h.score, 3),
             "hint": "发现历史正式用例与当前需求高度相关，可复用/作为参考/忽略"}
            for h in hits if h.score >= threshold
        ]
    except Exception as e:
        logger.warning("复用提示检索故障，跳过：{}", e)
        return []


async def _run_point_quality_checks(app, task_id: str, requirement: str, model: str | None) -> None:
    """拆解后的质量闭环（需求六十一：测试点生成 → 独立覆盖检查 → 重复检查）。

    独立查漏 Agent 产出覆盖矩阵与新增建议（只新增）；重复检查产出疑似重复对交人工处置。
    任何一步故障均降级跳过，不阻塞拆解确认主链路。
    """
    from app.agents.quality import run_dup_judge, run_gap_check
    from app.tasks.points import add_points, duplicate_candidates

    store = app.state.tasks
    record = store.get(task_id)
    modules = (record.analysis or {}).get("test_points", [])
    try:
        gap = await run_gap_check(app.state.llm, requirement, modules, model)
        record.coverage = gap["coverage"]
        added = add_points(
            modules,
            [{"module": a.get("module", ""), "point": a.get("point", ""),
              "dimension": a.get("dimension", "")} for a in gap["additions"]],
            source="gap",
        )
        if added:
            logger.info("独立查漏新增 {} 条待审核测试点", len(added))
    except Exception as e:
        logger.warning("独立查漏故障，跳过（不阻塞主链路）：{}", e)
    try:
        pairs = duplicate_candidates(modules)
        judged = await run_dup_judge(app.state.llm, requirement, pairs, model) if pairs else []
        record.dup_report = {"points": judged, "resolved": []}
    except Exception as e:
        logger.warning("重复检查故障，跳过（不阻塞主链路）：{}", e)
    store.save(record)


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
    """手工维护记忆（仅管理员；member 的记忆由使用习惯自动沉淀）。"""
    _require_admin(request)
    try:
        entry = request.app.state.memory.add(body.content, scope=body.scope, project=body.project)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return entry.model_dump()


@router.put("/api/v1/memories/{memory_id}")
async def update_memory(request: Request, memory_id: str, body: MemoryUpdateBody) -> dict:
    """记忆纠错（F-8-6，仅管理员）：直接改写记忆内容。"""
    _require_admin(request)
    try:
        entry = request.app.state.memory.update(memory_id, body.content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if entry is None:
        raise HTTPException(status_code=404, detail=f"记忆不存在: {memory_id}")
    return entry.model_dump()


@router.delete("/api/v1/memories/{memory_id}")
async def delete_memory(request: Request, memory_id: str) -> dict:
    _require_admin(request)
    if not request.app.state.memory.delete(memory_id):
        raise HTTPException(status_code=404, detail=f"记忆不存在: {memory_id}")
    return {"deleted": memory_id}


@router.delete("/api/v1/memories")
async def clear_memories(
    request: Request, scope: str | None = None, project: str | None = None
) -> dict:
    """一键清空（F-8-6，仅管理员），可按维度/项目过滤。"""
    _require_admin(request)
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
    project = (project or "").strip() or None
    if project is None and not _is_admin(request):
        raise HTTPException(status_code=400, detail="请选择任务所属项目")
    if project and _is_admin(request):  # 管理员直传的新项目名自动注册
        request.app.state.projects.ensure([project], created_by=_operator(request))
    _require_project(request, project, "point.ai")
    # 使用习惯沉淀（F-8-2）：直接生成与拆解确认两条路径统一在此记录模板/模型使用
    request.app.state.memory.record_usage("template", template.template_id)
    if model:
        request.app.state.memory.record_usage("model", model)
    logger.info(
        "任务 {} 创建：来源={} 共 {} 字（项目={} 模板={} 确认拆解={} 异步={}）",
        task_id, sources, len(requirement), project or "-", template.template_id, confirm_points, async_mode,
    )

    task_context = {
        "requirement": requirement,
        "model": model,
        "reviewer_model": reviewer_model,
        "template_id": template.template_id,
        "knowledge_space": knowledge_space,
        "project": project,
    }

    # 1.5 拆解确认流程（F-3-3）：只做需求分析，等待用户确认测试点。
    # 支持后台执行：任务先落库（queued），拆解与查漏/查重在后台跑，列表随时可见可管理。
    if confirm_points:
        async def _analyze() -> dict:
            # 拆解阶段注入历史用例做覆盖度查漏（知识管家 F-7-6，检索时机约束）
            knowledge, snapshot = await _gather_knowledge(
                request.app, requirement, ("analysis",), knowledge_space
            )
            store.set_progress(task_id, progress="analyzing")
            analysis = await run_analysis(
                requirement,
                llm=request.app.state.llm,
                model=model,
                knowledge_cases=knowledge["cases"],
            )
            from app.tasks.points import assign_entities

            analysis_data = analysis.model_dump()
            # 测试点实体化：tp_id + 审核状态机（通过/驳回/锁定），支撑逐条与批量审核
            analysis_data["test_points"] = assign_entities(analysis_data["test_points"])
            record = store.get(task_id) or TaskRecord(task_id=task_id)
            record.created_by = record.created_by or _operator(request)
            record.status = "awaiting_confirmation"
            record.progress = "quality_check"  # 拆解已可审核，查漏/查重继续后台补充
            record.error = None
            record.sources = sources
            record.analysis = analysis_data
            record.knowledge = snapshot
            record.reuse_hints = await _reuse_hints(request.app, requirement, knowledge_space)
            record.context = task_context
            store.save(record)
            # 拆解后自动执行：独立覆盖检查 + 重复检查（需求六十一闭环；故障降级不阻塞）
            await _run_point_quality_checks(request.app, task_id, requirement, model)
            store.set_progress(task_id, progress=None)
            record = store.get(task_id)
            # 版本历史（完整需求 10 章）：拆解产出即记 AI 原始版本（查漏新增的点一并入册）
            from app.versions import ensure_versions, point_entities
            ensure_versions(task_id, "point",
                            point_entities((record.analysis or {}).get("test_points", [])),
                            by=record.created_by)
            return {
                "task_id": task_id,
                "status": record.status,
                "test_points": (record.analysis or {}).get("test_points", []),
                "blind_spots": analysis.blind_spots,
                "coverage": record.coverage,
                "dup_report": record.dup_report,
                "reuse_hints": record.reuse_hints,
                "confirm_url": f"/api/v1/tasks/{task_id}/confirm",
            }

        if async_mode:
            store.save(TaskRecord(task_id=task_id, status="queued", sources=sources,
                                  context=task_context, created_by=_operator(request)))
            store.submit(task_id, _analyze)
            return {"task_id": task_id, "status": "queued", "poll_url": f"/api/v1/tasks/{task_id}"}
        try:
            return await _analyze()
        except (UnknownModelError,) as e:
            raise HTTPException(status_code=400, detail=str(e))
        except (MissingAPIKeyError, AllModelsFailedError) as e:
            store.save(TaskRecord(task_id=task_id, status="failed", sources=sources,
                                  context=task_context, error=str(e), created_by=_operator(request)))
            raise HTTPException(status_code=502, detail=str(e))

    # 2. 编排生成（超长需求自动分片并行，F-2-6）；知识管家按时机注入（F-7-6）

    creator = _operator(request)

    async def _generate() -> dict:
        if store.get(task_id) is None:  # 同步路径预建记录：归属与失败留痕都有主
            store.save(TaskRecord(task_id=task_id, status="running", sources=sources,
                                  context=task_context, created_by=creator))
        knowledge, snapshot = await _gather_knowledge(
            request.app, requirement, ("analysis", "generation"), knowledge_space
        )
        # 记忆检索注入（F-8-7）：独立预算，不占知识库配额
        memory_notes, memory_snapshot = _gather_memories(request.app, requirement, project)
        # 规则注入（需求三十九）：已确认生效的团队/项目规则
        rule_notes, rule_snapshot = _gather_rules(request.app, project)
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
            rule_notes=rule_notes,
            on_analyzed=lambda: store.set_progress(task_id, progress="generating_reviewing"),
        )
        store.set_progress(task_id, progress="exporting")
        return _finalize_task(
            store, task_id, task_dir, sources, result, template,
            knowledge=snapshot, context=task_context, memories=memory_snapshot,
            rules=rule_snapshot,
        )

    # 异步模式（F-6-1/2）：立即返回 task_id，后台执行，GET /tasks/{id} 轮询进度
    if async_mode:
        store.save(TaskRecord(task_id=task_id, status="queued", sources=sources,
                              context=task_context, created_by=_operator(request)))
        store.submit(task_id, _generate)
        return {"task_id": task_id, "status": "queued", "poll_url": f"/api/v1/tasks/{task_id}"}

    try:
        return await _generate()
    except UnknownModelError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except (MissingAPIKeyError, LLMOutputError) as e:
        raise HTTPException(status_code=502, detail=str(e))
    except AllModelsFailedError as e:
        record = TaskRecord(task_id=task_id, status="failed", sources=sources, error=str(e),
                            created_by=_operator(request))
        store.save(record)
        raise HTTPException(status_code=502, detail=str(e))


def _finalize_task(
    store, task_id: str, task_dir: Path, sources: list[str], result, template,
    knowledge: list[dict] | None = None, context: dict | None = None,
    memories: list[dict] | None = None, rules: list[dict] | None = None,
) -> dict:
    """导出多格式产物（F-5-1/2/3/4）并落库，返回任务响应。

    复用既有记录（异步任务/修订任务），保留 created_at 与修订历史等留痕。
    用例分配稳定 uid（审核状态跟随 uid，不受编号重排影响）并初始化审核状态机。
    """
    from app.tasks.points import new_uid

    for case in result.cases:
        if not case.uid:
            case.uid = new_uid()

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
    # 用例审核状态机：新 uid 初始化为 pending；已不存在的 uid 清理
    uids = {c.uid for c in result.cases}
    record.case_reviews = {
        uid: state for uid, state in record.case_reviews.items() if uid in uids
    }
    for uid in uids:
        record.case_reviews.setdefault(
            uid, {"status": "pending", "comment": "", "reject_count": 0, "locked": False}
        )
    record.quality = _quality_report(record, result)
    if knowledge is not None:
        record.knowledge = knowledge
    if context is not None:
        record.context = context
    if memories is not None:
        record.memories = memories
    if rules is not None:
        record.rules = rules
    store.save(record)
    # 版本历史（完整需求 10 章）：为尚无版本的用例补记首版（生成产出 / 存量任务打底）
    from app.versions import case_entities, ensure_versions
    ensure_versions(task_id, "case", case_entities(record.result["cases"]), by=record.created_by)
    return {
        "task_id": task_id,
        "status": record.status,
        "case_count": len(result.cases),
        "passed": result.passed,
        "review_rounds": result.review_rounds,
        "chunks": result.chunks,
        "unresolved": result.unresolved,
        "blind_spots": result.blind_spots,
        "quality": record.quality,
        "downloads": {fmt: f"/api/v1/tasks/{task_id}/files/{fmt}" for fmt in files_map},
    }


def _quality_report(record: TaskRecord, result) -> dict:
    """AI 自检评分（需求五十八）：由确定性信号汇总，仅作参考，不作为自动通过依据。"""
    from app.tasks.points import case_duplicate_candidates

    coverage = record.coverage or {}
    applicable = [d for d, s in coverage.items() if s in ("已覆盖", "未覆盖", "待确认")]
    covered = [d for d in applicable if coverage[d] == "已覆盖"]
    coverage_score = round(len(covered) / len(applicable) * 100) if applicable else None

    dup_pairs = case_duplicate_candidates([c.model_dump() for c in result.cases])
    dup_risk = "高" if len(dup_pairs) >= 3 else "中" if dup_pairs else "低"
    return {
        "测试维度覆盖度": coverage_score,
        "重复风险": dup_risk,
        "疑似重复用例对": dup_pairs[:10],
        "待确认问题": len(result.blind_spots),
        "未解决评审问题": len(result.unresolved),
        "历史用例参考": sum(1 for k in record.knowledge if k.get("category") == "test_cases"),
        "评审轮次": result.review_rounds,
        "note": "评分仅供参考，不作为自动通过依据（需求五十八）",
    }


class ConfirmBody(BaseModel):
    test_points: list[dict] | None = None  # 缺省沿用拆解草稿；传入则以用户修改后的为准


@router.post("/api/v1/tasks/{task_id}/confirm")
async def confirm_task(request: Request, task_id: str, body: ConfirmBody | None = None) -> dict:
    """确认（或修改后确认）测试点，继续生成（F-3-3 第二阶段）。多模块按模块并行生成。"""
    store = request.app.state.tasks
    record = _task(request, task_id, "point.review")
    if record.status != "awaiting_confirmation":
        raise HTTPException(status_code=409, detail=f"任务状态为 {record.status}，无待确认的拆解结果")

    from app.tasks.points import confirmable_points, normalize_points

    ctx = record.context or {}
    if body and body.test_points:
        # 用户直接提交修改后的测试点（兼容 JSON 编辑路径）
        test_points = normalize_points(body.test_points)
        test_points = [
            {"module": e["module"],
             "points": [{"point": p["point"], "dimension": p.get("dimension", "")} for p in e["points"]]}
            for e in test_points if e.get("points")
        ]
    else:
        # 正式测试点（需求六十一）：有审核记录时只用已通过的；驳回项永不进入生成
        test_points = confirmable_points((record.analysis or {}).get("test_points", []))
    if not test_points:
        raise HTTPException(status_code=400, detail="测试点为空，无法生成（请先通过至少一条测试点）")
    template = request.app.state.templates.get(ctx.get("template_id"))

    # 确认即锁定：状态先置 running（重复点击/重复请求直接 409，避免并行重复生成）
    store.set_progress(task_id, status="running", progress="generating_reviewing")

    # 生成前注入需求/规则库；历史用例注入评审 Agent（知识管家 F-7-6）
    knowledge, snapshot = await _gather_knowledge(
        request.app, ctx.get("requirement", ""), ("analysis", "generation"), ctx.get("knowledge_space")
    )
    memory_notes, memory_snapshot = _gather_memories(
        request.app, ctx.get("requirement", ""), ctx.get("project")
    )
    rule_notes, rule_snapshot = _gather_rules(request.app, ctx.get("project"))
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
            rule_notes=rule_notes,
        )
    except (MissingAPIKeyError, LLMOutputError) as e:
        # 可重试的故障：恢复待确认状态，用户可再次点击确认
        store.set_progress(task_id, status="awaiting_confirmation", progress=None)
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
        memories=memory_snapshot, rules=rule_snapshot,
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
    record = _task(request, task_id, "case.ai")
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
    rule_notes, rule_snapshot = _gather_rules(request.app, ctx.get("project"))
    # 使用习惯沉淀（F-8-2）：跨任务重复的修订指令固化为偏好，后续生成主动满足
    request.app.state.memory.record_usage("revision", body.instruction)
    # 已锁定用例退出 AI 修改队列（需求五十四）：不进入修订上下文，修订后原样合并回来
    all_cases: list[dict] = record.result["cases"]
    locked_uids = {uid for uid, s in record.case_reviews.items() if s.get("locked")}
    unlocked = [c for c in all_cases if str(c.get("uid") or "") not in locked_uids]
    locked = [c for c in all_cases if str(c.get("uid") or "") in locked_uids]
    try:
        result = await run_revision(
            ctx.get("requirement", ""),
            cases=unlocked,
            instruction=body.instruction,
            llm=request.app.state.llm,
            model=body.model or ctx.get("model"),
            reviewer_model=body.reviewer_model or ctx.get("reviewer_model"),
            template=template,
            test_points=(record.result or {}).get("test_points") or None,
            history=[r["instruction"] for r in record.revisions],
            knowledge_cases=knowledge["cases"],
            memory_notes=memory_notes,
            rule_notes=rule_notes,
        )
    except UnknownModelError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except (MissingAPIKeyError, AllModelsFailedError, LLMOutputError) as e:
        raise HTTPException(status_code=502, detail=str(e))

    if locked:
        from app.agents.service import _renumber
        from app.templates import TestCase as _TestCase

        result.cases.extend(_TestCase.model_validate(c) for c in locked)
        _renumber(result.cases)

    task_dir = store.output_dir / task_id
    response = _finalize_task(
        store, task_id, task_dir, record.sources, result, template,
        knowledge=record.knowledge + snapshot, memories=memory_snapshot, rules=rule_snapshot,
    )
    saved = store.get(task_id)
    saved.analysis = record.analysis
    saved.revisions = record.revisions + [
        {
            "by": _operator(request),
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


# 用例字段级驳回定位可选值（完整需求 9.2：标题/前置条件/操作步骤/预期结果/优先级/标签等）
CASE_REJECT_FIELDS = ("title", "precondition", "steps", "expected", "priority", "keywords", "module", "remark")


class ReviewItem(BaseModel):
    case_id: str
    action: str  # approve / reject / modify / delete / unlock（accept 为 approve 的兼容别名）
    case: dict | None = None  # modify 时提交修改后的完整用例
    comment: str = ""  # reject 时的驳回原因（AI 定点修改的输入）
    feedback: str = ""  # 一键反馈：问题类型或意见（学习语料）
    # 结构化驳回（完整需求 6.4/9.2）
    reject_types: list[str] = []  # 驳回类型，多选必填
    fix_request: str = ""         # 修改要求（告诉 AI 应该怎么改）
    fix_note: str = ""            # 修改备注
    fix_scope: str = ""           # 修改范围：当前项/选中项/只补遗漏/当前项重生成/整批重生成
    fields: list[str] = []        # 字段级定位：只允许 AI 修改这些字段
    steps: list[int] = []         # 步骤级定位：只允许 AI 修改第 N 步（1 起）
    base_version: int | None = None  # 乐观锁（完整需求 11 章）：modify 时基于的版本号，不一致则 409


class ReviewBody(BaseModel):
    items: list[ReviewItem]


@router.post("/api/v1/tasks/{task_id}/review")
async def review_task(request: Request, task_id: str, body: ReviewBody) -> dict:
    """在线评审（F-6-6 + 需求四十八/四十九/五十/五十四）：逐条与批量 通过/驳回/修改/删除。

    - 通过（approve）即锁定（APPROVED+LOCKED），退出 AI 修改队列；
    - 驳回（reject）必须带审核意见，供 AI 定点修改；连续驳回 2 次以上提示人工介入；
    - 人工修改（modify）视为人工定稿，直接通过并锁定；
    - 留痕（含修改前后对照与反馈）是学习 Agent 归因与 Prompt 优化的核心语料。
    """
    from app.agents import GenerationResult
    from app.agents.service import _renumber
    from app.tasks.points import (
        REJECT_HINT_THRESHOLD, ConcurrencyError, PointReviewError, check_base_version,
        clear_reject_fields, validate_rejection,
    )
    from app.templates import TestCase

    store = request.app.state.tasks
    record = _task(request, task_id, "case.review")
    if record.status != "completed" or not (record.result or {}).get("cases"):
        raise HTTPException(status_code=409, detail=f"任务状态为 {record.status}，无可评审的用例结果")

    from app.versions import case_entities, ensure_versions, record_version

    cases: list[dict] = list(record.result["cases"])
    by_id = {str(c.get("case_id")): c for c in cases}
    operator = _operator(request)
    # 存量任务打底：改动前先以当前内容补记首版（新任务已在生成时记录，此处幂等跳过）
    ensure_versions(task_id, "case", case_entities(cases), by=record.created_by)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    counts = {"approve": 0, "reject": 0, "modify": 0, "delete": 0, "unlock": 0}
    hints: list[str] = []
    deleted_any = False
    for item in body.items:
        action = "approve" if item.action == "accept" else item.action
        origin = by_id.get(item.case_id)
        if origin is None:
            raise HTTPException(status_code=400, detail=f"用例不存在: {item.case_id}")
        uid = str(origin.get("uid") or "")
        state = record.case_reviews.setdefault(
            uid, {"status": "pending", "comment": "", "reject_count": 0, "locked": False}
        )
        entry: dict = {"case_id": item.case_id, "action": action, "by": _operator(request),
                       "comment": item.comment, "feedback": item.feedback, "at": now}
        if action == "approve":
            state.update(status="approved", locked=True)
            clear_reject_fields(state)
            state.update(fields=[], steps=[])
            # 版本历史：评审通过即记终稿版本（需求 10.1）
            record_version(task_id, "case", uid, "final", origin, by=operator, reason="评审通过")
        elif action == "reject":
            try:
                rejection = validate_rejection(
                    {**item.model_dump(), "comment": item.comment.strip() or item.feedback.strip()},
                    f"用例 {item.case_id}",
                )
            except PointReviewError as e:
                raise HTTPException(status_code=400, detail=str(e))
            bad_fields = [f for f in item.fields if f not in CASE_REJECT_FIELDS]
            if bad_fields:
                raise HTTPException(
                    status_code=400,
                    detail=f"驳回用例 {item.case_id} 的指定字段不合法: {'、'.join(bad_fields)}"
                           f"（可用 {'/'.join(CASE_REJECT_FIELDS)}）",
                )
            step_total = len(origin.get("steps") or [])
            bad_steps = [n for n in item.steps if n < 1 or n > step_total]
            if bad_steps:
                raise HTTPException(
                    status_code=400,
                    detail=f"驳回用例 {item.case_id} 的指定步骤越界: {bad_steps}（共 {step_total} 步）",
                )
            state.update(status="rejected", locked=False, **rejection,
                         fields=list(item.fields), steps=list(item.steps))
            entry.update(rejection, fields=list(item.fields), steps=list(item.steps))
            state["reject_count"] = int(state.get("reject_count", 0)) + 1
            if state["reject_count"] >= REJECT_HINT_THRESHOLD:
                hints.append(
                    f"{item.case_id} 已连续 {state['reject_count']} 次未通过审核，"
                    "建议检查：1) 需求是否存在歧义 2) 是否需要人工直接修改 3) 是否需要补充需求信息"
                )
        elif action == "delete":
            entry["before"] = origin
            # 逻辑删除（需求 14.4）：完整快照移入回收站，可恢复；管理员永久删除才抹掉
            from app.recycle import add_to_bin
            add_to_bin(task_id, "case", uid, f"{item.case_id} {origin.get('title', '')}",
                       {"case": origin, "review": dict(state)}, by=operator)
            cases.remove(origin)
            record.case_reviews.pop(uid, None)
            deleted_any = True
        elif action == "modify":
            if not item.case:
                raise HTTPException(status_code=400, detail=f"修改操作需提交 case 字段: {item.case_id}")
            try:  # 乐观锁（需求 11）：并发修改冲突拦截
                check_base_version(item.model_dump(), origin.get("version", 1), f"用例 {item.case_id}")
            except ConcurrencyError as e:
                raise HTTPException(status_code=409, detail=str(e))
            try:
                updated = TestCase.model_validate({**item.case, "uid": uid}).model_dump()
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"修改后的用例不合法: {e}")
            updated["version"] = int(origin.get("version", 1)) + 1
            entry["before"], entry["after"] = origin, updated
            cases[cases.index(origin)] = updated
            by_id[item.case_id] = updated
            record_version(task_id, "case", uid, "manual", updated, by=operator,
                           reason=item.feedback.strip() or item.comment.strip() or "人工修改")
            # 人工定稿即通过并锁定（人工修改优于 AI 再改）
            state.update(status="approved", locked=True)
            clear_reject_fields(state)
            state.update(fields=[], steps=[])
        elif action == "unlock":
            state.update(status="pending", locked=False)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"未知评审操作: {action}（可用 approve/reject/modify/delete/unlock）",
            )
        counts[action] += 1
        record.review_log.append(entry)

    logger.info(
        "任务 {} 在线评审：通过 {} / 驳回 {} / 修改 {} / 删除 {} / 解锁 {}",
        task_id, counts["approve"], counts["reject"], counts["modify"], counts["delete"], counts["unlock"],
    )
    result = GenerationResult.model_validate({**record.result, "cases": cases})
    if deleted_any:  # 删除后重排各模块编号（审核状态跟随 uid，不受编号重排影响）
        _renumber(result.cases)
    task_dir = store.output_dir / task_id
    response = _finalize_task(store, task_id, task_dir, record.sources, result,
                              request.app.state.templates.get((record.context or {}).get("template_id")))
    response["review"] = {**counts, "log_entries": len(record.review_log), "hints": hints}
    return response


# ---- 测试点审核工作台（生成质量核心需求 · 四十六~五十六）----


class PointReviewItem(BaseModel):
    tp_id: str
    action: str  # approve / reject / modify / delete / unlock
    point: str | None = None       # modify 时的新描述
    dimension: str | None = None
    comment: str = ""              # reject 时的驳回原因
    # 结构化驳回（完整需求 6.4）
    reject_types: list[str] = []   # 驳回类型，多选必填
    fix_request: str = ""          # 修改要求
    fix_note: str = ""             # 修改备注
    fix_scope: str = ""            # 修改范围
    base_version: int | None = None  # 乐观锁（完整需求 11 章）：modify 时基于的版本号


class PointReviewBody(BaseModel):
    items: list[PointReviewItem]


def _points_record(request: Request, task_id: str, action: str):
    record = _task(request, task_id, action)
    modules = (record.analysis or {}).get("test_points")
    if not modules:
        raise HTTPException(status_code=409, detail="任务无测试点拆解结果")
    return record, modules


@router.post("/api/v1/tasks/{task_id}/points/review")
async def review_points(request: Request, task_id: str, body: PointReviewBody) -> dict:
    """测试点逐条/批量审核（需求四十八/四十九/五十）：✓通过（锁定）/ ✎修改 / ×驳回 / 删除。

    通过即锁定退出 AI 修改队列；驳回须带审核意见；连续驳回达阈值提示人工介入（需求三十五）。
    """
    from app.tasks.points import ConcurrencyError, PointReviewError, apply_point_review, find_point
    from app.versions import ensure_versions, point_entities, record_version

    store = request.app.state.tasks
    record, modules = _points_record(request, task_id, "point.review")
    operator = _operator(request)
    # 存量任务打底：改动前补记首版（新任务已在拆解时记录，幂等跳过）
    ensure_versions(task_id, "point", point_entities(modules), by=record.created_by)
    try:
        outcome = apply_point_review(modules, [i.model_dump() for i in body.items])
    except ConcurrencyError as e:  # 乐观锁冲突（需求 11）：不落任何改动
        raise HTTPException(status_code=409, detail=str(e))
    except PointReviewError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 版本历史（需求 10.1）：人工修改记 manual，通过记 final；删除移入回收站（需求 14.4）
    from app.recycle import add_to_bin

    for entry in outcome["log"]:
        if entry["action"] == "delete":
            add_to_bin(task_id, "point", entry["tp_id"], str(entry["before"].get("point", "")),
                       {"point": entry["before"], "module": entry.get("module", "")}, by=operator)
            continue
        if entry["action"] not in ("modify", "approve"):
            continue
        found = find_point(modules, entry["tp_id"])
        if found is None:
            continue
        _, point = found
        if entry["action"] == "modify":
            record_version(task_id, "point", entry["tp_id"], "manual", dict(point),
                           by=operator, reason=entry.get("comment") or "人工修改")
        else:
            record_version(task_id, "point", entry["tp_id"], "final", dict(point),
                           by=operator, reason="评审通过")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record.point_review_log.extend(dict(e, at=now, by=operator) for e in outcome["log"])
    store.save(record)
    logger.info("任务 {} 测试点审核：{}", task_id, outcome["counts"])
    return {
        "task_id": task_id,
        "counts": outcome["counts"],
        "hints": outcome["hints"],
        "test_points": modules,
    }


@router.post("/api/v1/tasks/{task_id}/points/fix")
async def fix_points(request: Request, task_id: str) -> dict:
    """AI 定点修改被驳回测试点（需求三十~三十四 / 完整需求 7.3）。

    只输入被驳回项+结构化驳回信息+关联需求；产出**修改提案**（不直接覆盖）：
    经 /fix/confirm 逐项接受/拒绝后才落地并回到待审核。
    """
    from app.agents.quality import run_point_fix
    from app.tasks.points import rejected_points
    from app.versions import ensure_versions, point_entities

    store = request.app.state.tasks
    record, modules = _points_record(request, task_id, "point.ai")
    if record.pending_fix:
        raise HTTPException(status_code=409, detail="存在待确认的 AI 修改提案，请先接受/拒绝后再发起新修改")
    rejected = rejected_points(modules)
    if not rejected:
        raise HTTPException(status_code=409, detail="没有被驳回的测试点，无需修改")
    operator = _operator(request)
    ensure_versions(task_id, "point", point_entities(modules), by=record.created_by)  # 存量打底
    ctx = record.context or {}
    try:
        outcome = await run_point_fix(
            request.app.state.llm, ctx.get("requirement", ""), modules, rejected, ctx.get("model")
        )
    except (MissingAPIKeyError, AllModelsFailedError, LLMOutputError) as e:
        raise HTTPException(status_code=502, detail=str(e))
    if not outcome["proposals"]:
        return {"task_id": task_id, "pending_fix": None, "message": "AI 未产出有效修改提案"}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record.pending_fix = {"kind": "points", "at": now, "by": operator,
                          "proposals": outcome["proposals"], "model_name": outcome.get("model_name")}
    store.save(record)
    logger.info("任务 {} 测试点定点修改：产出提案 {} 条（待确认）", task_id, len(outcome["proposals"]))
    return {"task_id": task_id, "pending_fix": record.pending_fix}


class PointAddBody(BaseModel):
    points: list[dict] | None = None   # 手工新增：[{module, point, dimension?}]
    instruction: str | None = None     # AI 补充：如「补充网络切换场景」


@router.post("/api/v1/tasks/{task_id}/points/add")
async def add_task_points(request: Request, task_id: str, body: PointAddBody) -> dict:
    """审核人主动补充测试点（需求五十六）：手工直接新增，或让 AI 只生成新增内容。"""
    from app.agents.quality import _points_view
    from app.agents.graph import _chat_json
    from app.tasks.points import add_points

    store = request.app.state.tasks
    record, modules = _points_record(request, task_id, "point.edit")
    if body.points:
        added = add_points(modules, body.points, source="manual")
    elif body.instruction and body.instruction.strip():
        ctx = record.context or {}
        import json as _json

        prompt = (
            f"需求内容：\n{ctx.get('requirement', '')}\n\n"
            f"已有测试点：\n{_json.dumps(_points_view(modules), ensure_ascii=False, indent=1)}\n\n"
            f"用户补充要求：{body.instruction.strip()}\n\n"
            "只生成满足补充要求的**新增**测试点，不要复述或修改已有测试点。"
            '只输出 JSON：{"additions": [{"module": "模块名", "point": "测试点描述", "dimension": "维度"}]}'
        )
        try:
            data, _ = await _chat_json(request.app.state.llm, [
                {"role": "system", "content": "你是资深测试分析师，负责按用户要求补充测试点。一个测试点对应一个明确验证目标。"},
                {"role": "user", "content": prompt},
            ], ctx.get("model"))
        except (MissingAPIKeyError, AllModelsFailedError, LLMOutputError) as e:
            raise HTTPException(status_code=502, detail=str(e))
        added = add_points(
            modules,
            [a for a in data.get("additions", []) if isinstance(a, dict)],
            source="supplement",
        )
    else:
        raise HTTPException(status_code=400, detail="请提供 points（手工新增）或 instruction（AI 补充）")
    store.save(record)
    return {"task_id": task_id, "added": added, "test_points": modules}


@router.post("/api/v1/tasks/{task_id}/points/gap-check")
async def gap_check_points(request: Request, task_id: str) -> dict:
    """查漏补缺（需求七~十/五十七）：独立查漏 Agent 输出覆盖矩阵，只新增不改存量。"""
    from app.agents.quality import run_gap_check
    from app.tasks.points import add_points

    store = request.app.state.tasks
    record, modules = _points_record(request, task_id, "point.ai")
    ctx = record.context or {}
    try:
        gap = await run_gap_check(
            request.app.state.llm, ctx.get("requirement", ""), modules, ctx.get("model")
        )
    except (MissingAPIKeyError, AllModelsFailedError, LLMOutputError) as e:
        raise HTTPException(status_code=502, detail=str(e))
    record.coverage = gap["coverage"]
    added = add_points(
        modules,
        [{"module": a.get("module", ""), "point": a.get("point", ""),
          "dimension": a.get("dimension", "")} for a in gap["additions"]],
        source="gap",
    )
    store.save(record)
    return {"task_id": task_id, "coverage": gap["coverage"], "added": added, "test_points": modules}


@router.post("/api/v1/tasks/{task_id}/points/dup-check")
async def dup_check_points(request: Request, task_id: str) -> dict:
    """重复检查（需求十一~十三）：文字初筛 + 语义复核；AI 不删除，人工决定处置。"""
    from app.agents.quality import run_dup_judge
    from app.tasks.points import duplicate_candidates

    store = request.app.state.tasks
    record, modules = _points_record(request, task_id, "point.ai")
    ctx = record.context or {}
    pairs = duplicate_candidates(modules)
    judged = await run_dup_judge(request.app.state.llm, ctx.get("requirement", ""), pairs, ctx.get("model"))
    record.dup_report = {"points": judged, "resolved": (record.dup_report or {}).get("resolved", [])}
    store.save(record)
    return {"task_id": task_id, "duplicates": judged}


# ---- 用例定点修改（需求三十~三十四/五十五）----


@router.post("/api/v1/tasks/{task_id}/cases/fix")
async def fix_cases(request: Request, task_id: str) -> dict:
    """AI 定点修改被驳回用例（完整需求 9.4）：只输入被驳回用例+结构化驳回信息；
    锁定用例确定性保护；产出**修改提案**，经 /fix/confirm 接受后才落地。"""
    from app.agents.quality import run_case_fix
    from app.versions import case_entities, ensure_versions

    store = request.app.state.tasks
    record = _task(request, task_id, "case.ai")
    if record.status != "completed" or not (record.result or {}).get("cases"):
        raise HTTPException(status_code=409, detail=f"任务状态为 {record.status}，无可修改的用例结果")
    if record.pending_fix:
        raise HTTPException(status_code=409, detail="存在待确认的 AI 修改提案，请先接受/拒绝后再发起新修改")
    if not any(s.get("status") == "rejected" for s in record.case_reviews.values()):
        raise HTTPException(status_code=409, detail="没有被驳回的用例，无需修改")
    operator = _operator(request)
    ensure_versions(task_id, "case", case_entities(record.result["cases"]),
                    by=record.created_by)  # 存量打底
    ctx = record.context or {}
    template = request.app.state.templates.get(ctx.get("template_id"))
    try:
        outcome = await run_case_fix(
            request.app.state.llm,
            ctx.get("requirement", ""),
            record.result["cases"],
            record.case_reviews,
            template,
            ctx.get("model"),
        )
    except (MissingAPIKeyError, AllModelsFailedError, LLMOutputError) as e:
        raise HTTPException(status_code=502, detail=str(e))
    if not outcome["proposals"]:
        return {"task_id": task_id, "pending_fix": None, "invalid": outcome.get("invalid", []),
                "message": "AI 未产出有效修改提案"}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record.pending_fix = {"kind": "cases", "at": now, "by": operator,
                          "proposals": outcome["proposals"], "invalid": outcome.get("invalid", []),
                          "model_name": outcome.get("model_name")}
    store.save(record)
    logger.info("任务 {} 用例定点修改：产出提案 {} 条（待确认）", task_id, len(outcome["proposals"]))
    return {"task_id": task_id, "pending_fix": record.pending_fix}


class FixDecision(BaseModel):
    proposal_id: str
    decision: str  # accept / reject


class FixConfirmBody(BaseModel):
    decisions: list[FixDecision] = []
    accept_all: bool = False


@router.post("/api/v1/tasks/{task_id}/fix/confirm")
async def confirm_fix(request: Request, task_id: str, body: FixConfirmBody) -> dict:
    """确认 AI 修改提案（完整需求 7.3/9.4 确认流）。

    接受项落地：记 ai_fix 版本、实体回待评审重新提交；未接受项一律视为拒绝丢弃
    （被驳回状态保留，可继续 AI 优化或人工编辑）。
    """
    from app.agents import GenerationResult
    from app.agents.quality import apply_case_proposals, apply_point_proposals
    from app.tasks.points import find_point
    from app.versions import ensure_versions, point_entities, record_version

    store = request.app.state.tasks
    record = _task(request, task_id, "case.edit")
    pf = record.pending_fix
    if not pf:
        raise HTTPException(status_code=409, detail="没有待确认的 AI 修改提案")
    decided = {d.proposal_id: d.decision for d in body.decisions}
    accepted = [p for p in pf["proposals"]
                if body.accept_all or decided.get(p["proposal_id"]) == "accept"]
    rejected_count = len(pf["proposals"]) - len(accepted)
    operator = _operator(request)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record.pending_fix = None

    if not accepted:
        record.fix_log.append({"kind": pf["kind"], "at": now, "by": operator, "diff": [],
                               "rejected_proposals": rejected_count})
        store.save(record)
        logger.info("任务 {} AI 修改提案全部拒绝（{} 条）", task_id, rejected_count)
        return {"task_id": task_id, "applied": 0, "rejected": rejected_count}

    from app.recycle import add_to_bin

    if pf["kind"] == "points":
        modules = (record.analysis or {}).get("test_points")
        if not modules:
            raise HTTPException(status_code=409, detail="任务无测试点拆解结果")
        # 接受的删除提案：应用前把完整快照移入回收站（需求 14.4）
        for p in accepted:
            if p.get("action") == "delete" and p.get("tp_id"):
                found = find_point(modules, p["tp_id"])
                if found is not None:
                    add_to_bin(task_id, "point", p["tp_id"], str(found[1].get("point", "")),
                               {"point": dict(found[1]), "module": found[0].get("module", "")},
                               by=operator)
        outcome = apply_point_proposals(modules, accepted)
        # 版本历史：接受的修改记 ai_fix；拆分/新增的新点以 ai_fix 入册首版
        for d in outcome["diff"]:
            if d.get("action") == "modify":
                found = find_point(modules, d["tp_id"])
                if found is not None:
                    record_version(task_id, "point", d["tp_id"], "ai_fix", dict(found[1]),
                                   by=operator, reason=d.get("comment") or "AI 定点修改")
        ensure_versions(task_id, "point", point_entities(modules),
                        by=operator, source="ai_fix", reason="AI 拆分/新增")
        record.fix_log.append({"kind": "points", "at": now, "by": operator, "diff": outcome["diff"],
                               "added": outcome["added"], "rejected_proposals": rejected_count})
        store.save(record)
        logger.info("任务 {} 测试点提案确认：应用 {} 条 / 拒绝 {} 条", task_id, len(accepted), rejected_count)
        return {"task_id": task_id, "applied": len(accepted), "rejected": rejected_count,
                "diff": outcome["diff"], "added": outcome["added"], "test_points": modules}

    # kind == cases
    for p in accepted:  # 接受的删除提案：快照入回收站（需求 14.4）
        if p.get("action") == "delete" and p.get("uid"):
            add_to_bin(task_id, "case", p["uid"],
                       f"{p.get('case_id', '')} {(p.get('before') or {}).get('title', '')}",
                       {"case": {**(p.get("before") or {}), "uid": p["uid"]},
                        "review": dict(record.case_reviews.get(p["uid"]) or {})},
                       by=operator)
    merged = apply_case_proposals(list(record.result["cases"]), accepted, record.case_reviews)
    modified_ids = {p["case_id"] for p in accepted if p["action"] == "modify"}
    for c in merged["cases"]:
        if str(c.get("case_id")) in modified_ids and c.get("uid"):
            record_version(task_id, "case", str(c["uid"]), "ai_fix", dict(c),
                           by=operator, reason="AI 定点修改")
    record.fix_log.append({"kind": "cases", "at": now, "by": operator, "diff": merged["diff"],
                           "rejected_proposals": rejected_count})
    result = GenerationResult.model_validate({**record.result, "cases": merged["cases"]})
    template = request.app.state.templates.get((record.context or {}).get("template_id"))
    response = _finalize_task(store, task_id, store.output_dir / task_id, record.sources, result, template)
    response.update(applied=len(accepted), rejected=rejected_count, diff=merged["diff"])
    logger.info("任务 {} 用例提案确认：应用 {} 条 / 拒绝 {} 条", task_id, len(accepted), rejected_count)
    return response


# ---- 版本历史与恢复（完整需求 10 章）----


@router.get("/api/v1/tasks/{task_id}/versions")
async def entity_version_chain(request: Request, task_id: str, kind: str, entity_id: str) -> dict:
    """某测试点/用例的完整版本链：版本、来源、修改人、时间、原因、字段差异。"""
    from app.versions import list_versions

    if kind not in ("point", "case"):
        raise HTTPException(status_code=400, detail="kind 须为 point 或 case")
    _task(request, task_id, "case.view")
    return {"task_id": task_id, "kind": kind, "entity_id": entity_id,
            "versions": list_versions(task_id, kind, entity_id)}


class VersionRestoreBody(BaseModel):
    kind: str        # point / case
    entity_id: str   # point: tp_id；case: uid
    version_no: int


@router.post("/api/v1/tasks/{task_id}/versions/restore")
async def restore_entity_version(request: Request, task_id: str, body: VersionRestoreBody) -> dict:
    """恢复历史版本（需求 10.1）：不覆盖历史——基于所选版本追加 manual 新版，实体回到待评审。"""
    from app.agents import GenerationResult
    from app.tasks.points import clear_reject_fields, coarse_warnings, find_point
    from app.templates import TestCase
    from app.versions import get_version, record_version

    store = request.app.state.tasks
    record = _task(request, task_id, "case.edit")
    content = get_version(task_id, body.kind, body.entity_id, body.version_no)
    if content is None:
        raise HTTPException(status_code=404, detail=f"版本不存在: {body.kind} {body.entity_id} v{body.version_no}")
    operator = _operator(request)
    reason = f"恢复自 v{body.version_no}"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if body.kind == "point":
        modules = (record.analysis or {}).get("test_points") or []
        found = find_point(modules, body.entity_id)
        if found is None:
            raise HTTPException(status_code=404, detail=f"测试点不存在: {body.entity_id}")
        _, point = found
        point["point"] = str(content.get("point", ""))
        point["dimension"] = str(content.get("dimension", ""))
        point["status"], point["locked"] = "pending", False
        point["version"] = int(point.get("version", 1)) + 1
        clear_reject_fields(point)
        point["warnings"] = coarse_warnings(point["point"])
        version_no = record_version(task_id, "point", body.entity_id, "manual", dict(point),
                                    by=operator, reason=reason)
        record.point_review_log.append(
            {"tp_id": body.entity_id, "action": "restore", "comment": reason, "at": now, "by": operator})
        store.save(record)
        logger.info("任务 {} 测试点 {} 恢复自 v{}（新版本 v{}）", task_id, body.entity_id, body.version_no, version_no)
        return {"task_id": task_id, "kind": "point", "entity_id": body.entity_id,
                "version_no": version_no, "test_points": modules}

    if body.kind != "case":
        raise HTTPException(status_code=400, detail="kind 须为 point 或 case")
    cases = list((record.result or {}).get("cases") or [])
    idx = next((i for i, c in enumerate(cases) if str(c.get("uid")) == body.entity_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail=f"用例不存在: {body.entity_id}")
    origin = cases[idx]
    restored = dict(origin)
    for field in ("module", "title", "priority", "precondition", "steps", "keywords", "remark", "extras"):
        if field in content:
            restored[field] = content[field]
    try:  # 历史内容按当前用例规范校验（case_id/uid 保持现值，编号不回退）
        restored = TestCase.model_validate({**restored, "uid": origin.get("uid")}).model_dump()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"历史版本内容不合法: {e}")
    restored["version"] = int(origin.get("version", 1)) + 1
    cases[idx] = restored
    state = record.case_reviews.setdefault(
        body.entity_id, {"status": "pending", "comment": "", "reject_count": 0, "locked": False})
    state.update(status="pending", locked=False)
    clear_reject_fields(state)
    state.update(fields=[], steps=[])
    version_no = record_version(task_id, "case", body.entity_id, "manual", restored,
                                by=operator, reason=reason)
    record.review_log.append(
        {"case_id": restored.get("case_id"), "action": "restore", "comment": reason, "at": now, "by": operator})
    result = GenerationResult.model_validate({**record.result, "cases": cases})
    response = _finalize_task(store, task_id, store.output_dir / task_id, record.sources, result,
                              request.app.state.templates.get((record.context or {}).get("template_id")))
    response["restored"] = {"kind": "case", "entity_id": body.entity_id, "version_no": version_no}
    logger.info("任务 {} 用例 {} 恢复自 v{}（新版本 v{}）", task_id, restored.get("case_id"), body.version_no, version_no)
    return response


# ---- 回收站（完整需求 14.4）----


@router.get("/api/v1/tasks/{task_id}/recycle-bin")
async def recycle_bin_list(request: Request, task_id: str) -> dict:
    """任务回收站：被删除的测试点/用例（deleted_by / deleted_at 留痕）。"""
    from app.recycle import list_bin

    _task(request, task_id, "case.view")
    return {"task_id": task_id, "items": list_bin(task_id)}


class RecycleRestoreBody(BaseModel):
    item_id: int


@router.post("/api/v1/tasks/{task_id}/recycle-bin/restore")
async def recycle_bin_restore(request: Request, task_id: str, body: RecycleRestoreBody) -> dict:
    """从回收站恢复：放回原任务并回到待评审，记 manual 版本；回收站条目移除。"""
    from app.agents import GenerationResult
    from app.recycle import get_item, purge
    from app.tasks.points import find_point
    from app.versions import record_version

    store = request.app.state.tasks
    record = _task(request, task_id, "case.edit")
    item = get_item(task_id, body.item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"回收站条目不存在: {body.item_id}")
    operator = _operator(request)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if item["kind"] == "point":
        modules = (record.analysis or {}).get("test_points")
        if modules is None:
            raise HTTPException(status_code=409, detail="任务无测试点拆解结果")
        if find_point(modules, item["entity_id"]) is not None:
            raise HTTPException(status_code=409, detail=f"测试点 {item['entity_id']} 已存在，无法恢复")
        point = dict(item["payload"]["point"])
        point.update(status="pending", locked=False,
                     version=int(point.get("version", 1)) + 1)
        module = item["payload"].get("module") or "未分组"
        entry = next((e for e in modules if e["module"] == module), None)
        if entry is None:
            entry = {"module": module, "points": []}
            modules.append(entry)
        entry["points"].append(point)
        record_version(task_id, "point", item["entity_id"], "manual", dict(point),
                       by=operator, reason="从回收站恢复")
        record.point_review_log.append(
            {"tp_id": item["entity_id"], "action": "restore_bin", "comment": "从回收站恢复",
             "at": now, "by": operator})
        store.save(record)
        purge(task_id, body.item_id)
        return {"task_id": task_id, "restored": item["entity_id"], "test_points": modules}

    # kind == case
    cases = list((record.result or {}).get("cases") or [])
    if any(str(c.get("uid")) == item["entity_id"] for c in cases):
        raise HTTPException(status_code=409, detail="该用例已存在，无法恢复")
    case = dict(item["payload"]["case"])
    case["uid"] = item["entity_id"]
    if any(str(c.get("case_id")) == str(case.get("case_id")) for c in cases):
        # 编号已被复用（删除后重排）：按所在模块顺延新编号
        module = str(case.get("module", ""))
        seqs = [int(m.group(2)) for c in cases
                if (m := re.match(r"^(.*?)(\d+)\s*$", str(c.get("case_id", ""))))
                and str(c.get("module", "")) == module]
        prefix = re.match(r"^(.*?)(\d+)\s*$", str(case.get("case_id", "")))
        case["case_id"] = f"{prefix.group(1) if prefix else f'TC-{module}-'}{(max(seqs) if seqs else 0) + 1:03d}"
    case["version"] = int(case.get("version", 1)) + 1
    cases.append(case)
    record.case_reviews[item["entity_id"]] = {
        "status": "pending", "comment": "", "reject_count": 0, "locked": False}
    record_version(task_id, "case", item["entity_id"], "manual", case,
                   by=operator, reason="从回收站恢复")
    record.review_log.append(
        {"case_id": case.get("case_id"), "action": "restore_bin", "comment": "从回收站恢复",
         "at": now, "by": operator})
    result = GenerationResult.model_validate({**record.result, "cases": cases})
    response = _finalize_task(store, task_id, store.output_dir / task_id, record.sources, result,
                              request.app.state.templates.get((record.context or {}).get("template_id")))
    purge(task_id, body.item_id)
    response["restored"] = case.get("case_id")
    return response


@router.get("/api/v1/recycle-bin")
async def recycle_bin_by_project(request: Request, project: str) -> dict:
    """项目回收站：聚合项目下全部任务的逻辑删除条目（恢复/永久删除仍走任务级接口）。"""
    from app.recycle import list_bin_for_tasks

    _require_project(request, project, "case.view")
    task_ids = [
        r.task_id for r in request.app.state.tasks.list(limit=100000)
        if ((r.context or {}).get("project") or "（未指定）") == project
    ]
    return {"project": project, "items": list_bin_for_tasks(task_ids)}


@router.delete("/api/v1/tasks/{task_id}/recycle-bin/{item_id}")
async def recycle_bin_purge(request: Request, task_id: str, item_id: int) -> dict:
    """管理员永久删除：从回收站抹掉快照（版本历史仍留档）。"""
    from app.recycle import purge

    user = getattr(request.state, "user", None)
    if user is not None and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可永久删除")
    _task(request, task_id, "case.view")
    if not purge(task_id, item_id):
        raise HTTPException(status_code=404, detail=f"回收站条目不存在: {item_id}")
    logger.info("任务 {} 回收站条目 {} 已被 {} 永久删除", task_id, item_id, _operator(request))
    return {"task_id": task_id, "purged": item_id}


# ---- 需求变更差异分析（需求四十~四十五）----


class RequirementDiffBody(BaseModel):
    new_requirement: str


@router.post("/api/v1/tasks/{task_id}/requirement-diff")
async def requirement_diff(request: Request, task_id: str, body: RequirementDiffBody) -> dict:
    """需求变更差异分析：识别变化类型（文案/规则/新增/删除），标记受影响测试点与用例。

    只分析不改动；删除类需求对应资产仅标记受影响，由人工决定保留/作废（需求四十五）。
    """
    from app.agents.quality import run_requirement_diff

    store = request.app.state.tasks
    record = _task(request, task_id, "point.ai")
    if not body.new_requirement.strip():
        raise HTTPException(status_code=400, detail="新版需求内容不能为空")
    ctx = record.context or {}
    modules = (record.analysis or {}).get("test_points", [])
    cases = (record.result or {}).get("cases", [])
    try:
        diff = await run_requirement_diff(
            request.app.state.llm, ctx.get("requirement", ""), body.new_requirement,
            modules, cases, ctx.get("model"),
        )
    except (MissingAPIKeyError, AllModelsFailedError, LLMOutputError) as e:
        raise HTTPException(status_code=502, detail=str(e))
    record.requirement_diff = {
        **diff,
        "new_requirement": body.new_requirement,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "applied": False,
    }
    store.save(record)
    return {"task_id": task_id, **diff}


@router.post("/api/v1/tasks/{task_id}/requirement-diff/apply")
async def apply_requirement_diff(request: Request, task_id: str) -> dict:
    """应用最小范围更新（需求四十三/四十四）：只修改受影响用例，新增需求只生成新增内容。

    受影响用例走定点修正创建新版本（原版本留痕于 Diff）；未受影响资产不动；
    删除类变更不自动删除任何资产。更新后的用例回到待审核状态。
    """
    from app.agents.quality import merge_case_fix
    from app.agents import GenerationResult
    from app.agents.graph import _chat_json
    from app.agents.prompts import CASE_FIX_SYSTEM

    store = request.app.state.tasks
    record = _task(request, task_id, "point.review")
    rdiff = record.requirement_diff
    if not rdiff:
        raise HTTPException(status_code=409, detail="请先执行需求差异分析")
    if rdiff.get("applied"):
        raise HTTPException(status_code=409, detail="该需求变更已应用，请重新执行差异分析后再应用")
    ctx = record.context or {}
    cases: list[dict] = (record.result or {}).get("cases", [])
    template = request.app.state.templates.get(ctx.get("template_id"))
    affected_ids = {
        cid for ch in rdiff.get("changes", [])
        if ch.get("type") not in ("删除", "无变化")
        for cid in ch.get("affected_cases", [])
    }
    new_req = rdiff.get("new_requirement", "")
    instructions = [
        {"变更": ch.get("description", ""), "类型": ch.get("type", ""),
         "受影响用例": ch.get("affected_cases", []), "处理建议": ch.get("action_hint", "")}
        for ch in rdiff.get("changes", []) if ch.get("type") not in ("删除", "无变化")
    ]
    diff_out: list[dict] = []
    if affected_ids or rdiff.get("new_requirements"):
        import json as _json

        affected = [c for c in cases if str(c.get("case_id")) in affected_ids]
        payload = {
            "新版需求": new_req,
            "变更清单": instructions,
            "新增需求": rdiff.get("new_requirements", []),
            "受影响用例": [{k: v for k, v in c.items() if k != "uid"} for c in affected],
        }
        system = CASE_FIX_SYSTEM.format(template_spec=(template.prompt_spec() if template else ""))
        try:
            data, _ = await _chat_json(request.app.state.llm, [
                {"role": "system", "content": system},
                {"role": "user", "content": (
                    "需求发生变更，请按新版需求**只更新下列受影响用例**（创建新版本），"
                    "并为「新增需求」生成新用例；其余用例系统已锁定不可改动。\n\n"
                    + _json.dumps(payload, ensure_ascii=False, indent=1)
                )},
            ], ctx.get("model"))
        except (MissingAPIKeyError, AllModelsFailedError, LLMOutputError) as e:
            raise HTTPException(status_code=502, detail=str(e))
        outcome = merge_case_fix(cases, data, affected_ids, record.case_reviews)
        cases = outcome["cases"]
        diff_out = outcome["diff"]
    # 删除类变更：仅标记受影响，人工决定（需求四十五）
    removed_marks = [
        {"type": "删除", "description": ch.get("description", ""),
         "affected_cases": ch.get("affected_cases", []), "note": "仅标记受影响，请人工决定保留/作废"}
        for ch in rdiff.get("changes", []) if ch.get("type") == "删除"
    ]
    # 需求基线更新为新版，后续修订/定点修改以新版为准
    ctx["requirement"] = new_req or ctx.get("requirement", "")
    record.context = ctx
    rdiff["applied"] = True
    rdiff["applied_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record.fix_log.append({"kind": "requirement_change", "at": rdiff["applied_at"], "diff": diff_out})
    result = GenerationResult.model_validate({**record.result, "cases": cases})
    task_dir = store.output_dir / task_id
    response = _finalize_task(store, task_id, task_dir, record.sources, result, template)
    response["diff"] = diff_out
    response["removed_marks"] = removed_marks
    logger.info("任务 {} 需求变更最小范围更新：改动 {} 条 / 删除标记 {} 组", task_id, len(diff_out), len(removed_marks))
    return response


# ---- 用例执行（执行轮次 + 执行记录留痕）----

EXEC_STATUSES = ("pass", "fail", "blocked", "skipped")


def _exec_task(request: Request, task_id: str):
    record = _task(request, task_id, "exec.run")
    if record.exec_migrated_to:
        raise HTTPException(
            status_code=409,
            detail=f"该任务的执行已迁移至测试计划（{record.exec_migrated_to}），请在测试计划中执行",
        )
    if record.status != "completed" or not (record.result or {}).get("cases"):
        raise HTTPException(status_code=409, detail=f"任务状态为 {record.status}，无可执行的用例")
    return record


def _exec_run(record, run_id: str) -> dict:
    run = next((r for r in record.executions if r["run_id"] == run_id), None)
    if run is None:
        raise HTTPException(status_code=404, detail=f"执行轮次不存在: {run_id}")
    return run


def _run_summary(record, run: dict) -> dict:
    total = len((record.result or {}).get("cases", []))
    counts = {s: 0 for s in EXEC_STATUSES}
    for r in run["results"].values():
        if r["status"] in counts:
            counts[r["status"]] += 1
    executed = sum(counts.values())
    return {
        **counts, "executed": executed, "total": total,
        "pass_rate": round(counts["pass"] / executed, 3) if executed else None,
    }


class ExecRunBody(BaseModel):
    name: str | None = None


@router.post("/api/v1/tasks/{task_id}/executions")
async def create_execution_run(request: Request, task_id: str, body: ExecRunBody | None = None) -> dict:
    """新建执行轮次：一次完整的用例执行（冒烟/回归各开一轮，记录互不覆盖）。"""
    import uuid

    store = request.app.state.tasks
    record = _exec_task(request, task_id)
    if any(not r.get("finished_at") for r in record.executions):
        raise HTTPException(status_code=409, detail="存在未结束的执行轮次，请先结束后再新建")
    run = {
        "run_id": uuid.uuid4().hex[:8],
        "name": ((body.name if body else None) or f"第 {len(record.executions) + 1} 轮执行").strip(),
        "by": _operator(request),
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "finished_at": None,
        "results": {},  # case_uid -> {case_id, title, status, note, by, at}
    }
    record.executions.append(run)
    store.save(record)
    logger.info("任务 {} 新建执行轮次 {}（{}）", task_id, run["run_id"], run["name"])
    return {**run, "summary": _run_summary(record, run)}


class ExecResultItem(BaseModel):
    case_id: str
    status: str  # pass / fail / blocked / skipped
    note: str = ""  # 失败原因 / 缺陷号 / 阻塞说明


class ExecResultsBody(BaseModel):
    items: list[ExecResultItem]


@router.post("/api/v1/tasks/{task_id}/executions/{run_id}/results")
async def record_execution_results(
    request: Request, task_id: str, run_id: str, body: ExecResultsBody
) -> dict:
    """记录执行结果（支持批量）：同轮次内重复执行覆盖并保留历史（history）。"""
    store = request.app.state.tasks
    record = _exec_task(request, task_id)
    run = _exec_run(record, run_id)
    if run.get("finished_at"):
        raise HTTPException(status_code=409, detail="该执行轮次已结束，如需继续执行请新建轮次")
    by_id = {str(c.get("case_id")): c for c in record.result["cases"]}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for item in body.items:
        case = by_id.get(item.case_id)
        if case is None:
            raise HTTPException(status_code=400, detail=f"用例不存在: {item.case_id}")
        if item.status not in EXEC_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"未知执行状态: {item.status}（可用 {'/'.join(EXEC_STATUSES)}）",
            )
        if item.status in ("fail", "blocked") and not item.note.strip():
            raise HTTPException(status_code=400, detail=f"{item.case_id} 标记{'失败' if item.status=='fail' else '阻塞'}须填写原因/缺陷号")
        uid = str(case.get("uid") or case.get("case_id"))
        prev = run["results"].get(uid)
        entry = {
            "case_id": item.case_id, "title": case.get("title", ""),
            "status": item.status, "note": item.note.strip(),
            "by": _operator(request), "at": now,
            "history": (prev.get("history", []) + [
                {k: prev[k] for k in ("status", "note", "by", "at")}
            ]) if prev else [],
        }
        run["results"][uid] = entry
    store.save(record)
    summary = _run_summary(record, run)
    logger.info("任务 {} 轮次 {} 记录执行 {} 条（{}）", task_id, run_id, len(body.items), summary)
    return {"run_id": run_id, "summary": summary, "results": run["results"]}


@router.post("/api/v1/tasks/{task_id}/executions/{run_id}/finish")
async def finish_execution_run(request: Request, task_id: str, run_id: str) -> dict:
    """结束执行轮次：定格记录；未执行用例保持未执行状态留痕。"""
    store = request.app.state.tasks
    record = _exec_task(request, task_id)
    run = _exec_run(record, run_id)
    if run.get("finished_at"):
        raise HTTPException(status_code=409, detail="该执行轮次已结束")
    run["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store.save(record)
    summary = _run_summary(record, run)
    logger.info("任务 {} 轮次 {} 已结束：{}", task_id, run_id, summary)
    return {"run_id": run_id, "finished_at": run["finished_at"], "summary": summary}


# ---- 测试计划（完整需求 12/13 章 · M4）：计划实体 + 用例快照 + 分配 + 计划执行 ----


def _approved_cases(record) -> list[dict]:
    """任务中「已通过」的用例（12.2：计划只能加入评审通过的正式用例）。"""
    return [
        c for c in (record.result or {}).get("cases", [])
        if (record.case_reviews.get(str(c.get("uid") or "")) or {}).get("status") == "approved"
    ]


def _plan_view(plan: dict) -> dict:
    from app.plans import PLAN_STATUSES, plan_summary, run_summary

    return {
        **plan,
        "status_label": PLAN_STATUSES.get(plan["status"], plan["status"]),
        "summary": plan_summary(plan),
        "runs": [{**r, "summary": run_summary(plan, r)} for r in plan["runs"]],
    }


@router.get("/api/v1/plans")
async def list_plans(
    request: Request, project: str | None = None, mine: bool = False,
    task_id: str | None = None,
) -> dict:
    """计划列表（可按项目过滤）；mine=true 只看分配给我的；task_id 只看引用该任务快照的计划。"""
    from app.plans import PLAN_STATUSES, plan_summary

    me = _operator(request)
    if project:
        _require_project(request, project, "plan.view")
    out = []
    for plan in request.app.state.plans.list(project=project):
        if not _record_visible(request, plan.get("project")):
            continue
        task_items = [i for i in plan["items"] if i["task_id"] == task_id] if task_id else []
        if task_id and not task_items:
            continue
        my_items = [i for i in plan["items"] if i.get("assignee") == me]
        if mine and (not my_items or plan["status"] == "archived"):
            continue
        latest = plan["runs"][-1] if plan["runs"] else None
        out.append({
            "task_cases": len(task_items),
            **{k: plan[k] for k in ("plan_id", "name", "project", "owner",
                                    "start_date", "end_date", "status",
                                    "created_by", "created_at")},
            "status_label": PLAN_STATUSES.get(plan["status"], plan["status"]),
            "summary": plan_summary(plan),
            "my_pending": sum(
                1 for i in my_items
                if not latest or latest.get("finished_at")
                or i["item_id"] not in latest["results"]
            ) if my_items else 0,
            "my_items": len(my_items),
        })
    return {"plans": out}


class PlanBody(BaseModel):
    name: str
    project: str
    owner: str = ""
    start_date: str = ""
    end_date: str = ""


@router.post("/api/v1/plans")
async def create_plan(request: Request, body: PlanBody) -> dict:
    from app.plans import PlanError

    if _is_admin(request):
        request.app.state.projects.ensure([body.project], created_by=_operator(request))
    _require_project(request, body.project, "plan.manage")
    try:
        plan = request.app.state.plans.create(
            body.name, body.project, owner=body.owner,
            start_date=body.start_date, end_date=body.end_date,
            created_by=_operator(request),
        )
    except PlanError as e:
        raise HTTPException(status_code=400, detail=str(e))
    request.app.state.projects.ensure([plan["project"]], created_by=_operator(request))
    logger.info("测试计划已创建：{}（{} / {}）", plan["plan_id"], plan["name"], plan["project"])
    return _plan_view(plan)


@router.get("/api/v1/plans/{plan_id}")
async def get_plan(request: Request, plan_id: str) -> dict:
    return _plan_view(_plan(request, plan_id, "plan.view"))


class PlanUpdateBody(BaseModel):
    name: str | None = None
    owner: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    status: str | None = None


@router.put("/api/v1/plans/{plan_id}")
async def update_plan(request: Request, plan_id: str, body: PlanUpdateBody) -> dict:
    from app.plans import PlanError

    _plan(request, plan_id, "plan.manage")
    try:
        plan = request.app.state.plans.update(plan_id, body.model_dump(exclude_none=True))
    except PlanError as e:
        code = 404 if "不存在" in str(e) else 400
        raise HTTPException(status_code=code, detail=str(e))
    return _plan_view(plan)


@router.delete("/api/v1/plans/{plan_id}")
async def delete_plan(request: Request, plan_id: str) -> dict:
    from app.plans import PlanError

    plan = _plan(request, plan_id, "plan.manage")
    user = _current_user(request)
    if user["role"] != "admin" and user["username"] not in (plan["created_by"], plan["owner"]):
        raise HTTPException(status_code=403, detail="仅计划创建人/负责人或管理员可删除计划")
    try:
        request.app.state.plans.delete(plan_id)
    except PlanError as e:
        raise HTTPException(status_code=409, detail=str(e))
    logger.info("测试计划已删除：{}（{}）", plan_id, plan["name"])
    return {"deleted": plan_id}


@router.get("/api/v1/plans/{plan_id}/candidates")
async def plan_candidates(
    request: Request, plan_id: str, task_id: str | None = None,
    module: str = "", priority: str = "", keyword: str = "",
) -> dict:
    """可加入计划的用例池：不带 task_id 列出本项目下有已通过用例的任务；带则列用例（含筛选）。"""
    from app.plans import match_case

    plan = _plan(request, plan_id, "plan.view")
    store = request.app.state.tasks
    if not task_id:
        tasks = []
        for r in store.list(limit=100000):
            if ((r.context or {}).get("project") or "（未指定）") != plan["project"]:
                continue
            approved = _approved_cases(r)
            if approved:
                tasks.append({
                    "task_id": r.task_id,
                    "source": r.sources[0] if r.sources else r.task_id,
                    "created_at": r.created_at, "approved": len(approved),
                })
        return {"tasks": tasks}
    record = _task(request, task_id, "case.view")
    added = {(i["task_id"], i["uid"]) for i in plan["items"]}
    cases = []
    for c in _approved_cases(record):
        if not match_case(c, module=module, priority=priority, keyword=keyword):
            continue
        cases.append({
            "uid": str(c.get("uid") or ""), "case_id": c.get("case_id", ""),
            "version": int(c.get("version", 1) or 1),
            "title": c.get("title", ""), "module": c.get("module", ""),
            "priority": c.get("priority", ""), "keywords": c.get("keywords", ""),
            "added": (task_id, str(c.get("uid") or "")) in added,
        })
    modules = sorted({c.get("module", "") for c in _approved_cases(record)})
    return {"cases": cases, "modules": modules}


class PlanCasesBody(BaseModel):
    task_id: str
    uids: list[str] = []   # 指定加入；为空时按筛选条件全量加入
    module: str = ""
    priority: str = ""
    keyword: str = ""


@router.post("/api/v1/plans/{plan_id}/cases")
async def add_plan_cases(request: Request, plan_id: str, body: PlanCasesBody) -> dict:
    """加入计划即快照（M3 冻结约定「快照引用方式」）：正式用例后续修改不影响计划。"""
    from app.plans import match_case, snapshot_item
    from app.versions import case_entities, ensure_versions, latest_version_no

    plan = _plan(request, plan_id, "plan.manage")
    if plan["status"] == "archived":
        raise HTTPException(status_code=409, detail="计划已归档，不可再加入用例")
    record = _task(request, body.task_id, "case.view")
    approved = {str(c.get("uid") or ""): c for c in _approved_cases(record)}
    if body.uids:
        pool = []
        for uid in body.uids:
            case = approved.get(uid)
            if case is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"用例 {uid} 不是「已通过」状态，只有评审通过的用例才能加入计划",
                )
            pool.append(case)
    else:
        pool = [
            c for c in approved.values()
            if match_case(c, module=body.module, priority=body.priority, keyword=body.keyword)
        ]
    operator = _operator(request)
    # 存量任务打底：无版本记录的用例先补记首版，保证快照有版本可引用
    ensure_versions(record.task_id, "case", case_entities(list(approved.values())),
                    by=record.created_by)
    added_keys = {(i["task_id"], i["uid"]) for i in plan["items"]}
    added = []
    for case in pool:
        uid = str(case.get("uid") or "")
        if (record.task_id, uid) in added_keys:
            continue
        item = snapshot_item(
            record.task_id, case, latest_version_no(record.task_id, "case", uid), by=operator
        )
        plan["items"].append(item)
        added.append(item)
    request.app.state.plans.save(plan)
    logger.info("计划 {} 加入用例 {} 条（任务 {}）", plan_id, len(added), body.task_id)
    return {"added": len(added), "skipped": len(pool) - len(added), "plan": _plan_view(plan)}


@router.delete("/api/v1/plans/{plan_id}/cases/{item_id}")
async def remove_plan_case(request: Request, plan_id: str, item_id: str) -> dict:
    plan = _plan(request, plan_id, "plan.manage")
    item = next((i for i in plan["items"] if i["item_id"] == item_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail=f"用例不在计划中: {item_id}")
    if any(item_id in r["results"] for r in plan["runs"]):
        raise HTTPException(status_code=409, detail="该用例已有执行记录，不可从计划移除")
    plan["items"].remove(item)
    request.app.state.plans.save(plan)
    return {"removed": item_id, "plan": _plan_view(plan)}


class PlanAssignBody(BaseModel):
    assignee: str
    item_ids: list[str] = []  # 按用例分配
    module: str = ""          # 按模块分配（item_ids 为空时生效）
    expected: dict[str, str | None] | None = None  # 页面快照 item_id -> 当时执行人：多人同时分配的冲突拦截依据


def _plan_assignees(request: Request, plan: dict) -> list[dict]:
    """可被分配的执行人：计划所属项目的正常状态成员 + 系统管理员（附姓名与项目角色）。"""
    from app.permissions import PROJECT_ROLES

    auth = request.app.state.auth
    project = request.app.state.projects.get(plan.get("project") or "")
    members = dict((project or {}).get("members", {}))
    out = []
    for u in auth.list_users():
        role = members.get(u["username"])
        if role is None and u["role"] != "admin":
            continue
        if u.get("status", "active") != "active" or role == "viewer":
            continue
        out.append({"username": u["username"], "name": u.get("name", ""),
                    "role": role, "role_label": PROJECT_ROLES.get(role, "系统管理员" if role is None else role)})
    return out


@router.get("/api/v1/plans/{plan_id}/assignees")
async def list_plan_assignees(request: Request, plan_id: str) -> dict:
    """分配弹窗数据：候选执行人（项目成员）与计划内模块及其用例数。"""
    plan = _plan(request, plan_id, "plan.view")
    modules: dict[str, int] = {}
    for it in plan["items"]:
        modules[it.get("module") or ""] = modules.get(it.get("module") or "", 0) + 1
    return {"assignees": _plan_assignees(request, plan),
            "modules": [{"module": m, "count": c} for m, c in sorted(modules.items())]}


@router.post("/api/v1/plans/{plan_id}/assign")
async def assign_plan_cases(request: Request, plan_id: str, body: PlanAssignBody) -> dict:
    """任务分配（13 章）：按用例 / 按模块，重新分配留痕原执行人、新执行人与操作人。

    多人同时分配：不同用例的并发分配互不影响；同一用例被他人先行分配时不覆盖，
    以 conflicts 返回由操作人确认后再改派（重新提交时不带 expected 即为明确改派）。
    执行人限定为计划所属项目的成员（系统管理员亦可）——M1 项目成员落地后收紧。
    """
    from app.plans import PlanError, assign_items

    plan = _plan(request, plan_id, "plan.assign")
    if plan["status"] == "archived":
        raise HTTPException(status_code=409, detail="计划已归档，不可再分配")
    assignee = body.assignee.strip()
    if assignee not in {a["username"] for a in _plan_assignees(request, plan)}:
        raise HTTPException(status_code=400, detail=f"执行人不存在或不是项目成员: {assignee}")
    item_ids = body.item_ids or [
        i["item_id"] for i in plan["items"] if not body.module or i["module"] == body.module
    ]
    if not item_ids:
        raise HTTPException(status_code=400, detail="没有可分配的用例（检查模块名或选中项）")
    try:
        changed, conflicts = assign_items(
            plan, item_ids, assignee, by=_operator(request), expected=body.expected
        )
    except PlanError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if changed:
        request.app.state.plans.save(plan)
    logger.info("计划 {} 分配 {} 条用例给 {}（操作人 {}，冲突跳过 {} 条）",
                plan_id, len(changed), assignee, _operator(request), len(conflicts))
    return {"assigned": len(changed), "conflicts": conflicts, "plan": _plan_view(plan)}


# ---- 计划执行（执行轮次挂计划）与执行附件 ----

# 附件类型白名单（13.4：图片/视频/日志/压缩包）
_ATTACHMENT_SUFFIXES = {
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"},
    "video": {".mp4", ".mov", ".avi", ".mkv", ".webm"},
    "log": {".log", ".txt", ".json", ".xml", ".har"},
    "archive": {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"},
}


class PlanRunBody(BaseModel):
    name: str = ""


@router.post("/api/v1/plans/{plan_id}/runs")
async def create_plan_run(request: Request, plan_id: str, body: PlanRunBody | None = None) -> dict:
    from app.plans import PlanError, new_run, run_summary

    plan = _plan(request, plan_id, "exec.run")
    if plan["status"] == "archived":
        raise HTTPException(status_code=409, detail="计划已归档，不可再执行")
    try:
        run = new_run(plan, (body.name if body else ""), by=_operator(request))
    except PlanError as e:
        raise HTTPException(status_code=409, detail=str(e))
    request.app.state.plans.save(plan)
    logger.info("计划 {} 新建执行轮次 {}（{}）", plan_id, run["run_id"], run["name"])
    return {**run, "summary": run_summary(plan, run)}


def _plan_run(plan: dict, run_id: str) -> dict:
    from app.plans import get_run

    run = get_run(plan, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"执行轮次不存在: {run_id}")
    return run


class PlanExecItem(BaseModel):
    item_id: str
    status: str  # pass / fail / blocked / skipped
    note: str = ""
    reason: str = ""  # 失败分类（status=fail 必选）：用例问题类失败进入提示词优化学习语料


class PlanExecBody(BaseModel):
    items: list[PlanExecItem]


@router.post("/api/v1/plans/{plan_id}/runs/{run_id}/results")
async def record_plan_results(
    request: Request, plan_id: str, run_id: str, body: PlanExecBody
) -> dict:
    from app.plans import EXEC_STATUSES as PLAN_EXEC_STATUSES
    from app.plans import FAIL_REASONS, run_summary

    plan = _plan(request, plan_id, "exec.run")
    run = _plan_run(plan, run_id)
    if run.get("finished_at"):
        raise HTTPException(status_code=409, detail="该执行轮次已结束，如需继续执行请新建轮次")
    by_id = {i["item_id"]: i for i in plan["items"]}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    operator = _operator(request)
    for entry in body.items:
        item = by_id.get(entry.item_id)
        if item is None:
            raise HTTPException(status_code=400, detail=f"用例不在计划中: {entry.item_id}")
        if entry.status not in PLAN_EXEC_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"未知执行状态: {entry.status}（可用 {'/'.join(PLAN_EXEC_STATUSES)}）",
            )
        if entry.status in ("fail", "blocked") and not entry.note.strip():
            raise HTTPException(
                status_code=400,
                detail=f"{item['case_id']} 标记{'失败' if entry.status == 'fail' else '阻塞'}须填写原因/缺陷号",
            )
        if entry.status == "fail" and entry.reason not in FAIL_REASONS:
            raise HTTPException(
                status_code=400,
                detail=f"{item['case_id']} 标记失败须选择失败分类（可用 {'/'.join(FAIL_REASONS)}）",
            )
        prev = run["results"].get(entry.item_id)
        run["results"][entry.item_id] = {
            "case_id": item["case_id"], "title": item["title"],
            "status": entry.status, "note": entry.note.strip(),
            "reason": entry.reason if entry.status == "fail" else "",
            "by": operator, "at": now,
            "history": (prev.get("history", []) + [
                {k: prev.get(k, "") for k in ("status", "note", "reason", "by", "at")}
            ]) if prev else [],
        }
    request.app.state.plans.save(plan)
    summary = run_summary(plan, run)
    logger.info("计划 {} 轮次 {} 记录执行 {} 条（{}）", plan_id, run_id, len(body.items), summary)
    return {"run_id": run_id, "summary": summary, "results": run["results"]}


@router.post("/api/v1/plans/{plan_id}/runs/{run_id}/finish")
async def finish_plan_run(request: Request, plan_id: str, run_id: str) -> dict:
    from app.plans import run_summary

    plan = _plan(request, plan_id, "exec.run")
    run = _plan_run(plan, run_id)
    if run.get("finished_at"):
        raise HTTPException(status_code=409, detail="该执行轮次已结束")
    run["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    request.app.state.plans.save(plan)
    summary = run_summary(plan, run)
    logger.info("计划 {} 轮次 {} 已结束：{}", plan_id, run_id, summary)
    return {"run_id": run_id, "finished_at": run["finished_at"], "summary": summary}


@router.post("/api/v1/plans/{plan_id}/runs/{run_id}/attachments")
async def upload_plan_attachment(
    request: Request, plan_id: str, run_id: str,
    file: UploadFile = File(...), item_id: str = Form(""),
) -> dict:
    """执行附件（13.4）：图片/视频/日志/压缩包，记录上传人、时间与关联执行记录。"""
    import uuid as _uuid

    plan = _plan(request, plan_id, "exec.attach")
    run = _plan_run(plan, run_id)
    suffix = Path(file.filename or "").suffix.lower()
    kind = next((k for k, s in _ATTACHMENT_SUFFIXES.items() if suffix in s), None)
    if kind is None:
        allowed = "、".join(sorted(s for v in _ATTACHMENT_SUFFIXES.values() for s in v))
        raise HTTPException(status_code=400, detail=f"不支持的附件类型 {suffix or '（无后缀）'}（可用 {allowed}）")
    if item_id and not any(i["item_id"] == item_id for i in plan["items"]):
        raise HTTPException(status_code=400, detail=f"用例不在计划中: {item_id}")
    att_id = _uuid.uuid4().hex[:12]
    dest_dir = get_settings().outputs_dir / "attachments" / plan_id / run_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{att_id}{suffix}"
    content = await file.read()
    dest.write_bytes(content)
    att = {
        "att_id": att_id, "item_id": item_id or None, "kind": kind,
        "filename": file.filename, "stored": str(dest),
        "content_type": file.content_type, "size": len(content),
        "by": _operator(request),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    run["attachments"].append(att)
    request.app.state.plans.save(plan)
    logger.info("计划 {} 轮次 {} 上传附件 {}（{}，{} 字节）",
                plan_id, run_id, file.filename, kind, len(content))
    return att


@router.get("/api/v1/plans/{plan_id}/attachments/{att_id}")
async def download_plan_attachment(request: Request, plan_id: str, att_id: str) -> FileResponse:
    plan = _plan(request, plan_id, "exec.view")
    for run in plan["runs"]:
        for att in run.get("attachments", []):
            if att["att_id"] == att_id:
                path = Path(att["stored"])
                if not path.exists():
                    raise HTTPException(status_code=404, detail="附件文件已不存在")
                return FileResponse(path, media_type=att.get("content_type"),
                                    filename=att.get("filename"))
    raise HTTPException(status_code=404, detail=f"附件不存在: {att_id}")


# ---- 项目视角（项目管理信息架构 / 完整需求 3.2 多项目管理）----


_EMPTY_STATS = {"tasks": 0, "cases": 0, "pending": 0, "rejected": 0,
                "approved": 0, "executed": 0, "exec_pass": 0, "last_activity": ""}


def _project_view(request: Request, p: dict, stats: dict | None = None) -> dict:
    from app.projects import PROJECT_STATUSES

    me = _operator(request)
    prefs = request.app.state.user_prefs.get(me)
    recent = {r["project"]: r["at"] for r in prefs["recent"]}
    return {
        **_EMPTY_STATS, **(stats or {}),
        "project": p["name"], "code": p.get("code", ""), "description": p.get("description", ""),
        "owner": p.get("owner", ""), "status": p.get("status", "active"),
        "status_label": PROJECT_STATUSES.get(p.get("status", "active"), p.get("status")),
        "members": p.get("members", {}), "member_count": len(p.get("members", {})),
        "my_role": _project_role(request, p["name"]),
        "favorite": p["name"] in prefs["favorites"], "last_visited": recent.get(p["name"]),
        "created_by": p.get("created_by"), "created_at": p.get("created_at"),
        "updated_by": p.get("updated_by"), "updated_at": p.get("updated_at"),
    }


@router.get("/api/v1/projects")
async def list_projects(
    request: Request, keyword: str = "", status: str = "", include_archived: bool = True,
) -> dict:
    """项目列表：实体字段 + 汇总统计 + 我的角色/收藏/最近访问；非管理员只见所属项目。

    历史任务中出现过的项目名自动注册为项目实体（兼容项目实体化之前的数据）。
    """
    from app.reports import UNASSIGNED, project_rollup

    records = request.app.state.tasks.list(limit=100000)
    stats = {p["project"]: p for p in project_rollup(records)}
    pstore = request.app.state.projects
    pstore.ensure([n for n in stats if n != UNASSIGNED])
    visible = _visible_projects(request)
    kw = keyword.strip().lower()
    merged = []
    for p in pstore.list():
        if visible is not None and p["name"] not in visible:
            continue
        if status and p["status"] != status:
            continue
        if not include_archived and p["status"] == "archived":
            continue
        if kw and kw not in p["name"].lower() and kw not in p.get("code", "").lower():
            continue
        merged.append(_project_view(request, p, stats.get(p["name"])))
    if UNASSIGNED in stats and not kw and not status:
        # 未指定项目的任务聚合行（不可编辑/删除），仅管理员可见
        if visible is None:
            merged.append({**_EMPTY_STATS, **stats[UNASSIGNED], "description": "", "code": "",
                           "owner": "", "status": "active", "status_label": "进行中", "members": {},
                           "member_count": 0, "my_role": "project_admin", "favorite": False,
                           "last_visited": None, "created_by": None, "builtin": True})
    # 收藏置顶，其余按最近活动倒序
    merged.sort(key=lambda x: x["last_activity"], reverse=True)
    merged.sort(key=lambda x: not x.get("favorite"))
    return {"projects": merged}


class ProjectBody(BaseModel):
    name: str
    description: str = ""
    code: str = ""
    owner: str = ""


@router.post("/api/v1/projects")
async def create_project(request: Request, body: ProjectBody) -> dict:
    """创建项目（系统管理员）：创建人与负责人自动成为项目管理员。"""
    from app.projects import ProjectError

    _require_admin(request)
    auth = request.app.state.auth
    if body.owner.strip() and not auth.exists(body.owner.strip()) and get_settings().auth_enabled:
        raise HTTPException(status_code=400, detail=f"负责人不存在: {body.owner}")
    try:
        project = request.app.state.projects.create(
            body.name, body.description, created_by=_operator(request),
            code=body.code, owner=body.owner,
        )
    except ProjectError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _project_view(request, project)


class ProjectUpdateBody(BaseModel):
    name: str | None = None         # 改名（联动更新引用该项目的全部任务/计划/版本/模块）
    description: str | None = None
    code: str | None = None
    owner: str | None = None
    status: str | None = None       # active / paused / archived


@router.put("/api/v1/projects/{name}")
async def update_project(request: Request, name: str, body: ProjectUpdateBody) -> dict:
    from app.projects import ProjectError

    # 归档项目只允许「恢复」这一种修改；其余修改需 project.edit
    if body.status is not None and body.status != "archived":
        _require_project(request, name, "project.view")
        if not _is_admin(request) and _project_role(request, name) != "project_admin":
            raise HTTPException(status_code=403, detail="仅项目管理员可变更项目状态")
    else:
        _require_project(request, name, "project.edit")
    if body.owner and body.owner.strip() and get_settings().auth_enabled \
            and not request.app.state.auth.exists(body.owner.strip()):
        raise HTTPException(status_code=400, detail=f"负责人不存在: {body.owner}")
    store = request.app.state.tasks
    try:
        project = request.app.state.projects.update(
            name, body.name, body.description, code=body.code, owner=body.owner,
            status=body.status, operator=_operator(request),
        )
    except ProjectError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if body.name and body.name.strip() and body.name.strip() != name:
        # 改名联动：任务上下文、测试计划、版本、模块、个人偏好
        renamed = 0
        for r in store.list(limit=100000):
            if (r.context or {}).get("project") == name:
                r.context["project"] = project["name"]
                store.save(r)
                renamed += 1
        plans = request.app.state.plans
        for plan in plans.list(project=name):
            plan["project"] = project["name"]
            plans.save(plan)
        request.app.state.versions.rename_project(name, project["name"])
        request.app.state.modules.rename_project(name, project["name"])
        request.app.state.user_prefs.rename_project(name, project["name"])
        logger.info("项目改名 {} → {}：联动更新 {} 个任务", name, project["name"], renamed)
    return _project_view(request, project)


@router.delete("/api/v1/projects/{name}")
async def delete_project(request: Request, name: str) -> dict:
    """删除项目（系统管理员）：仅允许空项目；有任务/计划引用时拒绝（先迁移或删除）。"""
    from app.projects import ProjectError

    _require_admin(request)
    referenced = sum(
        1 for r in request.app.state.tasks.list(limit=100000)
        if (r.context or {}).get("project") == name
    )
    if referenced:
        raise HTTPException(status_code=400, detail=f"项目下仍有 {referenced} 个任务，不可删除")
    if request.app.state.plans.list(project=name):
        raise HTTPException(status_code=400, detail="项目下仍有测试计划，不可删除")
    try:
        request.app.state.projects.delete(name)
    except ProjectError as e:
        raise HTTPException(status_code=404, detail=str(e))
    request.app.state.versions.drop_project(name)
    request.app.state.modules.drop_project(name)
    request.app.state.user_prefs.drop_project(name)
    return {"deleted": name}


@router.post("/api/v1/projects/{name}/favorite")
async def toggle_project_favorite(request: Request, name: str) -> dict:
    _require_project(request, name, "project.view")
    fav = request.app.state.user_prefs.toggle_favorite(_operator(request), name)
    return {"project": name, "favorite": fav}


# ---- 项目成员与角色（3.4）----


class MemberBody(BaseModel):
    username: str
    role: str  # project_admin / test_lead / tester / viewer


@router.get("/api/v1/projects/{name}/members")
async def list_members(request: Request, name: str) -> dict:
    from app.permissions import PROJECT_ROLES

    p = _require_project(request, name, "project.view")
    if p is None:
        raise HTTPException(status_code=404, detail=f"项目不存在: {name}")
    auth = request.app.state.auth
    members = []
    for username, role in p["members"].items():
        info = auth.public_user(username) if auth.exists(username) else {"username": username, "name": "", "status": "unknown"}
        members.append({"username": username, "role": role, "role_label": PROJECT_ROLES.get(role, role),
                        "name": info.get("name", ""), "status": info.get("status", "active"),
                        "system_role": info.get("role")})
    return {"project": name, "members": members, "roles": PROJECT_ROLES}


@router.put("/api/v1/projects/{name}/members")
async def set_member(request: Request, name: str, body: MemberBody) -> dict:
    """添加成员或修改项目角色（项目管理员 / 系统管理员）。"""
    from app.projects import ProjectError

    _require_project(request, name, "project.members")
    if get_settings().auth_enabled and not request.app.state.auth.exists(body.username.strip()):
        raise HTTPException(status_code=400, detail=f"用户不存在: {body.username}")
    try:
        p = request.app.state.projects.set_member(name, body.username, body.role, operator=_operator(request))
    except ProjectError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info("项目 {} 成员变更：{} → {}（操作人 {}）", name, body.username, body.role, _operator(request))
    return {"project": name, "members": p["members"]}


@router.delete("/api/v1/projects/{name}/members/{username}")
async def remove_member(request: Request, name: str, username: str) -> dict:
    from app.projects import ProjectError

    _require_project(request, name, "project.members")
    try:
        p = request.app.state.projects.remove_member(name, username, operator=_operator(request))
    except ProjectError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"project": name, "members": p["members"]}


# ---- 项目版本（4.1）----


class VersionBody(BaseModel):
    name: str
    code: str = ""
    description: str = ""
    start_date: str = ""
    planned_end: str = ""
    actual_end: str = ""
    status: str = "not_started"


class VersionUpdateBody(BaseModel):
    name: str | None = None
    code: str | None = None
    description: str | None = None
    start_date: str | None = None
    planned_end: str | None = None
    actual_end: str | None = None
    status: str | None = None


@router.get("/api/v1/projects/{name}/versions")
async def list_versions(request: Request, name: str) -> dict:
    from app.projects import VERSION_STATUSES

    _require_project(request, name, "version.view")
    return {"project": name, "versions": request.app.state.versions.list(name),
            "statuses": VERSION_STATUSES}


@router.post("/api/v1/projects/{name}/versions")
async def create_version(request: Request, name: str, body: VersionBody) -> dict:
    from app.projects import ProjectError

    _require_project(request, name, "version.manage")
    try:
        return request.app.state.versions.create(
            name, created_by=_operator(request), **body.model_dump()
        )
    except ProjectError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _version_of(request: Request, name: str, version_id: str, action: str) -> dict:
    _require_project(request, name, action)
    v = request.app.state.versions.get(version_id)
    if v is None or v["project"] != name:
        raise HTTPException(status_code=404, detail=f"版本不存在: {version_id}")
    return v


@router.put("/api/v1/projects/{name}/versions/{version_id}")
async def update_version(request: Request, name: str, version_id: str, body: VersionUpdateBody) -> dict:
    from app.projects import ProjectError

    _version_of(request, name, version_id, "version.manage")
    try:
        return request.app.state.versions.update(version_id, **body.model_dump(exclude_none=True))
    except ProjectError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/api/v1/projects/{name}/versions/{version_id}")
async def delete_version(request: Request, name: str, version_id: str) -> dict:
    _version_of(request, name, version_id, "version.manage")
    request.app.state.versions.delete(version_id)
    return {"deleted": version_id}


# ---- 项目模块树（4.2）----


class ModuleBody(BaseModel):
    name: str
    parent_id: str | None = None
    description: str = ""


class ModuleUpdateBody(BaseModel):
    name: str | None = None
    description: str | None = None
    parent_id: str | None = None
    move: bool = False  # True 时按 parent_id 移动（None 表示移到根）


class ModuleReorderBody(BaseModel):
    parent_id: str | None = None
    ordered_ids: list[str]


def _module_referenced(request: Request, project: str):
    """模块是否被项目内用例引用（按模块名或路径匹配）。"""
    names: set[str] = set()
    for r in request.app.state.tasks.list(limit=100000):
        if (r.context or {}).get("project") != project:
            continue
        for c in (r.result or {}).get("cases", []):
            if c.get("module"):
                names.add(str(c["module"]))
    return lambda key: key in names


@router.get("/api/v1/projects/{name}/modules")
async def list_modules(request: Request, name: str, include_deleted: bool = False) -> dict:
    _require_project(request, name, "version.view")
    mstore = request.app.state.modules
    referenced = _module_referenced(request, name)
    deleted = [
        {**m, "path": mstore.path(m["module_id"]), "referenced": referenced(mstore.path(m["module_id"])) or referenced(m["name"])}
        for m in mstore.list(name, include_deleted=True) if m.get("deleted_at")
    ] if include_deleted else []
    return {"project": name, "tree": mstore.tree(name), "deleted": deleted,
            "max_depth": 5}


@router.post("/api/v1/projects/{name}/modules")
async def create_module(request: Request, name: str, body: ModuleBody) -> dict:
    from app.projects import ProjectError

    _require_project(request, name, "version.manage")
    try:
        m = request.app.state.modules.create(
            name, body.name, parent_id=body.parent_id, description=body.description,
            created_by=_operator(request),
        )
    except ProjectError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {**m, "path": request.app.state.modules.path(m["module_id"])}


def _module_of(request: Request, name: str, module_id: str, action: str, allow_deleted: bool = False) -> dict:
    _require_project(request, name, action)
    m = request.app.state.modules.get(module_id)
    if m is None or m["project"] != name or (m.get("deleted_at") and not allow_deleted):
        raise HTTPException(status_code=404, detail=f"模块不存在: {module_id}")
    return m


@router.put("/api/v1/projects/{name}/modules/{module_id}")
async def update_module(request: Request, name: str, module_id: str, body: ModuleUpdateBody) -> dict:
    from app.projects import ProjectError

    _module_of(request, name, module_id, "version.manage")
    try:
        m = request.app.state.modules.update(
            module_id, name=body.name, description=body.description,
            parent_id=body.parent_id if body.move else ..., operator=_operator(request),
        )
    except ProjectError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {**m, "path": request.app.state.modules.path(m["module_id"])}


@router.post("/api/v1/projects/{name}/modules/reorder")
async def reorder_modules(request: Request, name: str, body: ModuleReorderBody) -> dict:
    from app.projects import ProjectError

    _require_project(request, name, "version.manage")
    try:
        request.app.state.modules.reorder(name, body.parent_id, body.ordered_ids)
    except ProjectError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"project": name, "tree": request.app.state.modules.tree(name)}


@router.delete("/api/v1/projects/{name}/modules/{module_id}")
async def delete_module(request: Request, name: str, module_id: str, permanent: bool = False) -> dict:
    """删除模块：默认逻辑删除（含子树，可恢复）；permanent=true 物理删除，被用例引用时拒绝。"""
    from app.projects import ProjectError

    mstore = request.app.state.modules
    try:
        if permanent:
            _module_of(request, name, module_id, "version.manage", allow_deleted=True)
            if not _is_admin(request) and _project_role(request, name) != "project_admin":
                raise HTTPException(status_code=403, detail="永久删除仅限项目管理员")
            mstore.purge(module_id, _module_referenced(request, name))
            return {"deleted": module_id, "permanent": True}
        _module_of(request, name, module_id, "version.manage")
        removed = mstore.delete(module_id, operator=_operator(request))
    except ProjectError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"deleted": module_id, "permanent": False, "removed": [m["module_id"] for m in removed]}


@router.post("/api/v1/projects/{name}/modules/{module_id}/restore")
async def restore_module(request: Request, name: str, module_id: str) -> dict:
    from app.projects import ProjectError

    _module_of(request, name, module_id, "version.manage", allow_deleted=True)
    try:
        m = request.app.state.modules.restore(module_id)
    except ProjectError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {**m, "path": request.app.state.modules.path(m["module_id"])}


@router.get("/api/v1/projects/cases")
async def list_project_cases(request: Request, project: str) -> dict:
    """项目用例库：跨任务聚合全部用例，带生命周期阶段与最新执行结果。"""
    from app.reports import project_cases

    _require_project(request, project, "case.view")
    records = request.app.state.tasks.list(limit=100000)
    return {"project": project,
            "cases": project_cases(records, project, plans=request.app.state.plans.list())}


CASE_PAGE_SIZES = (20, 50, 100, 200)



@router.get("/api/v1/projects/{name}")
async def get_project(request: Request, name: str) -> dict:
    """项目详情（记入最近访问）：实体字段、成员、版本与模块数。"""
    _require_project(request, name, "project.view")
    p = request.app.state.projects.get(name)
    if p is None:
        raise HTTPException(status_code=404, detail=f"项目不存在: {name}")
    request.app.state.user_prefs.record_visit(_operator(request), name)
    view = _project_view(request, p)
    view["versions"] = len(request.app.state.versions.list(name))
    view["modules"] = len(request.app.state.modules.list(name))
    return view


@router.get("/api/v1/cases")
async def list_all_cases(
    request: Request, project: str | None = None, module: str = "", priority: str = "",
    review: str = "", keyword: str = "", page: int = 1, page_size: int = 20,
) -> dict:
    """全库用例列表（测试用例页）：跨项目/任务聚合，支持筛选与分页（20/50/100/200，默认 20）。"""
    from app.reports import project_cases

    if page_size not in CASE_PAGE_SIZES:
        raise HTTPException(
            status_code=400,
            detail=f"page_size 仅支持 {'/'.join(map(str, CASE_PAGE_SIZES))}",
        )
    if project:
        _require_project(request, project, "case.view")
    records = [r for r in request.app.state.tasks.list(limit=100000)
               if _record_visible(request, (r.context or {}).get("project"))]
    rows = project_cases(records, project or None, plans=request.app.state.plans.list())
    modules = sorted({r["module"] for r in rows if r["module"]})
    if module:
        rows = [r for r in rows if r["module"] == module]
    if priority:
        rows = [r for r in rows if r["priority"] == priority]
    if review:
        rows = [r for r in rows if r["review"] == review]
    if keyword:
        kw = keyword.lower()
        rows = [
            r for r in rows
            if kw in f"{r['title']} {r['case_id']} {r['keywords']} {r['module']}".lower()
        ]
    total = len(rows)
    pages = max(1, -(-total // page_size))
    page = min(max(1, page), pages)
    start = (page - 1) * page_size
    return {
        "cases": rows[start:start + page_size],
        "total": total, "page": page, "page_size": page_size, "pages": pages,
        "modules": modules,
    }


# ---- 报表 ----


@router.get("/api/v1/reports/summary")
async def reports_summary(
    request: Request, days: int = 30, project: str | None = None
) -> dict:
    """报表聚合：任务/用例产出、AI 一次通过率、采纳率、审核动作、趋势与分布。

    days=0 表示全部历史。纯留痕统计，不产生模型调用。
    """
    from app.reports import summarize

    if project:
        _require_project(request, project, "project.view")
    records = [r for r in request.app.state.tasks.list(limit=100000)
               if _record_visible(request, (r.context or {}).get("project"))]
    data = summarize(records, days=max(0, days), project=project,
                     plans=request.app.state.plans.list())
    rules = request.app.state.rules
    data["rules"] = {
        "candidates": len(rules.list("candidate")),
        "active": len(rules.list("active")),
    }
    return data


# ---- 学习候选与规则库（需求三十六~三十九）----


@router.get("/api/v1/learning/rules")
async def list_rules(request: Request, status: str | None = None, project: str | None = None) -> dict:
    _require_admin(request)  # 学习规则页仅管理员可见；规则注入生成走服务端内部逻辑，不受影响
    return {"rules": [r.model_dump() for r in request.app.state.rules.list(status, project)]}


class LearningAnalyzeBody(BaseModel):
    project: str | None = None
    model: str | None = None


@router.post("/api/v1/learning/analyze")
async def analyze_learning(request: Request, body: LearningAnalyzeBody | None = None) -> dict:
    """分析人工修改留痕，提炼规则候选（需求三十八，仅管理员）：候选须人工确认后才生效。"""
    _require_admin(request)
    from app.agents.quality import run_learning_analysis
    from app.learning import collect_samples

    body = body or LearningAnalyzeBody()
    samples = collect_samples(request.app.state.tasks, project=body.project,
                              plans=request.app.state.plans)
    if not samples:
        return {"candidates": [], "samples": 0, "message": "暂无人工修改留痕可供学习"}
    try:
        candidates = await run_learning_analysis(request.app.state.llm, samples, body.model)
    except (MissingAPIKeyError, AllModelsFailedError, LLMOutputError) as e:
        raise HTTPException(status_code=502, detail=str(e))
    added = request.app.state.rules.add_candidates(candidates, project=body.project)
    logger.info("学习分析：样本 {} 条 → 新候选 {} 条", len(samples), len(added))
    return {
        "candidates": [r.model_dump() for r in added],
        "samples": len(samples),
        "total_candidates": len(request.app.state.rules.list("candidate")),
    }


class RuleConfirmBody(BaseModel):
    scope: str  # system / team / project / module
    project: str | None = None
    module: str | None = None


@router.post("/api/v1/learning/rules/{rule_id}/confirm")
async def confirm_rule(request: Request, rule_id: str, body: RuleConfirmBody) -> dict:
    """负责人确认候选生效（需求三十八：加入项目规则/团队规则），并指定适用范围（需求三十九）。"""
    _require_admin(request)
    try:
        rule = request.app.state.rules.confirm(rule_id, body.scope, body.project, body.module)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e.args[0]))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return rule.model_dump()


@router.post("/api/v1/learning/rules/{rule_id}/ignore")
async def ignore_rule(request: Request, rule_id: str) -> dict:
    _require_admin(request)
    try:
        return request.app.state.rules.ignore(rule_id).model_dump()
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e.args[0]))


class RuleUpdateBody(BaseModel):
    content: str


@router.put("/api/v1/learning/rules/{rule_id}")
async def update_rule(request: Request, rule_id: str, body: RuleUpdateBody) -> dict:
    _require_admin(request)
    try:
        return request.app.state.rules.update(rule_id, body.content).model_dump()
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e.args[0]))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/api/v1/learning/rules/{rule_id}")
async def delete_rule(request: Request, rule_id: str) -> dict:
    _require_admin(request)
    if not request.app.state.rules.delete(rule_id):
        raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    return {"deleted": rule_id}


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
    record = _task(request, task_id, "case.review")
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
        "by": _operator(request),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **diff,
    }
    store.save(record)
    return {"task_id": task_id, "stats": diff["stats"], "added": diff["added"],
            "deleted": diff["deleted"], "modified": diff["modified"]}


@router.get("/api/v1/tasks")
async def list_tasks(
    request: Request, status: str | None = None, project: str | None = None,
    created_by: str | None = None, limit: int = 50
) -> dict:
    """任务列表（F-6-1）：倒序返回任务概要（含项目名与创建人），支持按状态/项目/创建人过滤。"""
    records = request.app.state.tasks.list(status=status, limit=100000)
    records = [r for r in records if _record_visible(request, (r.context or {}).get("project"))]
    if project is not None:
        records = [r for r in records if (r.context or {}).get("project") == project]
    records = records[:limit]
    if created_by is not None:
        records = [r for r in records if r.created_by == created_by]
    return {
        "tasks": [
            {
                "task_id": r.task_id,
                "status": r.status,
                "progress": r.progress,
                "created_at": r.created_at,
                "sources": r.sources,
                "project": (r.context or {}).get("project"),
                "created_by": r.created_by,
                "case_count": len((r.result or {}).get("cases", [])),
                "revision_count": len(r.revisions),
                "error": r.error,
            }
            for r in records
        ]
    }


@router.post("/api/v1/tasks/{task_id}/cancel")
async def cancel_task(request: Request, task_id: str) -> dict:
    """取消进行中的后台任务（任务管理）：中断执行，任务标记失败并留痕取消原因。"""
    store = request.app.state.tasks
    record = _task(request, task_id, "point.ai")
    if record.status not in ("queued", "running"):
        raise HTTPException(status_code=409, detail=f"任务状态为 {record.status}，无可取消的执行")
    if not store.cancel(task_id):
        raise HTTPException(
            status_code=409, detail="该任务正在前台请求中执行，无法从后台取消，请等待其完成"
        )
    return {"task_id": task_id, "status": "failed", "canceled": True}


@router.get("/api/v1/tasks/{task_id}")
async def get_task(request: Request, task_id: str) -> dict:
    record = _task(request, task_id, "case.view")
    return {**record.model_dump(), "editing": _active_editing(request.app, task_id)}


# ---- 编辑占用提示（完整需求 11 章）：内存瞬态状态，重启即清（占用本就随会话失效）----

_EDITING_TTL_SECONDS = 300


def _editing_registry(app) -> dict:
    if not hasattr(app.state, "editing"):
        app.state.editing = {}
    return app.state.editing


def _active_editing(app, task_id: str) -> list[dict]:
    registry = _editing_registry(app)
    cutoff = datetime.now(timezone.utc).timestamp() - _EDITING_TTL_SECONDS
    stale = [k for k, v in registry.items() if v["ts"] < cutoff]
    for k in stale:
        registry.pop(k, None)
    return [
        {"kind": k[1], "entity_id": k[2], "by": v["by"], "at": v["at"]}
        for k, v in registry.items() if k[0] == task_id
    ]


class EditingBody(BaseModel):
    kind: str        # point / case
    entity_id: str   # point: tp_id；case: uid
    action: str      # start / stop / force_release


@router.post("/api/v1/tasks/{task_id}/editing")
async def task_editing(request: Request, task_id: str, body: EditingBody) -> dict:
    """「某某正在编辑」占用提示（需求 11）：start 登记 / stop 释放 / force_release 管理员解除。

    占用只用于提示与协作提醒，不阻塞保存——并发覆盖由乐观锁版本号拦截，解除占用不绕过版本冲突。
    """
    _task(request, task_id, "case.edit")
    if body.kind not in ("point", "case"):
        raise HTTPException(status_code=400, detail="kind 须为 point 或 case")
    registry = _editing_registry(request.app)
    key = (task_id, body.kind, str(body.entity_id))
    operator = _operator(request)
    now = datetime.now(timezone.utc)
    holder = registry.get(key)
    if body.action == "start":
        if holder is None or holder["by"] == operator:
            registry[key] = {"by": operator, "at": now.isoformat(timespec="seconds"), "ts": now.timestamp()}
            holder = None
        # 他人占用中：不抢占，返回占用者供前端提示
    elif body.action == "stop":
        if holder and holder["by"] == operator:
            registry.pop(key, None)
        holder = None
    elif body.action == "force_release":
        user = getattr(request.state, "user", None)
        if user is not None and user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="仅管理员可强制解除编辑占用")
        registry.pop(key, None)
        holder = None
    else:
        raise HTTPException(status_code=400, detail="action 须为 start/stop/force_release")
    return {"task_id": task_id, "kind": body.kind, "entity_id": body.entity_id,
            "holder": ({"by": holder["by"], "at": holder["at"]} if holder else None),
            "editing": _active_editing(request.app, task_id)}


_MEDIA_TYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
    "xmind": "application/vnd.xmind.workbook",
}


@router.get("/api/v1/tasks/{task_id}/files/{fmt}")
async def download_file(request: Request, task_id: str, fmt: str) -> FileResponse:
    record = _task(request, task_id, "case.export")
    path = record.files.get(fmt)
    if path is None or not Path(path).exists():
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 无 {fmt} 产物")
    return FileResponse(path, media_type=_MEDIA_TYPES.get(fmt), filename=Path(path).name)

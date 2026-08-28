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


class LoginBody(BaseModel):
    username: str
    password: str
    otp: str | None = None  # 两步验证动态码（绑定用户必填，二段式提交）


@router.post("/api/v1/auth/login")
async def auth_login(request: Request, body: LoginBody) -> dict:
    from app.auth import AuthError
    from app.auth.store import OtpRequired

    try:
        token, user = request.app.state.auth.login(body.username, body.password, otp=body.otp)
    except OtpRequired:
        # 口令正确但需动态码：不签发会话，前端展示验证码输入后重新提交
        return {"otp_required": True}
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    logger.info("用户 {} 登录成功", user["username"])
    return {"token": token, "user": user}


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


@router.get("/api/v1/auth/me")
async def auth_me(request: Request) -> dict:
    return _current_user(request)


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


@router.get("/api/v1/auth/users")
async def auth_list_users(request: Request) -> dict:
    _require_admin(request)
    return {"users": request.app.state.auth.list_users()}


@router.post("/api/v1/auth/users")
async def auth_add_user(request: Request, body: UserBody) -> dict:
    from app.auth import AuthError

    _require_admin(request)
    try:
        return request.app.state.auth.add_user(body.username, body.password, body.role)
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))


class UserUpdateBody(BaseModel):
    role: str | None = None          # 修改角色（admin/member）
    new_password: str | None = None  # 重置密码（无需原密码，重置后该用户需重新登录）
    reset_totp: bool = False         # 重置两步验证（手机丢失等场景解绑，用户可重新绑定）


@router.put("/api/v1/auth/users/{username}")
async def auth_update_user(request: Request, username: str, body: UserUpdateBody) -> dict:
    """管理员管理用户（重置密码 / 修改角色 / 重置两步验证）。"""
    from app.auth import AuthError

    _require_admin(request)
    if not body.role and not body.new_password and not body.reset_totp:
        raise HTTPException(status_code=400, detail="请提供要修改的角色、新密码或重置两步验证")
    try:
        user = request.app.state.auth.admin_update(
            username, role=body.role, new_password=body.new_password, reset_totp=body.reset_totp
        )
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info("管理员更新用户 {}：角色={} 重置密码={} 重置两步验证={}",
                username, body.role or "-", bool(body.new_password), body.reset_totp)
    return user


@router.delete("/api/v1/auth/users/{username}")
async def auth_delete_user(request: Request, username: str) -> dict:
    from app.auth import AuthError

    operator = _require_admin(request)
    try:
        request.app.state.auth.delete_user(username, operator=operator["username"])
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
    record = store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
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


class ReviewItem(BaseModel):
    case_id: str
    action: str  # approve / reject / modify / delete / unlock（accept 为 approve 的兼容别名）
    case: dict | None = None  # modify 时提交修改后的完整用例
    comment: str = ""  # reject 时的审核意见（AI 定点修改的输入）
    feedback: str = ""  # 一键反馈：问题类型或意见（学习语料）


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
    from app.tasks.points import REJECT_HINT_THRESHOLD
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
            state.update(status="approved", locked=True, comment="")
        elif action == "reject":
            comment = item.comment.strip() or item.feedback.strip()
            if not comment:
                raise HTTPException(status_code=400, detail=f"驳回用例 {item.case_id} 必须填写审核意见")
            state.update(status="rejected", locked=False, comment=comment)
            state["reject_count"] = int(state.get("reject_count", 0)) + 1
            if state["reject_count"] >= REJECT_HINT_THRESHOLD:
                hints.append(
                    f"{item.case_id} 已连续 {state['reject_count']} 次未通过审核，"
                    "建议检查：1) 需求是否存在歧义 2) 是否需要人工直接修改 3) 是否需要补充需求信息"
                )
        elif action == "delete":
            entry["before"] = origin
            cases.remove(origin)
            record.case_reviews.pop(uid, None)
            deleted_any = True
        elif action == "modify":
            if not item.case:
                raise HTTPException(status_code=400, detail=f"修改操作需提交 case 字段: {item.case_id}")
            try:
                updated = TestCase.model_validate({**item.case, "uid": uid}).model_dump()
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"修改后的用例不合法: {e}")
            entry["before"], entry["after"] = origin, updated
            cases[cases.index(origin)] = updated
            by_id[item.case_id] = updated
            # 人工定稿即通过并锁定（人工修改优于 AI 再改）
            state.update(status="approved", locked=True, comment="")
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
    comment: str = ""              # reject 时的审核意见


class PointReviewBody(BaseModel):
    items: list[PointReviewItem]


def _points_record(store, task_id: str):
    record = store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    modules = (record.analysis or {}).get("test_points")
    if not modules:
        raise HTTPException(status_code=409, detail="任务无测试点拆解结果")
    return record, modules


@router.post("/api/v1/tasks/{task_id}/points/review")
async def review_points(request: Request, task_id: str, body: PointReviewBody) -> dict:
    """测试点逐条/批量审核（需求四十八/四十九/五十）：✓通过（锁定）/ ✎修改 / ×驳回 / 删除。

    通过即锁定退出 AI 修改队列；驳回须带审核意见；连续驳回达阈值提示人工介入（需求三十五）。
    """
    from app.tasks.points import PointReviewError, apply_point_review

    store = request.app.state.tasks
    record, modules = _points_record(store, task_id)
    try:
        outcome = apply_point_review(modules, [i.model_dump() for i in body.items])
    except PointReviewError as e:
        raise HTTPException(status_code=400, detail=str(e))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record.point_review_log.extend(dict(e, at=now, by=_operator(request)) for e in outcome["log"])
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
    """AI 定点修改被驳回测试点（需求三十~三十四）。

    只输入被驳回项+审核意见+关联需求；先识别意见类型再修改；输出修改前后 Diff；
    修改后的测试点回到待审核状态（局部修改 → 再审核）。
    """
    from app.agents.quality import run_point_fix
    from app.tasks.points import rejected_points

    store = request.app.state.tasks
    record, modules = _points_record(store, task_id)
    rejected = rejected_points(modules)
    if not rejected:
        raise HTTPException(status_code=409, detail="没有被驳回的测试点，无需修改")
    ctx = record.context or {}
    try:
        outcome = await run_point_fix(
            request.app.state.llm, ctx.get("requirement", ""), modules, rejected, ctx.get("model")
        )
    except (MissingAPIKeyError, AllModelsFailedError, LLMOutputError) as e:
        raise HTTPException(status_code=502, detail=str(e))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record.fix_log.append({"kind": "points", "at": now, "by": _operator(request),
                           "diff": outcome["diff"], "added": outcome["added"]})
    store.save(record)
    logger.info("任务 {} 测试点定点修改：改动 {} 条 / 新增 {} 条", task_id, len(outcome["diff"]), len(outcome["added"]))
    return {"task_id": task_id, "diff": outcome["diff"], "added": outcome["added"], "test_points": modules}


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
    record, modules = _points_record(store, task_id)
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
    record, modules = _points_record(store, task_id)
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
    record, modules = _points_record(store, task_id)
    ctx = record.context or {}
    pairs = duplicate_candidates(modules)
    judged = await run_dup_judge(request.app.state.llm, ctx.get("requirement", ""), pairs, ctx.get("model"))
    record.dup_report = {"points": judged, "resolved": (record.dup_report or {}).get("resolved", [])}
    store.save(record)
    return {"task_id": task_id, "duplicates": judged}


# ---- 用例定点修改（需求三十~三十四/五十五）----


@router.post("/api/v1/tasks/{task_id}/cases/fix")
async def fix_cases(request: Request, task_id: str) -> dict:
    """AI 定点修改被驳回用例：只输入被驳回用例+审核意见；锁定用例确定性保护；输出 Diff。"""
    from app.agents import GenerationResult
    from app.agents.quality import run_case_fix

    store = request.app.state.tasks
    record = store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if record.status != "completed" or not (record.result or {}).get("cases"):
        raise HTTPException(status_code=409, detail=f"任务状态为 {record.status}，无可修改的用例结果")
    if not any(s.get("status") == "rejected" for s in record.case_reviews.values()):
        raise HTTPException(status_code=409, detail="没有被驳回的用例，无需修改")
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
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record.fix_log.append({
        "kind": "cases", "at": now, "by": _operator(request), "diff": outcome["diff"],
        "fixes": outcome.get("fixes", []), "invalid": outcome.get("invalid", []),
    })
    result = GenerationResult.model_validate({**record.result, "cases": outcome["cases"]})
    task_dir = store.output_dir / task_id
    response = _finalize_task(store, task_id, task_dir, record.sources, result, template)
    response["diff"] = outcome["diff"]
    response["fixes"] = outcome.get("fixes", [])
    logger.info("任务 {} 用例定点修改：Diff {} 条", task_id, len(outcome["diff"]))
    return response


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
    record = store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
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
    record = store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
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


# ---- 报表 ----


@router.get("/api/v1/reports/summary")
async def reports_summary(
    request: Request, days: int = 30, project: str | None = None
) -> dict:
    """报表聚合：任务/用例产出、AI 一次通过率、采纳率、审核动作、趋势与分布。

    days=0 表示全部历史。纯留痕统计，不产生模型调用。
    """
    from app.reports import summarize

    records = request.app.state.tasks.list(limit=100000)
    data = summarize(records, days=max(0, days), project=project)
    rules = request.app.state.rules
    data["rules"] = {
        "candidates": len(rules.list("candidate")),
        "active": len(rules.list("active")),
    }
    return data


# ---- 学习候选与规则库（需求三十六~三十九）----


@router.get("/api/v1/learning/rules")
async def list_rules(request: Request, status: str | None = None, project: str | None = None) -> dict:
    return {"rules": [r.model_dump() for r in request.app.state.rules.list(status, project)]}


class LearningAnalyzeBody(BaseModel):
    project: str | None = None
    model: str | None = None


@router.post("/api/v1/learning/analyze")
async def analyze_learning(request: Request, body: LearningAnalyzeBody | None = None) -> dict:
    """分析人工修改留痕，提炼规则候选（需求三十八）：候选须人工确认后才生效。"""
    from app.agents.quality import run_learning_analysis
    from app.learning import collect_samples

    body = body or LearningAnalyzeBody()
    samples = collect_samples(request.app.state.tasks, project=body.project)
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
    try:
        rule = request.app.state.rules.confirm(rule_id, body.scope, body.project, body.module)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e.args[0]))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return rule.model_dump()


@router.post("/api/v1/learning/rules/{rule_id}/ignore")
async def ignore_rule(request: Request, rule_id: str) -> dict:
    try:
        return request.app.state.rules.ignore(rule_id).model_dump()
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e.args[0]))


class RuleUpdateBody(BaseModel):
    content: str


@router.put("/api/v1/learning/rules/{rule_id}")
async def update_rule(request: Request, rule_id: str, body: RuleUpdateBody) -> dict:
    try:
        return request.app.state.rules.update(rule_id, body.content).model_dump()
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e.args[0]))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/api/v1/learning/rules/{rule_id}")
async def delete_rule(request: Request, rule_id: str) -> dict:
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
    records = request.app.state.tasks.list(status=status, limit=limit)
    if project is not None:
        records = [r for r in records if (r.context or {}).get("project") == project]
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
    record = store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if record.status not in ("queued", "running"):
        raise HTTPException(status_code=409, detail=f"任务状态为 {record.status}，无可取消的执行")
    if not store.cancel(task_id):
        raise HTTPException(
            status_code=409, detail="该任务正在前台请求中执行，无法从后台取消，请等待其完成"
        )
    return {"task_id": task_id, "status": "failed", "canceled": True}


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

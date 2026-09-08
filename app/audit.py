"""操作日志与系统安全日志（完整需求 17 章）。

- 业务操作：中间件统一记录全部写接口（方法/路径 → 中文动作、对象类型、目标 ID），路由可补充
  detail（如批量条数）与所属项目（_require_project 自动登记）；
- 安全日志：登录成功/失败、登出、改密、两步验证、用户增删改与禁用、系统/项目角色变更、批量操作，
  含 IP 与 User-Agent；
- 查看权限：系统管理员看全部；项目成员看所属项目的业务日志（log.view）与自己的操作。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import Column, Index, Integer, String, Table, Text, func, select
from sqlalchemy.dialects.mysql import LONGTEXT

from app.db import get_engine, metadata

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", String(32), nullable=False),
    Column("user", String(64), nullable=True),
    Column("ip", String(64), nullable=False, default=""),
    Column("ua", String(300), nullable=False, default=""),
    Column("project", String(191), nullable=True),
    Column("kind", String(24), nullable=False, default="other"),
    Column("action", String(64), nullable=False, default=""),
    Column("target", String(191), nullable=False, default=""),
    Column("method", String(8), nullable=False, default=""),
    Column("path", String(300), nullable=False, default=""),
    Column("status", Integer, nullable=False, default=0),
    Column("detail", Text().with_variant(LONGTEXT, "mysql"), nullable=False, default=""),
    Column("duration_ms", Integer, nullable=False, default=0),
    Column("security", Integer, nullable=False, default=0),  # 1 = 安全日志
    Index("ix_audit_at", "at"),
    Index("ix_audit_project_at", "project", "at"),
    Index("ix_audit_user_at", "user", "at"),
)

KINDS = {
    "auth": "登录安全", "user": "用户管理", "member": "项目成员", "project": "项目", "requirement": "需求",
    "point": "测试点", "case": "测试用例", "plan": "测试计划", "exec": "测试执行", "ai": "AI 操作",
    "knowledge": "知识库", "learning": "学习规则", "memory": "记忆", "model": "模型配置",
    "template": "模板", "prompt": "Prompt", "system": "系统设置", "other": "其他",
}
SECURITY_KINDS = {"auth", "user", "member", "model", "prompt", "system"}

# (方法, 路径正则) → (kind, action)；正则命名组 target 为对象 ID
_RULES: list[tuple[str, re.Pattern, str, str]] = [(m, re.compile(p), k, a) for m, p, k, a in [
    ("POST", r"^/api/v1/auth/logout$", "auth", "登出"),
    ("POST", r"^/api/v1/auth/password$", "auth", "修改密码"),
    ("POST", r"^/api/v1/auth/totp/setup$", "auth", "两步验证·开始绑定"),
    ("POST", r"^/api/v1/auth/totp/enable$", "auth", "两步验证·绑定"),
    ("POST", r"^/api/v1/auth/totp/disable$", "auth", "两步验证·解绑"),
    ("PUT", r"^/api/v1/auth/settings$", "system", "修改安全设置"),
    ("PUT", r"^/api/v1/auth/me$", "user", "修改个人资料"),
    ("POST", r"^/api/v1/auth/users$", "user", "创建用户"),
    ("PUT", r"^/api/v1/auth/users/(?P<target>[^/]+)$", "user", "修改用户"),
    ("DELETE", r"^/api/v1/auth/users/(?P<target>[^/]+)$", "user", "删除用户"),
    ("POST", r"^/api/v1/projects$", "project", "创建项目"),
    ("PUT", r"^/api/v1/projects/(?P<target>[^/]+)$", "project", "修改项目"),
    ("DELETE", r"^/api/v1/projects/(?P<target>[^/]+)$", "project", "删除项目"),
    ("PUT", r"^/api/v1/projects/(?P<target>[^/]+)/members$", "member", "设置成员角色"),
    ("DELETE", r"^/api/v1/projects/(?P<target>[^/]+)/members/[^/]+$", "member", "移出成员"),
    ("POST", r"^/api/v1/projects/(?P<target>[^/]+)/versions$", "project", "新建版本"),
    ("PUT", r"^/api/v1/projects/(?P<target>[^/]+)/versions/[^/]+$", "project", "修改版本"),
    ("DELETE", r"^/api/v1/projects/(?P<target>[^/]+)/versions/[^/]+$", "project", "删除版本"),
    ("POST", r"^/api/v1/projects/(?P<target>[^/]+)/modules/reorder$", "project", "模块排序"),
    ("POST", r"^/api/v1/projects/(?P<target>[^/]+)/modules/[^/]+/restore$", "project", "恢复模块"),
    ("POST", r"^/api/v1/projects/(?P<target>[^/]+)/modules$", "project", "新建模块"),
    ("PUT", r"^/api/v1/projects/(?P<target>[^/]+)/modules/[^/]+$", "project", "修改/移动模块"),
    ("DELETE", r"^/api/v1/projects/(?P<target>[^/]+)/modules/[^/]+$", "project", "删除模块"),
    ("POST", r"^/api/v1/requirements$", "requirement", "创建需求"),
    ("PUT", r"^/api/v1/requirements/(?P<target>[^/]+)$", "requirement", "修改需求"),
    ("DELETE", r"^/api/v1/requirements/(?P<target>[^/]+)$", "requirement", "删除需求"),
    ("POST", r"^/api/v1/requirements/(?P<target>[^/]+)/restore$", "requirement", "恢复需求"),
    ("POST", r"^/api/v1/requirements/(?P<target>[^/]+)/attachments$", "requirement", "上传需求附件"),
    ("POST", r"^/api/v1/requirements/(?P<target>[^/]+)/attachments/[^/]+/reparse$", "requirement", "重新解析附件"),
    ("POST", r"^/api/v1/requirements/(?P<target>[^/]+)/analyze$", "ai", "AI 需求分析"),
    ("POST", r"^/api/v1/requirements/(?P<target>[^/]+)/questions$", "requirement", "补充待确认事项"),
    ("POST", r"^/api/v1/requirements/(?P<target>[^/]+)/questions/[^/]+$", "requirement", "确认待确认事项"),
    ("POST", r"^/api/v1/requirements/(?P<target>[^/]+)/design$", "ai", "发起测试设计"),
    ("POST", r"^/api/v1/tasks$", "ai", "创建生成任务"),
    ("POST", r"^/api/v1/tasks/(?P<target>[^/]+)/confirm$", "point", "确认测试点并生成"),
    ("POST", r"^/api/v1/tasks/(?P<target>[^/]+)/revise$", "ai", "对话修订"),
    ("POST", r"^/api/v1/tasks/(?P<target>[^/]+)/review$", "case", "用例评审"),
    ("POST", r"^/api/v1/tasks/(?P<target>[^/]+)/points/review$", "point", "测试点评审"),
    ("POST", r"^/api/v1/tasks/(?P<target>[^/]+)/points/fix$", "ai", "AI 修改测试点"),
    ("POST", r"^/api/v1/tasks/(?P<target>[^/]+)/points/add$", "point", "补充测试点"),
    ("POST", r"^/api/v1/tasks/(?P<target>[^/]+)/points/gap-check$", "ai", "覆盖查漏"),
    ("POST", r"^/api/v1/tasks/(?P<target>[^/]+)/points/dup-check$", "ai", "测试点查重"),
    ("POST", r"^/api/v1/tasks/(?P<target>[^/]+)/cases/fix$", "ai", "AI 修改用例"),
    ("POST", r"^/api/v1/tasks/(?P<target>[^/]+)/fix/confirm$", "case", "确认 AI 修改提案"),
    ("POST", r"^/api/v1/tasks/(?P<target>[^/]+)/versions/restore$", "case", "恢复历史版本"),
    ("POST", r"^/api/v1/tasks/(?P<target>[^/]+)/recycle-bin/restore$", "case", "回收站恢复"),
    ("DELETE", r"^/api/v1/tasks/(?P<target>[^/]+)/recycle-bin/[^/]+$", "case", "永久删除"),
    ("POST", r"^/api/v1/tasks/(?P<target>[^/]+)/requirement-diff/apply$", "ai", "应用需求变更"),
    ("POST", r"^/api/v1/tasks/(?P<target>[^/]+)/requirement-diff$", "ai", "需求变更分析"),
    ("POST", r"^/api/v1/tasks/(?P<target>[^/]+)/final$", "case", "终稿回传"),
    ("POST", r"^/api/v1/tasks/(?P<target>[^/]+)/cancel$", "ai", "取消任务"),
    ("POST", r"^/api/v1/tasks/(?P<target>[^/]+)/retry$", "ai", "重试任务"),
    ("POST", r"^/api/v1/tasks/(?P<target>[^/]+)/executions.*$", "exec", "任务级执行（旧）"),
    ("POST", r"^/api/v1/plans$", "plan", "创建测试计划"),
    ("PUT", r"^/api/v1/plans/(?P<target>[^/]+)$", "plan", "修改测试计划"),
    ("DELETE", r"^/api/v1/plans/(?P<target>[^/]+)$", "plan", "删除测试计划"),
    ("POST", r"^/api/v1/plans/(?P<target>[^/]+)/cases$", "plan", "加入用例快照"),
    ("DELETE", r"^/api/v1/plans/(?P<target>[^/]+)/cases/[^/]+$", "plan", "移出用例"),
    ("POST", r"^/api/v1/plans/(?P<target>[^/]+)/assign$", "plan", "任务分配"),
    ("POST", r"^/api/v1/plans/(?P<target>[^/]+)/runs$", "exec", "新建执行轮次"),
    ("POST", r"^/api/v1/plans/(?P<target>[^/]+)/runs/[^/]+/results$", "exec", "记录执行结果"),
    ("POST", r"^/api/v1/plans/(?P<target>[^/]+)/runs/[^/]+/finish$", "exec", "结束执行轮次"),
    ("POST", r"^/api/v1/plans/(?P<target>[^/]+)/runs/[^/]+/attachments$", "exec", "上传执行附件"),
    ("POST", r"^/api/v1/knowledge/docs$", "knowledge", "上传知识文档"),
    ("DELETE", r"^/api/v1/knowledge/docs/(?P<target>[^/]+)$", "knowledge", "删除知识文档"),
    ("POST", r"^/api/v1/knowledge/cases$", "knowledge", "导入历史用例"),
    ("POST", r"^/api/v1/learning/analyze$", "ai", "修改习惯分析"),
    ("POST", r"^/api/v1/learning/rules/(?P<target>[^/]+)/confirm$", "learning", "确认规则"),
    ("POST", r"^/api/v1/learning/rules/(?P<target>[^/]+)/ignore$", "learning", "忽略规则"),
    ("PUT", r"^/api/v1/learning/rules/(?P<target>[^/]+)$", "learning", "修改规则"),
    ("DELETE", r"^/api/v1/learning/rules/(?P<target>[^/]+)$", "learning", "删除规则"),
    ("POST", r"^/api/v1/memories$", "memory", "新增记忆"),
    ("PUT", r"^/api/v1/memories/(?P<target>[^/]+)$", "memory", "修改记忆"),
    ("DELETE", r"^/api/v1/memories(/(?P<target>[^/]+))?$", "memory", "删除记忆"),
    ("PUT", r"^/api/v1/models/config$", "model", "修改模型配置"),
    ("POST", r"^/api/v1/templates$", "template", "上传模板"),
    ("PUT", r"^/api/v1/templates/(?P<target>[^/]+)$", "template", "修改模板"),
    ("DELETE", r"^/api/v1/templates/(?P<target>[^/]+)$", "template", "删除模板"),
    ("POST", r"^/api/v1/templates/(?P<target>[^/]+)/default$", "template", "设为默认模板"),
    ("POST", r"^/api/v1/ai/prompts/(?P<target>[^/]+)/versions$", "prompt", "新建 Prompt 版本"),
    ("POST", r"^/api/v1/ai/prompts/(?P<target>[^/]+)/versions/[^/]+/activate$", "prompt", "激活 Prompt 版本"),
    ("POST", r"^/api/v1/ai/prompts/(?P<target>[^/]+)/versions/[^/]+/archive$", "prompt", "归档 Prompt 版本"),
]]

# 不记日志的写接口（高频噪音）
_SKIP = [re.compile(p) for p in (
    r"^/api/v1/tasks/[^/]+/editing$", r"^/api/v1/projects/[^/]+/favorite$", r"^/api/v1/models/test$",
    r"^/api/v1/knowledge/search$", r"^/api/v1/parse$", r"^/api/v1/auth/login$",
)]


def describe(method: str, path: str) -> tuple[str, str, str] | None:
    """(kind, action, target)；None 表示不记录。"""
    if method in ("GET", "HEAD", "OPTIONS") or any(p.match(path) for p in _SKIP):
        return None
    for m, pat, kind, action in _RULES:
        if m == method:
            mm = pat.match(path)
            if mm:
                return kind, action, (mm.groupdict().get("target") or "")
    if path.startswith("/api/v1"):
        return "other", f"{method} {path}", ""
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record(*, user: str | None, ip: str, ua: str, kind: str, action: str, target: str = "",
           project: str | None = None, method: str = "", path: str = "", status: int = 200,
           detail: str = "", duration_ms: int = 0, security: bool | None = None) -> None:
    try:
        from sqlalchemy import insert

        with get_engine().begin() as conn:
            conn.execute(insert(audit_log).values(
                at=_now(), user=(str(user)[:64] if user else None), ip=(ip or "")[:64], ua=(ua or "")[:300],
                project=(str(project)[:191] if project else None),
                kind=kind, action=action[:64], target=str(target or "")[:191], method=method, path=path[:300],
                status=int(status), detail=(detail or "")[:5000], duration_ms=int(duration_ms),
                security=1 if (security if security is not None else kind in SECURITY_KINDS) else 0,
            ))
    except Exception as e:  # pragma: no cover
        from loguru import logger

        logger.warning("操作日志写入失败：{}", e)


def list_logs(*, project: str | None = None, kind: str | None = None, user: str | None = None,
              keyword: str | None = None, since: str | None = None, security: bool | None = None,
              visible: set[str] | None = None, me: str | None = None, failed_only: bool = False,
              page: int = 1, page_size: int = 50) -> dict:
    conds = []
    if project:
        conds.append(audit_log.c.project == project)
    if kind:
        conds.append(audit_log.c.kind == kind)
    if user:
        conds.append(audit_log.c.user == user)
    if since:
        conds.append(audit_log.c.at >= since)
    if security is not None:
        conds.append(audit_log.c.security == (1 if security else 0))
    if failed_only:
        conds.append(audit_log.c.status >= 400)
    if keyword:
        kw = f"%{keyword}%"
        conds.append(audit_log.c.action.like(kw) | audit_log.c.target.like(kw) | audit_log.c.detail.like(kw)
                     | audit_log.c.path.like(kw))
    if visible is not None:
        # 非管理员：所属项目的业务日志 + 自己的操作；安全日志不可见
        scope = audit_log.c.project.in_(sorted(visible)) if visible else audit_log.c.project == "__none__"
        conds.append((scope | (audit_log.c.user == me)) & (audit_log.c.security == 0))
    with get_engine().begin() as conn:
        total = conn.execute(select(func.count()).select_from(audit_log).where(*conds)).scalar_one()
        rows = conn.execute(select(audit_log).where(*conds).order_by(audit_log.c.id.desc())
                            .offset((page - 1) * page_size).limit(page_size)).mappings().all()
    return {"total": total, "page": page, "page_size": page_size,
            "items": [{**dict(r), "kind_label": KINDS.get(r["kind"], r["kind"])} for r in rows]}


def rename_project(old: str, new: str) -> int:
    from sqlalchemy import update

    with get_engine().begin() as conn:
        return conn.execute(update(audit_log).where(audit_log.c.project == old).values(project=new)).rowcount


def purge_before(at: str) -> int:
    from sqlalchemy import delete

    with get_engine().begin() as conn:
        return conn.execute(delete(audit_log).where(audit_log.c.at < at)).rowcount


def recent_by_user(user: str, limit: int = 10) -> list[dict]:
    with get_engine().begin() as conn:
        rows = conn.execute(select(audit_log).where(audit_log.c.user == user, audit_log.c.status < 400)
                            .order_by(audit_log.c.id.desc()).limit(limit)).mappings().all()
    return [{**dict(r), "kind_label": KINDS.get(r["kind"], r["kind"])} for r in rows]

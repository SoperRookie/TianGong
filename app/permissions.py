"""项目级权限矩阵（完整需求 3.4 / 3.5）。

系统级角色（admin / member，见 app.auth）与项目级角色分离：
- 系统管理员：全平台配置与管理，对任何项目视同项目管理员；
- 项目级角色由项目成员关系决定，同一用户在不同项目可拥有不同角色。

动作粒度按需求 3.5 定义，各业务接口以 (项目, 动作) 校验；
未加入项目的用户对该项目数据一律不可见（接口级数据隔离，3.3）。
"""

from __future__ import annotations

PROJECT_ROLES = {
    "project_admin": "项目管理员",
    "test_lead": "测试负责人",
    "tester": "测试人员",
    "viewer": "只读人员",
}

ACTIONS = frozenset({
    # 项目：查看、编辑、成员管理
    "project.view", "project.edit", "project.members",
    # 版本/模块：查看、新增/编辑/归档/删除
    "version.view", "version.manage",
    # 需求（M2 需求中心接入时启用）：查看、新增/编辑/删除/上传、AI 分析
    "requirement.view", "requirement.edit", "requirement.ai",
    # 测试点：查看、新增/编辑/删除、提交评审/评审、AI 生成/AI 修改
    "point.view", "point.edit", "point.review", "point.ai",
    # 用例：查看、新增/编辑/删除/复制/导入、评审、AI 生成/修改、导出
    "case.view", "case.edit", "case.review", "case.ai", "case.export",
    # 计划：查看、创建/编辑/删除/结束、任务分配
    "plan.view", "plan.manage", "plan.assign",
    # 执行：查看、执行/修改结果、上传附件
    "exec.view", "exec.run", "exec.attach",
    # 日志：项目日志与 AI 操作日志查看
    "log.view",
})

_VIEW_ACTIONS = frozenset(a for a in ACTIONS if a.endswith(".view"))

ROLE_ACTIONS: dict[str, frozenset[str]] = {
    "project_admin": ACTIONS,
    "test_lead": ACTIONS - {"project.edit", "project.members"},
    "tester": _VIEW_ACTIONS | {
        "point.edit", "point.ai", "case.edit", "case.ai", "case.export",
        "exec.run", "exec.attach",
    },
    "viewer": _VIEW_ACTIONS,
}


def role_allows(role: str | None, action: str) -> bool:
    if action not in ACTIONS:
        raise ValueError(f"未知权限动作: {action}")
    return action in ROLE_ACTIONS.get(role or "", frozenset())


def is_write_action(action: str) -> bool:
    """非查看类动作：已归档项目一律拒绝。"""
    return action not in _VIEW_ACTIONS

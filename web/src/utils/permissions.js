/* 与 app/permissions.py 同口径的项目级权限矩阵（前端只做显隐，后端仍校验）。 */
export const PROJECT_ROLES = { project_admin: '项目管理员', test_lead: '测试负责人', tester: '测试人员', viewer: '只读人员' }
const ALL = [
  'project.view', 'project.edit', 'project.members', 'version.view', 'version.manage',
  'requirement.view', 'requirement.edit', 'requirement.ai',
  'point.view', 'point.edit', 'point.review', 'point.ai',
  'case.view', 'case.edit', 'case.review', 'case.ai', 'case.export',
  'plan.view', 'plan.manage', 'plan.assign', 'exec.view', 'exec.run', 'exec.attach',
  'log.view', 'knowledge.view', 'knowledge.manage',
]
const VIEW = ALL.filter(a => a.endsWith('.view'))
export const ROLE_ACTIONS = {
  project_admin: new Set(ALL),
  test_lead: new Set(ALL.filter(a => !['project.edit', 'project.members'].includes(a))),
  tester: new Set([...VIEW, 'point.edit', 'point.ai', 'case.edit', 'case.ai', 'case.export', 'exec.run', 'exec.attach']),
  viewer: new Set(VIEW),
}
export const roleAllows = (role, action) => !!(ROLE_ACTIONS[role] && ROLE_ACTIONS[role].has(action))

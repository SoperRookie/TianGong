/* 项目通用动作：收藏、状态流转、删除权限口径、角色文案；项目列表 / 我的项目 / 项目详情共用。 */
import { computed } from 'vue'
import { api, seg } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useScopeStore } from '@/stores/scope'
import { useUiStore } from '@/stores/ui'
import { PROJECT_ROLES } from '@/utils/permissions'

/** 后端 app/reports.py:UNASSIGNED —— 未指定项目的任务聚合行 */
export const UNASSIGNED = '（未指定）'
export const LINE_ROLES = { line_admin: '产品线管理员', member: '成员', viewer: '只读' }
export const roleLabel = r => PROJECT_ROLES[r] || r || ''
export const lineRoleLabel = r => LINE_ROLES[r] || r || ''
export const STATUS_FLOW = { active: '进行中', paused: '暂停', archived: '已归档' }

export function useProjectActions() {
  const auth = useAuthStore(), scope = useScopeStore(), ui = useUiStore()

  /** 删除类动作（版本 / 模块 / 依赖边等）：开放 / 按产品线模式下仅系统管理员；按项目成员模式按项目角色。 */
  const canDelete = computed(() => auth.isAdmin || auth.accessMode === 'member')
  /** 我在某产品线的角色（/auth/me 的 business_lines：[{business_line, role}]） */
  const lineRole = line => (auth.user?.business_lines || []).find(x => x.business_line === line)?.role || null
  /** 产品线维护：系统管理员 / 开放模式任何人 / 按产品线模式该产品线管理员 */
  const canManageLine = line => auth.isAdmin || auth.accessMode === 'open' || (auth.accessMode === 'line' && !!line && lineRole(line) === 'line_admin')
  /** 新建项目：系统管理员 / 开放模式 / 按产品线模式下至少是一个产品线的管理员 */
  const canCreateProject = computed(() => auth.isAdmin || auth.accessMode === 'open'
    || (auth.accessMode === 'line' && (auth.user?.business_lines || []).some(x => x.role === 'line_admin')))
  const isProjectAdmin = name => auth.roleIn(name) === 'project_admin'

  async function refreshAll() { await Promise.all([scope.load(true), auth.fetchMe()]) }

  async function toggleFavorite(name) {
    const r = await api.post(`/api/v1/projects/${seg(name)}/favorite`, {})
    ui.toast(r.favorite ? `已收藏「${name}」，列表置顶` : '已取消收藏')
    await refreshAll()
    return r.favorite
  }

  /** 归档 / 恢复 / 暂停：只发 status（归档项目唯一允许的修改是恢复） */
  async function setStatus(name, status) {
    const label = STATUS_FLOW[status] || status
    if (status === 'archived' && !await ui.confirm(`归档项目「${name}」？`, '归档后项目内数据只读，恢复后才能修改。', { ok: '归档' })) return false
    await api.put(`/api/v1/projects/${seg(name)}`, { status })
    ui.toast(`项目已${status === 'active' ? '恢复为进行中' : label}`)
    await scope.load(true)
    return true
  }

  return { auth, scope, ui, canDelete, lineRole, canManageLine, canCreateProject, isProjectAdmin, toggleFavorite, setStatus, refreshAll }
}

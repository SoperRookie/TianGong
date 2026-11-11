import { defineStore } from 'pinia'
import { api } from '@/api/client'
import { roleAllows } from '@/utils/permissions'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem('tg_token') || '',
    user: null,              // GET /api/v1/auth/me：{username,name,role,email,phone,avatar,totp_bound,must_change_password,projects[{project,role,status}],prefs,access_mode,business_lines}
    settings: null,          // GET /api/v1/auth/settings：{totp_enabled, access_mode}
    ready: false,
    mustChangePassword: false,
    expired: false,
    version: '',
  }),
  getters: {
    isLogged: s => !!s.token && !!s.user,
    isAdmin: s => s.user?.role === 'admin',
    me: s => s.user?.username || '',
    displayName: s => s.user?.name || s.user?.username || '',
    accessMode: s => s.user?.access_mode || s.settings?.access_mode || 'members',
    /** 项目内角色：管理员视同项目管理员 */
    roleIn: s => (project) => {
      if (s.user?.role === 'admin') return 'project_admin'
      return (s.user?.projects || []).find(p => p.project === project)?.role || null
    },
  },
  actions: {
    /** 项目级权限显隐：auth.can(project, 'case.review') */
    can(project, action) {
      if (this.isAdmin) return true
      return roleAllows(this.roleIn(project), action)
    },
    async login({ username, password, otp }) {
      const r = await api.post('/api/v1/auth/login', { username, password, otp: otp || null })
      if (r.otp_required) return { otpRequired: true }
      this.token = r.token
      localStorage.setItem('tg_token', r.token)
      this.user = r.user
      this.expired = false
      this.mustChangePassword = !!r.user?.must_change_password
      this.loadSettings()
      return { ok: true }
    },
    async fetchMe() {
      if (!this.token) { this.ready = true; return null }
      try {
        this.user = await api.get('/api/v1/auth/me')
        this.mustChangePassword = !!this.user?.must_change_password
        this.loadSettings()
      } catch (e) {
        if (e.status === 401) this.sessionExpired()
      } finally {
        this.ready = true
      }
      return this.user
    },
    async loadSettings() {
      try { this.settings = await api.get('/api/v1/auth/settings') } catch { /* 非关键 */ }
      try { const h = await fetch('/health').then(r => r.json()); this.version = h.version || '' } catch { /* ignore */ }
    },
    async logout() {
      try { if (this.token) await api.post('/api/v1/auth/logout', {}) } catch { /* ignore */ }
      this.clear()
    },
    clear() {
      this.token = ''; this.user = null; this.mustChangePassword = false
      localStorage.removeItem('tg_token')
    },
    sessionExpired() {
      const hadToken = !!this.token   // 从未登录过的 401 不算「会话过期」，登录页不提示
      this.clear(); this.expired = hadToken
    },
    requirePasswordChange() { this.mustChangePassword = true },
    async changePassword(old_password, new_password) {
      await api.post('/api/v1/auth/password', { old_password, new_password })
      this.mustChangePassword = false
      if (this.user) this.user.must_change_password = false
    },
  },
})

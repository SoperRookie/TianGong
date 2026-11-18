/* 工作范围：产品线 + 项目（全局筛选，持久化）；模块树与端筛选（只在需求 / 设计类页面显示）。 */
import { defineStore } from 'pinia'
import { api, seg } from '@/api/client'

export const useScopeStore = defineStore('scope', {
  state: () => ({
    projects: [],            // GET /api/v1/projects 的 projects[]（含 my_role / favorite / 统计）
    lines: [],               // 产品线名列表
    project: localStorage.getItem('tg_project') || '',
    line: localStorage.getItem('tg_line') || '',
    loaded: false,
    modules: { tree: [], deleted: [], max_depth: 5 },
    modulesFor: '',
    module: '',              // 模块筛选（模块名；'' = 全部）
    terminal: '',            // 端筛选（key；'' = 全部）
    moduleCounts: null,      // 页面可写入 {模块名: 数量} 用于树上计数
    pickerOpen: false,
  }),
  getters: {
    current: s => s.projects.find(p => p.project === s.project) || null,
    projectsInLine: s => s.line ? s.projects.filter(p => p.business_line === s.line) : s.projects,
    label: s => {
      const p = s.projects.find(x => x.project === s.project)
      return s.project ? `${p?.business_line || s.line || '未分产品线'} / ${s.project}` : (s.line ? `${s.line} / 全部项目` : '全部项目')
    },
    /** 列表接口通用查询参数 */
    query: s => ({ project: s.project || undefined, business_line: !s.project && s.line ? s.line : undefined }),
    flatModules: s => {
      const out = []
      const walk = (nodes, depth, parentPath) => (nodes || []).forEach(n => {
        const path = parentPath ? `${parentPath}/${n.name}` : n.name
        out.push({ ...n, depth, path })
        walk(n.children, depth + 1, path)
      })
      walk(s.modules.tree, 0, '')
      return out
    },
  },
  actions: {
    async load(force = false) {
      if (this.loaded && !force) return
      const r = await api.get('/api/v1/projects', { include_archived: true })
      this.projects = r.projects || []
      this.lines = r.business_lines || []
      if (this.project && !this.projects.some(p => p.project === this.project)) this.project = ''
      if (!this.project && !this.line) {
        const fav = this.projects.find(p => p.favorite && p.status === 'active') || this.projects.find(p => p.status === 'active') || this.projects[0]
        if (fav) this.setProject(fav.project, false)
      }
      this.loaded = true
      if (this.project) this.loadModules()
    },
    setProject(name, persist = true) {
      this.project = name || ''
      const p = this.projects.find(x => x.project === name)
      if (p) this.line = p.business_line || ''
      this.module = ''; this.terminal = ''; this.moduleCounts = null
      if (persist) { localStorage.setItem('tg_project', this.project); localStorage.setItem('tg_line', this.line) }
      if (this.project) this.loadModules(); else this.modules = { tree: [], deleted: [], max_depth: 5 }
    },
    setLine(line) {
      this.line = line || ''
      this.project = ''
      this.module = ''; this.terminal = ''
      localStorage.setItem('tg_project', ''); localStorage.setItem('tg_line', this.line)
      this.modules = { tree: [], deleted: [], max_depth: 5 }
    },
    async loadModules(force = false) {
      if (!this.project) return
      if (this.modulesFor === this.project && !force) return
      try {
        this.modules = await api.get(`/api/v1/projects/${seg(this.project)}/modules`)
        this.modulesFor = this.project
      } catch { this.modules = { tree: [], deleted: [], max_depth: 5 } }
    },
    setModule(name) { this.module = name || '' },
    setTerminal(key) { this.terminal = key || '' },
  },
})

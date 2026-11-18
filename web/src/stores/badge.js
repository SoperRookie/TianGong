/* 顶栏角标与通知：来自工作台聚合接口（待审核 / 待修改 / 待执行 / 待确认需求 / 我的 AI 任务）。 */
import { defineStore } from 'pinia'
import { api } from '@/api/client'

export const useBadgeStore = defineStore('badge', {
  state: () => ({ wb: null, at: 0, loading: false }),
  getters: {
    design: s => (s.wb?.pending_review?.count || 0) + (s.wb?.to_fix?.count || 0),
    exec: s => s.wb?.to_execute?.count || 0,
    requirement: s => s.wb?.pending_requirements?.count || 0,
    ai: s => (s.wb?.my_ai_tasks?.failed || 0) + (s.wb?.my_ai_tasks?.awaiting || 0),
    total() { return this.design + this.exec + this.requirement + this.ai },
    items() {
      const w = this.wb; if (!w) return []
      const out = []
      if (w.pending_review?.count) out.push({ key: 'review', label: '待我审核的测试点 / 用例', count: w.pending_review.count, to: { name: 'designs', query: { filter: 'review' } } })
      if (w.to_fix?.count) out.push({ key: 'fix', label: '被驳回待修改', count: w.to_fix.count, to: { name: 'designs', query: { filter: 'fix' } } })
      if (w.to_execute?.count) out.push({ key: 'exec', label: '分配给我待执行', count: w.to_execute.count, to: { name: 'exec' } })
      if (w.pending_requirements?.count) out.push({ key: 'req', label: '待确认的需求', count: w.pending_requirements.count, to: { name: 'requirements', query: { status: 'pending_confirm' } } })
      if (w.my_ai_tasks?.failed) out.push({ key: 'aifail', label: '失败的 AI 任务', count: w.my_ai_tasks.failed, to: { name: 'aitasks', query: { status: 'failed' } } })
      if (w.my_ai_tasks?.awaiting) out.push({ key: 'aiwait', label: '等待确认测试点的任务', count: w.my_ai_tasks.awaiting, to: { name: 'aitasks', query: { status: 'awaiting_confirmation' } } })
      return out
    },
  },
  actions: {
    async refresh(silent = false) {
      if (this.loading) return
      this.loading = true
      try { this.wb = await api.get('/api/v1/workbench'); this.at = Date.now() } catch { /* 静默 */ } finally { this.loading = false }
    },
  },
})

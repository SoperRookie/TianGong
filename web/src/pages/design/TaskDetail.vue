<script setup>
/* 设计任务详情（5.5–5.11 / 5.13–5.15 / 5.18 / 5.20–5.23 / 7.12 / 7.13）：页头主按钮按状态变化，页签 overview / revise / export / versions / recycle / reqdiff / final / executions。 */
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { useBadgeStore } from '@/stores/badge'
import { fmtTime } from '@/utils/format'
import { PageHeading, TgPanel, TgBadge, TgTabs, TgDropdown, TgEmpty, TgIcon } from '@/components/ui'
import TaskPipeline from './components/TaskPipeline.vue'
import TaskOverview from './components/TaskOverview.vue'
import TaskRevise from './components/TaskRevise.vue'
import TaskExport from './components/TaskExport.vue'
import TaskVersions from './components/TaskVersions.vue'
import TaskRecycle from './components/TaskRecycle.vue'
import TaskReqDiff from './components/TaskReqDiff.vue'
import TaskFinal from './components/TaskFinal.vue'
import TaskExecutions from './components/TaskExecutions.vue'
import { taskTitle, isManual, progressText, resumeLabel, resumeHint, retryToast, pointStats, caseStats } from './components/taskUtils'

const route = useRoute(), router = useRouter(), auth = useAuthStore(), ui = useUiStore(), badge = useBadgeStore()
const id = computed(() => route.params.id)
const sub = computed(() => route.params.sub || 'overview')
const { data: t, loading, error, reload } = useLoad(() => api.get(`/api/v1/tasks/${id.value}`), { deps: [id] })
const ctx = computed(() => t.value?.context || {})
const project = computed(() => ctx.value.project || '')
const running = computed(() => ['queued', 'running'].includes(t.value?.status))
const ps = computed(() => pointStats(t.value)), cs = computed(() => caseStats(t.value))
const hasCases = computed(() => cs.value.total > 0)
const title = computed(() => taskTitle(t.value))
watch(title, v => ui.setCrumb(v), { immediate: true })

// 执行中轮询
let timer = null
watch(running, v => { clearInterval(timer); timer = null; if (v) timer = setInterval(() => reload(true), 5000) }, { immediate: true })
onBeforeUnmount(() => clearInterval(timer))

const tabs = computed(() => {
  const tv = t.value || {}
  const list = [{ key: 'overview', label: '概览' }]
  if (!isManual(tv)) list.push({ key: 'revise', label: '对话修订', count: (tv.revisions || []).length || undefined })
  list.push({ key: 'export', label: '导出' })
  list.push({ key: 'versions', label: '版本链' })
  list.push({ key: 'recycle', label: '回收站' })
  if (!isManual(tv)) list.push({ key: 'reqdiff', label: '需求变更分析', count: tv.requirement_diff ? (tv.requirement_diff.changes || []).length : undefined })
  list.push({ key: 'final', label: '离线终稿', count: tv.offline_review ? 1 : undefined })
  if ((tv.executions || []).length || tv.exec_migrated_to) list.push({ key: 'executions', label: '执行记录（旧）', count: (tv.executions || []).length || undefined })
  return list
})
function setSub(k) { router.replace({ name: 'design-detail', params: { id: id.value, sub: k === 'overview' ? undefined : k }, query: k === 'versions' ? route.query : {} }) }

// ---- 主按钮与更多 ----
const primary = computed(() => {
  const tv = t.value; if (!tv) return null
  if (tv.status === 'awaiting_confirmation') return { label: '去审核测试点', to: { name: 'points', params: { tid: tv.task_id } } }
  if (tv.status === 'completed' && (hasCases.value || isManual(tv))) return { label: isManual(tv) ? '去维护用例' : '去审核用例', to: { name: 'cases', params: { tid: tv.task_id } } }
  if (tv.status === 'completed' && ps.value.total) return { label: '去审核测试点', to: { name: 'points', params: { tid: tv.task_id } } }
  if (tv.status === 'failed' && tv.resume?.resumable && auth.can(project.value, 'point.ai')) return { label: resumeLabel(tv), action: retry }
  if (running.value && auth.can(project.value, 'point.ai')) return { label: tv.progress_detail?.job_active === false ? '标记失败' : '取消任务', action: () => cancel(tv.progress_detail?.job_active === false), danger: true }
  return null
})
async function retry() {
  try { const r = await api.post(`/api/v1/tasks/${id.value}/retry`, {}); ui.toast(retryToast(r)); reload(true); badge.refresh(true) } catch (e) { ui.error(e) }
}
async function cancel(force) {
  if (force) { if (!await ui.confirm('标记失败？', '该任务在后台没有执行句柄（取消 / 重启打断或投入后台时出错），不会再有进展。标记失败后可点「继续完成」从检查点续跑。', { ok: '标记失败', danger: true })) return }
  else if (!await ui.confirm('取消任务？', '中断后台执行，任务将标记为失败（可继续完成）。', { ok: '取消任务', danger: true })) return
  try { await api.post(`/api/v1/tasks/${id.value}/cancel${force ? '?force=true' : ''}`, {}); ui.toast(force ? '已标记失败，可点「继续完成」' : '任务已取消'); reload(true); badge.refresh(true) } catch (e) { ui.error(e) }
}
async function remove() {
  if (!await ui.confirm('删除任务？', `「${title.value}」及其测试点 / 用例将从列表与详情隐藏；版本历史与审计留档保留，可在任务列表「已删除」里恢复。`, { ok: '删除', danger: true })) return
  try { await api.del(`/api/v1/tasks/${id.value}`); ui.toast('任务已删除，可在任务列表「已删除」里恢复'); router.push({ name: 'designs' }) } catch (e) { ui.error(e) }
}
const menu = computed(() => {
  const tv = t.value; if (!tv) return []
  const items = []
  if (ps.value.total) items.push({ label: '测试点审核', action: () => router.push({ name: 'points', params: { tid: tv.task_id } }) })
  if (hasCases.value || isManual(tv)) items.push({ label: isManual(tv) ? '用例维护' : '用例审核', action: () => router.push({ name: 'cases', params: { tid: tv.task_id } }) })
  items.push({ label: '刷新', action: () => reload() })
  if (running.value && auth.can(project.value, 'point.ai')) {
    items.push({ divider: true }, { label: '取消任务', action: () => cancel(false) }, { label: '标记失败（无后台句柄）', action: () => cancel(true), hint: 'force' })
  }
  if (tv.status === 'failed' && tv.resume?.resumable && auth.can(project.value, 'point.ai')) items.push({ label: resumeLabel(tv), action: retry })
  if (tv.status === 'completed' && tv.resume?.mode === 'fill' && auth.can(project.value, 'point.ai')) items.push({ label: '补全失败模块', action: retry })
  if (!running.value && auth.isAdmin) items.push({ divider: true }, { label: '删除任务', danger: true, action: remove })
  return items
})
</script>
<template>
  <PageHeading :title="t ? title : '设计任务'">
    <template #back><router-link :to="{ name: 'designs' }">← 设计任务</router-link></template>
    <template #badge><TgBadge v-if="t" :status="t.status" /><span v-if="t && isManual(t)" class="tag manual">人工</span><span v-if="t?.progress" class="tiny muted" style="margin-left:6px"><span class="spinner"></span> {{ progressText(t.progress) }}</span></template>
    <template #sub><template v-if="t">{{ project || '（未指定项目）' }} · <span class="mono">{{ t.task_id }}</span> · {{ isManual(t) ? '人工用例集' : (ctx.model || '默认模型') }} · {{ fmtTime(t.created_at) }} · {{ t.created_by || '—' }}</template></template>
    <template #actions>
      <template v-if="primary">
        <router-link v-if="primary.to" class="btn primary lg" :to="primary.to">{{ primary.label }}</router-link>
        <button v-else type="button" class="btn lg" :class="primary.danger ? 'danger' : 'primary'" @click="primary.action">{{ primary.label }}</button>
      </template>
      <TgDropdown :items="menu" label="更多" />
    </template>
  </PageHeading>
  <div v-if="error" class="err-box">{{ error.message }} <router-link :to="{ name: 'designs' }">返回列表</router-link></div>
  <div v-else-if="!t && loading" class="page-loading"><span class="spinner"></span> 加载中…</div>
  <template v-else-if="t">
    <div v-if="t.error" class="attention" style="margin-bottom:12px"><TgIcon name="alert" :size="14" /><span><b>{{ t.error }}</b><span v-if="resumeHint(t)" class="muted"> · {{ resumeHint(t) }}</span></span><button v-if="t.resume?.resumable && t.status === 'failed' && auth.can(project, 'point.ai')" type="button" class="btn small" @click="retry">{{ resumeLabel(t) }}</button></div>
    <div v-if="t.status === 'completed' && t.resume?.mode === 'fill'" class="attention" style="margin-bottom:12px"><TgIcon name="alert" :size="14" /><span>{{ t.resume.failed_modules.length }} 个模块生成失败（{{ t.resume.failed_modules.join('、') }}），已生成的用例保留 · {{ resumeHint(t) }}</span><button v-if="auth.can(project, 'point.ai')" type="button" class="btn small" @click="retry">补全失败模块</button></div>
    <div v-if="t.deleted_at" class="attention" style="margin-bottom:12px"><TgIcon name="alert" :size="14" /> 该任务已于 {{ fmtTime(t.deleted_at) }} 被 {{ t.deleted_by }} 删除。</div>
    <TgPanel v-if="!isManual(t)" title="流水线" :hint="ctx.confirm_points ? '拆解 → 质量检查 → 人工确认测试点 → 按模块并行生成 → 评审回环 ≤ 3 轮' : '直接生成路径：拆解后不经人工确认直接生成用例'">
      <template #actions><TgBadge :status="t.status" /><span v-if="t.progress_detail?.job_active === false" class="badge orange">无后台句柄（孤儿任务）</span></template>
      <TaskPipeline :task="t" />
    </TgPanel>
    <TgTabs :model-value="sub" :items="tabs" style="margin-top:14px" @update:modelValue="setSub" />
    <TaskOverview v-if="sub === 'overview'" :task="t" @reload="reload(true)" />
    <TaskRevise v-else-if="sub === 'revise'" :task="t" @reload="reload(true)" />
    <TaskExport v-else-if="sub === 'export'" :task="t" />
    <TaskVersions v-else-if="sub === 'versions'" :task="t" @reload="reload(true)" />
    <TaskRecycle v-else-if="sub === 'recycle'" :task="t" @reload="reload(true)" />
    <TaskReqDiff v-else-if="sub === 'reqdiff'" :task="t" @reload="reload(true)" />
    <TaskFinal v-else-if="sub === 'final'" :task="t" @reload="reload(true)" />
    <TaskExecutions v-else-if="sub === 'executions'" :task="t" />
    <TgEmpty v-else title="未知页签" />
  </template>
</template>

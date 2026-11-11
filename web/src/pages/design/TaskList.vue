<script setup>
/* 设计任务列表（5.4 / 5.14–5.17 / 5.19）：需求名作主标题，状态分段 + 工作台跳转筛选，新建 / 人工用例集 / 批量删除 / 取消 / 重试 / 恢复 / 改所属项目。 */
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, seg } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { useBadgeStore } from '@/stores/badge'
import { fmtTime, fmtAgo, num, shortId } from '@/utils/format'
import { TASK_STATUSES } from '@/utils/status'
import { PageHeading, TgPanel, TgTable, TgBadge, TgSeg, TgDialog, TgDropdown, TgField, TgPager, TgStat, TgIcon } from '@/components/ui'
import TaskNewDialog from './components/TaskNewDialog.vue'
import TaskManualDialog from './components/TaskManualDialog.vue'
import { taskTitle, progressText, resumeLabel, retryToast, moduleCounts, moduleMatch } from './components/taskUtils'

const route = useRoute(), router = useRouter(), scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore(), badge = useBadgeStore()
const status = ref(route.query.status || '')
const filter = ref(route.query.filter || '')          // review | fix（工作台 / 角标跳转）
const creator = ref(route.query.created_by || '')
const kw = ref(route.query.kw || '')
const page = ref(+route.query.page || 1), size = ref(20)
const selected = ref(new Set())
const openNew = ref(false), openManual = ref(false)
const projDlg = ref({ open: false, task: null, project: '', busy: false })
const canDelete = computed(() => auth.isAdmin)

// ---- 任务列表 ----
async function fetchTasks() {
  const q = { ...scope.query, limit: 5000 }
  if (canDelete.value) {
    try { return await api.get('/api/v1/tasks', { ...q, include_deleted: true }) } catch (e) { if (e.status !== 403) throw e }
  }
  return api.get('/api/v1/tasks', q)
}
const { data, loading, error, reload } = useLoad(fetchTasks, { deps: [() => scope.project, () => scope.line] })
const tasks = computed(() => data.value?.tasks || [])

// ---- 补充数据：当前项目的用例行（待审 / 驳回 / 模块）+ 工作台待办（测试点待审 / 被驳回）----
const projCases = ref(null)
async function loadProjectCases() {
  projCases.value = null
  if (!scope.project) return
  try { projCases.value = (await api.get('/api/v1/projects/cases', { project: scope.project })).cases || [] } catch { projCases.value = [] }
}
watch(() => scope.project, loadProjectCases, { immediate: true })
const caseInfo = computed(() => {
  const m = {}
  for (const c of projCases.value || []) {
    const x = m[c.task_id] || (m[c.task_id] = { pending: 0, rejected: 0, approved: 0, modules: new Set() })
    x[c.review === 'approved' || c.review === 'rejected' ? c.review : 'pending']++
    if (c.module) x.modules.add(c.module)
  }
  return m
})
const wbInfo = computed(() => {
  const m = {}
  const w = badge.wb; if (!w) return m
  for (const it of w.pending_review?.items || []) { const x = m[it.task_id] || (m[it.task_id] = {}); x[it.kind === 'point' ? 'pointPending' : 'casePending'] = it.count }
  for (const it of w.to_fix?.items || []) { const x = m[it.task_id] || (m[it.task_id] = {}); x.fix = (it.points || 0) + (it.cases || 0); x.proposal = it.has_proposal }
  return m
})
onMounted(() => { if (!badge.wb) badge.refresh(true) })

// 模块 / 端筛选：模块优先用项目用例行推断，不够时按需拉任务详情（缓存）
const details = ref({})
async function ensureDetails(list) {
  const todo = list.filter(t => !details.value[t.task_id] && !t.deleted_at).slice(0, 80)
  await Promise.all(todo.map(async t => {
    try {
      const d = await api.get(`/api/v1/tasks/${t.task_id}`)
      const modules = new Set([...((d.analysis || {}).test_points || []).map(m => m.module), ...((d.result || {}).cases || []).map(c => c.module)].filter(Boolean))
      const terms = new Set([...(d.point_terminals || []).map(x => x.key), ...Object.values(d.case_terminals || {})])
      details.value = { ...details.value, [t.task_id]: { modules, terms } }
    } catch { details.value = { ...details.value, [t.task_id]: { modules: new Set(), terms: new Set() } } }
  }))
}
watch([() => scope.module, () => scope.terminal, tasks], ([m, t, list]) => { if ((m || t) && list.length) ensureDetails(list) }, { immediate: true })
const modulesOf = t => details.value[t.task_id]?.modules || caseInfo.value[t.task_id]?.modules || new Set()

// ---- 筛选 ----
const matchStatus = (t, v) => {
  if (!v) return !t.deleted_at
  if (v === 'deleted') return !!t.deleted_at
  if (t.deleted_at) return false
  if (v === 'cancelled') return t.status === 'failed' && /取消/.test(t.error || '')
  if (v === 'failed') return t.status === 'failed' && !/取消/.test(t.error || '')
  return t.status === v
}
const matchFilter = (t) => {
  if (!filter.value) return true
  const w = wbInfo.value[t.task_id] || {}, c = caseInfo.value[t.task_id] || {}
  if (filter.value === 'review') return !!(w.pointPending || w.casePending || c.pending)
  if (filter.value === 'fix') return !!(w.fix || c.rejected)
  return true
}
const matchKw = (t) => {
  const k = kw.value.trim().toLowerCase(); if (!k) return true
  return [t.task_id, t.project, t.created_by, taskTitle(t), t.error, (t.sources || []).join(' ')].filter(Boolean).join(' ').toLowerCase().includes(k)
}
const filtered = computed(() => tasks.value.filter(t => matchStatus(t, status.value) && matchFilter(t) && matchKw(t)
  && (!creator.value || t.created_by === creator.value)
  && (!scope.module || [...modulesOf(t)].some(m => moduleMatch(m, scope.module)))
  && (!scope.terminal || (details.value[t.task_id]?.terms || new Set()).has(scope.terminal))))
const pageRows = computed(() => filtered.value.slice((page.value - 1) * size.value, page.value * size.value))
const creators = computed(() => [...new Set(tasks.value.map(t => t.created_by).filter(Boolean))].sort())
const segItems = computed(() => {
  const base = tasks.value.filter(t => matchFilter(t))
  const items = TASK_STATUSES.map(([k, l]) => ({ key: k, label: l, count: base.filter(t => matchStatus(t, k)).length }))
  if (canDelete.value) items.push({ key: 'deleted', label: '已删除', count: base.filter(t => !!t.deleted_at).length })
  return items
})
watch([status, filter, creator, kw], () => { page.value = 1 })
watch([status, filter, creator, kw, page], () => router.replace({ query: { ...route.query, status: status.value || undefined, filter: filter.value || undefined, created_by: creator.value || undefined, kw: kw.value || undefined, page: page.value > 1 ? page.value : undefined } }))
// 外部跳转（工作台角标 / 顶栏）只改 query 不重建组件：把 query 同步回筛选
watch(() => route.query, q => {
  if (route.name !== 'designs') return
  if ((q.status || '') !== status.value) status.value = q.status || ''
  if ((q.filter || '') !== filter.value) filter.value = q.filter || ''
  if ((q.created_by || '') !== creator.value) creator.value = q.created_by || ''
  if ((q.kw || '') !== kw.value) kw.value = q.kw || ''
})

// 模块树计数（按任务数）
watch([tasks, caseInfo, details], ([list]) => {
  if (!scope.project || !list.length) { scope.moduleCounts = null; return }
  const names = []
  for (const t of list) if (!t.deleted_at) names.push(...modulesOf(t))
  scope.moduleCounts = moduleCounts(names, scope.flatModules)
}, { immediate: true })
onBeforeUnmount(() => { scope.moduleCounts = null; clearInterval(timer) })

// 执行中任务轮询
let timer = null
timer = setInterval(() => { if (tasks.value.some(t => ['queued', 'running'].includes(t.status))) reload(true) }, 8000)

// ---- 统计卡 ----
const alive = computed(() => tasks.value.filter(t => !t.deleted_at))
const stats = computed(() => ({
  total: alive.value.length, completed: alive.value.filter(t => t.status === 'completed').length, failed: alive.value.filter(t => t.status === 'failed').length,
  running: alive.value.filter(t => ['queued', 'running'].includes(t.status)).length,
  awaiting: alive.value.filter(t => t.status === 'awaiting_confirmation').length,
  points: alive.value.reduce((s, t) => s + (t.point_count || 0), 0), cases: alive.value.reduce((s, t) => s + (t.case_count || 0), 0),
  approved: Object.values(caseInfo.value).reduce((s, x) => s + x.approved, 0),
  review: alive.value.filter(t => { const w = wbInfo.value[t.task_id] || {}, c = caseInfo.value[t.task_id] || {}; return w.pointPending || w.casePending || c.pending }).length,
  fix: alive.value.filter(t => { const w = wbInfo.value[t.task_id] || {}, c = caseInfo.value[t.task_id] || {}; return w.fix || c.rejected }).length,
}))

// ---- 动作 ----
const DELETE_NOTE = '任务及其测试点 / 用例将从列表与详情隐藏，不再计入用例库与统计；版本历史与审计留档保留，可在「已删除」筛选里恢复。测试计划里的用例快照不受影响。'
const deletable = t => canDelete.value && !t.deleted_at && !['queued', 'running'].includes(t.status)
function open(t) { if (!t.deleted_at) router.push({ name: 'design-detail', params: { id: t.task_id } }) }
async function afterWrite() { await reload(true); badge.refresh(true); if (scope.project) loadProjectCases() }
async function remove(t) {
  if (!await ui.confirm('删除任务？', `「${taskTitle(t)}」${DELETE_NOTE}`, { ok: '删除', danger: true })) return
  try { await api.del(`/api/v1/tasks/${t.task_id}`); selected.value.delete(t.task_id); ui.toast('任务已删除，可在「已删除」里恢复', { undo: () => restore(t, true) }); afterWrite() } catch (e) { ui.error(e) }
}
async function batchDelete() {
  const ids = [...selected.value].filter(id => { const t = tasks.value.find(x => x.task_id === id); return t && deletable(t) })
  if (!ids.length) return ui.toast('请先勾选可删除的任务', 'warn')
  if (!await ui.confirm('批量删除任务？', `删除选中的 ${ids.length} 个任务？${DELETE_NOTE}`, { ok: '删除', danger: true })) return
  try {
    const r = await api.post('/api/v1/tasks/batch-delete', { ids })
    selected.value = new Set()
    ui.toast(`已删除 ${r.deleted.length} 个` + (r.skipped.length ? `，${r.skipped.length} 个跳过：${[...new Set(r.skipped.map(x => x.reason))].join('；')}` : ''), r.skipped.length ? 'warn' : 'ok')
    afterWrite()
  } catch (e) { ui.error(e) }
}
async function restore(t, silent) {
  try { await api.post(`/api/v1/tasks/${t.task_id}/restore`, {}); if (!silent) ui.toast('任务已恢复'); afterWrite() } catch (e) { ui.error(e) }
}
async function cancel(t) {
  if (!await ui.confirm('取消任务？', '中断后台执行，任务将标记为失败（可继续完成）。', { ok: '取消任务', danger: true })) return
  try { await api.post(`/api/v1/tasks/${t.task_id}/cancel`, {}); ui.toast('任务已取消'); afterWrite() }
  catch (e) {
    if (e.status === 409 && /句柄/.test(e.message) && await ui.confirm('没有后台执行句柄', e.message + '\n\n确认它确实没在跑后，标记失败？', { ok: '标记失败', danger: true })) {
      try { await api.post(`/api/v1/tasks/${t.task_id}/cancel?force=true`, {}); ui.toast('已标记失败，可点「继续完成」'); afterWrite() } catch (e2) { ui.error(e2) }
    } else ui.error(e)
  }
}
async function retry(t) {
  try { const r = await api.post(`/api/v1/tasks/${t.task_id}/retry`, {}); ui.toast(retryToast(r)); afterWrite() } catch (e) { ui.error(e) }
}
function askProject(t) { projDlg.value = { open: true, task: t, project: scope.projects.find(p => p.status !== 'archived')?.project || '', busy: false } }
async function saveProject() {
  const d = projDlg.value; if (!d.project) return
  d.busy = true
  try {
    const r = await api.put(`/api/v1/tasks/${d.task.task_id}/project`, { project: d.project })
    ui.toast(`已归属「${d.project}」${r.requirement_migrated ? '，需求实体已建' : ''}${r.plans_moved ? '，迁移计划已改挂' : ''}`)
    d.open = false; afterWrite(); scope.load(true)
  } catch (e) { ui.error(e) } finally { d.busy = false }
}
function onCreated(r) { afterWrite(); router.push({ name: 'design-detail', params: { id: r.task_id } }) }
function onManualCreated(r) { afterWrite(); ui.toast('已创建人工用例集，可在用例审核页新增或导入用例'); router.push({ name: 'cases', params: { tid: r.task_id } }) }
function rowMenu(t) {
  const items = []
  if (t.deleted_at) return [{ label: '恢复任务', action: () => restore(t) }]
  items.push({ label: '概览', action: () => open(t) })
  if (t.point_count) items.push({ label: '审核测试点', action: () => router.push({ name: 'points', params: { tid: t.task_id } }) })
  if (t.case_count) items.push({ label: '用例审核', action: () => router.push({ name: 'cases', params: { tid: t.task_id } }) })
  if (t.case_count && auth.can(t.project, 'case.export')) items.push({ label: '导出', action: () => router.push({ name: 'design-detail', params: { id: t.task_id, sub: 'export' } }) })
  if (['queued', 'running'].includes(t.status) && auth.can(t.project, 'point.ai')) items.push({ label: '取消任务', action: () => cancel(t) })
  if (t.status === 'failed' && auth.can(t.project, 'point.ai')) items.push({ label: '继续完成 / 重试', action: () => retry(t) })
  if (!t.project && auth.isAdmin) items.push({ label: '归属项目', action: () => askProject(t) })
  if (deletable(t)) items.push({ divider: true }, { label: '删除任务', danger: true, action: () => remove(t) })
  return items
}
const cols = computed(() => [
  { key: 'title', title: '任务（需求名）' }, { key: 'status', title: '状态', w: '150px' }, { key: 'points', title: '测试点', w: '110px' }, { key: 'cases', title: '用例', w: '120px' },
  { key: 'review', title: '待审', w: '110px' }, { key: 'created_by', title: '创建人', w: '90px' }, { key: 'created_at', title: '创建时间', w: '110px' }, { key: 'ops', title: '', w: '150px' },
])
const isManualRow = t => !t.point_count && !t.requirement_id && (t.sources || []).length === 1 && t.sources[0] !== 'text' && !/\.[a-z0-9]{2,5}$/i.test(t.sources[0])
const pendingText = t => {
  const w = wbInfo.value[t.task_id] || {}, c = caseInfo.value[t.task_id] || {}
  const parts = []
  if (w.pointPending) parts.push(`测试点 ${w.pointPending}`)
  const cp = c.pending ?? w.casePending
  if (cp) parts.push(`用例 ${cp}`)
  return parts
}
</script>
<template>
  <PageHeading title="设计任务" :sub="`一次测试设计 = 一个任务：拆解 → 质量检查 → 人工确认测试点 → 按模块并行生成 → 评审回环 ≤ 3 轮。${scope.project ? '当前项目 ' + scope.project : '全部可见项目'}。`">
    <template #actions>
      <button type="button" class="btn" @click="reload()"><TgIcon name="refresh" :size="14" /> 刷新</button>
      <button v-if="!scope.project || auth.can(scope.project, 'case.edit')" type="button" class="btn" @click="openManual = true">＋ 人工用例集</button>
      <button v-if="!scope.project || auth.can(scope.project, 'point.ai')" type="button" class="btn primary" @click="openNew = true">＋ 新建任务</button>
    </template>
  </PageHeading>
  <div v-if="error" class="err-box">{{ error.message }}</div>
  <div class="stat-grid four">
    <TgStat label="全部任务" :value="num(stats.total)" :foot="`${stats.completed} 完成 · ${stats.running} 执行中 · ${stats.failed} 失败`" icon="layers" />
    <TgStat label="等待人工确认测试点" :value="num(stats.awaiting)" foot="拆解完成后停在待确认，人工审核后才生成用例" icon="checklist" color="orange" />
    <TgStat label="测试点 / 用例产出" :value="`${num(stats.points)} / ${num(stats.cases)}`" :foot="scope.project ? `正式用例 ${num(stats.approved)}` : '选择项目后显示正式用例数'" icon="file" color="green" />
    <TgStat label="待审核 / 待修改" :value="`${stats.review} / ${stats.fix}`" foot="点下方分段「待审」「待修改」查看" icon="alert" color="red" />
  </div>
  <TgPanel :pad="false">
    <div class="filters" style="padding:12px 14px 10px">
      <TgSeg v-model="status" :items="segItems" />
      <TgSeg v-model="filter" :items="[['', '全部'], ['review', '待我审核'], ['fix', '被驳回待修改']]" />
      <select v-model="creator" class="select"><option value="">全部创建人</option><option v-for="c in creators" :key="c" :value="c">{{ c }}{{ c === auth.me ? '（我）' : '' }}</option></select>
      <input v-model="kw" class="field" style="width:220px" placeholder="搜索需求名 / 任务 ID / 错误" />
      <span class="grow"></span>
      <span v-if="scope.module || scope.terminal" class="tiny muted">按模块 / 端筛选中（取自任务的测试点与用例）</span>
      <button v-if="status || filter || creator || kw" type="button" class="btn small ghost" @click="status = ''; filter = ''; creator = ''; kw = ''">清除筛选</button>
    </div>
    <div v-if="selected.size" class="selection-bar" style="margin:0 14px 10px">
      <span class="muted">已勾选 <b>{{ selected.size }}</b> 个任务</span>
      <button type="button" class="btn small danger" @click="batchDelete">批量删除</button>
      <button type="button" class="btn small" @click="selected = new Set()">取消全选</button>
    </div>
    <TgTable :cols="cols" :rows="pageRows" row-key="task_id" :loading="loading" clickable :selectable="canDelete" v-model:selected="selected" :empty="tasks.length ? '没有符合筛选条件的任务' : '暂无任务，点右上角「新建任务」开始'" @row-click="open">
      <template #cell-title="{ row: t }">
        <div style="font-weight:650">{{ taskTitle(t) }}
          <span v-if="(t.sources || []).length > 1 && t.requirement_title" class="tag" style="margin-left:4px">{{ t.sources.length }} 份材料</span>
          <span v-if="isManualRow(t)" class="tag manual" style="margin-left:4px">人工</span>
        </div>
        <div class="sub">{{ t.project || '（未指定项目）' }} · <span class="mono">{{ t.task_id }}</span> · {{ t.created_by || '—' }} · {{ fmtTime(t.created_at) }}<template v-if="t.revision_count"> · 修订 {{ t.revision_count }} 轮</template></div>
      </template>
      <template #cell-status="{ row: t }">
        <template v-if="t.deleted_at"><TgBadge status="deleted" text="已删除" color="red" /><div class="sub">{{ t.deleted_by }} · {{ fmtAgo(t.deleted_at) }}</div></template>
        <template v-else>
          <TgBadge :status="t.status" />
          <div v-if="t.progress" class="sub"><span class="spinner" style="width:10px;height:10px"></span> {{ progressText(t.progress) }}</div>
          <div v-if="t.error" class="sub" style="color:#db5361;white-space:normal;max-width:220px" :title="t.error">{{ t.error.slice(0, 80) }}</div>
        </template>
      </template>
      <template #cell-points="{ row: t }">
        <template v-if="t.point_count">{{ num(t.point_count) }}<div class="sub">通过 {{ num(t.point_approved || 0) }}<template v-if="wbInfo[t.task_id]?.pointPending"> · 待审 {{ wbInfo[t.task_id].pointPending }}</template></div></template>
        <span v-else class="muted">—</span>
      </template>
      <template #cell-cases="{ row: t }">
        <template v-if="t.case_count">{{ num(t.case_count) }}<div v-if="caseInfo[t.task_id]" class="sub">正式 {{ caseInfo[t.task_id].approved }} · 待审 {{ caseInfo[t.task_id].pending }}<template v-if="caseInfo[t.task_id].rejected"> · 驳回 {{ caseInfo[t.task_id].rejected }}</template></div></template>
        <span v-else class="muted">—</span>
      </template>
      <template #cell-review="{ row: t }">
        <template v-if="pendingText(t).length"><div v-for="p in pendingText(t)" :key="p" class="small">{{ p }}</div></template>
        <span v-else class="muted">—</span>
        <div v-if="wbInfo[t.task_id]?.fix || caseInfo[t.task_id]?.rejected" class="sub" style="color:#db5361">驳回 {{ wbInfo[t.task_id]?.fix || caseInfo[t.task_id]?.rejected }}<template v-if="wbInfo[t.task_id]?.proposal"> · 有提案</template></div>
      </template>
      <template #cell-created_by="{ row: t }"><span v-if="t.created_by" class="tag">{{ t.created_by }}</span><span v-else class="muted">—</span></template>
      <template #cell-created_at="{ row: t }"><span class="muted" :title="fmtTime(t.created_at, true)">{{ fmtAgo(t.created_at) }}</span></template>
      <template #cell-ops="{ row: t }">
        <div class="row nowrap" style="gap:4px" @click.stop>
          <template v-if="t.deleted_at"><button type="button" class="btn small" @click="restore(t)">恢复</button></template>
          <template v-else>
            <router-link v-if="t.status === 'awaiting_confirmation'" class="btn small primary" :to="{ name: 'points', params: { tid: t.task_id } }">审核测试点</router-link>
            <router-link v-else-if="t.case_count" class="btn small" :to="{ name: 'cases', params: { tid: t.task_id } }">用例</router-link>
            <button v-else-if="t.status === 'failed' && auth.can(t.project, 'point.ai')" type="button" class="btn small" @click="retry(t)">{{ resumeLabel(t) }}</button>
            <button v-else-if="['queued', 'running'].includes(t.status) && auth.can(t.project, 'point.ai')" type="button" class="btn small" @click="cancel(t)">取消</button>
            <TgDropdown icon small :items="rowMenu(t)" />
          </template>
        </div>
      </template>
    </TgTable>
    <TgPager v-model:page="page" v-model:size="size" :total="filtered.length" />
  </TgPanel>

  <TaskNewDialog v-model="openNew" @created="onCreated" />
  <TaskManualDialog v-model="openManual" @created="onManualCreated" />
  <TgDialog v-model="projDlg.open" title="归属项目" ok="保存" :busy="projDlg.busy" :ok-disabled="!projDlg.project" @ok="saveProject">
    <TgField label="项目" required><select v-model="projDlg.project" class="select"><option v-for="p in scope.projects.filter(p => p.status !== 'archived')" :key="p.project" :value="p.project">{{ p.project }}</option></select></TgField>
    <div class="tiny muted">把无项目的历史任务挂到项目，并自动建需求实体、改挂迁移计划（PUT /tasks/{id}/project）；已归属的任务不允许改挂。</div>
  </TgDialog>
</template>

<script setup>
/* 用例库（7.14）：跨任务只收录审核通过的正式用例；筛选 / 分页进 URL；按端页签；右侧抽屉看详情；批量加入计划 / 删除。 */
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtTime } from '@/utils/format'
import { EXEC_STATUSES } from '@/utils/consts'
import { orderTerminalKeys, terminalLabel } from '@/utils/terminals'
import { PageHeading, TgPanel, TgTable, TgBadge, TgPrio, TgSeg, TgTabs, TgPager, TgDialog, TgField, TgDropdown, TgIcon } from '@/components/ui'
import LibCaseDrawer from './components/LibCaseDrawer.vue'
import { moduleCounts, moduleMatch } from './components/taskUtils'
const route = useRoute(), router = useRouter(), scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore()
const q = route.query
const priority = ref(q.priority || ''), execStatus = ref(q.exec_status || ''), creator = ref(q.created_by || ''), kw = ref(q.kw || ''), kwInput = ref(q.kw || '')
const page = ref(+q.page || 1), size = ref([20, 50, 100, 200].includes(+q.page_size) ? +q.page_size : 20)
const terminal = ref(q.terminal || '')
const selected = ref(new Set())
const drawer = ref({ open: false, row: null })
const planDlg = ref({ open: false, plan: '', plans: [], busy: false })

// 模块树节点名（如「活动入口」）与用例模块名（如「H5 活动页-活动入口」）口径不同：能唯一对上就传后端，否则取回后在本页过滤
const knownModules = ref([])
const backendModule = computed(() => {
  const sel = scope.module; if (!sel) return ''
  if (knownModules.value.includes(sel)) return sel
  const c = knownModules.value.filter(m => moduleMatch(m, sel))
  return c.length === 1 ? c[0] : ''
})
const { data, loading, error, reload } = useLoad(() => api.get('/api/v1/cases', { ...scope.query, module: backendModule.value, priority: priority.value, exec_status: execStatus.value, created_by: creator.value, keyword: kw.value, page: page.value, page_size: size.value }),
  { deps: [() => scope.project, () => scope.line, () => scope.module, priority, execStatus, creator, kw, page, size] })
watch([priority, execStatus, creator, kw, () => scope.module, () => scope.project], () => { page.value = 1 })
watch([priority, execStatus, creator, kw, page, size, terminal], () => router.replace({ query: { priority: priority.value || undefined, exec_status: execStatus.value || undefined, created_by: creator.value || undefined, kw: kw.value || undefined, page: page.value > 1 ? page.value : undefined, page_size: size.value !== 20 ? size.value : undefined, terminal: terminal.value || undefined } }))
watch(data, d => { if (d) selected.value = new Set(); if (d?.modules) knownModules.value = d.modules; scope.moduleCounts = d?.module_counts ? moduleCounts(Object.entries(d.module_counts).flatMap(([m, n]) => Array(n).fill(m)), scope.flatModules) : null })
onBeforeUnmount(() => { scope.moduleCounts = null })

// 端：用例行没有端字段，按所属任务的 case_terminals 补（按任务缓存）
const termCache = ref({})
async function ensureTerminals(rows) {
  const ids = [...new Set(rows.map(r => r.task_id))].filter(id => !termCache.value[id])
  await Promise.all(ids.map(async id => { try { const d = await api.get(`/api/v1/tasks/${id}`); termCache.value = { ...termCache.value, [id]: d.case_terminals || {} } } catch { termCache.value = { ...termCache.value, [id]: {} } } }))
}
watch(data, d => { if (d?.cases?.length) ensureTerminals(d.cases) }, { immediate: true })
const termOf = r => (termCache.value[r.task_id] || {})[r.uid] || ''
const rows = computed(() => {
  const list = data.value?.cases || []
  const t = terminal.value || scope.terminal
  const sel = scope.module && !backendModule.value ? scope.module : ''
  return list.filter(r => (!t || termOf(r) === t) && (!sel || moduleMatch(r.module, sel)))
})
const termTabs = computed(() => {
  const list = data.value?.cases || []
  const counts = {}
  for (const r of list) { const t = termOf(r) || 'other'; counts[t] = (counts[t] || 0) + 1 }
  return [{ key: '', label: '全部端', count: list.length }, ...orderTerminalKeys(Object.keys(counts)).map(k => ({ key: k, label: terminalLabel(k), count: counts[k] }))]
})
const cols = [{ key: 'case_id', title: '编号', w: '150px' }, { key: 'title', title: '标题' }, { key: 'module', title: '模块', w: '150px' }, { key: 'priority', title: '优先级', w: '64px' }, { key: 'project', title: '项目', w: '120px' }, { key: 'terminal', title: '端', w: '90px' }, { key: 'review', title: '状态', w: '96px' }, { key: 'exec', title: '最近执行', w: '150px' }, { key: 'version', title: '版本', w: '56px' }]
const canDeleteAny = computed(() => auth.isAdmin)
const selRows = computed(() => (data.value?.cases || []).filter(r => selected.value.has(r.uid)))
function openRow(r) { drawer.value = { open: true, row: r } }

// 批量：按任务分组
function groupByTask(list) { const by = {}; for (const r of list) (by[r.task_id] || (by[r.task_id] = [])).push(r); return by }
async function batchDelete() {
  const list = selRows.value.filter(r => auth.can(r.project, 'case.edit'))
  if (!list.length) return ui.toast('请先勾选用例', 'warn')
  if (!await ui.confirm('删除选中用例？', `${list.length} 条用例将移入所属任务的回收站，可恢复；已入测试计划的快照不受影响。`, { ok: '删除', danger: true })) return
  let ok = 0, bad = 0
  for (const [tid, rs] of Object.entries(groupByTask(list))) {
    try { const r = await api.post(`/api/v1/tasks/${tid}/cases/batch`, { uids: rs.map(x => x.uid), action: 'delete', base_versions: Object.fromEntries(rs.map(x => [x.uid, x.version || 1])) }); ok += (r.applied || []).length; bad += (r.conflicts || []).length + (r.skipped || []).length } catch (e) { bad += rs.length; ui.error(e) }
  }
  ui.toast(`已删除 ${ok} 条${bad ? `，${bad} 条跳过` : ''}`, bad ? 'warn' : 'ok'); reload(true)
}
async function openPlan() {
  if (!selRows.value.length) return ui.toast('请先勾选用例', 'warn')
  const projects = [...new Set(selRows.value.map(r => r.project))]
  if (projects.length > 1) return ui.toast('一次只能加入同一项目的用例到计划', 'warn')
  try { const r = await api.get('/api/v1/plans', { project: projects[0] }); planDlg.value = { open: true, plan: '', plans: (r.plans || []).filter(p => p.status !== 'archived'), busy: false } } catch (e) { ui.error(e) }
}
async function addToPlan() {
  const d = planDlg.value; if (!d.plan) return
  d.busy = true
  let n = 0
  try {
    for (const [tid, rs] of Object.entries(groupByTask(selRows.value))) { const r = await api.post(`/api/v1/plans/${d.plan}/cases`, { task_id: tid, uids: rs.map(x => x.uid) }); n += r.added ?? rs.length }
    ui.toast(`已加入 ${n} 条（快照引用版本号，后续修改不影响计划）`); d.open = false; selected.value = new Set()
  } catch (e) { ui.error(e) } finally { d.busy = false }
}
function exportMenu() { return [{ label: '按任务导出 xlsx / csv / xmind', hint: '任务详情 › 导出', action: () => { const r = selRows.value[0] || rows.value[0]; if (r) router.push({ name: 'design-detail', params: { id: r.task_id, sub: 'export' } }); else ui.toast('没有可导出的用例', 'warn') } }] }
</script>
<template>
  <PageHeading title="用例库" :sub="`跨任务只收录审核通过的正式用例${data ? `（当前范围 ${data.total} 条）` : ''}；待审核 / 已驳回 / 草稿留在各任务的用例审核页。行点击打开右侧抽屉看步骤、执行记录、计划与版本。`">
    <template #actions>
      <TgDropdown label="导出" :items="exportMenu()" />
      <button type="button" class="btn primary" :disabled="!selected.size" @click="openPlan">加入测试计划{{ selected.size ? ` (${selected.size})` : '' }}</button>
    </template>
  </PageHeading>
  <div v-if="error" class="err-box">{{ error.message }}</div>
  <TgPanel :pad="false">
    <div class="filters" style="padding:12px 14px 6px">
      <input v-model="kwInput" class="field" style="width:230px" placeholder="搜索编号 / 标题 / 标签 / 步骤…（回车）" @keydown.enter="kw = kwInput" @change="kw = kwInput" />
      <TgSeg v-model="priority" :items="[['', '全部'], ['P0', 'P0'], ['P1', 'P1'], ['P2', 'P2'], ['P3', 'P3']]" />
      <select v-model="execStatus" class="select"><option value="">最近执行：全部</option><option value="none">未执行</option><option v-for="[k, l] in EXEC_STATUSES" :key="k" :value="k">{{ l }}</option></select>
      <select v-if="(data?.creators || []).length > 1" v-model="creator" class="select"><option value="">全部创建人</option><option v-for="c in data.creators" :key="c" :value="c">{{ c }}</option></select>
      <span class="grow"></span>
      <span v-if="scope.module" class="tiny muted">模块：{{ scope.module }}</span>
      <button v-if="priority || execStatus || creator || kw" type="button" class="btn small ghost" @click="priority = ''; execStatus = ''; creator = ''; kw = ''; kwInput = ''">清除筛选</button>
      <button type="button" class="btn small" @click="reload()"><TgIcon name="refresh" :size="13" /></button>
    </div>
    <div style="padding:0 14px"><TgTabs v-model="terminal" :items="termTabs" style="margin-bottom:0" /></div>
    <div v-if="selected.size" class="selection-bar" style="margin:10px 14px 0">
      <span class="muted">已勾选 <b>{{ selected.size }}</b> 条</span>
      <button type="button" class="btn small primary" @click="openPlan">加入测试计划</button>
      <button v-if="canDeleteAny" type="button" class="btn small danger" @click="batchDelete">删除（入回收站）</button>
      <button type="button" class="btn small" @click="selected = new Set()">取消全选</button>
    </div>
    <TgTable :cols="cols" :rows="rows" row-key="uid" :loading="loading" clickable selectable v-model:selected="selected" :active-key="drawer.open ? drawer.row?.uid : null" :empty="data?.total ? '当前端没有用例' : (priority || execStatus || creator || kw || scope.module ? '没有符合筛选条件的正式用例' : '没有正式用例（用例须在任务的用例审核页通过后才会进入用例库）')" @row-click="openRow">
      <template #cell-case_id="{ row }"><span class="mono">{{ row.case_id }}</span></template>
      <template #cell-title="{ row }"><span style="font-weight:600">{{ row.title }}</span><span v-if="row.keywords" class="tag" style="margin-left:6px">{{ row.keywords }}</span></template>
      <template #cell-module="{ row }"><span class="small">{{ row.module }}</span></template>
      <template #cell-priority="{ row }"><TgPrio :p="row.priority" /></template>
      <template #cell-project="{ row }"><span class="tag">{{ row.project }}</span></template>
      <template #cell-terminal="{ row }"><span v-if="termOf(row)" class="tag">{{ terminalLabel(termOf(row)) }}</span><span v-else class="muted">—</span></template>
      <template #cell-review="{ row }"><TgBadge status="approved" :text="row.locked ? '正式 · 锁定' : '已通过'" /></template>
      <template #cell-exec="{ row }"><template v-if="row.exec"><TgBadge :status="row.exec.status" /> <span class="tiny muted">{{ fmtTime(row.exec.at) }}</span></template><span v-else class="muted">未执行</span></template>
      <template #cell-version="{ row }">v{{ row.version || 1 }}</template>
    </TgTable>
    <TgPager v-model:page="page" v-model:size="size" :total="data?.total || 0" />
  </TgPanel>
  <LibCaseDrawer v-model="drawer.open" :row="drawer.row" :terminal="drawer.row ? termOf(drawer.row) : ''" @copied="reload(true)" />
  <TgDialog v-model="planDlg.open" title="加入测试计划" ok="加入" :busy="planDlg.busy" :ok-disabled="!planDlg.plan" @ok="addToPlan">
    <TgField label="计划" required>
      <select v-model="planDlg.plan" class="select"><option value="">请选择</option><option v-for="p in planDlg.plans" :key="p.plan_id" :value="p.plan_id">{{ p.name }}（{{ p.status_label || p.status }}）</option></select>
      <small v-if="!planDlg.plans.length" class="muted">该项目还没有测试计划，请先在「测试计划」中创建</small>
    </TgField>
    <div class="information"><TgIcon name="info" :size="13" /> <span>只收已通过用例；加入即完整快照并引用版本号，此后正式用例再改不影响计划里的快照。</span></div>
  </TgDialog>
</template>

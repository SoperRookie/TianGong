<script setup>
/* 用例审核工作台（路由 cases，tid 可选）：与测试点审核同一套壳，详情换成用例卡；按端页签在工作台上方；
   键盘流 j / k / a / r / e / d / space / u；驳回后 AI 定点修改 → 提案逐项接受；⋯ 收纳新增 / 复制 / 导入 / 查重 / 导出；页签：用例 / 重复对 / 修改提案 / 评审记录。 */
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useKeys } from '@/utils/useKeys'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { useBadgeStore } from '@/stores/badge'
import { terminalLabel } from '@/utils/terminals'
import { PRIORITIES } from '@/utils/status'
import { fmtTime } from '@/utils/format'
import { TgSeg, TgTabs, TgEmpty, TgDropdown, TgDialog, TgField, TgBadge, TgPrio } from '@/components/ui'
import WbShell from './components/WbShell.vue'
import WbTaskPicker from './components/WbTaskPicker.vue'
import WbRejectForm from './components/WbRejectForm.vue'
import WbFixProposals from './components/WbFixProposals.vue'
import WbDupPairs from './components/WbDupPairs.vue'
import WbImportDialog from './components/WbImportDialog.vue'
import CaseDetail from './components/CaseDetail.vue'
import CaseNewDialog from './components/CaseNewDialog.vue'
import { useReviewSession, nextPendingId, sourceText } from './components/WbSession'

const route = useRoute(), router = useRouter(), scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore(), badge = useBadgeStore()
const tid = computed(() => route.params.tid || '')
const session = useReviewSession()

/* ---------- 数据 ---------- */
const { data: task, loading, error, reload } = useLoad(() => api.get(`/api/v1/tasks/${tid.value}`), { deps: [tid], immediate: false })
watch(tid, v => { if (v) reload() }, { immediate: true })
const project = computed(() => task.value?.context?.project || scope.project)
const can = a => auth.can(project.value, a)
const completed = computed(() => task.value?.status === 'completed')
const canReview = computed(() => can('case.review') && completed.value), canEdit = computed(() => can('case.edit') && completed.value), canAi = computed(() => can('case.ai') && completed.value), canExport = computed(() => can('case.export'))
const canDelete = computed(() => canEdit.value && (auth.isAdmin || auth.accessMode === 'members'))
const me = computed(() => auth.me)
const taskTitle = computed(() => task.value?.context?.requirement_title || (task.value?.sources || []).join('、') || tid.value)
const DEFAULT_RV = { status: 'pending', comment: '', reject_count: 0, locked: false }
const cases = computed(() => (task.value?.result?.cases || []).map(c => ({ ...c, rv: task.value.case_reviews?.[c.uid] || DEFAULT_RV, st: (task.value.case_reviews?.[c.uid] || DEFAULT_RV).status || 'pending', terminal: task.value.case_terminals?.[c.uid] || 'other' })))
const modules = computed(() => [...new Set(cases.value.map(c => c.module))])
const pointMap = computed(() => { const m = {}; for (const e of task.value?.analysis?.test_points || []) for (const p of e.points || []) if (p?.tp_id) m[p.tp_id] = p.point; return m })
const pointText = id => pointMap.value[id] || ''

/* ---------- URL 状态 ---------- */
const setQ = patch => { const q = { ...route.query, ...patch }; Object.keys(q).forEach(k => (q[k] === '' || q[k] == null) && delete q[k]); router.replace({ query: q }) }
const tab = computed({ get: () => route.query.tab || 'cases', set: v => setQ({ tab: v === 'cases' ? '' : v, sel: '' }) })
const filter = computed({ get: () => route.query.filter || 'pending', set: v => setQ({ filter: v === 'pending' ? '' : v }) })
const terminal = computed({ get: () => route.query.terminal ?? scope.terminal ?? '', set: v => { setQ({ terminal: v }); if (scope.terminal !== v) scope.setTerminal(v) } })
watch(() => scope.terminal, v => { if ((route.query.terminal || '') !== (v || '')) setQ({ terminal: v }) })
const sel = computed({ get: () => route.query.sel || '', set: v => setQ({ sel: v }) })
const q = ref(''), prio = ref('')

/* ---------- 筛选 ---------- */
const inScope = computed(() => cases.value.filter(c => (!scope.module || (c.module || '').includes(scope.module)) && (!terminal.value || c.terminal === terminal.value)))
const stat = computed(() => ({ total: inScope.value.length, pending: inScope.value.filter(c => c.st === 'pending').length, approved: inScope.value.filter(c => c.st === 'approved').length, rejected: inScope.value.filter(c => c.st === 'rejected').length, draft: inScope.value.filter(c => c.st === 'draft').length }))
const shown = computed(() => {
  const kw = q.value.trim().toLowerCase()
  return inScope.value.filter(c => (filter.value === 'all' || c.st === filter.value) && (!prio.value || c.priority === prio.value)
    && (!kw || `${c.case_id} ${c.title} ${c.module} ${c.keywords || ''} ${c.precondition || ''} ${(c.point_ids || []).join(' ')} ${(c.steps || []).map(s => `${s.action} ${s.expected}`).join(' ')}`.toLowerCase().includes(kw)))
})
const groups = computed(() => { const m = new Map(); shown.value.forEach(c => { if (!m.has(c.module)) m.set(c.module, []); m.get(c.module).push(c) }); return [...m.entries()] })
const cur = computed(() => cases.value.find(c => c.uid === sel.value) || null)
watch([shown, loading], () => { if (loading.value || tab.value !== 'cases') return; if (!cur.value || !shown.value.some(c => c.uid === sel.value)) { const first = shown.value[0]; if (first && first.uid !== sel.value) sel.value = first.uid } })
watch([cases, () => scope.flatModules], () => { if (!task.value) return; const counts = {}; scope.flatModules.forEach(n => { counts[n.name] = cases.value.filter(c => (c.module || '').includes(n.name)).length }); scope.moduleCounts = counts }, { immediate: true })
onBeforeUnmount(() => { scope.moduleCounts = null; stopEditing() })
const terminalTabs = computed(() => {
  const cnt = {}; cases.value.forEach(c => { cnt[c.terminal] = (cnt[c.terminal] || 0) + 1 })
  return [{ key: '', label: '全部端', count: cases.value.length }, ...(task.value?.terminals || []).filter(t => cnt[t.key]).map(t => ({ key: t.key, label: t.label, count: cnt[t.key] }))]
})
const curLog = computed(() => (task.value?.review_log || []).filter(l => l.uid === cur.value?.uid))
const holder = computed(() => (task.value?.editing || []).find(e => e.kind === 'case' && e.entity_id === cur.value?.uid && e.by !== me.value) || null)

/* ---------- 审核动作 ---------- */
const mode = ref(''), busy = ref(false), hints = ref([]), conflict = ref('')
const checked = ref(new Set())
const scrollTo = id => setTimeout(() => document.querySelector(`[data-uid="${id}"]`)?.scrollIntoView({ block: 'nearest' }), 30)
function select(id) { sel.value = id; mode.value = ''; scrollTo(id) }
function move(d) { const list = shown.value; if (!list.length) return; const i = list.findIndex(c => c.uid === sel.value); select(list[(i + d + list.length) % list.length].uid) }
function fail(e, prefix = '') { if (e.status === 409) { conflict.value = e.message; session.note(`${prefix}冲突：${e.message}`, 'err') } else session.note(`${prefix}失败：${e.message}`, 'err') }
async function review(items) {
  busy.value = true
  try { const r = await api.post(`/api/v1/tasks/${tid.value}/review`, { items }); hints.value = r.review?.hints || []; conflict.value = ''; await reload(true); badge.refresh?.(true); return r } finally { busy.value = false }
}
const nextOf = c => nextPendingId(shown.value, c.uid, 'uid', x => x.st === 'pending' && x.uid !== c.uid)
const byId = id => cases.value.find(c => c.case_id === id)
async function approve(c = cur.value) {
  if (!c || !canReview.value || c.st === 'approved') return
  if (c.st === 'draft') return submitDraft(c)
  const nx = nextOf(c)
  try { await review([{ case_id: c.case_id, action: 'approve' }]); session.done(`通过 ${c.case_id}`, async () => { await review([{ case_id: c.case_id, action: 'unlock' }]); select(c.uid) }, c.uid); session.note(`已通过并锁定 ${c.case_id}`); mode.value = ''; if (nx) select(nx) } catch (e) { fail(e, '通过') }
}
async function submitDraft(c = cur.value) {
  if (!c || c.st !== 'draft') return
  try { await review([{ case_id: c.case_id, action: 'submit' }]); session.done(`提交评审 ${c.case_id}`); session.note(`${c.case_id} 已提交评审（草稿 → 待审核）`) } catch (e) { fail(e, '提交') }
}
async function reject(payload, c = cur.value) {
  if (!c) return
  const nx = nextOf(c)
  try {
    const r = await review([{ case_id: c.case_id, action: 'reject', ...payload }])
    session.done(`驳回 ${c.case_id}`, async () => { await review([{ case_id: c.case_id, action: 'unlock' }]); select(c.uid) }, c.uid)
    const h = r.review?.hints || []
    session.note(`已驳回 ${c.case_id}${h.length ? ' · 已连续多次驳回，建议人工介入' : ''}`, h.length ? 'warn' : ''); mode.value = ''; if (nx) select(nx)
  } catch (e) { fail(e, '驳回') }
}
async function unlock(c = cur.value) {
  if (!c) return
  try { await review([{ case_id: c.case_id, action: 'unlock' }]); session.done(`解锁 ${c.case_id}`, async () => { await review([{ case_id: c.case_id, action: 'approve' }]); select(c.uid) }, c.uid); session.note(`已解锁 ${c.case_id}，回到待审核`) } catch (e) { fail(e, '解锁') }
}
async function remove(c = cur.value) {
  if (!c || !canDelete.value) return
  if (!await ui.confirm('删除用例', `删除 ${c.case_id}「${c.title}」？将移入回收站（可恢复），删除后重排编号。`, { ok: '删除', danger: true })) return
  const nx = nextPendingId(shown.value, c.uid, 'uid', x => x.uid !== c.uid)
  try {
    await review([{ case_id: c.case_id, action: 'delete' }])
    session.done(`删除 ${c.case_id}`, async () => {
      const bin = await api.get(`/api/v1/tasks/${tid.value}/recycle-bin`)
      const item = (bin.items || []).find(x => x.kind === 'case' && x.entity_id === c.uid)
      if (!item) throw new Error('回收站里找不到该用例')
      await api.post(`/api/v1/tasks/${tid.value}/recycle-bin/restore`, { item_id: item.id }); await reload(true); select(c.uid)
    }, c.uid)
    session.note(`已删除 ${c.case_id}（入回收站）`); if (nx) select(nx)
  } catch (e) { fail(e, '删除') }
}
let editingId = ''
async function startEdit() {
  if (!cur.value || !canEdit.value) return
  mode.value = 'edit'; editingId = cur.value.uid
  try { const d = await api.post(`/api/v1/tasks/${tid.value}/editing`, { kind: 'case', entity_id: editingId, action: 'start' }); if (d.holder) session.note(`${d.holder.by} 正在编辑此用例，注意保存冲突`, 'warn') } catch { /* 占用提示失败不影响编辑 */ }
}
function stopEditing() { if (editingId) { api.post(`/api/v1/tasks/${tid.value}/editing`, { kind: 'case', entity_id: editingId, action: 'stop' }).catch(() => {}); editingId = '' } }
function cancelEdit() { mode.value = ''; stopEditing() }
async function saveEdit({ case: edited, feedback, base_version }) {
  const c = cur.value; if (!c) return
  const { rv, st, terminal: _t, ...before } = c
  const body = { ...edited }; delete body.rv; delete body.st; delete body.terminal
  try {
    await review([{ case_id: c.case_id, action: 'modify', case: body, feedback, base_version }])
    session.done(`修改 ${c.case_id}`, async () => { const now = byId(c.case_id) || cases.value.find(x => x.uid === c.uid); await review([{ case_id: now?.case_id || c.case_id, action: 'modify', case: before, feedback: '撤销修改', base_version: now?.version }]); select(c.uid) }, c.uid)
    session.note(`已保存 ${c.case_id}，版本 v${(c.version || 1) + 1}，人工定稿即通过并锁定`); cancelEdit()
  } catch (e) { fail(e, '保存') }
}
async function copyCase(c = cur.value) {
  if (!c || !canEdit.value) return
  try { busy.value = true; const r = await api.post(`/api/v1/tasks/${tid.value}/cases/${c.uid}/copy`, {}); await reload(true); session.note(`已复制为 ${r.case.case_id}（草稿）`); filter.value = 'draft'; select(r.case.uid) } catch (e) { fail(e, '复制') } finally { busy.value = false }
}
function toggleCheck(id = sel.value) { if (!id) return; const s = new Set(checked.value); s.has(id) ? s.delete(id) : s.add(id); checked.value = s }
function toggleModule(m) { const ids = (groups.value.find(([k]) => k === m) || [, []])[1].map(c => c.uid); const s = new Set(checked.value); const all = ids.every(i => s.has(i)); ids.forEach(i => all ? s.delete(i) : s.add(i)); checked.value = s }
const checkedCases = computed(() => [...checked.value].map(id => cases.value.find(c => c.uid === id)).filter(Boolean))
async function batchReview(action, payload = {}) {
  const list = checkedCases.value.filter(c => action === 'approve' ? c.st !== 'approved' && c.st !== 'draft' : action === 'reject' ? c.st !== 'draft' : true); if (!list.length) { session.note('没有可操作的选中项', 'warn'); return }
  try {
    await review(list.map(c => ({ case_id: c.case_id, action, ...payload })))
    const label = { approve: '通过', reject: '驳回' }[action] || action
    session.done(`批量${label} ${list.length} 条`, async () => { await review(list.map(c => ({ case_id: c.case_id, action: 'unlock' }))) })
    session.note(`已批量${label} ${list.length} 条（批量原子）`); checked.value = new Set(); mode.value = ''
  } catch (e) { fail(e, '批量') }
}
const batchOpen = ref(false), batch = ref({ action: 'set_priority', value: 'P1' })
async function batchMaintain(action, value = '') {
  const list = checkedCases.value; if (!list.length) return
  if (action === 'delete' && !await ui.confirm('批量删除', `删除选中的 ${list.length} 条用例？将移入回收站，可恢复。`, { ok: '删除', danger: true })) return
  const base = {}; list.forEach(c => { base[c.uid] = c.version || 1 })
  busy.value = true
  try {
    const r = await api.post(`/api/v1/tasks/${tid.value}/cases/batch`, { uids: list.map(c => c.uid), action, value, base_versions: base })
    await reload(true)
    let msg = `已处理 ${r.applied.length} 条`
    if (r.conflicts.length) msg += `，${r.conflicts.length} 条被他人修改过已跳过（请刷新后重试）`
    if (r.skipped.length) msg += `，${r.skipped.length} 条跳过：${[...new Set(r.skipped.map(x => x.reason))].join('；')}`
    session.done(`批量维护 ${r.applied.length} 条`); session.note(msg, r.conflicts.length || r.skipped.length ? 'warn' : ''); checked.value = new Set(); batchOpen.value = false
  } catch (e) { fail(e, '批量维护') } finally { busy.value = false }
}
async function undo() { const h = await session.undoLast(); if (h?.id) select(h.id) }

/* ---------- 键盘流 ---------- */
const newOpen = ref(false), importOpen = ref(false)
const dialogOpen = computed(() => newOpen.value || importOpen.value || batchOpen.value || !!ui.dialog || ui.search)
useKeys({
  j: () => move(1), k: () => move(-1),
  a: () => approve(), r: () => { if (!cur.value || !canReview.value || ['approved', 'draft'].includes(cur.value.st)) return; mode.value = mode.value === 'reject' ? '' : 'reject' },
  e: () => startEdit(), d: () => remove(), space: () => toggleCheck(), u: undo,
  escape: () => { if (mode.value && mode.value !== 'edit') mode.value = '' },
}, { enabled: () => tab.value === 'cases' && !dialogOpen.value && mode.value !== 'edit' })

/* ---------- AI 定点修改 / 查重 / 工具 ---------- */
async function tool(label, fn, after) {
  busy.value = true; session.note(`${label}执行中…`)
  try { const r = await fn(); await reload(true); session.note(after ? after(r) : `${label}完成`) } catch (e) { session.note(`${label}失败：${e.message}`, 'err'); ui.error(e) } finally { busy.value = false }
}
const post = (path, body) => api.post(`/api/v1/tasks/${tid.value}${path}`, body ?? {})
const fixCases = () => tool('AI 定点修改', () => post('/cases/fix'), r => { if (r.pending_fix) tab.value = 'fix'; return r.pending_fix ? `AI 产出 ${r.pending_fix.proposals.length} 条修改提案，请逐项确认` : (r.message || '无提案') })
const confirmFix = decisions => tool('提案确认', () => post('/fix/confirm', { decisions }), r => `提案确认完成：应用 ${r.applied || 0} 条 / 拒绝 ${r.rejected || 0} 条`)
const dupCheck = () => tool('用例查重', () => post('/cases/dup-check'), r => { const n = (r.case_dup_pairs || []).filter(x => x.status === 'open').length; if (n) tab.value = 'dup'; return `查重完成：候选 ${r.candidates} 对，${n} 组待处置` })
const dupAct = ({ key, action, note }) => tool(action === 'ignore' ? '忽略重复对' : '重开重复对', () => post(`/cases/dup-pairs/${encodeURIComponent(key)}`, { action, note }))
const dupDelete = async id => { const c = byId(id); if (c) await remove(c) }
const addCase = body => tool('新增用例', () => post('/cases', body), r => { newOpen.value = false; filter.value = 'draft'; select(r.case.uid); return `已新增 ${r.case.case_id}（草稿，提交评审后进入待审核）` })
const imported = r => { reload(true); filter.value = 'draft'; session.note(`已导入 ${r.imported} 条草稿，跳过 ${r.skipped} 行`) }
async function exportCases(fmt, scopeArg, layout = '') {
  try { const name = await api.download(`/api/v1/tasks/${tid.value}/files/${fmt}`, '', { scope: scopeArg, terminal: terminal.value, layout }); session.note(`已导出 ${name}`) } catch (e) { ui.error(e) }
}
const moreItems = computed(() => [
  { label: '新增用例…', hint: '草稿', action: () => { newOpen.value = true }, disabled: !canEdit.value },
  { label: '复制当前用例', action: () => copyCase(), disabled: !canEdit.value || !cur.value },
  { label: '表格导入…', hint: 'Excel / CSV / XMind', action: () => { importOpen.value = true }, disabled: !canEdit.value },
  { label: '下载导入模板', action: () => api.download('/api/v1/cases/import-template', '用例导入模板.xlsx').catch(ui.error) },
  { divider: true },
  { label: '用例查重', hint: '文字初筛 + 语义裁判', action: dupCheck, disabled: !canAi.value || busy.value },
  { label: 'AI 定点修改驳回用例', action: fixCases, disabled: !canAi.value || busy.value || !stat.rejected || !!pf.value },
  { divider: true },
  { label: '导出 xlsx', hint: terminal.value ? terminalLabel(terminal.value) : '全部端', action: () => exportCases('xlsx', 'all'), disabled: !canExport.value },
  { label: '导出 csv', action: () => exportCases('csv', 'all'), disabled: !canExport.value },
  { label: '导出 xmind', action: () => exportCases('xmind', 'all'), disabled: !canExport.value },
  { label: '导出 xmind（紧凑）', action: () => exportCases('xmind', 'all', 'compact'), disabled: !canExport.value },
  { label: '只导正式用例 xlsx', action: () => exportCases('xlsx', 'approved'), disabled: !canExport.value },
])
const pf = computed(() => task.value?.pending_fix?.kind === 'cases' ? task.value.pending_fix : null)
const lastFix = computed(() => [...(task.value?.fix_log || [])].reverse().find(f => f.kind === 'cases') || null)
const openDup = computed(() => (task.value?.case_dup_pairs || []).filter(d => d.status === 'open').length)
const logs = computed(() => [...(task.value?.review_log || [])].reverse())
const logSel = computed(() => logs.value[Number(route.query.sel) || 0] || null)
const ACTS = { approve: '通过', reject: '驳回', modify: '修改', delete: '删除', unlock: '解锁', submit: '提交评审', restore: '恢复版本', restore_bin: '回收站恢复', final: '终稿覆盖' }
const tabs = computed(() => [
  { key: 'cases', label: '用例', count: cases.value.length }, { key: 'dup', label: '疑似重复', count: openDup.value },
  { key: 'fix', label: '修改提案', count: pf.value ? pf.value.proposals.length : undefined }, { key: 'log', label: '评审记录', count: (task.value?.review_log || []).length },
])
const offline = computed(() => task.value?.offline_review || null)
const KEYS = [['j', ''], ['k', '移动'], ['a', '通过'], ['r', '驳回'], ['e', '修改'], ['d', '删除'], ['space', '勾选'], ['?', '说明']]
</script>
<template>
  <WbTaskPicker v-if="!tid" kind="case" />
  <template v-else>
    <div v-if="error" class="err-box">{{ error.message }}</div>
    <div v-else-if="!task" class="page-loading"><span class="spinner" /> 加载任务…</div>
    <WbShell v-else title="用例审核" badge="审核用例" :back-to="{ name: 'design-detail', params: { id: tid } }" :back-label="taskTitle" :task-status="task.status"
      :stats="[['用例', stat.total], ['待审', stat.pending], ['正式', stat.approved], ['驳回', stat.rejected], ...(stat.draft ? [['草稿', stat.draft]] : [])]"
      :processed="session.processed.value" :elapsed="session.elapsed.value" :msg="session.msg.value" :msg-kind="session.msgKind.value" :keys="KEYS" :can-undo="session.history.value.length > 0"
      :checked="tab === 'cases' ? checked.size : 0" @undo="undo" @clear="checked = new Set()">
      <template #actions>
        <span v-if="stat.pending" class="warn">还有 {{ stat.pending }} 条待审</span>
        <span v-else-if="stat.total" class="badge green">待审已清零</span>
        <button v-if="stat.rejected && canAi && !pf" type="button" class="btn" :disabled="busy" @click="fixCases">AI 定点修改（{{ stat.rejected }}）</button>
        <button v-else-if="pf" type="button" class="btn" @click="tab = 'fix'">确认 AI 提案（{{ pf.proposals.length }}）</button>
        <router-link class="btn primary lg" :to="{ name: 'caselib', query: { project: project } }" title="已通过的正式用例进入用例库，可加入测试计划">用例库 / 加入计划</router-link>
        <TgDropdown label="⋯" :items="moreItems" />
      </template>
      <template #banner>
        <div v-if="!completed" class="information mb">任务状态「{{ task.status }}」，用例评审只在任务完成后可用。<router-link :to="{ name: 'points', params: { tid } }">去测试点审核 →</router-link></div>
        <div v-if="offline" class="information mb">离线终稿已回传{{ offline.applied ? '并以终稿为准' : '（仅留痕）' }}：{{ offline.filename }} · {{ offline.by }} {{ fmtTime(offline.at) }}<template v-if="offline.stats"> · 采纳率 {{ ((offline.stats.adoption_rate || 0) * 100).toFixed(1) }}%（改 {{ offline.stats.modified }} / 增 {{ offline.stats.added }} / 删 {{ offline.stats.deleted }}）</template></div>
        <div v-if="conflict" class="attention mb">{{ conflict }}<button type="button" class="btn small" @click="reload(); conflict = ''">重新加载</button></div>
        <div v-for="(h, i) in hints" :key="i" class="attention mb">{{ h }}<button type="button" class="btn small" @click="hints.splice(i, 1)">知道了</button></div>
        <div v-if="holder && tab === 'cases'" class="attention mb">{{ holder.by }} 正在编辑当前用例 {{ cur?.case_id }}，注意保存冲突</div>
        <TgTabs v-model="tab" :items="tabs" />
        <TgTabs v-if="tab === 'cases'" v-model="terminal" :items="terminalTabs" />
      </template>

      <template v-if="tab === 'cases'" #list>
        <div class="list-tools">
          <div class="row">
            <TgSeg v-model="filter" :items="[['pending', '待审', stat.pending], ['approved', '通过', stat.approved], ['rejected', '驳回', stat.rejected], ...(stat.draft ? [['draft', '草稿', stat.draft]] : []), ['all', '全部', stat.total]]" />
            <input v-model="q" class="field small cw-search" placeholder="搜索编号 / 标题 / 步骤 / 来源测试点" />
          </div>
          <div class="row">
            <select v-model="prio" class="select small"><option value="">全部优先级</option><option v-for="p in PRIORITIES" :key="p" :value="p">{{ p }}</option></select>
            <span class="tiny muted">{{ scope.module ? `模块：${scope.module}` : '模块树可筛选' }}</span>
          </div>
        </div>
        <div class="list-scroll">
          <template v-for="([m, list]) in groups" :key="m">
            <div class="grp"><span :title="m" class="cw-grp" @click="toggleModule(m)">{{ m }}</span><span>{{ list.length }}</span></div>
            <div v-for="c in list" :key="c.uid" :data-uid="c.uid" class="item" :class="{ ok: c.st === 'approved', rej: c.st === 'rejected', sel: c.uid === sel }" @click="select(c.uid)">
              <input type="checkbox" :checked="checked.has(c.uid)" @click.stop @change="toggleCheck(c.uid)" />
              <TgPrio :p="c.priority" /><span class="ttl" :title="c.title">{{ c.title }}</span>
              <span v-if="c.st === 'draft'" class="badge gray">草稿</span>
              <span v-else-if="(c.source || 'ai') !== 'ai'" class="badge" :class="['manual', 'import'].includes(c.source) ? 'green' : 'purple'">{{ sourceText(c.source) }}</span>
              <span class="st"></span>
            </div>
          </template>
          <TgEmpty v-if="!groups.length" :title="filter === 'pending' ? '待审已清空' : '这一类为空'" hint="切到「全部」或调整端 / 模块筛选" />
        </div>
      </template>
      <template v-if="tab === 'cases'" #detail>
        <template v-if="cur">
          <CaseDetail v-if="mode !== 'batchReject'" :c="cur" :log="curLog" :holder="holder" :point-text="pointText" :mode="mode" :can-review="canReview" :can-edit="canEdit" :can-delete="canDelete" :can-ai="canAi" :busy="busy"
            @approve="approve()" @submit="submitDraft()" @reject="v => reject(v)" @edit="startEdit" @save="saveEdit" @cancel="mode === 'edit' ? cancelEdit() : (mode = '')" @delete="remove()" @unlock="unlock()" @copy="copyCase()" @fix="fixCases" />
          <template v-else>
            <div class="detail-title">批量驳回 {{ checked.size }} 条用例</div>
            <div class="inline-list mb"><span v-for="c in checkedCases" :key="c.uid" class="tag mono">{{ c.case_id }}</span></div>
            <WbRejectForm kind="case" :label="`${checked.size} 条 · 统一驳回意见`" :step-max="0" scope="选中项" :busy="busy" @submit="v => batchReview('reject', v)" @cancel="mode = ''" />
          </template>
        </template>
        <TgEmpty v-else title="没有可审核的用例" hint="调整筛选，或通过「⋯」新增 / 导入用例" />
      </template>

      <template v-if="tab !== 'cases'" #workbench>
        <WbDupPairs v-if="tab === 'dup'" :pairs="task.case_dup_pairs || []" kind="cases" :tid="tid" :sel="sel" :cases="cases" :can-review="canReview" :can-delete="canDelete" :can-ai="canAi" :busy="busy" @act="dupAct" @delete="dupDelete" @recheck="dupCheck" @select="v => sel = v" />
        <div v-else-if="tab === 'fix'" class="panel panel-pad"><WbFixProposals :pf="pf" :last-fix="lastFix" :busy="busy" :can-confirm="can('case.edit')" @confirm="confirmFix" /><div v-if="!pf && stat.rejected && canAi" class="row mt"><button type="button" class="btn primary" :disabled="busy" @click="fixCases">AI 定点修改驳回用例（{{ stat.rejected }}）</button></div></div>
        <div v-else-if="tab === 'log'" class="workbench">
          <div class="panel list-pane"><div class="list-scroll">
            <div v-for="(l, i) in logs" :key="i" class="item" :class="{ sel: i === (Number(route.query.sel) || 0) }" @click="sel = String(i)"><TgBadge :status="l.action === 'approve' ? 'approved' : l.action === 'reject' ? 'rejected' : l.action === 'modify' ? 'modified' : ''" :text="ACTS[l.action] || l.action" /><span class="ttl">{{ l.case_id }} · {{ l.by }}</span><span class="tiny muted">{{ fmtTime(l.at) }}</span></div>
            <TgEmpty v-if="!logs.length" title="还没有评审记录" />
          </div></div>
          <div class="panel panel-pad detail-pane">
            <template v-if="logSel">
              <div class="detail-top"><span class="id">{{ logSel.case_id }}</span><TgBadge :text="ACTS[logSel.action] || logSel.action" color="blue" /><span class="meta">{{ logSel.by }} · {{ fmtTime(logSel.at) }}</span></div>
              <dl class="kv mt">
                <dt v-if="logSel.comment">意见</dt><dd v-if="logSel.comment">{{ logSel.comment }}</dd>
                <dt v-if="logSel.feedback">反馈</dt><dd v-if="logSel.feedback">{{ logSel.feedback }}</dd>
                <dt v-if="(logSel.reject_types || []).length">驳回类型</dt><dd v-if="(logSel.reject_types || []).length">{{ logSel.reject_types.join('、') }}<span v-if="logSel.fix_request"> · 要求：{{ logSel.fix_request }}</span><span v-if="logSel.fix_scope"> · 范围：{{ logSel.fix_scope }}</span></dd>
                <template v-if="logSel.before && logSel.after"><dt>修改前</dt><dd class="diff-del">{{ logSel.before.title }}<div class="tiny">{{ (logSel.before.steps || []).map(s => `${s.action} ⇒ ${s.expected}`).join('\n') }}</div></dd><dt>修改后</dt><dd class="diff-add">{{ logSel.after.title }}<div class="tiny">{{ (logSel.after.steps || []).map(s => `${s.action} ⇒ ${s.expected}`).join('\n') }}</div></dd></template>
                <template v-else-if="logSel.before"><dt>删除前</dt><dd>{{ logSel.before.title }}</dd></template>
              </dl>
              <div class="row mt"><button v-if="cases.some(c => c.uid === logSel.uid)" type="button" class="btn small" @click="tab = 'cases'; filter = 'all'; select(logSel.uid)">在工作台打开</button></div>
            </template>
            <TgEmpty v-else title="选择左侧一条记录" />
          </div>
        </div>
      </template>

      <template #batch>
        <button type="button" class="btn" :disabled="busy || !canReview" @click="batchReview('approve')">通过</button>
        <button type="button" class="btn" :disabled="busy || !canReview" @click="mode = 'batchReject'">驳回</button>
        <button type="button" class="btn" :disabled="busy || !canEdit" @click="batchMaintain('submit')">提交评审</button>
        <button type="button" class="btn" :disabled="busy || !canEdit" @click="batchOpen = true">改优先级 / 模块 / 标签</button>
        <button type="button" class="btn" :disabled="busy || !canDelete" @click="batchMaintain('delete')">删除</button>
      </template>
    </WbShell>

    <CaseNewDialog v-model="newOpen" :modules="modules" :busy="busy" @save="addCase" />
    <WbImportDialog v-model="importOpen" :tid="tid" @imported="imported" />
    <TgDialog v-model="batchOpen" title="批量维护" :ok="`应用到 ${checked.size} 条`" :busy="busy" :ok-disabled="!batch.value.trim()" @ok="batchMaintain(batch.action, batch.value.trim())">
      <TgField label="操作"><select v-model="batch.action" class="select" @change="batch.value = batch.action === 'set_priority' ? 'P1' : ''"><option value="set_priority">改优先级</option><option value="set_module">改模块</option><option value="set_keywords">替换标签</option><option value="add_keywords">追加标签</option></select></TgField>
      <TgField :label="batch.action === 'set_priority' ? '优先级' : batch.action === 'set_module' ? '模块名' : '标签（逗号或顿号分隔）'" required>
        <select v-if="batch.action === 'set_priority'" v-model="batch.value" class="select"><option v-for="p in PRIORITIES" :key="p" :value="p">{{ p }}</option></select>
        <input v-else v-model="batch.value" class="field" :list="batch.action === 'set_module' ? 'cw-modules' : null" /><datalist id="cw-modules"><option v-for="m in modules" :key="m" :value="m" /></datalist>
      </TgField>
      <div class="tiny muted">逐行乐观锁：被他人改过的行跳过；已通过锁定的用例须先解锁。</div>
    </TgDialog>
  </template>
</template>
<style scoped>
.cw-search { flex: 1; min-width: 120px; }
.cw-grp { cursor: pointer; max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.list-tools .seg { flex-wrap: wrap; }
.kv .tiny { white-space: pre-line; }
</style>

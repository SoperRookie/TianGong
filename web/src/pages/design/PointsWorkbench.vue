<script setup>
/* 测试点审核工作台（路由 points，tid 可选）：左列表按模块分组 + 分段筛选 + 按端 + 维度 / 搜索；右详情带需求来源与评审记录；
   键盘流 j / k / a / r / e / d / space / u；底部状态条只记已处理数与用时，不弹 toast；页签：测试点 / 盲区 / 重复对 / 修改提案 / 人工对比。 */
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useKeys } from '@/utils/useKeys'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { useBadgeStore } from '@/stores/badge'
import { taskTerminals, terminalLabel } from '@/utils/terminals'
import { TgSeg, TgTabs, TgEmpty, TgDropdown, TgDialog, TgField, TgBadge } from '@/components/ui'
import WbShell from './components/WbShell.vue'
import WbTaskPicker from './components/WbTaskPicker.vue'
import WbRejectForm from './components/WbRejectForm.vue'
import WbFixProposals from './components/WbFixProposals.vue'
import WbDupPairs from './components/WbDupPairs.vue'
import PointDetail from './components/PointDetail.vue'
import PointBlindSpots from './components/PointBlindSpots.vue'
import PointCompare from './components/PointCompare.vue'
import PointConfirmDialog from './components/PointConfirmDialog.vue'
import { useReviewSession, nextPendingId, sourceText, DIMENSIONS, pointTerminal } from './components/WbSession'

const route = useRoute(), router = useRouter(), scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore(), badge = useBadgeStore()
const tid = computed(() => route.params.tid || '')
const session = useReviewSession()

/* ---------- 数据 ---------- */
const { data: task, loading, error, reload } = useLoad(() => api.get(`/api/v1/tasks/${tid.value}`), { deps: [tid], immediate: false })
watch(tid, v => { if (v) reload() }, { immediate: true })
const project = computed(() => task.value?.context?.project || scope.project)
const can = a => auth.can(project.value, a)
const canReview = computed(() => can('point.review')), canEdit = computed(() => can('point.edit')), canAi = computed(() => can('point.ai')), canExport = computed(() => can('case.export'))
const canDelete = computed(() => canEdit.value && (auth.isAdmin || auth.accessMode === 'members'))
const me = computed(() => auth.me)
const taskTitle = computed(() => task.value?.context?.requirement_title || (task.value?.sources || []).join('、') || tid.value)
const confirmable = computed(() => task.value?.status === 'awaiting_confirmation')
const generatedIds = computed(() => new Set(task.value?.context?.generated_tp_ids || []))
const pendingGen = computed(() => new Set(task.value?.points_pending_generation || []))

/** 平铺测试点（跳过旧版字符串条目） */
const points = computed(() => {
  const out = []
  for (const e of task.value?.analysis?.test_points || []) for (const p of e.points || []) if (p && typeof p === 'object' && p.tp_id) out.push({ ...p, module: e.module, status: p.status || 'pending' })
  return out
})
const modules = computed(() => [...new Set(points.value.map(p => p.module))])

/* ---------- URL 状态：sel / tab / filter / terminal ---------- */
const setQ = patch => { const q = { ...route.query, ...patch }; Object.keys(q).forEach(k => (q[k] === '' || q[k] == null) && delete q[k]); router.replace({ query: q }) }
const tab = computed({ get: () => route.query.tab || 'points', set: v => setQ({ tab: v === 'points' ? '' : v, sel: '' }) })
const filter = computed({ get: () => route.query.filter || 'pending', set: v => setQ({ filter: v === 'pending' ? '' : v }) })
const terminal = computed({ get: () => route.query.terminal ?? scope.terminal ?? '', set: v => { setQ({ terminal: v }); if (scope.terminal !== v) scope.setTerminal(v) } })
watch(() => scope.terminal, v => { if ((route.query.terminal || '') !== (v || '')) setQ({ terminal: v }) })
const sel = computed({ get: () => route.query.sel || '', set: v => setQ({ sel: v }) })
const q = ref(''), dim = ref('')

/* ---------- 列表筛选 ---------- */
const inScope = computed(() => points.value.filter(p => (!scope.module || (p.module || '').includes(scope.module)) && (!terminal.value || pointTerminal(p) === terminal.value)))
const stat = computed(() => ({ total: inScope.value.length, pending: inScope.value.filter(p => p.status === 'pending').length, approved: inScope.value.filter(p => p.status === 'approved').length, rejected: inScope.value.filter(p => p.status === 'rejected').length, ai: inScope.value.filter(p => p.ai_review).length }))
const shown = computed(() => {
  const kw = q.value.trim().toLowerCase()
  return inScope.value.filter(p => (filter.value === 'all' || (filter.value === 'ai_review' ? !!p.ai_review : p.status === filter.value)) && (!dim.value || p.dimension === dim.value)
    && (!kw || `${p.tp_id} ${p.point} ${p.module} ${p.ref || ''} ${p.basis || ''}`.toLowerCase().includes(kw)))
})
const groups = computed(() => { const m = new Map(); shown.value.forEach(p => { if (!m.has(p.module)) m.set(p.module, []); m.get(p.module).push(p) }); return [...m.entries()] })
const cur = computed(() => points.value.find(p => p.tp_id === sel.value) || null)
watch([shown, loading], () => { if (loading.value) return; if (!cur.value || !shown.value.some(p => p.tp_id === sel.value)) { const first = shown.value[0]; if (first && first.tp_id !== sel.value) sel.value = first.tp_id } })
// 模块树计数（离开置 null）
watch([points, () => scope.flatModules], () => {
  if (!task.value) return
  const counts = {}; scope.flatModules.forEach(n => { counts[n.name] = points.value.filter(p => (p.module || '').includes(n.name)).length }); scope.moduleCounts = counts
}, { immediate: true })
onBeforeUnmount(() => { scope.moduleCounts = null; stopEditing() })
const terminalTabs = computed(() => [{ key: '', label: '全部端', count: points.value.length }, ...(task.value?.point_terminals || []).map(t => ({ key: t.key, label: t.label, count: t.pending ? `${t.pending}/${t.total}` : t.total }))])
const curLog = computed(() => (task.value?.point_review_log || []).filter(l => l.tp_id === cur.value?.tp_id))
const holder = computed(() => (task.value?.editing || []).find(e => e.kind === 'point' && e.entity_id === cur.value?.tp_id && e.by !== me.value) || null)

/* ---------- 审核动作 ---------- */
const mode = ref(''), busy = ref(false), hints = ref([]), conflict = ref('')
const checked = ref(new Set())
const scrollTo = id => setTimeout(() => document.querySelector(`[data-tp="${id}"]`)?.scrollIntoView({ block: 'nearest' }), 30)
function select(id) { sel.value = id; mode.value = ''; scrollTo(id) }
function move(d) { const list = shown.value; if (!list.length) return; const i = list.findIndex(p => p.tp_id === sel.value); select(list[(i + d + list.length) % list.length].tp_id) }
function fail(e, prefix = '') {
  if (e.status === 409) { conflict.value = e.message; session.note(`${prefix}冲突：${e.message}`, 'err') } else session.note(`${prefix}失败：${e.message}`, 'err')
}
/** 提交审核项；返回响应（失败抛出） */
async function review(items) {
  busy.value = true
  try {
    const r = await api.post(`/api/v1/tasks/${tid.value}/points/review`, { items })
    hints.value = r.hints || []
    conflict.value = ''
    await reload(true)
    badge.refresh?.(true)
    return r
  } finally { busy.value = false }
}
function advanceFrom(id) { const nx = nextPendingId(shown.value, id, 'tp_id', p => p.status === 'pending' && p.tp_id !== id); if (nx) select(nx) }
async function approve(p = cur.value) {
  if (!p || !canReview.value || p.status === 'approved') return
  const prev = p.status
  const nx = nextPendingId(shown.value, p.tp_id, 'tp_id', x => x.status === 'pending' && x.tp_id !== p.tp_id)
  try {
    await review([{ tp_id: p.tp_id, action: 'approve' }])
    session.done(`通过 ${p.tp_id}`, async () => { await review([{ tp_id: p.tp_id, action: 'unlock' }]); select(p.tp_id) }, p.tp_id)
    session.note(`已通过并锁定 ${p.tp_id}${prev === 'rejected' ? '（原已驳回）' : ''}`)
    mode.value = ''
    if (nx) select(nx)
  } catch (e) { fail(e, '通过') }
}
async function reject(payload, p = cur.value) {
  if (!p) return
  const nx = nextPendingId(shown.value, p.tp_id, 'tp_id', x => x.status === 'pending' && x.tp_id !== p.tp_id)
  try {
    const r = await review([{ tp_id: p.tp_id, action: 'reject', ...payload }])
    session.done(`驳回 ${p.tp_id}`, async () => { await review([{ tp_id: p.tp_id, action: 'unlock' }]); select(p.tp_id) }, p.tp_id)
    session.note(`已驳回 ${p.tp_id}${(r.hints || []).length ? ' · 已连续多次驳回，建议人工介入' : ''}`, (r.hints || []).length ? 'warn' : '')
    mode.value = ''
    if (nx) select(nx)
  } catch (e) { fail(e, '驳回') }
}
async function unlock(p = cur.value) {
  if (!p) return
  try { await review([{ tp_id: p.tp_id, action: 'unlock' }]); session.done(`解锁 ${p.tp_id}`, async () => { await review([{ tp_id: p.tp_id, action: 'approve' }]); select(p.tp_id) }, p.tp_id); session.note(`已解锁 ${p.tp_id}，回到待审核`) } catch (e) { fail(e, '解锁') }
}
async function remove(p = cur.value) {
  if (!p || !canDelete.value) return
  if (!await ui.confirm('删除测试点', `删除 ${p.tp_id}「${p.point.slice(0, 40)}」？将移入回收站，可恢复。`, { ok: '删除', danger: true })) return
  const nx = nextPendingId(shown.value, p.tp_id, 'tp_id', x => x.tp_id !== p.tp_id)
  try {
    await review([{ tp_id: p.tp_id, action: 'delete' }])
    session.done(`删除 ${p.tp_id}`, async () => {
      const bin = await api.get(`/api/v1/tasks/${tid.value}/recycle-bin`)
      const item = (bin.items || []).find(x => x.kind === 'point' && x.entity_id === p.tp_id)
      if (!item) throw new Error('回收站里找不到该测试点')
      await api.post(`/api/v1/tasks/${tid.value}/recycle-bin/restore`, { item_id: item.id }); await reload(true); select(p.tp_id)
    }, p.tp_id)
    session.note(`已删除 ${p.tp_id}（入回收站）`)
    if (nx) select(nx)
  } catch (e) { fail(e, '删除') }
}
/* 就地编辑：进入时登记占用，保存 / 取消释放 */
let editingId = ''
async function startEdit(prefill) {
  if (!cur.value || !canEdit.value) return
  mode.value = 'edit'; editingId = cur.value.tp_id
  if (prefill) editPrefill.value = prefill
  try { const d = await api.post(`/api/v1/tasks/${tid.value}/editing`, { kind: 'point', entity_id: editingId, action: 'start' }); if (d.holder) session.note(`${d.holder.by} 正在编辑此测试点，注意保存冲突`, 'warn') } catch { /* 占用提示失败不影响编辑 */ }
}
const editPrefill = ref('')
function stopEditing() { if (editingId) { api.post(`/api/v1/tasks/${tid.value}/editing`, { kind: 'point', entity_id: editingId, action: 'stop' }).catch(() => {}); editingId = '' } }
function cancelEdit() { mode.value = ''; editPrefill.value = ''; stopEditing() }
async function saveEdit(v) {
  const p = cur.value; if (!p) return
  const before = { point: p.point, dimension: p.dimension || '', ref: p.ref || '', terminal: pointTerminal(p) }
  const item = { tp_id: p.tp_id, action: 'modify', point: v.point, dimension: v.dimension, ref: v.ref, base_version: v.base_version, ...(v.terminal !== before.terminal ? { terminal: v.terminal } : {}) }
  try {
    await review([item])
    session.done(`修改 ${p.tp_id}`, async () => { await review([{ tp_id: p.tp_id, action: 'modify', ...before }]); select(p.tp_id) }, p.tp_id)
    session.note(`已保存 ${p.tp_id}，版本 v${(p.version || 1) + 1}，回到待审核`)
    cancelEdit()
  } catch (e) { fail(e, '保存') }
}
function toggleCheck(id = sel.value) { if (!id) return; const s = new Set(checked.value); s.has(id) ? s.delete(id) : s.add(id); checked.value = s }
function toggleModule(m) { const ids = (groups.value.find(([k]) => k === m) || [, []])[1].map(p => p.tp_id); const s = new Set(checked.value); const all = ids.every(i => s.has(i)); ids.forEach(i => all ? s.delete(i) : s.add(i)); checked.value = s }
const checkedList = computed(() => [...checked.value])
async function batchApprove() {
  const ids = checkedList.value.filter(id => points.value.find(p => p.tp_id === id)?.status !== 'approved'); if (!ids.length) return
  try { await review(ids.map(tp_id => ({ tp_id, action: 'approve' }))); session.done(`批量通过 ${ids.length} 条`, async () => { await review(ids.map(tp_id => ({ tp_id, action: 'unlock' }))) }); session.note(`已通过并锁定 ${ids.length} 条（批量原子）`); checked.value = new Set() } catch (e) { fail(e, '批量通过') }
}
async function batchReject(payload) {
  const ids = checkedList.value; if (!ids.length) return
  try { await review(ids.map(tp_id => ({ tp_id, action: 'reject', ...payload }))); session.done(`批量驳回 ${ids.length} 条`, async () => { await review(ids.map(tp_id => ({ tp_id, action: 'unlock' }))) }); session.note(`已驳回 ${ids.length} 条`); checked.value = new Set(); mode.value = '' } catch (e) { fail(e, '批量驳回') }
}
async function batchDelete() {
  const ids = checkedList.value; if (!ids.length || !canDelete.value) return
  if (!await ui.confirm('批量删除', `删除选中的 ${ids.length} 条测试点？将移入回收站，可恢复。`, { ok: '删除', danger: true })) return
  try { await review(ids.map(tp_id => ({ tp_id, action: 'delete' }))); session.done(`批量删除 ${ids.length} 条`); session.note(`已删除 ${ids.length} 条（入回收站，可在回收站页恢复）`); checked.value = new Set() } catch (e) { fail(e, '批量删除') }
}
const batchTerminal = ref('h5')
async function batchSetTerminal() {
  const items = checkedList.value.map(id => points.value.find(p => p.tp_id === id)).filter(p => p && pointTerminal(p) !== batchTerminal.value).map(p => ({ tp_id: p.tp_id, action: 'modify', point: p.point, terminal: batchTerminal.value, base_version: p.version || 1 }))
  if (!items.length) { session.note('选中项已是该端', 'warn'); return }
  try { await review(items); session.done(`批量改端 ${items.length} 条`); session.note(`已把 ${items.length} 条改为「${terminalLabel(batchTerminal.value)}」（版本 +1，回到待审核）`); checked.value = new Set() } catch (e) { fail(e, '批量改端') }
}
async function undo() { const h = await session.undoLast(); if (h?.id) select(h.id) }

/* ---------- 键盘流 ---------- */
const dialogOpen = computed(() => confirmOpen.value || aiAddOpen.value || manualOpen.value || !!ui.dialog || ui.search)
useKeys({
  j: () => move(1), k: () => move(-1),
  a: () => approve(), r: () => { if (!cur.value || !canReview.value || cur.value.status === 'approved') return; mode.value = mode.value === 'reject' ? '' : 'reject' },
  e: () => startEdit(), d: () => remove(), space: () => toggleCheck(), u: undo,
  escape: () => { if (mode.value && mode.value !== 'edit') mode.value = '' },
}, { enabled: () => tab.value === 'points' && !dialogOpen.value && mode.value !== 'edit' })

/* ---------- 工具（⋯）与对话框 ---------- */
async function tool(label, fn, after) {
  busy.value = true; session.note(`${label}执行中…`)
  try { const r = await fn(); await reload(true); session.note(after ? after(r) : `${label}完成`) } catch (e) { session.note(`${label}失败：${e.message}`, 'err'); ui.error(e) } finally { busy.value = false }
}
const post = (path, body) => api.post(`/api/v1/tasks/${tid.value}${path}`, body ?? {})
const gapCheck = () => tool('查漏补缺', () => post('/points/gap-check'), r => `查漏完成：新增 ${r.added.length} 条待审核测试点（存量不变）`)
const extend = () => tool('经验延伸', () => post('/points/extend'), r => `新增 ${r.added.length} 条（标「经验延伸」并带依据），${r.skipped.length} 条重复或没依据已跳过`)
const combine = () => tool(scope.module ? '组合延伸（本模块）' : '组合延伸', () => post('/points/combine', scope.module ? { modules: modules.value.filter(m => m.includes(scope.module)) } : {}), r => `组合延伸：${r.modules} 个模块，新增 ${r.added.length} 条，跳过 ${r.skipped.length}${r.failed ? `，${r.failed} 个模块失败` : ''}`)
const aiReview = () => tool('AI 评审', () => post('/points/ai-review'), r => { if (r.marked) filter.value = 'ai_review'; return `AI 评审：${r.reviewed} 条送评，${r.marked} 条标出意见${r.failed ? `，${r.failed} 批失败` : ''}` })
const dupCheck = () => tool('重复检查', () => post('/points/dup-check'), r => { const n = (r.dup_pairs || []).filter(x => x.status === 'open').length; if (n) tab.value = 'dup'; return `重复检查：${n} 组待处置` })
const fixPoints = () => tool('AI 修改驳回项', () => post('/points/fix'), r => { if (r.pending_fix) tab.value = 'fix'; return r.pending_fix ? `AI 产出 ${r.pending_fix.proposals.length} 条修改提案，请逐项确认` : (r.message || '无提案') })
const confirmFix = decisions => tool('提案确认', () => post('/fix/confirm', { decisions }), r => `提案确认完成：应用 ${r.applied || 0} 条 / 拒绝 ${r.rejected || 0} 条`)
const aiAddOpen = ref(false), aiInstruction = ref('')
const aiAdd = () => { aiAddOpen.value = false; tool('AI 补充', () => post('/points/add', { instruction: aiInstruction.value.trim() }), r => `AI 补充了 ${r.added.length} 条待审核测试点，${r.skipped.length} 条相似已跳过`) }
const manualOpen = ref(false), manual = ref({ module: '', point: '', dimension: '', ref: '' })
const manualAdd = () => { if (!manual.value.point.trim()) return; manualOpen.value = false; tool('手工新增', () => post('/points/add', { points: [{ ...manual.value, point: manual.value.point.trim() }] }), r => { const a = r.added[0]; if (a) { select(a.tp_id); filter.value = 'all' } manual.value = { module: '', point: '', dimension: '', ref: '' }; return a ? `已新增 ${a.tp_id}（待审核）` : '未新增' }) }
const manualBatch = ({ text, module, thenCompare }) => tool('批量录入', async () => { const r = await post('/points/manual-batch', { text, module }); if (thenCompare) await post('/points/compare'); return r }, r => `已录入 ${r.added.length} 条人工测试点${thenCompare ? '，对比完成' : ''}`)
const compare = () => tool('人工 vs AI 对比', () => post('/points/compare'), r => { const s = r.point_compare.stats; return `对比完成：AI 覆盖人工 ${s.ai_recall == null ? '—' : (s.ai_recall * 100).toFixed(0) + '%'}，AI 遗漏 ${s.manual_only} 条，额外发现 ${s.ai_only} 条` })
const blindAct = ({ id, body }) => tool('盲区处置', () => post(`/blind-spots/${id}`, body), r => r.added ? `已转为测试点 ${r.added[0].tp_id}` : r.question ? `已转为待确认事项 ${r.question.q_id}` : '已处置')
const blindBatch = ({ ids, body }) => tool('批量处置盲区', () => post('/blind-spots/batch', { ...body, ids }), r => `处置 ${r.applied.length} 条，跳过 ${r.skipped.length}${r.skipped.length ? '：' + r.skipped.map(x => x.reason).filter((v, i, a) => a.indexOf(v) === i).join('；') : ''}`)
const dupAct = ({ key, action, note }) => tool(action === 'ignore' ? '忽略重复对' : '重开重复对', () => post(`/points/dup-pairs/${encodeURIComponent(key)}`, { action, note }))
const dupBatch = ({ keys, action, note }) => tool('批量处置重复对', () => post('/points/dup-pairs/batch', { keys, action, note }), r => `处置 ${r.applied.length} 组，跳过 ${r.skipped.length}`)
const dupDelete = async id => { const p = points.value.find(x => x.tp_id === id); if (p) await remove(p) }
async function exportPoints(fmt, scopeArg) {
  try { const name = await api.download(`/api/v1/tasks/${tid.value}/points/export`, '', { fmt, scope: scopeArg, terminal: terminal.value }); session.note(`已导出 ${name}`) } catch (e) { ui.error(e) }
}
const moreItems = computed(() => [
  { label: '查漏补缺', hint: '只新增', action: gapCheck, disabled: !canAi.value || busy.value },
  { label: '经验延伸', hint: '带依据', action: extend, disabled: !canAi.value || busy.value },
  { label: scope.module ? '组合延伸（本模块）' : '组合延伸', hint: '每模块一次', action: combine, disabled: !canAi.value || busy.value },
  { label: 'AI 评审', hint: '挑硬伤', action: aiReview, disabled: !canAi.value || busy.value },
  { label: '重复检查', hint: 'Jaccard + 语义', action: dupCheck, disabled: !canAi.value || busy.value },
  { divider: true },
  { label: 'AI 补充…', hint: '一句话要求', action: () => { aiAddOpen.value = true }, disabled: !canEdit.value },
  { label: '手工新增…', action: () => { manualOpen.value = true }, disabled: !canEdit.value },
  { label: '批量录入 / 人工对比', action: () => { tab.value = 'compare' } },
  { divider: true },
  { label: '导出 xlsx', hint: terminal.value ? terminalLabel(terminal.value) : '全部端', action: () => exportPoints('xlsx', 'all'), disabled: !canExport.value },
  { label: '导出脑图 xmind', action: () => exportPoints('xmind', 'all'), disabled: !canExport.value },
  { label: '只导已通过 xlsx', action: () => exportPoints('xlsx', 'approved'), disabled: !canExport.value },
])

/* ---------- 确认生成 ---------- */
const confirmOpen = ref(false)
const okScope = computed(() => {
  const ts = task.value?.point_terminals || []
  if (task.value?.status === 'completed') return pendingGen.value.size > 0
  if (!confirmable.value) return false
  const sum = k => ts.reduce((s, t) => s + (t[k] || 0), 0)
  return (sum('pending') === 0 && sum('approved') > 0) || ts.some(t => t.pending === 0 && t.approved > 0)
})
async function confirmGenerate(t) {
  confirmOpen.value = false
  try { await post('/confirm', { async_mode: true, terminal: t || null }); await reload(true); session.note(`已开始生成用例${t ? `（${terminalLabel(t)}）` : ''}，生成完成后到「用例审核」`) } catch (e) { session.note(`确认失败：${e.message}`, 'err'); ui.error(e) }
}
// 生成中 / 质量检查中：轮询
let pollTimer = null
watch(() => [task.value?.status, task.value?.progress], ([s, p]) => { clearTimeout(pollTimer); if (s === 'running' || s === 'queued' || p === 'quality_check') pollTimer = setTimeout(() => reload(true), 4000) }, { immediate: true })
onBeforeUnmount(() => clearTimeout(pollTimer))

const openBlind = computed(() => (task.value?.blind_spots || []).filter(b => b.status === 'open').length)
const openDup = computed(() => (task.value?.dup_pairs || []).filter(d => d.status === 'open').length)
const pf = computed(() => task.value?.pending_fix?.kind === 'points' ? task.value.pending_fix : null)
const lastFix = computed(() => [...(task.value?.fix_log || [])].reverse().find(f => f.kind === 'points') || null)
const tabs = computed(() => [
  { key: 'points', label: '测试点', count: points.value.length }, { key: 'blind', label: '需求盲区', count: openBlind.value }, { key: 'dup', label: '疑似重复', count: openDup.value },
  { key: 'fix', label: '修改提案', count: pf.value ? pf.value.proposals.length : undefined }, { key: 'compare', label: '人工对比' },
])
const KEYS = [['j', ''], ['k', '移动'], ['a', '通过'], ['r', '驳回'], ['e', '修改'], ['d', '删除'], ['space', '勾选'], ['?', '说明']]
</script>
<template>
  <WbTaskPicker v-if="!tid" kind="point" />
  <template v-else>
    <div v-if="error" class="err-box">{{ error.message }}</div>
    <div v-else-if="!task" class="page-loading"><span class="spinner" /> 加载任务…</div>
    <WbShell v-else title="测试点审核" badge="审核测试点" :back-to="{ name: 'design-detail', params: { id: tid } }" :back-label="taskTitle" :task-status="task.status"
      :stats="[['测试点', stat.total], ['待审', stat.pending], ['通过', stat.approved], ['驳回', stat.rejected]]"
      :processed="session.processed.value" :elapsed="session.elapsed.value" :msg="session.msg.value" :msg-kind="session.msgKind.value" :keys="KEYS" :can-undo="session.history.value.length > 0"
      :checked="tab === 'points' ? checked.size : 0" @undo="undo" @clear="checked = new Set()">
      <template #actions>
        <span v-if="stat.pending" class="warn">还有 {{ stat.pending }} 条待审</span>
        <span v-else-if="stat.total" class="badge green">{{ terminal || scope.module ? '本范围' : '' }}待审已清零</span>
        <button v-if="stat.rejected && canAi && !pf && confirmable" type="button" class="btn" :disabled="busy" @click="fixPoints">AI 修改驳回项（{{ stat.rejected }}）</button>
        <button v-if="task.status === 'running' || task.status === 'queued'" type="button" class="btn primary lg" disabled><span class="spinner" /> 生成中…</button>
        <button v-else type="button" class="btn primary lg" :disabled="!okScope || !canReview || busy || task.progress === 'quality_check'" :title="task.progress === 'quality_check' ? '查漏 / 查重仍在后台进行' : !okScope ? '待审清零且有已通过测试点后可用' : ''" @click="confirmOpen = true">{{ task.status === 'completed' ? `为新通过的 ${pendingGen.size} 条生成用例` : '确认生成用例' }}</button>
        <TgDropdown label="⋯" :items="moreItems" />
      </template>
      <template #banner>
        <div v-if="task.progress === 'quality_check'" class="information mb"><span class="spinner" /> 拆解后的查漏 / 组合延伸 / 查重 / AI 评审仍在后台进行，期间可以审核，稍候再确认生成。</div>
        <div v-if="task.status === 'completed' && task.result?.cases?.length" class="information mb">用例已生成（{{ task.result.cases.length }} 条）。<router-link :to="{ name: 'cases', params: { tid } }">去用例审核 →</router-link><span v-if="pendingGen.size" class="muted"> · 另有 {{ pendingGen.size }} 条新通过的测试点待生成</span></div>
        <div v-if="task.status === 'failed' && task.error" class="err-box mb">任务失败：{{ task.error }}</div>
        <div v-if="conflict" class="attention mb">{{ conflict }}<button type="button" class="btn small" @click="reload(); conflict = ''">重新加载</button></div>
        <div v-for="(h, i) in hints" :key="i" class="attention mb">{{ h }}<button type="button" class="btn small" @click="hints.splice(i, 1)">知道了</button></div>
        <div v-if="openBlind && tab !== 'blind'" class="attention mb">{{ openBlind }} 条需求盲区待处置：「{{ (task.blind_spots.find(b => b.status === 'open') || {}).text?.slice(0, 60) }}…」<button type="button" class="btn small" @click="tab = 'blind'">处理盲区</button></div>
        <div v-if="holder && tab === 'points'" class="attention mb">{{ holder.by }} 正在编辑当前测试点 {{ cur?.tp_id }}，注意保存冲突</div>
        <TgTabs v-model="tab" :items="tabs" />
      </template>

      <template v-if="tab === 'points'" #list>
        <div class="list-tools">
          <div class="row">
            <TgSeg v-model="filter" :items="[['pending', '待审', stat.pending], ['approved', '通过', stat.approved], ['rejected', '驳回', stat.rejected], ['ai_review', 'AI 意见', stat.ai], ['all', '全部', stat.total]]" />
            <input v-model="q" class="field small pw-search" placeholder="搜索编号 / 描述 / 来源" />
          </div>
          <div class="row">
            <TgSeg v-model="terminal" :items="terminalTabs" />
            <select v-model="dim" class="select small"><option value="">全部维度</option><option v-for="d in [...new Set(points.map(p => p.dimension).filter(Boolean))]" :key="d" :value="d">{{ d }}</option></select>
          </div>
        </div>
        <div class="list-scroll">
          <template v-for="([m, list]) in groups" :key="m">
            <div class="grp"><span :title="m" class="pw-grp" @click="toggleModule(m)">{{ m }}</span><span>{{ list.length }}</span></div>
            <div v-for="p in list" :key="p.tp_id" :data-tp="p.tp_id" class="item" :class="{ ok: p.status === 'approved', rej: p.status === 'rejected', sel: p.tp_id === sel }" @click="select(p.tp_id)">
              <input type="checkbox" :checked="checked.has(p.tp_id)" @click.stop @change="toggleCheck(p.tp_id)" />
              <span class="st"></span><span class="id">{{ p.tp_id }}</span><span class="ttl" :title="p.point">{{ p.point }}</span>
              <span v-if="p.ai_review" class="badge purple" title="AI 评审意见">AI</span>
              <span v-if="(p.source || 'ai') !== 'ai'" class="badge" :class="p.source === 'manual' ? 'green' : 'purple'">{{ sourceText(p.source) }}</span>
            </div>
          </template>
          <TgEmpty v-if="!groups.length" :title="filter === 'pending' ? '待审已清空' : '这一类为空'" hint="切到「全部」或调整端 / 模块筛选" />
        </div>
      </template>
      <template v-if="tab === 'points'" #detail>
        <template v-if="cur">
          <PointDetail v-if="mode !== 'batchReject'" :p="cur" :log="curLog" :holder="holder" :mode="mode" :can-review="canReview" :can-edit="canEdit" :can-delete="canDelete" :can-ai="canAi" :busy="busy"
            :generated="generatedIds.has(cur.tp_id)" :pending-generation="pendingGen.has(cur.tp_id)" :model="task.context?.model || ''" :terminals="taskTerminals(task)"
            @approve="approve()" @reject="v => reject(v)" @edit="v => startEdit(typeof v === 'string' ? v : '')" @save="saveEdit" @cancel="mode === 'edit' ? cancelEdit() : (mode = '')" @delete="remove()" @unlock="unlock()" @fix="fixPoints" />
          <template v-else>
            <div class="detail-title">批量驳回 {{ checked.size }} 条测试点</div>
            <div class="inline-list mb"><span v-for="id in checkedList" :key="id" class="tag mono">{{ id }}</span></div>
            <WbRejectForm kind="point" :label="`${checked.size} 条 · 统一驳回意见`" scope="选中项" :busy="busy" @submit="batchReject" @cancel="mode = ''" />
          </template>
        </template>
        <TgEmpty v-else title="没有可审核的测试点" hint="调整筛选，或通过「⋯」查漏 / 延伸 / 手工新增" />
      </template>

      <template v-if="tab !== 'points'" #workbench>
        <PointBlindSpots v-if="tab === 'blind'" :items="task.blind_spots || []" :tid="tid" :sel="sel" :modules="modules" :req-ids="task.context?.requirement_ids || (task.context?.requirement_id ? [task.context.requirement_id] : [])" :can-edit="canEdit" :can-review="canReview" :busy="busy" @act="blindAct" @batch="blindBatch" @select="v => sel = v" />
        <WbDupPairs v-else-if="tab === 'dup'" :pairs="task.dup_pairs || []" kind="points" :tid="tid" :sel="sel" :can-review="canReview" :can-delete="canDelete && confirmable" :can-ai="canAi" :busy="busy" @act="dupAct" @batch="dupBatch" @delete="dupDelete" @recheck="dupCheck" @select="v => sel = v" />
        <div v-else-if="tab === 'fix'" class="panel panel-pad"><WbFixProposals :pf="pf" :last-fix="lastFix" :busy="busy" :can-confirm="can('case.edit')" @confirm="confirmFix" /><div v-if="!pf && stat.rejected && canAi" class="row mt"><button type="button" class="btn primary" :disabled="busy" @click="fixPoints">AI 修改驳回项（{{ stat.rejected }}）</button></div></div>
        <PointCompare v-else-if="tab === 'compare'" :report="task.point_compare" :has-manual="points.some(p => p.source === 'manual')" :can-edit="canEdit" :can-ai="canAi" :busy="busy" @import="manualBatch" @compare="compare" />
      </template>

      <template #batch>
        <button type="button" class="btn" :disabled="busy || !canReview" @click="batchApprove">通过</button>
        <button type="button" class="btn" :disabled="busy || !canReview" @click="mode = 'batchReject'">驳回</button>
        <select v-model="batchTerminal" class="select small pw-bt"><option v-for="t in taskTerminals(task)" :key="t.key" :value="t.key">{{ t.label }}</option></select>
        <button type="button" class="btn" :disabled="busy || !canEdit" @click="batchSetTerminal">改端</button>
        <button type="button" class="btn" :disabled="busy || !canDelete" @click="batchDelete">删除</button>
      </template>
    </WbShell>

    <PointConfirmDialog v-model="confirmOpen" :task="task" :busy="busy" @confirm="confirmGenerate" />
    <TgDialog v-model="aiAddOpen" title="AI 补充测试点" ok="让 AI 补充" :ok-disabled="!aiInstruction.trim()" @ok="aiAdd">
      <TgField label="补充要求" hint="AI 只生成满足要求的新增测试点，不改已有；与已有点高度相似的会跳过" required><textarea v-model="aiInstruction" class="field" rows="3" placeholder="如：补充网络切换与弱网场景" /></TgField>
    </TgDialog>
    <TgDialog v-model="manualOpen" title="手工新增测试点" ok="新增" :ok-disabled="!manual.point.trim()" @ok="manualAdd">
      <TgField label="模块"><input v-model="manual.module" class="field" list="pw-modules" placeholder="默认「未分组」" /><datalist id="pw-modules"><option v-for="m in modules" :key="m" :value="m" /></datalist></TgField>
      <TgField label="测试点描述" required><textarea v-model="manual.point" class="field" rows="3" /></TgField>
      <div class="form-grid">
        <TgField label="维度"><select v-model="manual.dimension" class="select"><option value="">不指定</option><option v-for="d in DIMENSIONS" :key="d" :value="d">{{ d }}</option></select></TgField>
        <TgField label="需求来源"><input v-model="manual.ref" class="field" placeholder="页面或章节 › 需求点" /></TgField>
      </div>
    </TgDialog>
  </template>
</template>
<style scoped>
.pw-search { flex: 1; min-width: 120px; }
.pw-grp { cursor: pointer; max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pw-bt { height: 28px; background: #2f4468; color: #fff; border-color: #3f5884; }
.list-tools .seg { flex-wrap: wrap; }
</style>

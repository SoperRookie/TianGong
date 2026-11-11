<script setup>
/* 依赖关系（3.13–3.15）：GET /projects/{name}/dependencies?kind=&module=；人工加边 POST .../edges（立即生效）；AI 识别 POST .../infer（草稿 proposed）；
   草稿确认 POST .../edges/{id}/confirm?kind=；删除 DELETE .../edges/{id}?kind=（需 knowledge.manage + 删除权限）。
   分层图：display_levels（含草稿）为列，上游在左；箭头指向被依赖方。本版不在打开视图时自动触发 AI 识别，只保留按钮。 */
import { ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, seg } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useUiStore } from '@/stores/ui'
import { useProjectActions } from './useProjectActions'
import { TgPanel, TgSeg, TgBadge, TgDialog, TgField, TgEmpty, TgIcon } from '@/components/ui'
const props = defineProps({ project: { type: String, required: true } })
const route = useRoute(), router = useRouter(), ui = useUiStore()
const { canDelete } = useProjectActions()
const KINDS = [['requirement', '需求功能依赖'], ['case', '用例依赖链路']]
const kind = ref(route.query.kind === 'case' ? 'case' : 'requirement'), module = ref(route.query.module || ''), focus = ref('')
watch([kind, module], ([k, m]) => router.replace({ query: { ...route.query, kind: k === 'requirement' ? undefined : k, module: m || undefined } }))
watch(kind, () => { module.value = ''; focus.value = '' })
const { data: g, loading, error, reload } = useLoad(() => api.get(`/api/v1/projects/${seg(props.project)}/dependencies`, { kind: kind.value, module: kind.value === 'case' ? module.value : undefined }), { deps: [() => props.project, kind, module] })
const nodes = computed(() => g.value?.nodes || {}), edges = computed(() => g.value?.edges || [])
const label = id => { const n = nodes.value[id]; if (!n) return id; return kind.value === 'case' ? `${n.case_id} ${n.title}` : n.title }
const confirmed = computed(() => edges.value.filter(e => e.status === 'confirmed')), proposed = computed(() => edges.value.filter(e => e.status === 'proposed'))
const nodeList = computed(() => Object.values(nodes.value).sort((a, b) => (a.case_id || a.title || '').localeCompare(b.case_id || b.title || '', 'zh')))
const inferring = ref(false), busy = ref(false)

/* ---- 分层布局 ---- */
const W = 190, H = 46, GX = 70, GY = 16, PAD = 24
const layout = computed(() => {
  if (!g.value) return null
  const levels = g.value.display_levels || g.value.levels || {}
  const shown = new Set(edges.value.flatMap(e => [e.from, e.to]).filter(id => nodes.value[id]))
  if (focus.value && nodes.value[focus.value]) shown.add(focus.value)
  if (!shown.size) return null
  const maxLvl = Math.max(0, ...Object.values(levels))
  const col = id => (id in levels ? levels[id] : maxLvl + 1)
  const cols = {}
  for (const id of shown) (cols[col(id)] = cols[col(id)] || []).push(id)
  const ncols = Object.keys(cols).map(Number).sort((a, b) => a - b)
  const pos = {}; let maxRows = 0
  ncols.forEach((c, ci) => {
    cols[c].sort((a, b) => label(a).localeCompare(label(b), 'zh'))
    cols[c].forEach((id, ri) => { pos[id] = { x: PAD + ci * (W + GX), y: PAD + 22 + ri * (H + GY) } })
    maxRows = Math.max(maxRows, cols[c].length)
  })
  const width = PAD * 2 + ncols.length * W + (ncols.length - 1) * GX, height = PAD * 2 + 22 + maxRows * (H + GY)
  const cyc = new Set(g.value.cycles || [])
  const lines = edges.value.filter(e => pos[e.from] && pos[e.to]).map(e => {
    const a = pos[e.from], b = pos[e.to]
    const x1 = a.x, y1 = a.y + H / 2, x2 = b.x + W, y2 = b.y + H / 2
    const dx = Math.max(30, Math.abs(x1 - x2) / 2)
    const d = x1 >= x2 ? `M${x1},${y1} C${x1 - dx},${y1} ${x2 + dx},${y2} ${x2},${y2}` : `M${a.x + W},${y1} C${a.x + W + dx},${y1} ${b.x - dx},${y2} ${b.x},${y2}`
    return { e, d }
  })
  const boxes = [...shown].map(id => { const n = nodes.value[id]; return { id, n, p: pos[id], cyc: cyc.has(id), t1: kind.value === 'case' ? n.case_id : n.title, t2: kind.value === 'case' ? n.title : (n.status || n.module || '') } })
  const heads = ncols.map((c, ci) => ({ x: PAD + ci * (W + GX), text: c <= maxLvl ? (c === 0 ? '上游 · 无前置' : `第 ${c} 层`) : '仅关联' }))
  return { width, height, lines, boxes, heads }
})
function openNode(id) { const n = nodes.value[id]; if (!n) return; kind.value === 'case' ? router.push({ name: 'cases', params: { tid: n.task_id } }) : router.push({ name: 'req-detail', params: { id } }) }

/* ---- 动作 ---- */
async function act(fn, msg) { busy.value = true; try { await fn(); if (msg) ui.toast(msg); await reload(true) } catch (e) { ui.error(e) } finally { busy.value = false } }
function addEdge(from, to, relation, note) { return act(() => api.post(`/api/v1/projects/${seg(props.project)}/dependencies/edges`, { kind: kind.value, from, to, relation, note: note || '' }), '已添加依赖') }
function confirmEdge(id) { return act(() => api.post(`/api/v1/projects/${seg(props.project)}/dependencies/edges/${id}/confirm`, {}, { query: { kind: kind.value } }), '已确认') }
async function removeEdge(e, isDraft) {
  if (!isDraft && !await ui.confirm('删除依赖关系？', `${label(e.from)} → ${label(e.to)}`, { ok: '删除', danger: true })) return
  return act(() => api.del(`/api/v1/projects/${seg(props.project)}/dependencies/edges/${e.edge_id}`, undefined, { query: { kind: kind.value } }), isDraft ? '已丢弃草稿' : '已删除')
}
async function confirmAll() {
  const ids = proposed.value.map(e => e.edge_id); let ok = 0; const fail = []
  busy.value = true
  for (const id of ids) { try { await api.post(`/api/v1/projects/${seg(props.project)}/dependencies/edges/${id}/confirm`, {}, { query: { kind: kind.value } }); ok++ } catch (e) { fail.push(e.message) } }
  busy.value = false
  ui.toast(`已确认 ${ok} 条${fail.length ? `，${fail.length} 条失败：${fail[0]}` : ''}`, fail.length ? 'warn' : 'ok'); reload(true)
}
async function infer() {
  const msg = kind.value === 'case'
    ? (module.value ? `将把模块「${module.value}」内的用例（编号、标题、前置条件、第一步）发给模型识别前置 / 数据依赖，结果为草稿，需人工确认。` : '将逐模块把本项目用例发给模型识别依赖（每模块一次调用，单次最多 20 个模块），结果为草稿，需人工确认。')
    : '将把本项目全部需求（标题、原文摘要、模块、测试点、外部依赖）发给模型识别功能依赖，结果为草稿，需人工确认。'
  if (!await ui.confirm('AI 识别依赖？', msg, { ok: '开始识别' })) return
  inferring.value = true
  try {
    const body = kind.value === 'case' && !module.value ? { kind: 'case', all_modules: true, force: true } : { kind: kind.value, module: module.value }
    const r = await api.post(`/api/v1/projects/${seg(props.project)}/dependencies/infer`, body)
    const n = (r.proposed || []).length
    ui.toast(n ? `识别完成：提案 ${n} 条${r.skipped?.length ? `，忽略 ${r.skipped.length} 条无效输出` : ''}，请在「待确认」区确认` : `识别完成：未发现明确依赖${r.reason ? `（${r.reason}）` : ''}，可手动添加`, n ? 'ok' : 'warn')
    reload(true)
  } catch (e) { ui.error(e) } finally { inferring.value = false }
}
/* 添加依赖对话框 */
const dlg = ref(false), form = ref({ from: '', to: '', relation: '', note: '' })
function openDlg() { const rel = Object.keys(g.value?.relations || {})[0] || ''; form.value = { from: focus.value || nodeList.value[0]?.id || '', to: nodeList.value[1]?.id || '', relation: rel, note: '' }; dlg.value = true }
async function saveEdge() {
  const f = form.value
  if (!f.from || !f.to) return ui.toast('请选择两端', 'warn')
  if (f.from === f.to) return ui.toast('不能依赖自身', 'warn')
  await addEdge(f.from, f.to, f.relation, f.note.trim()); dlg.value = false
}
</script>
<template>
  <div v-if="error" class="err-box mb">{{ error.message }}</div>
  <div class="filters mb">
    <TgSeg v-model="kind" :items="KINDS" />
    <select v-if="kind === 'case'" v-model="module" class="select"><option value="">全部模块（只画有依赖的用例）</option><option v-for="m in g?.modules || []" :key="m" :value="m">{{ m }}</option></select>
    <span class="muted small">节点 {{ Object.keys(nodes).length }} · 已生效 {{ confirmed.length }} 条<template v-if="proposed.length"> · <b style="color:var(--amber)">待确认 {{ proposed.length }} 条</b></template><template v-if="g?.cycles?.length"> · <b style="color:var(--red)">成环 {{ g.cycles.length }} 个节点</b></template><template v-if="g && !g.auto_done"> · <span class="muted">本范围尚未 AI 识别{{ g.pending_modules?.length ? `（${g.pending_modules.length} 个模块待跑）` : '' }}</span></template></span>
    <span class="grow" />
    <template v-if="g?.can_manage">
      <button type="button" class="btn small" :disabled="inferring || Object.keys(nodes).length < 2" :title="Object.keys(nodes).length < 2 ? '节点不足 2 个' : ''" @click="infer"><TgIcon name="spark" :size="13" /> {{ inferring ? '识别中…' : 'AI 识别依赖' }}</button>
      <button type="button" class="btn small primary" :disabled="Object.keys(nodes).length < 2" @click="openDlg"><TgIcon name="plus" :size="13" /> 添加依赖</button>
    </template>
  </div>
  <div v-if="g?.hints?.length" class="information mb"><TgIcon name="info" :size="14" /><span>需求分析里提到的外部依赖命中了本项目需求，可一键建立：
    <span v-for="h in g.hints" :key="h.from + h.to" class="tag clickable" style="margin:2px 4px 2px 0" :title="h.reason" @click="g.can_manage && addEdge(h.from, h.to, 'depends', h.reason)">{{ label(h.from) }} → {{ label(h.to) }} ＋</span></span></div>
  <TgPanel v-if="proposed.length" class="mb" title="AI 识别待确认" hint="草稿：确认后才进入链路；丢弃即删除" :pad="false">
    <template #actions><button v-if="g.can_manage" type="button" class="btn small" :disabled="busy" @click="confirmAll">全部确认（{{ proposed.length }}）</button></template>
    <table class="table dense">
      <thead><tr><th>下游（依赖方）</th><th style="width:24px"></th><th>上游（被依赖）</th><th style="width:100px">关系</th><th>依据</th><th style="width:130px"></th></tr></thead>
      <tbody><tr v-for="e in proposed" :key="e.edge_id"><td>{{ label(e.from) }}</td><td class="muted">→</td><td>{{ label(e.to) }}</td><td><span class="tag">{{ g.relations[e.relation] || e.relation }}</span></td><td class="muted">{{ e.reason || '' }}</td>
        <td><span v-if="g.can_manage" class="row" style="gap:2px"><button type="button" class="btn small success" :disabled="busy" @click="confirmEdge(e.edge_id)">确认</button><button type="button" class="btn ghost small" :disabled="busy" @click="removeEdge(e, true)">丢弃</button></span></td></tr></tbody>
    </table>
  </TgPanel>
  <TgPanel title="分层图" hint="上游（被依赖方）在左；实线为已生效，虚线为草稿；成环节点标红；点击节点进入对应需求 / 用例" :pad="false">
    <div v-if="loading && !g" class="muted panel-pad">加载中…</div>
    <TgEmpty v-else-if="!layout" title="还没有依赖关系" :hint="g?.can_manage ? '点「添加依赖」或「AI 识别依赖」开始' : ''" />
    <div v-else class="graph-wrap">
      <svg :width="layout.width" :height="layout.height" :viewBox="`0 0 ${layout.width} ${layout.height}`">
        <defs><marker id="tg-dep-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#7c8aa6" /></marker></defs>
        <text v-for="h in layout.heads" :key="h.x" class="lvl" :x="h.x" :y="PAD + 4">{{ h.text }}</text>
        <path v-for="l in layout.lines" :key="l.e.edge_id" class="edge" :class="[l.e.relation, { proposed: l.e.status === 'proposed' }]" :d="l.d" marker-end="url(#tg-dep-arrow)"><title>{{ g.relations[l.e.relation] || l.e.relation }}{{ l.e.reason ? '：' + l.e.reason : '' }}</title></path>
        <g v-for="b in layout.boxes" :key="b.id" class="node" :class="{ cyc: b.cyc, focus: focus === b.id }" :transform="`translate(${b.p.x},${b.p.y})`" @click="openNode(b.id)">
          <rect :width="W" :height="H" rx="3"><title>{{ label(b.id) }}</title></rect>
          <text x="10" y="19" font-weight="600">{{ String(b.t1 || '').slice(0, 16) }}</text><text class="sub" x="10" y="36">{{ String(b.t2 || '').slice(0, 22) }}</text>
        </g>
      </svg>
    </div>
  </TgPanel>
  <div class="grid-2 mt" style="grid-template-columns:1fr 1fr">
    <TgPanel title="依赖链路" hint="从最上游到最下游，仅已生效的前置关系；最多 30 条">
      <div v-if="g?.chains?.length" class="list">
        <div v-for="(c, i) in g.chains" :key="i" class="chain"><template v-for="(id, j) in c" :key="id"><span v-if="j" class="muted"> → </span><span class="link" :title="label(id)" @click="focus = id">{{ label(id).slice(0, 28) }}</span></template></div>
      </div>
      <span v-else class="muted small">还没有形成链路：添加两条以上前置关系后自动生成</span>
    </TgPanel>
    <TgPanel title="全部关系" :hint="`${confirmed.length} 条已生效`" :pad="false">
      <table class="table dense">
        <thead><tr><th>下游</th><th style="width:22px"></th><th>上游</th><th style="width:90px">关系</th><th style="width:70px">来源</th><th style="width:40px"></th></tr></thead>
        <tbody>
          <tr v-for="e in confirmed" :key="e.edge_id"><td>{{ label(e.from) }}</td><td class="muted">→</td><td>{{ label(e.to) }}</td><td><span class="tag">{{ g.relations[e.relation] || e.relation }}</span></td><td><span class="tag" :class="e.source === 'ai' ? 'ai' : 'manual'">{{ e.source === 'ai' ? 'AI' : '人工' }}</span><div v-if="e.note" class="muted tiny" :title="e.note">{{ e.note.slice(0, 20) }}</div></td>
            <td><button v-if="g.can_manage && canDelete" type="button" class="btn ghost small" style="color:var(--red)" title="删除" @click="removeEdge(e, false)"><TgIcon name="trash" :size="13" /></button></td></tr>
          <tr v-if="!confirmed.length"><td colspan="6"><TgEmpty title="暂无依赖关系" /></td></tr>
        </tbody>
      </table>
    </TgPanel>
  </div>
  <TgDialog v-model="dlg" title="添加依赖" ok="添加" :busy="busy" @ok="saveEdge">
    <div class="information mb"><TgIcon name="info" :size="14" /><span>{{ kind === 'case' ? '下游用例执行前必须先执行上游用例（前置用例）；列表为本项目全部用例。' : '下游需求的功能以上游需求为前置；列表为本项目全部需求。' }}前置关系不能成环。</span></div>
    <TgField label="下游（依赖方）" required><select v-model="form.from" class="select"><option v-for="n in nodeList" :key="n.id" :value="n.id">{{ label(n.id) }}</option></select></TgField>
    <TgField label="上游（被依赖）" required><select v-model="form.to" class="select"><option v-for="n in nodeList" :key="n.id" :value="n.id">{{ label(n.id) }}</option></select></TgField>
    <TgField label="关系"><select v-model="form.relation" class="select"><option v-for="(l, k) in g?.relations || {}" :key="k" :value="k">{{ l }}</option></select></TgField>
    <TgField label="备注" hint="≤ 300 字"><input v-model="form.note" class="field" maxlength="300" /></TgField>
  </TgDialog>
</template>
<style scoped>
.graph-wrap { overflow: auto; padding: 8px; }
svg .lvl { font-size: 11px; fill: #8b98ad; font-weight: 700; }
svg .edge { fill: none; stroke: #7c8aa6; stroke-width: 1.5; }
svg .edge.depends, svg .edge.precondition { stroke: #1f6fe5; }
svg .edge.data { stroke: #8261d3; }
svg .edge.proposed { stroke-dasharray: 5 4; stroke: #d68a28; }
svg .node { cursor: pointer; }
svg .node rect { fill: #fff; stroke: #c9d4e4; }
svg .node:hover rect { stroke: #1f6fe5; }
svg .node.cyc rect { stroke: #db5361; fill: #fff8f8; }
svg .node.focus rect { stroke: #1f6fe5; stroke-width: 2; }
svg .node text { font-size: 11.5px; fill: #2b3b5b; }
svg .node text.sub { font-size: 10.5px; fill: #8b98ad; }
.chain { font-size: 12.5px; }
</style>

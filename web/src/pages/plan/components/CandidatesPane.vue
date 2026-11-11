<script setup>
/* 候选池：GET /plans/{id}/candidates（任务 → 已通过用例，按模块 / 优先级 / 关键字筛选），勾选加入 / 按筛选全量加入 POST /plans/{id}/cases；AI 挑选 POST /plans/{id}/select */
import { ref, computed, watch, onMounted } from 'vue'
import { api } from '@/api/client'
import { useUiStore } from '@/stores/ui'
import { fmtTime } from '@/utils/format'
import { PRIORITIES } from '@/utils/status'
import { TgTable, TgPrio, TgBadge, TgDialog, TgField, TgEmpty } from '@/components/ui'
const props = defineProps({ plan: { type: Object, required: true }, canManage: Boolean })
const emit = defineEmits(['reload'])
const ui = useUiStore()
const base = computed(() => `/api/v1/plans/${props.plan.plan_id}`)
const tasks = ref([]), taskId = ref(''), module = ref(''), priority = ref(''), kw = ref('')
const cases = ref([]), modules = ref([]), loading = ref(false), adding = ref(false), selected = ref(new Set())
const taskLabel = t => `${t.source || t.task_id} · 已通过 ${t.approved} 条 · ${fmtTime(t.created_at).slice(0, 10)}`
async function loadTasks() {
  try { tasks.value = (await api.get(`${base.value}/candidates`)).tasks || [] } catch (e) { ui.error(e); return }
  if (!taskId.value && tasks.value.length) taskId.value = tasks.value[0].task_id
}
let timer = null
async function loadCases() {
  if (!taskId.value) { cases.value = []; return }
  loading.value = true
  try {
    const r = await api.get(`${base.value}/candidates`, { task_id: taskId.value, module: module.value, priority: priority.value, keyword: kw.value.trim() })
    cases.value = r.cases || []; modules.value = r.modules || []
    selected.value = new Set([...selected.value].filter(u => cases.value.some(c => c.uid === u && !c.added)))
  } catch (e) { ui.error(e) } finally { loading.value = false }
}
onMounted(loadTasks)
watch(taskId, () => { module.value = ''; selected.value = new Set(); loadCases() })
watch([module, priority], loadCases)
watch(kw, () => { clearTimeout(timer); timer = setTimeout(loadCases, 300) })
watch(() => props.plan.items.length, loadCases)
const addable = computed(() => cases.value.filter(c => !c.added))
const cols = [{ key: 'case_id', title: '编号 / 版本', w: '190px' }, { key: 'title', title: '标题' }, { key: 'module', title: '模块', w: '170px' }, { key: 'priority', title: '优先级', w: '64px' }, { key: 'state', title: '状态', w: '80px' }]
async function add(all) {
  const body = { task_id: taskId.value }
  if (all) Object.assign(body, { module: module.value, priority: priority.value, keyword: kw.value.trim() })
  else { body.uids = [...selected.value]; if (!body.uids.length) return ui.toast('请先勾选要加入的用例', 'warn') }
  if (all && !await ui.confirm('按筛选全量加入？', `当前筛选下可加入 ${addable.value.length} 条已通过用例。`)) return
  adding.value = true
  try {
    const r = await api.post(`${base.value}/cases`, body)
    ui.toast(`已加入 ${r.added} 条${r.skipped ? `（跳过已在计划 ${r.skipped} 条）` : ''}`)
    selected.value = new Set(); emit('reload'); loadCases()
  } catch (e) { ui.error(e) } finally { adding.value = false }
}
/* ---- AI 挑选 ---- */
const GOALS = ['冒烟', '全量回归', '变更影响回归', '模块专项']
const GOAL_HINT = { '冒烟': '主流程 + 高优先级，预算内控制条数', '全量回归': '尽量覆盖全部模块，按优先级排序', '变更影响回归': '按变更摘要挑受影响用例；不填则自动带本项目最近一次需求变更分析', '模块专项': '只盯重点模块，填模块名（逗号分隔）' }
const selOpen = ref(false), selRunning = ref(false), selAdding = ref(false)
const sel = ref({ goal: '冒烟', budget: 0, focus_modules: '', change_summary: '', task_ids: [] })
const draft = ref(props.plan.selection_draft || null), picked = ref(new Set())
watch(() => props.plan.selection_draft, d => { draft.value = d || null })
function openSelect() {
  const d = draft.value
  if (d) sel.value = { goal: d.goal || '冒烟', budget: d.budget || 0, focus_modules: (d.focus_modules || []).join('，'), change_summary: d.change_summary || '', task_ids: [] }
  picked.value = new Set((d?.selected || []).map(c => c.key))
  selOpen.value = true
}
async function runSelect() {
  selRunning.value = true
  try {
    const body = { goal: sel.value.goal, budget: +sel.value.budget || 0, change_summary: sel.value.change_summary.trim(), focus_modules: sel.value.focus_modules.split(/[,，、\s]+/).map(s => s.trim()).filter(Boolean), task_ids: sel.value.task_ids }
    draft.value = await api.post(`${base.value}/select`, body)
    picked.value = new Set((draft.value.selected || []).map(c => c.key))
    ui.toast(`候选 ${draft.value.candidates} 条，AI 选出 ${draft.value.selected.length} 条`)
  } catch (e) { ui.error(e) } finally { selRunning.value = false }
}
const inPlan = computed(() => new Set(props.plan.items.map(i => `${i.task_id}:${i.uid}`)))
async function addPicked() {
  const list = (draft.value?.selected || []).filter(c => picked.value.has(c.key) && !inPlan.value.has(c.key))
  if (!list.length) return ui.toast('请先勾选要加入的用例', 'warn')
  const byTask = {}
  list.forEach(c => (byTask[c.task_id] ||= []).push(c.uid))
  selAdding.value = true
  let added = 0
  try {
    for (const [tid, uids] of Object.entries(byTask)) added += (await api.post(`${base.value}/cases`, { task_id: tid, uids })).added
    ui.toast(`已加入 ${added} 条`); emit('reload'); loadCases()
  } catch (e) { ui.error(e) } finally { selAdding.value = false }
}
function togglePick(k) { const s = new Set(picked.value); s.has(k) ? s.delete(k) : s.add(k); picked.value = s }
</script>
<template>
  <div v-if="!canManage" class="information">只有计划管理权限（测试负责人 / 项目管理员）可以把用例加入计划。</div>
  <template v-else>
    <div class="filters" style="margin-bottom:10px">
      <select v-model="taskId" class="select" style="max-width:360px"><option v-if="!tasks.length" value="">本项目还没有「已通过」的用例</option><option v-for="t in tasks" :key="t.task_id" :value="t.task_id">{{ taskLabel(t) }}</option></select>
      <select v-model="module" class="select"><option value="">全部模块</option><option v-for="m in modules" :key="m" :value="m">{{ m || '（无模块）' }}</option></select>
      <select v-model="priority" class="select"><option value="">优先级：全部</option><option v-for="p in PRIORITIES" :key="p" :value="p">{{ p }}</option></select>
      <input v-model="kw" class="field" style="width:180px" placeholder="关键词（编号 / 标题 / 关键词）" />
      <span class="grow" />
      <button type="button" class="btn small" @click="openSelect">AI 挑选<span v-if="draft" class="tag" style="margin-left:4px">草稿 {{ draft.selected?.length || 0 }}</span></button>
      <button type="button" class="btn small" :disabled="adding || !addable.length" @click="add(true)">按筛选全量加入 {{ addable.length }}</button>
      <button type="button" class="btn primary small" :disabled="adding || !selected.size" @click="add(false)">加入选中 {{ selected.size || '' }}</button>
    </div>
    <TgTable :cols="cols" :rows="cases" row-key="uid" :loading="loading" dense selectable v-model:selected="selected" empty="该筛选条件下没有可加入的已通过用例" empty-hint="候选池只列评审通过的正式用例">
      <template #cell-case_id="{ row }"><span class="mono">{{ row.case_id }}</span><div class="sub">v{{ row.version || 1 }}</div></template>
      <template #cell-title="{ row }">{{ row.title }}<div v-if="row.keywords" class="sub">{{ row.keywords }}</div></template>
      <template #cell-module="{ row }"><span class="small">{{ row.module || '—' }}</span></template>
      <template #cell-priority="{ row }"><TgPrio :p="row.priority" /></template>
      <template #cell-state="{ row }"><TgBadge v-if="row.added" status="approved" text="已在计划" /><span v-else class="muted tiny">可加入</span></template>
    </TgTable>
    <div class="tiny muted" style="padding:8px 2px">只收「已通过」的正式用例；加入即完整快照并引用版本号，之后正式用例修改不影响计划。</div>
  </template>
  <TgDialog v-model="selOpen" title="AI 挑选计划用例" wide no-foot>
    <div class="grid-sel">
      <div>
        <TgField label="目标"><select v-model="sel.goal" class="select"><option v-for="g in GOALS" :key="g">{{ g }}</option></select><small class="muted">{{ GOAL_HINT[sel.goal] }}</small></TgField>
        <TgField label="条数上限" hint="0 = 不限"><input v-model="sel.budget" type="number" min="0" class="field" /></TgField>
        <TgField label="重点模块" hint="模块专项必填，逗号分隔"><input v-model="sel.focus_modules" class="field" placeholder="如：活动入口，奖励领取" /></TgField>
        <TgField label="变更摘要" hint="变更影响回归的依据；留空自动带最近一次需求变更分析"><textarea v-model="sel.change_summary" class="field" rows="3" /></TgField>
        <TgField label="只从这些任务挑" hint="不选 = 本项目全部有正式用例的任务"><select v-model="sel.task_ids" class="select" multiple size="4"><option v-for="t in tasks" :key="t.task_id" :value="t.task_id">{{ t.source || t.task_id }}（{{ t.approved }}）</option></select></TgField>
        <button type="button" class="btn primary" :disabled="selRunning" @click="runSelect"><span v-if="selRunning" class="spinner" /> {{ selRunning ? 'AI 挑选中（候选每 300 条一批）…' : (draft ? '重新挑选' : 'AI 挑选') }}</button>
      </div>
      <div>
        <TgEmpty v-if="!draft" title="还没有挑选草稿" hint="候选为本项目已通过、还没在计划内的正式用例；选好目标点「AI 挑选」。" />
        <template v-else>
          <div class="small" style="margin-bottom:8px">候选 {{ draft.candidates }} 条 → 选 <b>{{ draft.selected.length }}</b> 条<template v-if="draft.truncated">（超预算截掉 {{ draft.truncated }} 条）</template><template v-if="draft.failed">，{{ draft.failed }} 批失败</template> · {{ draft.by }} {{ fmtTime(draft.at) }}</div>
          <div v-if="draft.excluded_summary" class="tiny muted" style="margin-bottom:4px">没选：{{ draft.excluded_summary }}</div>
          <div v-if="draft.coverage_note" class="tiny" style="color:#d68a28;margin-bottom:6px">{{ draft.coverage_note }}</div>
          <div class="list sel-list">
            <div v-for="c in draft.selected" :key="c.key" class="row" style="flex-wrap:nowrap;align-items:flex-start">
              <input type="checkbox" :checked="picked.has(c.key)" :disabled="inPlan.has(c.key)" @change="togglePick(c.key)" />
              <div style="min-width:0;flex:1"><span class="mono">{{ c.case_id }}</span> <TgPrio :p="c.priority" /> <TgBadge v-if="inPlan.has(c.key)" status="approved" text="已在计划" /><span v-if="c.failed_recent" class="badge red" style="margin-left:4px">最近失败</span><div>{{ c.title }}</div><div class="tiny muted">{{ c.module }} · {{ c.reason }}<span v-if="c.by === 'program'" class="tag" style="margin-left:4px">程序兜底</span></div></div>
            </div>
            <TgEmpty v-if="!draft.selected.length" title="没有选中任何用例" />
          </div>
          <div class="row" style="justify-content:flex-end;margin-top:10px"><button type="button" class="btn primary" :disabled="selAdding" @click="addPicked">加入勾选 {{ [...picked].filter(k => !inPlan.has(k)).length }} 条</button></div>
        </template>
      </div>
    </div>
  </TgDialog>
</template>
<style scoped>
.grid-sel { display: grid; grid-template-columns: 300px minmax(0, 1fr); gap: 18px; }
.sel-list { max-height: 420px; overflow: auto; }
.sel-list > div { padding: 8px 0; }
</style>

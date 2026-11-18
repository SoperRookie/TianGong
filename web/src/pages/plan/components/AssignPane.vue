<script setup>
/* 分配：GET /plans/{id}/assignees 可选执行人；POST /plans/{id}/assign 按勾选项或按模块分配，带 expected 快照做冲突拦截，冲突后由操作人确认改派；附按执行人统计 */
import { ref, computed, onMounted } from 'vue'
import { api } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { pct } from '@/utils/format'
import { TgTable, TgPrio, TgBadge, TgPanel, TgEmpty } from '@/components/ui'
import RunBar from './RunBar.vue'
const props = defineProps({ plan: { type: Object, required: true }, canAssign: Boolean })
const emit = defineEmits(['reload'])
const auth = useAuthStore(), ui = useUiStore()
const base = computed(() => `/api/v1/plans/${props.plan.plan_id}`)
const info = ref({ assignees: [], modules: [] }), assignee = ref(''), module = ref(''), filterModule = ref(''), filterAssignee = ref('')
const selected = ref(new Set()), busy = ref(false), conflicts = ref([])
onMounted(async () => { try { info.value = await api.get(`${base.value}/assignees`) } catch (e) { ui.error(e) } })
const latest = computed(() => props.plan.runs?.length ? props.plan.runs[props.plan.runs.length - 1] : null)
const results = computed(() => latest.value?.results || {})
const rows = computed(() => props.plan.items.filter(i => (!filterModule.value || (i.module || '') === filterModule.value) && (filterAssignee.value === '__none' ? !i.assignee : !filterAssignee.value || i.assignee === filterAssignee.value)))
const assigneesInPlan = computed(() => [...new Set(props.plan.items.map(i => i.assignee).filter(Boolean))].sort())
const cols = [{ key: 'case_id', title: '编号', w: '180px' }, { key: 'title', title: '标题' }, { key: 'module', title: '模块', w: '160px' }, { key: 'priority', title: '优先级', w: '64px' }, { key: 'assignee', title: '当前执行人', w: '130px' }, { key: 'result', title: '本轮结果', w: '90px' }]
const expectedOf = ids => Object.fromEntries(props.plan.items.filter(i => ids.includes(i.item_id)).map(i => [i.item_id, i.assignee || null]))
async function doAssign(body, label) {
  busy.value = true
  try {
    const r = await api.post(`${base.value}/assign`, body)
    conflicts.value = (r.conflicts || []).map(c => typeof c === 'string' ? { item_id: c } : c)
    ui.toast(`已分配 ${r.assigned} 条给 ${body.assignee}${conflicts.value.length ? `，${conflicts.value.length} 条被他人先行分配，请确认是否改派` : ''}`, conflicts.value.length ? 'warn' : 'ok')
    selected.value = new Set(); emit('reload')
  } catch (e) { ui.error(e) } finally { busy.value = false }
}
function assignSelected() {
  if (!assignee.value) return ui.toast('请选择执行人', 'warn')
  const ids = [...selected.value]
  if (!ids.length) return ui.toast('请先勾选用例', 'warn')
  doAssign({ assignee: assignee.value, item_ids: ids, expected: expectedOf(ids) })
}
async function assignModule() {
  if (!assignee.value) return ui.toast('请选择执行人', 'warn')
  const ids = props.plan.items.filter(i => !module.value || (i.module || '') === module.value).map(i => i.item_id)
  if (!ids.length) return ui.toast('该模块下没有用例', 'warn')
  if (!await ui.confirm('按模块分配？', `「${module.value || '全部模块'}」下 ${ids.length} 条用例将分配给 ${assignee.value}（已分配给他人的会改派并留痕）。`)) return
  doAssign({ assignee: assignee.value, item_ids: ids, expected: expectedOf(ids) })
}
async function forceReassign() {
  const ids = conflicts.value.map(c => c.item_id).filter(Boolean)
  if (!ids.length) return
  if (!await ui.confirm('确认改派？', `${ids.length} 条用例已被他人先行分配，确认改派给 ${assignee.value}（留痕原执行人）。`, { ok: '改派' })) return
  await doAssign({ assignee: assignee.value, item_ids: ids })
  conflicts.value = []
}
const conflictItem = c => props.plan.items.find(i => i.item_id === c.item_id)
const people = computed(() => props.plan.people || [])
const pcols = [{ key: 'username', title: '执行人' }, { key: 'assigned', title: '分配', w: '60px' }, { key: 'executed', title: '已执行', w: '160px' }, { key: 'pending', title: '待执行', w: '60px' }, { key: 'pass', title: '通过', w: '56px' }, { key: 'fail', title: '失败', w: '56px' }, { key: 'blocked', title: '阻塞', w: '56px' }, { key: 'skipped', title: '跳过', w: '56px' }, { key: 'proxied', title: '代执行', w: '60px' }]
</script>
<template>
  <div v-if="!canAssign" class="information" style="margin-bottom:12px">只有分配权限（测试负责人 / 项目管理员）可以分配执行人；下面是只读视图。</div>
  <div v-else class="assign-bar">
    <label class="small">执行人<select v-model="assignee" class="select" style="margin-left:6px;min-width:180px"><option value="">请选择</option><option v-for="a in info.assignees" :key="a.username" :value="a.username">{{ a.name ? `${a.name}（${a.username}）` : a.username }} · {{ a.role_label }}</option></select></label>
    <button type="button" class="btn primary small" :disabled="busy || !selected.size" @click="assignSelected">分配选中 {{ selected.size || '' }}</button>
    <span class="muted tiny">或</span>
    <select v-model="module" class="select"><option value="">全部模块（{{ plan.items.length }}）</option><option v-for="m in info.modules" :key="m.module" :value="m.module">{{ m.module || '（无模块）' }}（{{ m.count }}）</option></select>
    <button type="button" class="btn small" :disabled="busy" @click="assignModule">按模块分配</button>
    <span class="grow" /><span class="tiny muted">候选 = 项目正常状态的非只读成员 + 系统管理员 · 留痕原执行人 / 新执行人 / 操作人 · 条目级乐观锁</span>
  </div>
  <div v-if="conflicts.length" class="attention" style="margin-bottom:10px">
    <div><b>{{ conflicts.length }} 条用例被他人先行分配，未覆盖：</b><div class="tiny">{{ conflicts.map(c => `${c.case_id || conflictItem(c)?.case_id || c.item_id} → 现执行人 ${c.assignee ?? '—'}${c.by ? `（${c.by} 分配）` : ''}`).join('；') }}</div></div>
    <button type="button" class="btn small danger" @click="forceReassign">确认改派</button><button type="button" class="btn small" @click="conflicts = []">忽略</button>
  </div>
  <div class="filters" style="margin-bottom:8px">
    <select v-model="filterModule" class="select"><option value="">筛选模块：全部</option><option v-for="m in info.modules" :key="m.module" :value="m.module">{{ m.module || '（无模块）' }}</option></select>
    <select v-model="filterAssignee" class="select"><option value="">执行人：全部</option><option value="__none">未分配</option><option v-for="a in assigneesInPlan" :key="a" :value="a">{{ a }}</option></select>
    <span class="grow" /><span class="small muted">{{ rows.length }} / {{ plan.items.length }} 条 · 已分配 {{ plan.summary?.assigned || 0 }}</span>
  </div>
  <TgTable :cols="cols" :rows="rows" row-key="item_id" dense :selectable="canAssign" v-model:selected="selected" empty="没有用例">
    <template #cell-case_id="{ row }"><span class="mono">{{ row.case_id }}</span></template>
    <template #cell-module="{ row }"><span class="small">{{ row.module || '—' }}</span></template>
    <template #cell-priority="{ row }"><TgPrio :p="row.priority" /></template>
    <template #cell-assignee="{ row }"><template v-if="row.assignee">{{ row.assignee }}<span v-if="row.assignee === auth.me" class="tag" style="margin-left:4px">我</span></template><span v-else class="muted">未分配 · 执行即认领</span></template>
    <template #cell-result="{ row }"><TgBadge v-if="results[row.item_id]" :status="results[row.item_id].status" /><span v-else class="muted">—</span></template>
  </TgTable>
  <TgPanel class="mt" title="按执行人统计" :hint="latest ? `以最近一轮「${latest.name}」为准；代执行计入实际执行人` : '还没有执行轮次，只统计分配'">
    <TgTable :cols="pcols" :rows="people" row-key="username" dense empty="还没有分配">
      <template #cell-username="{ row }"><b>{{ row.username }}</b></template>
      <template #cell-executed="{ row }"><div class="row" style="flex-wrap:nowrap;gap:6px"><RunBar :summary="row" :total="Math.max(row.assigned, row.executed)" mini /><span class="tiny">{{ row.executed }} · {{ pct(row.executed, Math.max(row.assigned, row.executed)) }}</span></div></template>
      <template #cell-pass="{ row }"><span class="ok">{{ row.pass }}</span></template>
      <template #cell-fail="{ row }"><span style="color:#db5361">{{ row.fail }}</span></template>
    </TgTable>
  </TgPanel>
</template>
<style scoped>
.assign-bar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; padding: 10px 12px; border: 1px solid #e3e9f3; border-radius: 3px; background: #f7f9fd; margin-bottom: 10px; }
.assign-bar .grow { flex: 1; }
</style>

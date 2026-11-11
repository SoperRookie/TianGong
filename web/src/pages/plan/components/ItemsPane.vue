<script setup>
/* 用例清单：计划内快照（标题 / 模块 / 优先级 / 版本 / 执行人 / 最近结果），按模块 / 执行人 / 结果筛选；查看快照、移出（有执行记录不可移出） */
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtTime } from '@/utils/format'
import { TgTable, TgBadge, TgPrio, TgSeg, TgDrawer, TgEmpty } from '@/components/ui'
import CaseSteps from './CaseSteps.vue'
const props = defineProps({ plan: { type: Object, required: true }, canManage: Boolean })
const emit = defineEmits(['reload', 'go'])
const router = useRouter(), auth = useAuthStore(), ui = useUiStore()
const module = ref(''), assignee = ref(''), result = ref(''), kw = ref('')
const latest = computed(() => props.plan.runs?.length ? props.plan.runs[props.plan.runs.length - 1] : null)
const results = computed(() => latest.value?.results || {})
const modules = computed(() => [...new Set(props.plan.items.map(i => i.module || ''))].sort())
const assignees = computed(() => [...new Set(props.plan.items.map(i => i.assignee).filter(Boolean))].sort())
const rows = computed(() => props.plan.items.filter(i => {
  const r = results.value[i.item_id]
  if (module.value && (i.module || '') !== module.value) return false
  if (assignee.value === '__none' ? i.assignee : assignee.value && i.assignee !== assignee.value) return false
  if (result.value === 'todo' ? r : result.value === 'mine' ? i.assignee !== auth.me : result.value && r?.status !== result.value) return false
  if (kw.value && !`${i.case_id} ${i.title} ${i.module}`.toLowerCase().includes(kw.value.trim().toLowerCase())) return false
  return true
}))
const cnt = s => props.plan.items.filter(i => s === 'todo' ? !results.value[i.item_id] : s === 'mine' ? i.assignee === auth.me : results.value[i.item_id]?.status === s).length
const segItems = computed(() => [['', '全部', props.plan.items.length], ['mine', '分配给我', cnt('mine')], ['todo', '本轮未执行', cnt('todo')], ['pass', '通过', cnt('pass')], ['fail', '失败', cnt('fail')], ['blocked', '阻塞', cnt('blocked')], ['skipped', '跳过', cnt('skipped')]])
const cols = [{ key: 'case_id', title: '编号 / 版本', w: '190px' }, { key: 'title', title: '标题' }, { key: 'module', title: '模块', w: '170px' }, { key: 'priority', title: '优先级', w: '64px' }, { key: 'assignee', title: '执行人', w: '120px' }, { key: 'result', title: '最近结果', w: '160px' }, { key: 'ops', title: '操作', w: '100px' }]
const viewing = ref(null)
async function remove(i) {
  if (!await ui.confirm('从计划移出？', `「${i.case_id} ${i.title}」将从计划移出（快照删除，不影响正式用例）。`, { ok: '移出', danger: true })) return
  try { await api.del(`/api/v1/plans/${props.plan.plan_id}/cases/${i.item_id}`); ui.toast('已移出'); emit('reload') } catch (e) { ui.error(e) }
}
const hasRecord = i => props.plan.runs.some(r => r.results && r.results[i.item_id])
const assignTitle = i => (i.assign_log || []).map(l => `${l.prev || '—'} → ${l.assignee}（${l.by}${l.note ? '，' + l.note : ''}）`).join('\n')
</script>
<template>
  <div class="filters" style="margin-bottom:10px">
    <TgSeg v-model="result" :items="segItems" />
    <select v-model="module" class="select"><option value="">全部模块</option><option v-for="m in modules" :key="m" :value="m">{{ m || '（无模块）' }}</option></select>
    <select v-model="assignee" class="select"><option value="">执行人：全部</option><option value="__none">未分配</option><option v-for="a in assignees" :key="a" :value="a">{{ a }}</option></select>
    <input v-model="kw" class="field" style="width:180px" placeholder="搜索编号 / 标题" />
    <span class="grow" /><span class="small muted">{{ rows.length }} / {{ plan.items.length }} 条</span>
    <button v-if="canManage" type="button" class="btn small" @click="emit('go', 'candidates')">＋ 加入用例</button>
  </div>
  <TgTable :cols="cols" :rows="rows" row-key="item_id" dense empty="计划内还没有用例" :empty-hint="canManage ? '去「候选池」页签加入已通过的正式用例，或让 AI 挑选。' : ''">
    <template #cell-case_id="{ row }"><span class="mono">{{ row.case_id }}</span><div class="sub">快照 v{{ row.version_no }} · 加入 {{ row.added_by }} {{ fmtTime(row.added_at).slice(0, 10) }}</div></template>
    <template #cell-title="{ row }"><span class="link" style="font-weight:600" @click="viewing = row">{{ row.title }}</span></template>
    <template #cell-module="{ row }"><span class="small">{{ row.module || '—' }}</span></template>
    <template #cell-priority="{ row }"><TgPrio :p="row.priority" /></template>
    <template #cell-assignee="{ row }">
      <template v-if="row.assignee">{{ row.assignee }}<span v-if="(row.assign_log || []).length > 1" class="tag" style="margin-left:4px" :title="assignTitle(row)">改派 {{ row.assign_log.length - 1 }}</span></template>
      <span v-else class="muted">未分配</span>
    </template>
    <template #cell-result="{ row }">
      <template v-if="results[row.item_id]"><TgBadge :status="results[row.item_id].status" /><span v-if="results[row.item_id].history?.length" class="tiny muted"> 复测 {{ results[row.item_id].history.length }}</span><span v-if="results[row.item_id].on_behalf_of" class="tag" style="margin-left:4px">代执行</span><div class="sub">{{ results[row.item_id].by }} · {{ fmtTime(results[row.item_id].at) }}</div></template>
      <span v-else class="muted">{{ latest ? '未执行' : '—' }}</span>
    </template>
    <template #cell-ops="{ row }">
      <span class="link" @click="viewing = row">查看</span>
      <template v-if="canManage"> <span v-if="hasRecord(row)" class="muted tiny" style="margin-left:6px" title="有执行记录不可移出">移出</span><span v-else class="link" style="margin-left:6px" @click="remove(row)">移出</span></template>
    </template>
  </TgTable>
  <TgDrawer :model-value="!!viewing" :title="viewing?.title || ''" :sub="viewing ? `${viewing.case_id} · 快照 v${viewing.version_no} · ${viewing.module}` : ''" @update:modelValue="v => { if (!v) viewing = null }">
    <template v-if="viewing">
      <div class="row" style="margin-bottom:10px"><TgPrio :p="viewing.priority" /><span class="tag">{{ viewing.module || '无模块' }}</span><span class="tiny muted">执行人 {{ viewing.assignee || '未分配' }}</span></div>
      <CaseSteps :snapshot="viewing.snapshot" :result="results[viewing.item_id] || null" :failed-step="results[viewing.item_id]?.triage?.failed_step || 0" />
      <template v-if="results[viewing.item_id]">
        <h4>本轮结果</h4>
        <div class="small"><TgBadge :status="results[viewing.item_id].status" /> {{ results[viewing.item_id].by }} · {{ fmtTime(results[viewing.item_id].at) }}<template v-if="results[viewing.item_id].reason"> · <span class="tag">{{ results[viewing.item_id].reason }}</span></template><div v-if="results[viewing.item_id].note" class="muted">{{ results[viewing.item_id].note }}</div></div>
        <template v-if="results[viewing.item_id].history?.length"><h4>复测历史</h4><div class="list"><div v-for="(h, i) in results[viewing.item_id].history" :key="i"><TgBadge :status="h.status" /> {{ h.by }} · {{ fmtTime(h.at) }}<span v-if="h.reason" class="tag" style="margin-left:4px">{{ h.reason }}</span><span v-if="h.note" class="muted"> · {{ h.note }}</span></div></div></template>
      </template>
      <template v-if="(viewing.assign_log || []).length"><h4>分配留痕</h4><div class="list"><div v-for="(l, i) in viewing.assign_log" :key="i">{{ l.prev || '—' }} → <b>{{ l.assignee }}</b> · 操作 {{ l.by }} · {{ fmtTime(l.at) }}<span v-if="l.note" class="muted"> · {{ l.note }}</span></div></div></template>
    </template>
  </TgDrawer>
</template>
<style scoped>
h4 { margin: 14px 0 6px; font-size: 12.5px; color: #3768ce; }
</style>

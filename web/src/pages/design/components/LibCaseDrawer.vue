<script setup>
/* 用例库右侧抽屉：步骤表、最近执行、所在计划、版本；复制到本任务 / 其他任务。 */
import { ref, computed, watch } from 'vue'
import { api } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtTime } from '@/utils/format'
import { terminalLabel } from '@/utils/terminals'
import { TgDrawer, TgBadge, TgPrio, TgKv, TgField, TgIcon } from '@/components/ui'
import { taskTitle } from './taskUtils'
const props = defineProps({ modelValue: Boolean, row: { type: Object, default: null }, terminal: { type: String, default: '' } })
const emit = defineEmits(['update:modelValue', 'copied'])
const auth = useAuthStore(), ui = useUiStore()
const plans = ref(null), tasks = ref([])
const copyDlg = ref({ open: false, target: '', busy: false })
const canEdit = computed(() => props.row && auth.can(props.row.project, 'case.edit'))
watch(() => props.row?.uid, async () => {
  plans.value = null
  if (!props.row) return
  try { plans.value = (await api.get('/api/v1/plans', { task_id: props.row.task_id })).plans || [] } catch { plans.value = [] }
}, { immediate: true })
async function openCopy() {
  copyDlg.value = { open: true, target: props.row.task_id, busy: false }
  try { tasks.value = ((await api.get('/api/v1/tasks', { project: props.row.project, limit: 500 })).tasks || []).filter(t => t.status === 'completed' && auth.can(t.project, 'case.edit')) } catch { tasks.value = [] }
}
async function doCopy() {
  const d = copyDlg.value, c = props.row
  d.busy = true
  try {
    let r
    if (d.target === c.task_id) r = await api.post(`/api/v1/tasks/${c.task_id}/cases/${c.uid}/copy`, {})
    else r = await api.post(`/api/v1/tasks/${d.target}/cases`, { module: c.module, title: `${c.title}（副本）`, priority: c.priority, precondition: c.precondition || '', steps: c.steps || [], keywords: c.keywords || '', remark: c.remark ? `${c.remark}\n复制自 ${c.case_id}` : `复制自 ${c.case_id}（${c.task_id}）` })
    ui.toast(`已复制为 ${r.case?.case_id}（草稿，在目标任务的用例审核页提交评审）`)
    d.open = false; emit('copied', { task_id: d.target, case: r.case })
  } catch (e) { ui.error(e) } finally { d.busy = false }
}
</script>
<template>
  <TgDrawer :model-value="modelValue" wide @update:modelValue="$emit('update:modelValue', $event)">
    <template #title><span class="mono">{{ row?.case_id }}</span> · {{ row?.title }}</template>
    <template #sub>{{ row?.project }} · {{ row?.module }} · v{{ row?.version || 1 }}<template v-if="terminal"> · {{ terminalLabel(terminal) }}</template></template>
    <template #head-actions>
      <button v-if="canEdit" type="button" class="btn small" :class="{ primary: copyDlg.open }" @click="copyDlg.open ? (copyDlg.open = false) : openCopy()"><TgIcon name="copy" :size="13" /> 复制</button>
      <router-link class="btn small" :to="{ name: 'cases', params: { tid: row?.task_id } }">去所属任务</router-link>
    </template>
    <template v-if="row">
      <div v-if="copyDlg.open" class="source-block" style="margin-bottom:12px">
        <TgField label="复制到" hint="本任务：副本为草稿、来源 copy、标题加「（副本）」；其他任务：以人工新增用例进入目标任务（草稿），在其用例审核页提交评审" required>
          <select v-model="copyDlg.target" class="select">
            <option :value="row.task_id">本任务（{{ row.task_id }}）</option>
            <option v-for="t in tasks.filter(x => x.task_id !== row.task_id)" :key="t.task_id" :value="t.task_id">{{ taskTitle(t) }} · {{ t.task_id }}</option>
          </select>
        </TgField>
        <div class="row"><span class="grow"></span><button type="button" class="btn small" @click="copyDlg.open = false">取消</button><button type="button" class="btn small primary" :disabled="copyDlg.busy || !copyDlg.target" @click="doCopy">{{ copyDlg.busy ? '复制中…' : '复制' }}</button></div>
      </div>
      <div class="row" style="margin-bottom:12px"><TgPrio :p="row.priority" /><TgBadge :status="row.review" :text="row.review === 'approved' ? (row.locked ? '正式 · 锁定' : '已通过') : ''" /><span v-if="row.keywords" class="tag">{{ row.keywords }}</span></div>
      <TgKv :pairs="[['前置条件', row.precondition || '—'], ['备注', row.remark || '—'], ['来源测试点', (row.point_ids || []).join('、') || '—'], ['来源需求', row.requirement_title || '—'], ['创建', `${row.created_by || '—'} · ${fmtTime(row.created_at)}`]]" />
      <table class="case-table">
        <thead><tr><th class="n">#</th><th>操作步骤</th><th>预期结果</th></tr></thead>
        <tbody><tr v-for="(s, i) in row.steps || []" :key="i"><td class="n">{{ i + 1 }}</td><td>{{ s.action }}</td><td>{{ s.expected }}</td></tr></tbody>
      </table>
      <div class="panel-title" style="margin-top:16px">最近执行</div>
      <div v-if="row.exec" class="row small" style="margin-top:6px"><TgBadge :status="row.exec.status" /><span>{{ row.exec.plan_name || row.exec.run || '' }}</span><span class="tiny muted">{{ row.exec.by }} · {{ fmtTime(row.exec.at) }}</span><span v-if="row.exec.note" class="muted">· {{ row.exec.note }}</span><span class="tiny muted">· 共执行 {{ row.exec_count }} 次</span></div>
      <div v-else class="tiny muted" style="margin-top:6px">未执行</div>
      <div class="panel-title" style="margin-top:16px">所在计划</div>
      <div v-if="plans && plans.length" class="list"><div v-for="p in plans" :key="p.plan_id"><router-link :to="{ name: 'plan-detail', params: { id: p.plan_id } }">{{ p.name }}</router-link> <TgBadge :status="p.status" /> <span class="tiny muted">本任务 {{ p.task_cases }} 条快照</span></div></div>
      <div v-else class="tiny muted" style="margin-top:6px">{{ plans ? '所属任务的用例尚未加入任何计划' : '加载中…' }}</div>
      <div class="panel-title" style="margin-top:16px">版本</div>
      <div class="row small" style="margin-top:6px">当前 v{{ row.version || 1 }} <router-link class="btn small" :to="{ name: 'design-detail', params: { id: row.task_id, sub: 'versions' }, query: { kind: 'case', entity: row.uid } }">查看版本链</router-link></div>
      <div class="tiny muted" style="margin-top:14px">所属任务：<router-link :to="{ name: 'design-detail', params: { id: row.task_id } }">{{ row.task_id }}</router-link></div>
    </template>
  </TgDrawer>
</template>

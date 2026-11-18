<script setup>
/* 分配给我：GET /api/v1/plans/my-items（按计划 / 轮次分组），一键进入对应执行面板并定位；可就地打结果 */
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { FAIL_REASONS } from '@/utils/consts'
import { PageHeading, TgPanel, TgTable, TgBadge, TgPrio, TgSeg, TgEmpty, TgDialog, TgField, TgIcon } from '@/components/ui'
const router = useRouter(), scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore()
const tab = ref('todo')
const { data, loading, error, reload } = useLoad(() => api.get('/api/v1/plans/my-items', { include_done: 'true' }))
const all = computed(() => (data.value?.items || []).filter(x => !scope.project || x.project === scope.project))
const rows = computed(() => tab.value === 'all' ? all.value : all.value.filter(x => !x.result))
const groups = computed(() => {
  const g = new Map()
  rows.value.forEach(x => { const k = `${x.plan_id}:${x.run_id || ''}`; if (!g.has(k)) g.set(k, { key: k, plan_id: x.plan_id, plan_name: x.plan_name, project: x.project, plan_status: x.plan_status, run_id: x.run_id, run_name: x.run_name, run_active: x.run_active, items: [] }); g.get(k).items.push(x) })
  return [...g.values()]
})
const cols = [{ key: 'case_id', title: '用例', w: '220px' }, { key: 'priority', title: '优先级', w: '64px' }, { key: 'module', title: '模块' }, { key: 'result', title: '本轮结果', w: '110px' }, { key: 'ops', title: '操作', w: '220px' }]
const goPanel = (g, x) => router.push({ name: 'exec-panel', params: { id: g.plan_id, rid: g.run_id }, query: x ? { sel: x.item_id } : {} })
/* 就地打结果 */
const dlg = ref(null), form = ref({ status: 'fail', reason: '系统缺陷', note: '' }), busy = ref(false)
function quick(g, x, status) {
  if (status === 'pass' || status === 'skipped') return record(g, x, status)
  dlg.value = { g, x }; form.value = { status, reason: '系统缺陷', note: '' }
}
async function record(g, x, status, note = '', reason = '') {
  busy.value = true
  try { await api.post(`/api/v1/plans/${g.plan_id}/runs/${g.run_id}/results`, { items: [{ item_id: x.item_id, status, note, reason }] }); ui.toast('已记录'); dlg.value = null; reload(true) } catch (e) { ui.error(e) } finally { busy.value = false }
}
function confirmDlg() { if (!form.value.note.trim()) return ui.toast('请填写原因 / 缺陷号', 'warn'); record(dlg.value.g, dlg.value.x, form.value.status, form.value.note.trim(), form.value.status === 'fail' ? form.value.reason : '') }
async function newRun(g) {
  if (!await ui.confirm('开始新一轮执行？', `计划「${g.plan_name}」上一轮已结束，将以我（${auth.me}）为执行人开始新一轮。`)) return
  try { const r = await api.post(`/api/v1/plans/${g.plan_id}/runs`, { executor: auth.me }); ui.toast(`轮次「${r.name}」已开始`); router.push({ name: 'exec-panel', params: { id: g.plan_id, rid: r.run_id } }) } catch (e) { ui.error(e) }
}
</script>
<template>
  <PageHeading title="分配给我" :sub="`跨计划列出分配给 ${auth.me} 的用例及本轮结果；可就地打结果，或进入执行面板逐条执行。${scope.project ? `当前只看项目「${scope.project}」。` : ''}`">
    <template #actions><button type="button" class="btn" @click="reload()"><TgIcon name="refresh" :size="14" /> 刷新</button></template>
  </PageHeading>
  <div v-if="error" class="err-box">{{ error.message }}</div>
  <TgPanel :pad="false">
    <div class="filters" style="padding:12px 14px 10px"><TgSeg v-model="tab" :items="[['todo', '待执行', all.filter(x => !x.result).length], ['all', '含已执行', all.length]]" /><span class="grow" /><span class="small muted">{{ rows.length }} 条 · 进行中的轮次在前</span></div>
    <TgEmpty v-if="!groups.length && !loading" title="没有分配给我的待执行用例" hint="负责人在计划「分配」页签分配后会出现在这里；未分配的用例也可在执行面板直接执行认领。" />
    <div v-for="g in groups" :key="g.key" class="grp-block">
      <div class="grp-head">
        <div><b class="link" @click="router.push({ name: 'plan-detail', params: { id: g.plan_id } })">{{ g.plan_name }}</b> <span class="tag">{{ g.project }}</span> <span class="muted small">· {{ g.run_name || '还没有执行轮次' }}</span> <TgBadge v-if="g.run_id" :status="g.run_active ? 'running' : 'finished'" :text="g.run_active ? '进行中' : '已结束'" /><span class="muted small"> · 待执行 {{ g.items.filter(x => !x.result).length }} / {{ g.items.length }}</span></div>
        <div class="row">
          <button v-if="g.run_active" type="button" class="btn primary small" @click="goPanel(g)">进入执行面板</button>
          <button v-else-if="auth.can(g.project, 'exec.run')" type="button" class="btn small" @click="newRun(g)">开始新一轮执行</button>
        </div>
      </div>
      <TgTable :cols="cols" :rows="g.items" row-key="item_id" dense>
        <template #cell-case_id="{ row }"><span class="mono">{{ row.case_id }}</span><div class="sub">{{ row.title }}</div></template>
        <template #cell-priority="{ row }"><TgPrio :p="row.priority" /></template>
        <template #cell-module="{ row }"><span class="small">{{ row.module || '—' }}</span></template>
        <template #cell-result="{ row }"><TgBadge v-if="row.result" :status="row.result.status" /><span v-else class="muted">待执行</span></template>
        <template #cell-ops="{ row }">
          <template v-if="g.run_active">
            <span class="link" @click="goPanel(g, row)">去执行</span>
            <span class="row tiny" style="display:inline-flex;margin-left:8px;gap:6px"><span class="link" @click="quick(g, row, 'pass')">通过</span><span class="link" style="color:#db5361" @click="quick(g, row, 'fail')">失败</span><span class="link" @click="quick(g, row, 'blocked')">阻塞</span><span class="link muted" @click="quick(g, row, 'skipped')">跳过</span></span>
          </template>
          <span v-else class="muted">— 等待新一轮</span>
        </template>
      </TgTable>
    </div>
  </TgPanel>
  <TgDialog :model-value="!!dlg" :title="form.status === 'fail' ? '记录失败' : '记录阻塞'" :ok="form.status === 'fail' ? '记录失败' : '记录阻塞'" danger :busy="busy" @update:modelValue="v => { if (!v) dlg = null }" @ok="confirmDlg">
    <div v-if="dlg" class="small" style="margin-bottom:10px"><span class="mono">{{ dlg.x.case_id }}</span> {{ dlg.x.title }}</div>
    <TgField v-if="form.status === 'fail'" label="失败分类" required><select v-model="form.reason" class="select"><option v-for="r in FAIL_REASONS" :key="r">{{ r }}</option></select></TgField>
    <TgField label="原因 / 缺陷号" required><textarea v-model="form.note" class="field" rows="3" placeholder="实际结果是什么" /></TgField>
  </TgDialog>
</template>
<style scoped>
.grp-block { border-top: 1px solid #edf0f3; }
.grp-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; padding: 10px 14px; background: #f6f8fa; font-size: 12.5px; }
</style>

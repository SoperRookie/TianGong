<script setup>
/* 执行轮次：列表（名称 / 执行人 / 开始 / 结束 / 汇总），开始新一轮 POST /plans/{id}/runs（name / executor / reassign_all），结束 POST runs/{rid}/finish，进入执行面板 */
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtTime, pct } from '@/utils/format'
import { TgBadge, TgDialog, TgField, TgEmpty } from '@/components/ui'
import RunBar from './RunBar.vue'
const props = defineProps({ plan: { type: Object, required: true }, canRun: Boolean, canAssign: Boolean })
const emit = defineEmits(['reload', 'go'])
const router = useRouter(), auth = useAuthStore(), ui = useUiStore()
const base = computed(() => `/api/v1/plans/${props.plan.plan_id}`)
const runs = computed(() => [...(props.plan.runs || [])].reverse())
const active = computed(() => props.plan.runs.find(r => !r.finished_at) || null)
const total = computed(() => props.plan.items.length)
const unassigned = computed(() => props.plan.items.filter(i => !i.assignee).length)
/* 开始新一轮 */
const open = ref(false), busy = ref(false), assignees = ref([])
const form = ref({ name: '', executor: '', reassign_all: false })
async function openNew() {
  if (!total.value) return ui.toast('计划内还没有用例，请先加入用例', 'warn')
  if (active.value) return ui.toast('存在未结束的执行轮次，请先结束', 'warn')
  form.value = { name: `第 ${props.plan.runs.length + 1} 轮`, executor: auth.me, reassign_all: false }
  open.value = true
  if (!assignees.value.length) { try { assignees.value = (await api.get(`${base.value}/assignees`)).assignees || [] } catch { /* 无权限时只给本人 */ } }
}
const executorOptions = computed(() => {
  const list = assignees.value.length ? assignees.value : [{ username: auth.me, name: auth.displayName, role_label: '' }]
  return props.canAssign ? list : list.filter(a => a.username === auth.me)
})
async function create() {
  busy.value = true
  try {
    const r = await api.post(`${base.value}/runs`, { name: form.value.name.trim(), executor: form.value.executor || null, reassign_all: !!form.value.reassign_all })
    ui.toast(`轮次「${r.name}」已开始${r.assigned ? `，${r.assigned} 条用例分配给 ${form.value.executor}` : ''}`)
    open.value = false; emit('reload')
    router.push({ name: 'exec-panel', params: { id: props.plan.plan_id, rid: r.run_id } })
  } catch (e) { ui.error(e) } finally { busy.value = false }
}
async function finish(r) {
  const s = r.summary || {}
  if (!await ui.confirm('结束本轮？', `「${r.name}」已执行 ${s.executed || 0} / ${total.value}，结束后不能再记录结果 / 上传附件 / 采纳归因；本轮全部执行过则计划置为已完成。`, { ok: '结束本轮', danger: true })) return
  try { const out = await api.post(`${base.value}/runs/${r.run_id}/finish`, {}); ui.toast(`已结束，计划状态：${({ not_started: '未开始', in_progress: '进行中', done: '已完成', archived: '已归档' })[out.plan_status] || out.plan_status}`); emit('reload') } catch (e) { ui.error(e) }
}
defineExpose({ openNew })
const goPanel = r => router.push({ name: 'exec-panel', params: { id: props.plan.plan_id, rid: r.run_id } })
const attCount = r => (r.attachments || []).length
</script>
<template>
  <div class="row" style="margin-bottom:10px">
    <span class="small muted">一个计划同时只能有一轮未结束；结束后不能再记录结果 / 上传附件 / 采纳归因，归因与报告仍可做。</span><span class="right" />
    <template v-if="canRun">
      <template v-if="active"><button type="button" class="btn" @click="goPanel(active)">进入执行面板</button><button type="button" class="btn danger" @click="finish(active)">结束本轮</button></template>
      <button v-else type="button" class="btn primary" @click="openNew">＋ 开始新一轮</button>
    </template>
  </div>
  <TgEmpty v-if="!runs.length" title="还没有执行轮次" :hint="total ? '点「开始新一轮」开始执行；本轮执行人可指定并顺带分配未分配的用例。' : '先在「候选池」加入用例。'" />
  <div v-for="(r, idx) in runs" :key="r.run_id" class="panel panel-pad run-card">
    <div class="panel-head">
      <div>
        <div class="panel-title">{{ r.name }} <TgBadge :status="r.finished_at ? 'finished' : 'running'" :text="r.finished_at ? '已结束' : '进行中'" /><span class="tiny muted" style="margin-left:6px">第 {{ runs.length - idx }} 轮 · {{ r.run_id }}</span></div>
        <div class="panel-hint">发起 {{ r.by }} · 执行人 {{ r.executor || '多人（按用例分配）' }} · {{ fmtTime(r.started_at) }}<template v-if="r.finished_at"> → {{ fmtTime(r.finished_at) }}</template></div>
      </div>
      <div class="row">
        <button type="button" class="btn small" @click="emit('go', 'attachments', r.run_id)">附件 ({{ attCount(r) }})</button>
        <button type="button" class="btn small" :disabled="!r.summary?.executed" @click="emit('go', 'report', r.run_id)">{{ r.report ? '查看报告' : '生成报告' }}</button>
        <template v-if="canRun && !r.finished_at"><button type="button" class="btn small primary" @click="goPanel(r)">执行面板</button><button type="button" class="btn small danger" @click="finish(r)">结束</button></template>
        <button v-else-if="r.finished_at" type="button" class="btn small" @click="goPanel(r)">查看记录</button>
      </div>
    </div>
    <div class="grid-4">
      <div class="panel stat"><div class="stat-label">已执行</div><div class="stat-num">{{ r.summary?.executed || 0 }} <span class="muted small">/ {{ total }}</span></div><RunBar :summary="r.summary" :total="total" text /></div>
      <div class="panel stat"><div class="stat-label">通过</div><div class="stat-num ok">{{ r.summary?.pass || 0 }}</div><div class="stat-foot"><span>通过率 {{ r.summary?.pass_rate != null ? (r.summary.pass_rate * 100).toFixed(1) + '%' : '—' }}</span></div></div>
      <div class="panel stat"><div class="stat-label">失败 / 阻塞</div><div class="stat-num"><span style="color:#db5361">{{ r.summary?.fail || 0 }}</span> / {{ r.summary?.blocked || 0 }}</div><div class="stat-foot"><span>失败须分类 + 原因</span></div></div>
      <div class="panel stat"><div class="stat-label">跳过 / 未执行</div><div class="stat-num">{{ r.summary?.skipped || 0 }} / {{ Math.max(0, total - (r.summary?.executed || 0)) }}</div><div class="stat-foot"><span>未执行留痕为「未执行」</span></div></div>
    </div>
    <div v-if="r.report" class="tiny muted" style="margin-top:8px">测试报告：<TgBadge :color="r.report.conclusion === '可发布' ? 'green' : r.report.conclusion === '不可发布' ? 'red' : 'orange'" :text="r.report.conclusion" /> {{ r.report.verdict_basis || '' }}<template v-if="r.report.interim">（阶段性）</template> · {{ r.report.by }} {{ fmtTime(r.report.at) }}</div>
  </div>
  <TgDialog v-model="open" title="开始新一轮执行" ok="开始执行" :busy="busy" @ok="create">
    <TgField label="轮次名称"><input v-model="form.name" class="field" placeholder="默认「第 N 轮执行」" /></TgField>
    <TgField label="本轮执行人" :hint="unassigned ? `计划内 ${unassigned} 条未分配用例将分配给他；不指定则执行时认领` : '计划内用例已全部分配；不指定则按现有分配执行'">
      <select v-model="form.executor" class="select"><option value="">不指定（按现有分配 / 执行时认领）</option><option v-for="a in executorOptions" :key="a.username" :value="a.username">{{ a.name ? `${a.name}（${a.username}）` : a.username }}<template v-if="a.role_label"> · {{ a.role_label }}</template></option></select>
    </TgField>
    <label v-if="canAssign && form.executor" class="row small" style="gap:6px"><input v-model="form.reassign_all" type="checkbox" /> 连同已分配给他人的用例一并改派给该执行人（留痕原执行人）</label>
    <div class="tiny muted" style="margin-top:8px">计划内共 {{ total }} 条用例；开始后进入执行面板逐条执行。</div>
  </TgDialog>
</template>
<style scoped>
.run-card { margin-bottom: 10px; }
.grid-4 .stat { cursor: default; }
</style>

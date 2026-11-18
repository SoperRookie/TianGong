<script setup>
/* ⑥ 任务进度：覆盖识别四格（需求 → 测试点 → 用例 → 计划 → 执行）+ 关联任务的状态 / 进度 / 测试点 / 用例表 */
import { computed } from 'vue'
import { fmtTime, num, pct } from '@/utils/format'
import { TgBadge, TgEmpty, TgStat } from '@/components/ui'
const props = defineProps({ r: { type: Object, required: true }, tasks: { type: Array, default: () => [] } })
const t = computed(() => props.r.trace || { tasks: [], totals: {}, coverage: {} })
const tot = computed(() => t.value.totals || {})
const rows = computed(() => (t.value.tasks || []).map(x => ({ ...x, ...(props.tasks.find(y => y.task_id === x.task_id) || {}) })))
const nodes = computed(() => [
  ['已生成测试点', t.value.coverage?.has_points, tot.value.points ? `${tot.value.points} 条 · 通过 ${tot.value.points_approved}` : '尚未拆解'],
  ['已生成用例', t.value.coverage?.has_cases, tot.value.cases ? `${tot.value.cases} 条 · 正式 ${tot.value.cases_approved}` : '尚未生成'],
  ['已进入计划', t.value.coverage?.in_plan, tot.value.in_plan ? `${tot.value.in_plan} 条入计划` : '未入计划'],
  ['已执行', t.value.coverage?.executed, tot.value.executed ? `${tot.value.executed} 条 · 通过 ${tot.value.exec_pass}` : '未执行'],
])
</script>
<template>
  <div class="panel-head">
    <div><div class="panel-title">⑥ 任务进度</div><div class="panel-hint">需求 → 测试点 → 用例 → 计划 → 执行 的覆盖识别；测试点在「测试点审核」工作台评审，用例在「用例审核」工作台评审，通过即锁定进入用例库。</div></div>
    <router-link v-if="rows.length" class="btn small" :to="{ name: 'designs', query: { req: r.req_id } }">全部设计任务</router-link>
  </div>
  <div class="grid-4" style="margin-bottom:14px">
    <TgStat v-for="[label, ok, sub] in nodes" :key="label" :label="label" :value="ok ? '✓' : '—'" :foot="sub" :color="ok ? 'green' : 'blue'" :icon="ok ? 'check' : 'clock'" />
  </div>
  <table v-if="rows.length" class="table">
    <thead><tr><th style="width:110px">状态</th><th style="width:120px">任务</th><th>进度</th><th>测试点</th><th>用例</th><th>入计划</th><th>已执行</th><th style="width:90px">创建人</th><th style="width:130px">创建时间</th><th style="width:170px"></th></tr></thead>
    <tbody>
      <tr v-for="x in rows" :key="x.task_id">
        <td><TgBadge :status="x.status" /><div v-if="x.error" class="tiny" style="color:var(--red)">{{ x.error }}</div></td>
        <td class="mono"><router-link class="link" :to="{ name: 'design-detail', params: { id: x.task_id } }">{{ x.task_id.slice(0, 12) }}</router-link></td>
        <td><template v-if="x.progress != null"><span class="minibar"><span :style="{ width: Math.min(100, Math.round((x.progress <= 1 ? x.progress * 100 : x.progress))) + '%' }" /></span> <span class="tiny muted">{{ Math.round(x.progress <= 1 ? x.progress * 100 : x.progress) }}%</span></template><span v-else class="muted">—</span></td>
        <td>{{ x.points ? `${num(x.points)}` : '—' }}<span v-if="x.points" class="tiny muted"> 通过 {{ x.points_approved }}（{{ pct(x.points_approved, x.points) }}）</span></td>
        <td>{{ x.cases ? num(x.cases) : '—' }}<span v-if="x.cases" class="tiny muted"> 正式 {{ x.cases_approved }}</span></td>
        <td>{{ x.in_plan || '—' }}</td>
        <td>{{ x.executed ? num(x.executed) : '—' }}<span v-if="x.executed" class="tiny muted"> 通过 {{ x.exec_pass }}</span></td>
        <td class="tiny muted">{{ x.created_by || '' }}</td>
        <td class="tiny muted">{{ fmtTime(x.created_at) }}</td>
        <td class="nowrap"><router-link class="link" :to="{ name: 'points', params: { tid: x.task_id } }">测试点</router-link> <router-link class="link" style="margin-left:8px" :to="{ name: 'cases', params: { tid: x.task_id } }">用例</router-link> <router-link class="link" style="margin-left:8px" :to="{ name: 'design-detail', params: { id: x.task_id } }">概览</router-link></td>
      </tr>
    </tbody>
  </table>
  <TgEmpty v-else title="尚未发起测试设计" hint="在第 ⑤ 步发起后，这里显示任务状态、测试点与用例的审核进度" />
</template>

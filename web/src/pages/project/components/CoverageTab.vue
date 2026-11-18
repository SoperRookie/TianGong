<script setup>
/* 覆盖追溯（3.16）：GET /projects/{name}/coverage —— 每条需求的测试点 → 用例 → 计划 → 执行覆盖，及未挂需求的任务。 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { api, seg } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { fmtAgo } from '@/utils/format'
import { TgPanel, TgTable, TgBadge, TgEmpty } from '@/components/ui'
const props = defineProps({ project: { type: String, required: true } })
const router = useRouter()
const { data, loading, error } = useLoad(() => api.get(`/api/v1/projects/${seg(props.project)}/coverage`), { deps: [() => props.project] })
const s = computed(() => data.value?.summary || {})
const p = v => s.value.requirements ? Math.round(v / s.value.requirements * 100) + '%' : '0%'
const stages = computed(() => [['需求', s.value.requirements, '需求中心'], ['已生成测试点', s.value.with_points, p(s.value.with_points)], ['已生成用例', s.value.with_cases, p(s.value.with_cases)], ['已入计划', s.value.in_plan, p(s.value.in_plan)], ['已执行', s.value.executed, p(s.value.executed)]])
const cols = [{ key: 'title', title: '需求' }, { key: 'status', title: '状态', w: '80px' }, { key: 'tasks', title: '任务', w: '60px' }, { key: 'points', title: '测试点', w: '110px' }, { key: 'cases', title: '用例', w: '110px' }, { key: 'in_plan', title: '入计划', w: '70px' }, { key: 'executed', title: '执行', w: '100px' }, { key: 'cov', title: '覆盖识别', w: '220px' }]
const tcols = [{ key: 'task_id', title: '任务', w: '130px' }, { key: 'sources', title: '来源' }, { key: 'status', title: '状态', w: '100px' }, { key: 'created_at', title: '创建', w: '100px' }]
const COV = [['has_points', '测试点'], ['has_cases', '用例'], ['in_plan', '计划'], ['executed', '执行']]
</script>
<template>
  <div v-if="error" class="err-box mb">{{ error.message }}</div>
  <TgPanel title="覆盖管道" hint="每条需求的测试点 → 用例 → 计划 → 执行；百分比为占需求数比例">
    <div class="pipe">
      <div v-for="([lab, n, sub], i) in stages" :key="lab" class="stage" :class="{ ok: i > 0 && n }"><div class="lab">{{ lab }}</div><div class="n">{{ n ?? 0 }}</div><div class="sub">{{ sub }}</div></div>
    </div>
  </TgPanel>
  <TgPanel class="mt" title="需求覆盖" :hint="`${(data?.requirements || []).length} 条 · 点击行进入需求的追溯页签`" :pad="false">
    <TgTable :cols="cols" :rows="data?.requirements || []" row-key="req_id" :loading="loading" clickable empty="项目暂无需求" @row-click="r => router.push({ name: 'req-detail', params: { id: r.req_id, step: 5 } })">
      <template #cell-title="{ row }"><b>{{ row.title }}</b><div class="sub mono">{{ row.req_id }}</div></template>
      <template #cell-status="{ row }"><TgBadge :status="row.status" /></template>
      <template #cell-points="{ row }"><template v-if="row.points">{{ row.points }} <span class="muted tiny">通过 {{ row.points_approved }}</span></template><span v-else class="muted">—</span></template>
      <template #cell-cases="{ row }"><template v-if="row.cases">{{ row.cases }} <span class="muted tiny">正式 {{ row.cases_approved }}</span></template><span v-else class="muted">—</span></template>
      <template #cell-in_plan="{ row }">{{ row.in_plan || '—' }}</template>
      <template #cell-executed="{ row }"><template v-if="row.executed">{{ row.executed }} <span class="muted tiny">通过 {{ row.exec_pass }}</span></template><span v-else class="muted">—</span></template>
      <template #cell-cov="{ row }"><span class="row" style="gap:4px"><template v-for="([k, l], i) in COV" :key="k"><span v-if="i" class="muted">→</span><TgBadge :status="row.coverage?.[k] ? 'approved' : 'untested'" :text="l" /></template></span></template>
    </TgTable>
  </TgPanel>
  <TgPanel v-if="(data?.unlinked_tasks || []).length" class="mt" title="未挂需求的任务" :hint="`${data.unlinked_tasks.length} 个 · 项目化之前或直接创建的任务；追溯从需求开始，建议在需求中心录入后重新发起`" :pad="false">
    <TgTable :cols="tcols" :rows="data.unlinked_tasks" row-key="task_id" clickable @row-click="t => router.push({ name: 'design-detail', params: { id: t.task_id } })">
      <template #cell-task_id="{ row }"><span class="mono">{{ row.task_id }}</span></template>
      <template #cell-sources="{ row }">{{ (row.sources || []).join('、') || '—' }}</template>
      <template #cell-status="{ row }"><TgBadge :status="row.status" /></template>
      <template #cell-created_at="{ row }"><span class="muted">{{ fmtAgo(row.created_at) }}</span></template>
    </TgTable>
  </TgPanel>
  <TgEmpty v-if="!loading && data && !data.requirements.length && !data.unlinked_tasks.length" title="项目暂无需求与任务" />
</template>
<style scoped>
.pipe { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; }
.stage { border: 1px solid #e7edf7; border-radius: 3px; padding: 10px 10px 8px; background: #fafcff; position: relative; }
.stage:not(:last-child):after { content: '›'; position: absolute; right: -8px; top: 36%; color: #b9c8e8; font-size: 16px; }
.stage .lab { font-size: 11.5px; color: #8390a6; } .stage .n { font-size: 22px; font-weight: 780; margin: 2px 0; color: #1d2a44; } .stage .sub { font-size: 10.5px; color: #97a2b3; }
.stage.ok { border-color: #bbe9db; background: #f4fffa; } .stage.ok .n { color: var(--green); }
</style>

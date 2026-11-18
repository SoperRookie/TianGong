<script setup>
/* 没有 tid 时：列出当前工作范围内可审核的任务让人选（测试点：待确认测试点；用例：已完成且有用例）。 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useScopeStore } from '@/stores/scope'
import { fmtTime } from '@/utils/format'
import { PageHeading, TgPanel, TgTable, TgBadge } from '@/components/ui'
const props = defineProps({ kind: { type: String, default: 'point' } })
const router = useRouter(), scope = useScopeStore()
const { data, loading, error } = useLoad(() => api.get('/api/v1/tasks', { ...scope.query, limit: 200 }), { deps: [() => scope.project, () => scope.line] })
const rows = computed(() => (data.value?.tasks || []).filter(t => props.kind === 'point'
  ? (t.status === 'awaiting_confirmation' || (t.point_status && (t.point_status.pending || t.points)))
  : (t.status === 'completed' && (t.cases || (t.case_status && Object.values(t.case_status).some(Boolean))))))
const title = t => t.requirement_title || (t.sources || []).join('、') || t.task_id
const cols = computed(() => props.kind === 'point'
  ? [{ key: 'title', title: '任务' }, { key: 'project', title: '项目', w: '160px' }, { key: 'status', title: '状态', w: '120px' }, { key: 'points', title: '测试点', w: '90px', align: 'right' }, { key: 'pending', title: '待审', w: '80px', align: 'right' }, { key: 'created_at', title: '创建', w: '140px' }]
  : [{ key: 'title', title: '任务' }, { key: 'project', title: '项目', w: '160px' }, { key: 'status', title: '状态', w: '120px' }, { key: 'cases', title: '用例', w: '90px', align: 'right' }, { key: 'pending', title: '待审', w: '80px', align: 'right' }, { key: 'created_at', title: '创建', w: '140px' }])
const pending = t => props.kind === 'point' ? (t.point_status?.pending ?? '—') : (t.case_status?.pending ?? '—')
</script>
<template>
  <PageHeading :title="kind === 'point' ? '测试点审核' : '用例审核'" :sub="`选择一个${kind === 'point' ? '有待确认测试点' : '有用例'}的任务 · 当前范围：${scope.label}`" />
  <div v-if="error" class="err-box">{{ error.message }}</div>
  <TgPanel :pad="false">
    <TgTable :cols="cols" :rows="rows" row-key="task_id" :loading="loading" clickable :empty="kind === 'point' ? '当前范围内没有待确认测试点的任务' : '当前范围内没有可审核用例的任务'" empty-hint="到「测试设计 › 设计任务」发起或查看任务" @row-click="t => router.push({ name: kind === 'point' ? 'points' : 'cases', params: { tid: t.task_id } })">
      <template #cell-title="{ row }"><b>{{ title(row) }}</b><div class="tiny muted mono">{{ row.task_id }}</div></template>
      <template #cell-status="{ row }"><TgBadge :status="row.status" /></template>
      <template #cell-points="{ row }">{{ row.points ?? '—' }}</template>
      <template #cell-cases="{ row }">{{ row.cases ?? '—' }}</template>
      <template #cell-pending="{ row }">{{ pending(row) }}</template>
      <template #cell-created_at="{ row }">{{ fmtTime(row.created_at) }}</template>
    </TgTable>
  </TgPanel>
</template>

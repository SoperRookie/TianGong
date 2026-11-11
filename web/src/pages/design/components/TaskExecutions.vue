<script setup>
/* 旧任务级执行记录（7.16，只读）：M4 起执行统一挂测试计划；exec_migrated_to 指向迁移后的计划。 */
import { computed } from 'vue'
import { fmtTime, pct } from '@/utils/format'
import { TgPanel, TgBadge, TgTable, TgEmpty, TgIcon } from '@/components/ui'
const props = defineProps({ task: { type: Object, required: true } })
const runs = computed(() => (props.task.executions || []).slice().reverse())
const summary = r => { const c = { pass: 0, fail: 0, blocked: 0, skipped: 0 }; for (const x of Object.values(r.results || {})) if (c[x.status] != null) c[x.status]++; const executed = c.pass + c.fail + c.blocked + c.skipped; return { ...c, executed } }
</script>
<template>
  <div v-if="task.exec_migrated_to" class="information mt"><TgIcon name="info" :size="13" /> <span>该任务的执行已迁移到测试计划 <router-link :to="{ name: 'plan-detail', params: { id: task.exec_migrated_to } }">{{ task.exec_migrated_to }}</router-link>，请在测试计划中执行；下面是迁移前的历史记录（只读）。</span></div>
  <TgEmpty v-if="!runs.length" class="mt" title="没有任务级执行记录" hint="执行统一在「测试计划」里进行。" />
  <TgPanel v-for="r in runs" :key="r.run_id" class="mt" :title="r.name" :hint="`${r.by} · ${fmtTime(r.started_at)} → ${r.finished_at ? fmtTime(r.finished_at) : '未结束'}`">
    <template #actions><span class="tiny muted">通过 {{ summary(r).pass }} · 失败 {{ summary(r).fail }} · 阻塞 {{ summary(r).blocked }} · 跳过 {{ summary(r).skipped }} · 已执行 {{ summary(r).executed }} / {{ ((task.result || {}).cases || []).length }} · 通过率 {{ pct(summary(r).pass, summary(r).executed, 1) }}</span></template>
    <TgTable :cols="[{ key: 'case_id', title: '编号', w: '160px' }, { key: 'title', title: '标题' }, { key: 'status', title: '结果', w: '80px' }, { key: 'note', title: '备注' }, { key: 'by', title: '执行人', w: '90px' }, { key: 'at', title: '时间', w: '130px' }, { key: 'history', title: '复测', w: '60px', align: 'right' }]" :rows="Object.values(r.results || {})" row-key="case_id" dense empty="本轮没有记录结果">
      <template #cell-case_id="{ row }"><span class="mono">{{ row.case_id }}</span></template>
      <template #cell-status="{ row }"><TgBadge :status="row.status" /></template>
      <template #cell-at="{ row }">{{ fmtTime(row.at) }}</template>
      <template #cell-history="{ row }">{{ (row.history || []).length || '—' }}</template>
    </TgTable>
  </TgPanel>
</template>

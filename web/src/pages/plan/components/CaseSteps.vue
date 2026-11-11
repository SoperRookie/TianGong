<script setup>
/* 用例快照卡：前置 + 步骤 / 预期三列表 + 关键词 / 备注；result 为本轮结果时在「逐步结果」列标记 */
import { computed } from 'vue'
const props = defineProps({ snapshot: { type: Object, default: () => ({}) }, result: { type: Object, default: null }, failedStep: { type: Number, default: 0 } })
const steps = computed(() => (props.snapshot?.steps || []).map((s, i) => typeof s === 'string' ? { action: s, expected: '' } : { action: s?.action || '', expected: s?.expected || '' }))
const mark = i => {
  const st = props.result?.status
  if (!st) return { cls: 'todo', text: '—' }
  if (st === 'pass') return { cls: 'pass', text: '✓' }
  if (st === 'fail') return props.failedStep ? (i + 1 === props.failedStep ? { cls: 'fail', text: '✗' } : i + 1 < props.failedStep ? { cls: 'pass', text: '✓' } : { cls: 'todo', text: '—' }) : { cls: 'fail', text: '✗' }
  if (st === 'blocked') return { cls: 'block', text: '阻塞' }
  return { cls: 'skip', text: '跳过' }
}
</script>
<template>
  <dl class="kv"><dt>前置</dt><dd>{{ snapshot?.precondition || '—' }}</dd></dl>
  <table class="case-table"><thead><tr><th style="width:34px">#</th><th>操作步骤</th><th>预期结果</th><th style="width:70px">逐步结果</th></tr></thead>
    <tbody>
      <tr v-for="(s, i) in steps" :key="i"><td class="n">{{ i + 1 }}</td><td>{{ s.action }}</td><td>{{ s.expected }}</td><td><span class="r" :class="mark(i).cls">{{ mark(i).text }}</span></td></tr>
      <tr v-if="!steps.length"><td colspan="4" class="muted">（快照没有步骤）</td></tr>
    </tbody>
  </table>
  <div v-if="snapshot?.expected" class="tiny" style="margin-top:6px"><b>总体预期：</b>{{ snapshot.expected }}</div>
  <div class="tiny muted" style="margin-top:6px">关键词：{{ snapshot?.keywords || '—' }}<template v-if="snapshot?.remark"> · 备注：{{ snapshot.remark }}</template> · 来源测试点 {{ (snapshot?.point_ids || []).join(' ') || '—' }}</div>
</template>
<style scoped>
.r { font-size: 11px; font-weight: 700; min-width: 30px; display: inline-block; text-align: center; border-radius: 3px; padding: 2px 4px; }
</style>

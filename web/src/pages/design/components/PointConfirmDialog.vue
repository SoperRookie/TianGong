<script setup>
/* 确认生成用例：按端或全部端生成；只有待审清零且有已通过测试点的范围可选（5.12 / 5.21 按端确认生成）。 */
import { ref, computed, watch } from 'vue'
import { TgDialog } from '@/components/ui'
const props = defineProps({ modelValue: Boolean, task: { type: Object, default: null }, busy: Boolean })
const emit = defineEmits(['update:modelValue', 'confirm'])
const terminal = ref('')
const completed = computed(() => props.task?.status === 'completed')
const rows = computed(() => {
  const ts = props.task?.point_terminals || []
  const sum = k => ts.reduce((s, t) => s + (t[k] || 0), 0)
  const all = { key: '', label: '全部端', total: sum('total'), pending: sum('pending'), approved: sum('approved'), rejected: sum('rejected'), generated: sum('generated'), pending_generation: sum('pending_generation') }
  return [all, ...ts].map(t => ({ ...t, ok: completed.value ? t.pending_generation > 0 : (t.pending === 0 && t.approved > 0) }))
})
watch(() => props.modelValue, v => { if (v) { const first = rows.value.find(r => r.ok); terminal.value = first ? first.key : '' } })
const cur = computed(() => rows.value.find(r => r.key === terminal.value))
</script>
<template>
  <TgDialog :model-value="modelValue" :title="completed ? '为新通过的测试点生成用例' : '确认生成用例'" :ok="completed ? '增量生成' : '开始生成'" :busy="busy" :ok-disabled="!cur || !cur.ok" @update:modelValue="v => emit('update:modelValue', v)" @ok="emit('confirm', terminal)">
    <div class="small">以已通过的测试点为准，按模块并行生成；可只为一个端生成（各端审完各自生成、各自评审），其余端之后再确认。{{ completed ? '已完成任务上再次确认 = 增量生成并入现有用例。' : '' }}</div>
    <table class="case-table cf-table">
      <thead><tr><th></th><th>范围</th><th>测试点</th><th>待审</th><th>已通过</th><th>已驳回</th><th>已生成</th><th>待生成</th></tr></thead>
      <tbody>
        <tr v-for="r in rows" :key="r.key" :class="{ off: !r.ok }" @click="r.ok && (terminal = r.key)">
          <td><input type="radio" :value="r.key" v-model="terminal" :disabled="!r.ok" /></td>
          <td><b>{{ r.label }}</b></td><td>{{ r.total }}</td><td :class="{ warn: r.pending }">{{ r.pending }}</td><td>{{ r.approved }}</td><td>{{ r.rejected }}</td><td>{{ r.generated }}</td><td>{{ r.pending_generation }}</td>
        </tr>
      </tbody>
    </table>
    <div class="tiny muted cf-hint">{{ completed ? '只有「待生成」> 0 的范围可选。' : '待审没清零或没有已通过测试点的范围不可选；被驳回的测试点永不进入生成。' }}</div>
  </TgDialog>
</template>
<style scoped>
.cf-table { margin-top: 10px; }
.cf-table tr { cursor: pointer; }
.cf-table tr.off { color: #aab4c4; cursor: not-allowed; }
.cf-table td.warn { color: var(--amber); font-weight: 700; }
.cf-hint { margin-top: 8px; }
</style>

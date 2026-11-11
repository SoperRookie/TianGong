<script setup>
/* ⑤ 发起测试设计：已有设计记录列表（链接到 design-detail / points / cases）+ 发起对话框 */
import { ref, computed } from 'vue'
import { fmtTime, num } from '@/utils/format'
import { TgBadge, TgEmpty, TgIcon } from '@/components/ui'
import DesignDialog from './DesignDialog.vue'
const props = defineProps({ r: { type: Object, required: true }, canDesign: Boolean, tasks: { type: Array, default: () => [] } })
const emit = defineEmits(['reload', 'go'])
const open = ref(false)
const must = computed(() => props.r.blocking_questions || 0)
const blocked = computed(() => must.value ? `还有 ${must.value} 项必须确认的事项` : props.r.status === 'archived' ? '需求已归档' : props.r.parsing ? '附件仍在解析中' : '')
const rows = computed(() => (props.r.trace?.tasks || []).map(t => ({ ...t, ...(props.tasks.find(x => x.task_id === t.task_id) || {}) })).sort((a, b) => (b.created_at || '').localeCompare(a.created_at || '')))
</script>
<template>
  <div class="panel-head">
    <div><div class="panel-title">⑤ 发起测试设计</div><div class="panel-hint">一条需求发起一次设计就是一条记录；多需求合并设计在需求列表勾选后从底部批量条发起。设计使用整理稿 + 人工补充 + 待确认事项的确认结论。</div></div>
    <div class="row">
      <span v-if="blocked" class="tiny" style="color:var(--amber)">{{ blocked }}</span>
      <button v-if="canDesign" type="button" class="btn small primary" :disabled="!!blocked" @click="open = true">{{ rows.length ? '再次发起测试设计' : '发起测试设计' }}</button>
    </div>
  </div>
  <div v-if="must" class="attention" style="margin-bottom:10px"><TgIcon name="alert" :size="14" /> 还有 <b>{{ must }} 项「必须确认」</b>未处理，发起测试设计前需逐条确认（也可把不影响规则的事项降为建议）。<button type="button" class="btn small" @click="emit('go', 4)">去确认</button></div>
  <template v-if="rows.length">
    <div v-for="(t, i) in rows" :key="t.task_id" class="todo-card">
      <div class="ic stat-icon" :class="t.status === 'completed' ? 'green' : t.status === 'failed' ? 'orange' : 'blue'"><TgIcon :name="t.status === 'completed' ? 'check' : 'spark'" :size="16" /></div>
      <div class="t">
        <strong>第 {{ rows.length - i }} 次设计 · {{ fmtTime(t.created_at) }}<template v-if="(t.requirement_ids || []).length > 1"> · 合并设计（{{ t.requirement_ids.length }} 条需求）</template></strong>
        <small><TgBadge :status="t.status" /> {{ num(t.points) }} 测试点（通过 {{ num(t.points_approved) }}）· {{ num(t.cases) }} 用例（正式 {{ num(t.cases_approved) }}）· 入计划 {{ num(t.in_plan) }} · 已执行 {{ num(t.executed) }}<template v-if="t.created_by"> · {{ t.created_by }}</template><span v-if="t.error" style="color:var(--red)"> · {{ t.error }}</span></small>
      </div>
      <div class="row">
        <router-link class="btn small" :to="{ name: 'design-detail', params: { id: t.task_id } }">概览</router-link>
        <router-link class="btn small" :to="{ name: 'points', params: { tid: t.task_id } }">测试点</router-link>
        <router-link class="btn small primary" :class="{ disabled: !t.cases }" :to="{ name: 'cases', params: { tid: t.task_id } }">用例</router-link>
      </div>
    </div>
  </template>
  <TgEmpty v-else title="尚未发起测试设计" :hint="must ? '必须级待确认事项清零后可发起' : (r.analysis ? '点右上「发起测试设计」创建任务' : '建议先完成 AI 分析与待确认事项，再发起测试设计')" />
  <DesignDialog v-model="open" :reqs="[r]" @done="emit('reload')" />
</template>

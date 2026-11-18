<script setup>
/* 产出趋势柱图（纯 CSS）：trend [{date, tasks, cases}]，按日展示生成用例数；悬停显示日期与数值。 */
import { computed } from 'vue'
const props = defineProps({ trend: { type: Array, default: () => [] } })
const max = computed(() => Math.max(1, ...props.trend.map(d => d.cases || 0)))
const label = (d, i) => (props.trend.length <= 12 || i % Math.ceil(props.trend.length / 12) === 0) ? d.date.slice(5) : ''
</script>
<template>
  <div v-if="!trend.length" class="muted small">暂无数据</div>
  <template v-else>
    <div class="row mb" style="justify-content:space-between"><span class="muted tiny">{{ trend[0].date }} → {{ trend[trend.length - 1].date }}</span><span class="muted tiny">峰值 {{ max }} 条 / 日</span></div>
    <div class="chart"><div v-for="(d, i) in trend" :key="d.date" :style="{ height: Math.max(d.cases ? 3 : 0, Math.round((d.cases || 0) / max * 100)) + '%' }" :title="`${d.date} · ${d.cases} 条用例 / ${d.tasks} 个任务`"><i>{{ label(d, i) }}</i></div></div>
    <div style="height:20px" />
  </template>
</template>
<style scoped>
.chart div { max-width: 48px; }
</style>

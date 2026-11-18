<script setup>
/* 横向条形列表：rows 对象数组，labelKey / valueKey 取字段，unit 单位；按最大值归一。 */
import { computed } from 'vue'
const props = defineProps({ rows: { type: Array, default: () => [] }, labelKey: { type: String, required: true }, valueKey: { type: String, required: true }, unit: { type: String, default: '' }, max: { type: Number, default: 0 } })
const top = computed(() => Math.max(1, props.max, ...props.rows.map(r => r[props.valueKey] || 0)))
</script>
<template>
  <div v-if="!rows.length" class="muted small">暂无数据</div>
  <div v-for="r in rows" :key="r[labelKey]" class="hrow">
    <span class="lab ellipsis" :title="r[labelKey]">{{ r[labelKey] }}</span>
    <span class="track"><span class="fill" :style="{ width: Math.round((r[valueKey] || 0) / top * 100) + '%' }" :title="`${r[labelKey]} · ${r[valueKey]} ${unit}`" /></span>
    <b class="val">{{ r[valueKey] }} {{ unit }}</b>
  </div>
</template>
<style scoped>
.hrow { display: flex; align-items: center; gap: 10px; margin: 5px 0; font-size: 12.5px; }
.lab { width: 140px; flex: none; }
.track { flex: 1; height: 12px; background: var(--line-2); border-radius: 6px; overflow: hidden; }
.fill { display: block; height: 100%; background: var(--blue); border-radius: 6px; }
.val { width: 70px; flex: none; text-align: right; }
</style>

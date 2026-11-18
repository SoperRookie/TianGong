<script setup>
/* 分段条 + 图例：entries [[label, count, colorKey]]；colorKey ∈ green / orange / red / blue / purple / gray / p0–p3；颜色不单独承载身份，图例直接写数字。 */
import { computed } from 'vue'
const props = defineProps({ entries: { type: Array, default: () => [] } })
const total = computed(() => props.entries.reduce((s, e) => s + (e[1] || 0), 0))
</script>
<template>
  <div v-if="!total" class="muted small">暂无数据</div>
  <template v-else>
    <div class="segbar"><i v-for="e in entries.filter(x => x[1])" :key="e[0]" :class="e[2]" :style="{ flex: e[1] }" :title="`${e[0]} · ${e[1]}`" /></div>
    <div class="leg"><span v-for="e in entries" :key="e[0]"><i :class="e[2]" />{{ e[0] }} <b>{{ e[1] || 0 }}</b></span></div>
  </template>
</template>
<style scoped>
.segbar { display: flex; height: 10px; border-radius: 5px; overflow: hidden; background: #edf1f9; }
.segbar i { display: block; height: 100%; }
.leg { display: flex; flex-wrap: wrap; gap: 4px 14px; margin-top: 6px; font-size: 12px; color: var(--text-2); }
.leg i { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 5px; vertical-align: -1px; }
.green { background: var(--green); } .orange { background: var(--amber); } .red { background: var(--red); } .blue { background: var(--blue); } .purple { background: var(--purple); } .gray { background: #b7c0cf; }
.p0 { background: #29418f; } .p1 { background: #4b69c6; } .p2 { background: #7d97e0; } .p3 { background: #a2b4e6; }
</style>

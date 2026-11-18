<script setup>
/* 执行进度条：通过 / 失败 / 阻塞 / 跳过 四色分段，按计划用例总数占比；mini 为表格内窄条 */
import { computed } from 'vue'
const props = defineProps({ summary: { type: Object, default: null }, total: { type: Number, default: 0 }, mini: Boolean, text: Boolean })
const s = computed(() => props.summary || {})
const total = computed(() => props.total || s.value.total || 0)
const w = k => total.value ? (100 * (s.value[k] || 0) / total.value) + '%' : '0%'
const title = computed(() => `通过 ${s.value.pass || 0} · 失败 ${s.value.fail || 0} · 阻塞 ${s.value.blocked || 0} · 跳过 ${s.value.skipped || 0} · 未执行 ${Math.max(0, total.value - (s.value.executed || 0))}`)
</script>
<template>
  <div class="runbar-wrap" :title="title">
    <div class="bar runbar" :class="{ mini }">
      <span class="pass" :style="{ width: w('pass') }" /><span class="fail" :style="{ width: w('fail') }" /><span class="blocked" :style="{ width: w('blocked') }" /><span class="skipped" :style="{ width: w('skipped') }" />
    </div>
    <div v-if="text" class="tiny muted legend"><i class="pass" />{{ s.pass || 0 }} <i class="fail" />{{ s.fail || 0 }} <i class="blocked" />{{ s.blocked || 0 }} <i class="skipped" />{{ s.skipped || 0 }} <span class="muted">/ {{ total }}</span></div>
  </div>
</template>
<style scoped>
.runbar-wrap { min-width: 0; }
.runbar { display: flex; }
.runbar.mini { width: 110px; height: 6px; display: inline-flex; vertical-align: middle; }
.runbar span { border-radius: 0; background: none; transition: width .2s; }
.runbar span.pass, .legend i.pass { background: #179b75; }
.runbar span.fail, .legend i.fail { background: #db5361; }
.runbar span.blocked, .legend i.blocked { background: #f0a03d; }
.runbar span.skipped, .legend i.skipped { background: #b4bdcc; }
.legend { margin-top: 4px; white-space: nowrap; display: flex; gap: 4px; align-items: center; }
.legend i { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-left: 4px; }
.legend i:first-child { margin-left: 0; }
</style>

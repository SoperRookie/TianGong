<script setup>
/* 人工 vs AI 测试点对比：粘贴多行人工测试点 → 批量录入（manual-batch）→ 语义逐条匹配（compare），显示覆盖率与各自独有项。 */
import { ref, computed } from 'vue'
import { fmtTime } from '@/utils/format'
import { TgEmpty, TgField } from '@/components/ui'
const props = defineProps({ report: { type: Object, default: null }, hasManual: Boolean, canEdit: Boolean, canAi: Boolean, busy: Boolean })
const emit = defineEmits(['import', 'compare'])
const text = ref(''), module = ref('')
const st = computed(() => props.report?.stats || {})
const pct = v => v == null ? '—' : (v * 100).toFixed(0) + '%'
function go(compare) {
  const t = text.value.trim()
  if (t) emit('import', { text: t, module: module.value.trim(), thenCompare: compare })
  else if (compare) emit('compare')
  text.value = ''
}
const vColor = v => v === '一致' ? 'green' : v === '部分覆盖' ? 'orange' : 'gray'
</script>
<template>
  <div class="panel panel-pad">
    <div class="panel-title">人工 vs AI 测试点对比</div>
    <div class="information cp-info">人工测试点（来源「人工」）与 AI 测试点逐条语义匹配，给出一致 / 部分覆盖 / AI 遗漏 / AI 额外发现与覆盖率。先粘贴人工编写的测试点录入，再对比。</div>
    <div class="cp-form">
      <TgField label="人工测试点（每行一条，可写成「模块|测试点」）"><textarea v-model="text" class="field" rows="5" placeholder="H5 活动页-签到|当日已签到后再次签到提示今日已签到&#10;连续签到 7 天赠送 100 金币" /></TgField>
      <div class="row">
        <input v-model="module" class="field cp-mod" placeholder="未写模块时的默认模块（空则「未分组」）" />
        <button type="button" class="btn" :disabled="busy || !canEdit || !text.trim()" @click="go(false)">只录入</button>
        <button type="button" class="btn primary" :disabled="busy || !canAi || (!text.trim() && !hasManual)" @click="go(true)">{{ text.trim() ? '录入并对比' : '重新对比' }}</button>
        <span class="tiny muted">录入需 point.edit，对比需 point.ai；录入后旧对比报告作废</span>
      </div>
    </div>
    <template v-if="report">
      <div class="row tiny muted cp-meta">{{ fmtTime(report.at) }} · {{ report.by }}<span v-if="report.model_name"> · {{ report.model_name }}</span><b v-if="report.degraded" class="cp-warn">LLM 复核失败，为文字相似配对，请人工判断</b></div>
      <div class="stat-grid cp-stats">
        <div class="panel stat"><div class="stat-label">人工测试点</div><div class="stat-num">{{ st.manual_total }}</div></div>
        <div class="panel stat"><div class="stat-label">AI 测试点</div><div class="stat-num">{{ st.ai_total }}</div></div>
        <div class="panel stat"><div class="stat-label">AI 覆盖人工比例</div><div class="stat-num cp-ok">{{ pct(st.ai_recall) }}</div><div class="stat-foot">一致 {{ st.exact }} · 部分 {{ st.partial }}</div></div>
        <div class="panel stat"><div class="stat-label">AI 遗漏（人工独有）</div><div class="stat-num cp-bad">{{ st.manual_only }}</div></div>
        <div class="panel stat"><div class="stat-label">AI 额外发现</div><div class="stat-num">{{ st.ai_only }}</div><div class="stat-foot">AI 命中人工 {{ pct(st.ai_precision) }}</div></div>
      </div>
      <template v-if="(report.matched || []).length">
        <div class="panel-title cp-h">匹配对（人工 → AI）</div>
        <div v-for="(m, i) in report.matched" :key="i" class="cp-row"><b class="mono">{{ m.manual }}</b>「{{ m.manual_point }}」 → <b class="mono">{{ m.ai }}</b>「{{ m.ai_point }}」 <span class="badge" :class="vColor(m.verdict)">{{ m.verdict }}</span><div v-if="m.reason" class="tiny muted">{{ m.reason }}</div></div>
      </template>
      <template v-if="(report.manual_only || []).length">
        <div class="panel-title cp-h cp-bad">AI 遗漏：只有人工写到的测试点</div>
        <div v-for="(m, i) in report.manual_only" :key="i" class="cp-row"><b class="mono">{{ m.tp_id }}</b>「{{ m.point }}」<span class="tiny muted"> {{ m.module }}</span><div v-if="m.reason" class="tiny muted">{{ m.reason }}</div></div>
      </template>
      <template v-if="(report.ai_only || []).length">
        <div class="panel-title cp-h">AI 额外发现：只有 AI 给出的测试点（请判断是补充还是超出需求）</div>
        <div v-for="(m, i) in report.ai_only" :key="i" class="cp-row"><b class="mono">{{ m.tp_id }}</b>「{{ m.point }}」<span class="tiny muted"> {{ m.module }}</span> <span v-if="m.valuable === false" class="badge gray">疑似超出需求</span><span v-else-if="m.valuable === true" class="badge green">有价值补充</span><div v-if="m.reason" class="tiny muted">{{ m.reason }}</div></div>
      </template>
    </template>
    <TgEmpty v-else title="还没有对比报告" :hint="hasManual ? '已有人工测试点，点「重新对比」' : '先录入人工编写的测试点'" />
  </div>
</template>
<style scoped>
.cp-info { margin: 8px 0 12px; }
.cp-form { margin-bottom: 12px; }
.cp-mod { width: 280px; }
.cp-meta { margin: 6px 0; }
.cp-warn { color: var(--amber); margin-left: 8px; }
.cp-stats { grid-template-columns: repeat(5, 1fr); margin-bottom: 12px; }
.cp-ok { color: var(--green); } .cp-bad { color: var(--red); }
.cp-h { margin: 12px 0 6px; }
.cp-row { border: 1px solid var(--line); border-radius: 3px; padding: 7px 10px; margin-bottom: 6px; font-size: 12.5px; }
</style>

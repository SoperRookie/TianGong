<script setup>
/* AI 任务 8 节点流水线（设计方案 v2.3 §4.7）：解析材料 → 拆解测试点 → 经验延伸 → 查漏 / 组合 → 查重 / AI 评审
   → 人工审核测试点（门禁）→ 生成用例 → 评审 ≤ 3 轮。完成绿、进行蓝、待开始灰、失败红。
   task 为 GET /ai/tasks 的行；detail 为 GET /tasks/{id}（可选，有则显示各节点数字）。 */
import { computed } from 'vue'
import { num } from '@/utils/format'
const props = defineProps({ task: { type: Object, required: true }, detail: { type: Object, default: null } })

const LABELS = ['解析材料', '拆解测试点', '经验延伸', '查漏 / 组合', '查重 / AI 评审', '人工审核测试点', '生成用例', '评审 ≤ 3 轮']

/** 当前节点序号（1~8），9 表示全部完成 */
function currentOf(t) {
  if (t.status === 'completed') return 9
  if (t.status === 'awaiting_confirmation') return 6
  if (t.status === 'queued') return 1
  if (t.status === 'running') return { parsing: 1, analyzing: 2, generating_reviewing: 7, exporting: 8 }[t.progress] || 2
  const r = t.resume || {}
  if (r.mode === 'restore' || r.mode === 'fill') return 7
  return r.analysis_kept ? 7 : 2
}
const cur = computed(() => currentOf(props.task))
const failed = computed(() => props.task.status === 'failed' || props.task.status === 'cancelled')

const subs = computed(() => {
  const t = props.task, d = props.detail
  const out = new Array(8).fill('')
  out[0] = (t.sources || []).length ? `${t.sources.length} 份材料` : (t.requirement_id ? '需求实体' : '')
  if (!d) {
    if (t.status === 'completed') out[6] = `${num(t.case_count)} 条`
    return out
  }
  const modules = (d.analysis && d.analysis.test_points) || []
  const points = modules.flatMap(m => m.points || [])
  const st = k => points.filter(p => p.status === k).length
  const trace = ((d.result || {}).trace || []).concat(((d.analysis || {}).trace || []))
  const agents = new Set(trace.map(x => x.agent))
  if (points.length) out[1] = `${modules.length} 模块 · ${num(points.length)} 条`
  out[2] = agents.has('经验延伸') ? '已执行' : (d.point_combine ? '—' : '')
  const bs = (d.blind_spots || []).length
  out[3] = bs || d.point_combine ? `盲区 ${bs}${d.point_combine ? ` · 组合 +${d.point_combine.added || 0}` : ''}` : ''
  const dup = (d.dup_pairs || []).length
  out[4] = dup || d.point_review ? `疑似重复 ${dup}${d.point_review ? ` · 评审意见 ${d.point_review.issues || 0}` : ''}` : ''
  if (points.length) out[5] = `通过 ${st('approved')} · 待审 ${st('pending')} · 驳回 ${st('rejected')}`
  const cases = (d.result || {}).cases || []
  if (cases.length) {
    out[6] = `${num(cases.length)} 条`
    const reviews = Object.values(d.case_reviews || {})
    const approved = reviews.filter(r => r.status === 'approved').length
    const rounds = (d.result || {}).review_rounds
    out[7] = `正式 ${num(approved)}${rounds != null ? ` · ${rounds} 轮` : ''}`
  }
  return out
})
</script>
<template>
  <div class="pipeline">
    <div v-for="(label, i) in LABELS" :key="i" class="pnode" :class="{ done: i + 1 < cur, cur: i + 1 === cur && !failed, bad: i + 1 === cur && failed, gate: i === 5 }">
      <span>{{ i + 1 < cur ? '✓' : (i + 1 === cur && failed ? '!' : i + 1) }}</span>
      <b>{{ label }}</b>
      <small>{{ subs[i] || (i === 5 ? '门禁' : '—') }}</small>
    </div>
  </div>
</template>
<style scoped>
.pnode.bad span { background: #db5361; color: #fff; box-shadow: 0 0 0 4px #fdecee; }
.pnode.gate b::after { content: ' ⛉'; font-size: 10px; color: #d68a28; }
</style>

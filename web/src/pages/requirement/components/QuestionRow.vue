<script setup>
/* 待确认事项一行（.qrow）：等级 / 模块 / 来源标签、理由、AI 推荐或历史结论、结论框预填、采纳 / 确认 / 调级 / 重新打开。详情第 ④ 步与待确认事项汇总页共用。 */
import { ref, watch } from 'vue'
import { fmtTime } from '@/utils/format'
import { TgBadge } from '@/components/ui'
import { suggestedAnswer, questionKindTag } from './reqUtil'
const props = defineProps({ q: { type: Object, required: true }, canEdit: Boolean, checked: Boolean, labels: { type: Object, default: () => ({}) }, showReq: { type: String, default: '' } })
const emit = defineEmits(['answer', 'adopt', 'level', 'reopen', 'check', 'goto'])
const answer = ref(suggestedAnswer(props.q))
watch(() => props.q, q => { answer.value = suggestedAnswer(q) }, { deep: true })
defineExpose({ answer })
const kind = questionKindTag(props.q)
/* 调级：降为建议级时先填「不确认时的假设」再提交 */
const asking = ref(false), assumption = ref('')
function pickLevel(lv) {
  if (lv === (props.q.level || 'must')) return
  if (lv === 'suggest') { asking.value = true; assumption.value = props.q.assumption || '按行业惯例处理'; return }
  emit('level', { level: lv, assumption: null })
}
function submitLevel() { emit('level', { level: 'suggest', assumption: assumption.value.trim() || '按行业惯例处理' }); asking.value = false }
</script>
<template>
  <div class="qrow" :class="{ done: q.status !== 'open' }">
    <input type="checkbox" :checked="checked" :disabled="q.status !== 'open' || !canEdit" @change="emit('check', $event.target.checked)" />
    <div>
      <div class="q">
        {{ q.question }}
        <TgBadge :status="q.level || 'must'" />
        <span v-if="q.module" class="tag" :title="q.ref ? `出处：${q.ref}` : '所属页面 / 模块'">{{ q.module }}</span>
        <span class="tag" :class="kind.cls">{{ kind.text }}</span>
        <span v-if="q.inherited" class="tag" :title="`同项目需求「${q.inherited.title || ''}」已确认过同一问题（当时问法：${q.inherited.question || ''}），结论直接沿用`">沿用「{{ q.inherited.title }}」</span>
        <TgBadge v-if="q.status !== 'open'" status="confirmed" />
        <a v-if="showReq" class="link tiny" @click="emit('goto')">{{ showReq }} →</a>
      </div>
      <div class="meta">
        <template v-if="q.reason">{{ q.reason }}</template>
        <template v-if="q.status !== 'open'"> · 确认人 {{ q.by || '' }} · {{ fmtTime(q.at) }} · 结论：<b>{{ q.answer }}</b></template>
        <template v-else-if="(q.level || 'must') === 'suggest'"> · 不确认时假设：{{ q.assumption || '按行业惯例处理' }}</template>
      </div>
      <div v-if="q.status === 'open' && q.suggested" class="sugg" :title="`问法相近但没到自动沿用的门槛，只作参考：结论已预填，点「确认」才算`">历史结论参考（「{{ q.suggested.title }}」：{{ q.suggested.question }}）→ {{ q.suggested.answer }}</div>
      <div v-else-if="q.status === 'open' && q.suggested_answer" class="sugg"><span>AI 推荐（{{ q.suggested_answer.basis || '' }}{{ q.suggested_answer.unverified ? '·引文未核验' : '' }} · 置信度{{ q.suggested_answer.confidence || '中' }}）→ <b>{{ q.suggested_answer.answer }}</b><div v-if="q.suggested_answer.evidence">依据：{{ q.suggested_answer.evidence }}</div><div v-if="q.suggested_answer.risk">若不是这个答案：{{ q.suggested_answer.risk }}</div></span></div>
      <details v-if="(q.history || []).length" class="tiny" style="margin-top:4px"><summary class="muted" style="cursor:pointer">澄清记录 {{ q.history.length }} 条</summary><div v-for="(h, i) in q.history" :key="i" class="muted">{{ fmtTime(h.at) }} · {{ h.by || '系统' }} · <b>{{ h.action }}</b><template v-if="h.detail">：{{ h.detail }}</template></div></details>
      <div v-if="q.status === 'open' && canEdit" class="ans">
        <input v-model="answer" class="field" placeholder="确认结论（产品 / 开发口径；预填 AI 推荐 / 历史结论）" @keydown.enter="answer.trim() && emit('answer', answer.trim())" />
        <button type="button" class="btn success solid" :disabled="!answer.trim()" @click="emit('answer', answer.trim())">{{ answer && answer === suggestedAnswer(q) ? '采纳并确认' : '确认' }}</button>
        <button v-if="q.key && q.text" type="button" class="btn" :title="`确认该条成立：采纳回「${labels[q.key] || q.key}」并记为已确认`" @click="emit('adopt')">采纳回分析</button>
        <select class="select" style="width:92px" :value="asking ? 'suggest' : (q.level || 'must')" title="调整等级：必须（不确认不能发起设计）/ 建议（可带假设继续）" @change="pickLevel($event.target.value)"><option value="must">必须</option><option value="suggest">建议</option></select>
      </div>
      <div v-if="asking" class="row" style="margin-top:6px">
        <input v-model="assumption" class="field small" style="flex:1" placeholder="降为建议确认：不确认时按什么假设处理（写进用例备注）" @keydown.enter="submitLevel" />
        <button type="button" class="btn small primary" @click="submitLevel">降为建议</button>
        <button type="button" class="btn small" @click="asking = false">取消</button>
      </div>
      <div v-else-if="q.status !== 'open' && canEdit" class="row" style="margin-top:6px"><button type="button" class="btn small" @click="emit('reopen')">重新打开</button></div>
    </div>
  </div>
</template>

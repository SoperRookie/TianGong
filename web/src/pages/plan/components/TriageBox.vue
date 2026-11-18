<script setup>
/* AI 归因框：POST runs/{rid}/triage（item_ids）→ 五类归因 + 缺陷单草稿；采纳 POST results/{item_id}/triage/adopt；复制缺陷单 copyText */
import { ref, computed } from 'vue'
import { api } from '@/api/client'
import { useUiStore } from '@/stores/ui'
import { copyText, fmtTime } from '@/utils/format'
import { FAIL_REASONS } from '@/utils/consts'
import { TgBadge, TgKv } from '@/components/ui'
const props = defineProps({ planId: { type: String, required: true }, runId: { type: String, required: true }, item: { type: Object, required: true }, result: { type: Object, required: true }, active: Boolean, canRun: Boolean })
const emit = defineEmits(['updated', 'close'])
const ui = useUiStore()
const base = computed(() => `/api/v1/plans/${props.planId}/runs/${props.runId}`)
const running = ref(false), adopting = ref(false), reason = ref('')
const t = computed(() => props.result.triage || null)
const bug = computed(() => t.value?.bug || null)
async function run() {
  running.value = true
  try {
    const r = await api.post(`${base.value}/triage`, { item_ids: [props.item.item_id] })
    if (r.failed?.length) ui.toast(`归因失败：${r.failed[0].error}`, 'err'); else ui.toast('已完成 AI 归因')
    emit('updated', r.results)
  } catch (e) { ui.error(e) } finally { running.value = false }
}
async function adopt(custom) {
  adopting.value = true
  try {
    const r = await api.post(`${base.value}/results/${props.item.item_id}/triage/adopt`, custom ? { reason: reason.value } : {})
    ui.toast(`已采纳归因为「${r.result.reason}」，记采纳人`); emit('updated', { [props.item.item_id]: r.result })
  } catch (e) { ui.error(e) } finally { adopting.value = false }
}
function bugText() {
  const b = bug.value, it = props.item, r = props.result
  return [`【${b.severity}】${b.title}`, `用例：${it.case_id} ${it.title}（${it.module}）`, `前置：${b.precondition || it.snapshot?.precondition || '—'}`, '复现步骤：',
    ...(b.steps || []).map((s, i) => `${i + 1}. ${s}`), `预期：${b.expected || '—'}`, `实际：${b.actual || '—'}`, `影响范围：${b.scope || '—'}`,
    `执行说明：${r.note || ''}`, `归因依据：${t.value?.reason || ''}`].join('\n')
}
async function copyBug() { await copyText(bugText()); ui.toast('缺陷单已复制到剪贴板') }
const confColor = c => c === '高' ? 'green' : c === '低' ? 'red' : 'orange'
</script>
<template>
  <div class="panel panel-pad triage">
    <div class="panel-head">
      <div><div class="panel-title">AI 归因（exec_triage）</div><div class="panel-hint">输入：用例 + 执行人失败说明 + 截图理解 + 用例模块所在章节原文 + 项目画像 → 五类归因 + 缺陷单草稿</div></div>
      <div class="row">
        <button v-if="canRun" type="button" class="btn small" :disabled="running" @click="run"><span v-if="running" class="spinner" /> {{ running ? '归因中…' : (t ? '重新归因' : 'AI 归因') }}</button>
        <button type="button" class="btn small" @click="emit('close')">收起</button>
      </div>
    </div>
    <div v-if="!t" class="muted small">还没有归因结果。{{ canRun ? '点「AI 归因」调用模型分析这条失败记录（会读取关联的失败截图）。' : '' }}</div>
    <div v-else class="grid-2">
      <div>
        <TgKv :pairs="[]">
          <dt>归因</dt><dd><b>{{ t.category }}</b><template v-if="t.sub"> · {{ t.sub }}</template> <TgBadge :color="confColor(t.confidence)" :text="`置信度 ${t.confidence || '中'}`" /><span v-if="t.screenshots" class="tiny muted"> · 看了 {{ t.screenshots }} 张截图</span></dd>
          <dt>失败步骤</dt><dd>{{ t.failed_step ? `第 ${t.failed_step} 步` : '未定位' }}</dd>
          <dt>依据</dt><dd>{{ t.reason || '—' }}</dd>
          <dt>建议分类</dt>
          <dd>
            <b>{{ t.suggested_reason || '—' }}</b><span class="tiny muted">（当前分类「{{ result.reason }}」）</span>
            <div v-if="t.adopted" class="tiny" style="margin-top:4px"><span class="tag">已采纳</span> {{ t.adopted.by }} · {{ fmtTime(t.adopted.at) }} → {{ t.adopted.reason }}</div>
            <div v-else-if="active && canRun" class="row" style="margin-top:6px">
              <button v-if="t.suggested_reason && t.suggested_reason !== result.reason" type="button" class="btn success small" :disabled="adopting" @click="adopt(false)">采纳为「{{ t.suggested_reason }}」</button>
              <select v-model="reason" class="select small"><option value="">或改为…</option><option v-for="r in FAIL_REASONS" :key="r">{{ r }}</option></select>
              <button v-if="reason" type="button" class="btn small" :disabled="adopting" @click="adopt(true)">采纳为「{{ reason }}」</button>
            </div>
          </dd>
          <dt>模型</dt><dd class="tiny muted">{{ t.model || '—' }} · {{ t.by }} {{ fmtTime(t.at) }}</dd>
        </TgKv>
        <div class="tiny muted" style="margin-top:8px">五类：用例问题（步骤有误 / 预期有误 / 前置缺失）/ 系统缺陷 / 环境问题 / 数据问题 / 需求变更；枚举外按系统缺陷且置信度低。采纳后用例问题类进入学习语料。</div>
      </div>
      <div>
        <template v-if="bug">
          <div class="source-block"><div class="source-label">缺陷单草稿（复制到外部缺陷系统，平台不保存缺陷实体）</div>
            <div><b>【{{ bug.severity }}】{{ bug.title }}</b></div>
            <div>用例：{{ item.case_id }} {{ item.title }}</div>
            <div>前置：{{ bug.precondition || item.snapshot?.precondition || '—' }}</div>
            <div>复现步骤：<ol class="steps-mini" style="margin:2px 0;padding-left:20px"><li v-for="(s, i) in bug.steps" :key="i">{{ s }}</li></ol></div>
            <div>预期：{{ bug.expected || '—' }}</div><div>实际：{{ bug.actual || '—' }}</div><div>影响范围：{{ bug.scope || '—' }}</div>
          </div>
          <button type="button" class="btn small" style="margin-top:8px" @click="copyBug">复制缺陷单</button>
        </template>
        <div v-else class="muted small">非系统缺陷不起草缺陷单。</div>
      </div>
    </div>
  </div>
</template>
<style scoped>
.triage { margin-top: 12px; border-color: #e3d9fb; }
</style>

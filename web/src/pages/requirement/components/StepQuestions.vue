<script setup>
/*
 ④ 待确认事项：页签全部 / 必须待办 / 建议待办 / 已确认；AI 推荐答案预填（POST /questions/suggest）；采纳回分析（POST /questions/{q}/adopt）；
 回答 / 重新打开（POST /questions/{q}）；调级（PUT /questions/{q}/level）；补充新增（POST /questions）；澄清记录抽屉；批量采纳并确认已选。
*/
import { ref, computed } from 'vue'
import { api } from '@/api/client'
import { useUiStore } from '@/stores/ui'
import { fmtTime } from '@/utils/format'
import { TgTabs, TgDrawer, TgDialog, TgEmpty, TgField } from '@/components/ui'
import { suggestedAnswer } from './reqUtil'
import QuestionRow from './QuestionRow.vue'
const props = defineProps({ r: { type: Object, required: true }, canEdit: Boolean, canAi: Boolean })
const emit = defineEmits(['reload'])
const ui = useUiStore()
const tab = ref('all'), checked = ref(new Set()), logOpen = ref(false), addOpen = ref(false), suggesting = ref(false)
const form = ref({ question: '', level: 'must', module: '', assumption: '' })
const qs = computed(() => props.r.questions || [])
const must = computed(() => qs.value.filter(q => q.status === 'open' && (q.level || 'must') === 'must'))
const sugg = computed(() => qs.value.filter(q => q.status === 'open' && (q.level || 'must') === 'suggest'))
const done = computed(() => qs.value.filter(q => q.status !== 'open'))
const tabs = computed(() => [{ key: 'all', label: '全部', count: qs.value.length }, { key: 'must', label: '必须待办', count: must.value.length }, { key: 'suggest', label: '建议待办', count: sugg.value.length }, { key: 'done', label: '已确认', count: done.value.length }])
const list = computed(() => tab.value === 'must' ? must.value : tab.value === 'suggest' ? sugg.value : tab.value === 'done' ? done.value : qs.value)
const modules = computed(() => [...new Set(qs.value.map(q => q.module).filter(Boolean))])
const log = computed(() => { const rows = []; qs.value.forEach((q, i) => (q.history || []).forEach(h => rows.push({ ...h, n: i + 1, question: q.question }))); return rows.sort((a, b) => (a.at || '').localeCompare(b.at || '')) })
const base = computed(() => `/api/v1/requirements/${props.r.req_id}/questions`)
function check(q, on) { const s = new Set(checked.value); on ? s.add(q.q_id) : s.delete(q.q_id); checked.value = s }
async function run(fn, msg) { try { await fn(); if (msg) ui.toast(msg); emit('reload') } catch (e) { ui.error(e) } }
const answer = (q, a) => run(() => api.post(`${base.value}/${q.q_id}`, { answer: a, reopen: false }), '已确认')
const reopen = q => run(() => api.post(`${base.value}/${q.q_id}`, { answer: '', reopen: true }), '已重新打开')
const adopt = q => run(() => api.post(`${base.value}/${q.q_id}/adopt`, {}), '已采纳回分析结果，并记为已确认')
const level = (q, { level: lv, assumption }) => run(() => api.put(`${base.value}/${q.q_id}/level`, { level: lv, assumption }), lv === 'must' ? '已升为必须确认' : '已降为建议确认')
async function batch() {
  const picked = qs.value.filter(q => checked.value.has(q.q_id) && q.status === 'open' && suggestedAnswer(q))
  if (!picked.length) return ui.toast('已选事项没有可采纳的推荐答案', { kind: 'warn' })
  if (!await ui.confirm(`采纳并确认 ${picked.length} 项`, '以各自预填的 AI 推荐 / 历史结论作为确认结论。确定？', { ok: '采纳并确认' })) return
  try { for (const q of picked) await api.post(`${base.value}/${q.q_id}`, { answer: suggestedAnswer(q), reopen: false }); ui.toast(`已确认 ${picked.length} 项`); checked.value = new Set(); emit('reload') } catch (e) { ui.error(e); emit('reload') }
}
async function suggest() {
  suggesting.value = true
  try { const x = await api.post(`${base.value}/suggest`, {}); ui.toast(`已给 ${x.suggested} 条事项拟了推荐答案（预填在结论框，点「确认」才算）`, { kind: x.suggested ? 'ok' : 'warn' }); emit('reload') } catch (e) { ui.error(e) } finally { suggesting.value = false }
}
async function add() {
  if (!form.value.question.trim()) return ui.toast('请填写事项内容', { kind: 'warn' })
  try { await api.post(base.value, { question: form.value.question.trim(), level: form.value.level, module: form.value.module.trim(), assumption: form.value.assumption.trim() }); ui.toast('已补充'); addOpen.value = false; form.value = { question: '', level: 'must', module: '', assumption: '' }; emit('reload') } catch (e) { ui.error(e) }
}
</script>
<template>
  <div class="panel-head" style="margin-bottom:6px">
    <div><div class="panel-title">④ 待确认事项</div><div class="panel-hint">结论框预填 AI 推荐答案（原文 › 历史结论 › 行业惯例）；必须级未确认阻塞发起测试设计，建议级可带假设继续。同项目已确认结论相似度 ≥ 0.6 自动沿用。</div></div>
    <div class="row">
      <button v-if="canEdit && checked.size" type="button" class="btn success small" @click="batch">采纳并确认已选 {{ checked.size }}</button>
      <button v-if="canAi && must.length + sugg.length" type="button" class="btn small" :disabled="suggesting" @click="suggest" title="给全部未确认事项按原文 › 历史结论 › 行业惯例各拟一个确定答案 + 依据 + 风险，预填到结论框"><span v-if="suggesting" class="spinner" /> 推荐答案</button>
      <button type="button" class="btn small" @click="logOpen = true">澄清记录</button>
      <button v-if="canEdit" type="button" class="btn small primary" @click="addOpen = true">＋ 补充事项</button>
    </div>
  </div>
  <TgTabs v-model="tab" :items="tabs" />
  <TgEmpty v-if="!qs.length" title="没有待确认事项" hint="AI 分析完成后，原文核验不通过的事实项与模型提出的疑问会进入这里；也可人工补充" />
  <TgEmpty v-else-if="!list.length" title="该页签没有事项" />
  <QuestionRow v-for="q in list" :key="q.q_id" :q="q" :can-edit="canEdit" :checked="checked.has(q.q_id)" :labels="r.analysis_labels || {}" @check="on => check(q, on)" @answer="a => answer(q, a)" @adopt="adopt(q)" @level="p => level(q, p)" @reopen="reopen(q)" />
  <div v-if="sugg.length" class="tiny muted" style="padding:8px 0">{{ sugg.length }} 项建议级事项未确认，不阻塞测试设计，将按各自假设处理</div>

  <TgDrawer v-model="logOpen" title="澄清记录" sub="按时间看全部事项的澄清全程：谁提的、AI 推荐了什么、谁确认 / 重开 / 改等级" wide>
    <TgEmpty v-if="!log.length" title="还没有澄清记录" hint="之后的提出 / 推荐 / 确认 / 重开 / 改等级都会留痕" />
    <table v-else class="table dense">
      <thead><tr><th style="width:130px">时间</th><th style="width:90px">操作人</th><th style="width:36px">#</th><th>事项</th><th style="width:100px">动作</th><th>说明</th></tr></thead>
      <tbody><tr v-for="(h, i) in log" :key="i"><td class="tiny muted">{{ fmtTime(h.at) }}</td><td class="tiny">{{ h.by || '系统' }}</td><td class="tiny muted">{{ h.n }}</td><td>{{ h.question }}</td><td><b>{{ h.action }}</b></td><td class="tiny muted">{{ h.detail || '' }}</td></tr></tbody>
    </table>
  </TgDrawer>
  <TgDialog v-model="addOpen" title="补充待确认事项" ok="添加" @ok="add">
    <TgField label="事项" required><textarea v-model="form.question" class="field" rows="3" placeholder="需要产品 / 开发确认的问题…" /></TgField>
    <div class="form-grid">
      <TgField label="等级"><select v-model="form.level" class="select"><option value="must">必须确认（不确认不能发起设计）</option><option value="suggest">建议确认（可带假设继续）</option></select></TgField>
      <TgField label="所属模块" hint="可选，如「活动列表-基本配置」"><input v-model="form.module" class="field" list="req-q-modules" /><datalist id="req-q-modules"><option v-for="m in modules" :key="m" :value="m" /></datalist></TgField>
    </div>
    <TgField v-if="form.level === 'suggest'" label="不确认时的假设" hint="写进用例备注"><input v-model="form.assumption" class="field" placeholder="按行业惯例处理" /></TgField>
  </TgDialog>
</template>

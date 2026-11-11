<script setup>
/*
 待确认事项汇总：当前工作范围内所有需求（遍历需求列表 → 读每条有事项的需求详情），按需求分组；必须 / 建议 / 已确认筛选；
 就地回答 / 采纳 / 调级 / 重新打开；可跳到需求详情第 ④ 步。
*/
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { useBadgeStore } from '@/stores/badge'
import { num } from '@/utils/format'
import { PageHeading, TgPanel, TgStat, TgTabs, TgEmpty, TgBadge } from '@/components/ui'
import QuestionRow from './components/QuestionRow.vue'
const route = useRoute(), router = useRouter(), scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore(), badge = useBadgeStore()
const tab = ref(route.query.tab || 'open'), kw = ref('')
const reqs = ref([]), loading = ref(false), error = ref(null), suggesting = ref('')
let seq = 0
async function load() {
  const my = ++seq; loading.value = true; error.value = null
  try {
    const d = await api.get('/api/v1/requirements', { ...scope.query, page_size: 0 })
    const withQ = (d.requirements || []).filter(r => r.questions > 0)
    const out = []
    for (let i = 0; i < withQ.length; i += 6) {   // 分批并发读详情，避免一次打爆
      const part = await Promise.all(withQ.slice(i, i + 6).map(r => api.get(`/api/v1/requirements/${r.req_id}`).catch(() => null)))
      if (my !== seq) return
      part.filter(Boolean).forEach(r => out.push(r))
    }
    if (my !== seq) return
    reqs.value = out.sort((a, b) => (b.blocking_questions - a.blocking_questions) || (b.updated_at || '').localeCompare(a.updated_at || ''))
  } catch (e) { if (my === seq) error.value = e } finally { if (my === seq) loading.value = false }
}
watch([() => scope.project, () => scope.line], load, { immediate: true })
watch(tab, t => router.replace({ query: { ...route.query, tab: t === 'open' ? undefined : t } }))
async function refreshOne(reqId) {
  try { const r = await api.get(`/api/v1/requirements/${reqId}`); const i = reqs.value.findIndex(x => x.req_id === reqId); if (i >= 0) reqs.value[i] = r } catch { load() }
  badge.refresh(true)
}
const all = computed(() => reqs.value.flatMap(r => (r.questions || []).map(q => ({ q, r }))))
const inScope = computed(() => all.value.filter(({ q, r }) => (!scope.module || r.module_path === scope.module || (r.module_path || '').split('/').pop() === scope.module || q.module === scope.module) && (!kw.value.trim() || `${q.question} ${q.reason || ''} ${q.answer || ''} ${r.title}`.toLowerCase().includes(kw.value.trim().toLowerCase()))))
const isOpen = q => q.status === 'open', isMust = q => isOpen(q) && (q.level || 'must') === 'must', isSugg = q => isOpen(q) && (q.level || 'must') === 'suggest'
const counts = computed(() => ({ open: inScope.value.filter(x => isOpen(x.q)).length, must: inScope.value.filter(x => isMust(x.q)).length, suggest: inScope.value.filter(x => isSugg(x.q)).length, done: inScope.value.filter(x => !isOpen(x.q)).length, all: inScope.value.length }))
const tabs = computed(() => [{ key: 'open', label: '待确认', count: counts.value.open }, { key: 'must', label: '必须', count: counts.value.must }, { key: 'suggest', label: '建议', count: counts.value.suggest }, { key: 'done', label: '已确认', count: counts.value.done }, { key: 'all', label: '全部', count: counts.value.all }])
const groups = computed(() => {
  const f = tab.value
  const keep = q => f === 'all' || (f === 'open' ? isOpen(q) : f === 'must' ? isMust(q) : f === 'suggest' ? isSugg(q) : !isOpen(q))
  return reqs.value.map(r => ({ r, qs: inScope.value.filter(x => x.r === r && keep(x.q)).map(x => x.q) })).filter(g => g.qs.length)
})
watch(() => groups.value.length, () => { scope.moduleCounts = null })
onBeforeUnmount(() => { scope.moduleCounts = null })
async function run(reqId, fn, msg) { try { await fn(); if (msg) ui.toast(msg); await refreshOne(reqId) } catch (e) { ui.error(e) } }
const base = r => `/api/v1/requirements/${r.req_id}/questions`
const answer = (r, q, a) => run(r.req_id, () => api.post(`${base(r)}/${q.q_id}`, { answer: a, reopen: false }), '已确认')
const reopen = (r, q) => run(r.req_id, () => api.post(`${base(r)}/${q.q_id}`, { answer: '', reopen: true }), '已重新打开')
const adopt = (r, q) => run(r.req_id, () => api.post(`${base(r)}/${q.q_id}/adopt`, {}), '已采纳回分析结果')
const level = (r, q, { level: lv, assumption }) => run(r.req_id, () => api.put(`${base(r)}/${q.q_id}/level`, { level: lv, assumption }), lv === 'must' ? '已升为必须确认' : '已降为建议确认')
async function suggest(r) {
  suggesting.value = r.req_id
  try { const x = await api.post(`${base(r)}/suggest`, {}); ui.toast(`已给「${r.title}」的 ${x.suggested} 条事项拟了推荐答案`); await refreshOne(r.req_id) } catch (e) { ui.error(e) } finally { suggesting.value = '' }
}
const goto = (r) => router.push({ name: 'req-detail', params: { id: r.req_id, step: 4 } })
</script>
<template>
  <PageHeading title="待确认事项" sub="跨需求汇总当前工作范围内的全部事项；必须级未确认会阻塞该需求发起测试设计。确认结论会沉淀为同项目的历史结论供后续需求沿用。">
    <template #actions><button type="button" class="btn" @click="load"><span v-if="loading" class="spinner" /> 刷新</button></template>
  </PageHeading>
  <div v-if="error" class="err-box">{{ error.message }}</div>
  <div class="stat-grid four">
    <TgStat label="全部事项" :value="num(counts.all)" :foot="`${reqs.length} 条需求`" icon="message" color="blue" />
    <TgStat label="必须待确认" :value="num(counts.must)" foot="阻塞发起测试设计" icon="alert" color="orange" />
    <TgStat label="建议待确认" :value="num(counts.suggest)" foot="可带假设继续" icon="info" color="purple" />
    <TgStat label="已确认" :value="num(counts.done)" foot="进入历史结论池" icon="check" color="green" />
  </div>
  <TgPanel :pad="false">
    <div class="filters" style="padding:12px 14px 0">
      <TgTabs v-model="tab" :items="tabs" style="margin-bottom:0;border-bottom:0" />
      <input v-model="kw" class="field right" style="width:240px" placeholder="搜索事项 / 需求" />
    </div>
    <div v-if="loading && !reqs.length" class="page-loading"><span class="spinner" /> 正在汇总各需求的待确认事项…</div>
    <TgEmpty v-else-if="!groups.length" :title="counts.all ? '该页签没有事项' : '当前范围内没有待确认事项'" hint="AI 分析完成后，原文核验不通过的事实项与模型提出的疑问会进入各需求的待确认事项" />
    <div v-for="g in groups" :key="g.r.req_id" class="qgroup">
      <div class="qgroup-head">
        <router-link class="link" :to="{ name: 'req-detail', params: { id: g.r.req_id, step: 4 } }"><b>{{ g.r.title }}</b></router-link>
        <TgBadge :status="g.r.status" :text="g.r.status_label" />
        <span class="tiny muted">{{ g.r.project }}<template v-if="g.r.module_path"> · {{ g.r.module_path }}</template> · 必须待办 {{ g.r.blocking_questions }} · 待确认 {{ g.r.open_questions }}</span>
        <span class="right row">
          <button v-if="g.r.open_questions && auth.can(g.r.project, 'requirement.ai')" type="button" class="btn ghost small" :disabled="suggesting === g.r.req_id" @click="suggest(g.r)"><span v-if="suggesting === g.r.req_id" class="spinner" /> 推荐答案</button>
          <button type="button" class="btn ghost small" @click="goto(g.r)">去需求详情 ④ →</button>
        </span>
      </div>
      <div style="padding:0 14px">
        <QuestionRow v-for="q in g.qs" :key="q.q_id" :q="q" :can-edit="auth.can(g.r.project, 'requirement.edit')" :labels="g.r.analysis_labels || {}" @answer="a => answer(g.r, q, a)" @adopt="adopt(g.r, q)" @level="p => level(g.r, q, p)" @reopen="reopen(g.r, q)" />
      </div>
    </div>
  </TgPanel>
</template>
<style scoped>
.qgroup { border-top: 1px solid var(--line); }
.qgroup-head { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; padding: 10px 14px; background: #f6f8fa; }
.qgroup :deep(.qrow input[type=checkbox]) { visibility: hidden; }
</style>

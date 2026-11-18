<script setup>
/* 调用日志（11.10 / 11.12 / 11.14）：每次 LLMClient.chat 落 ai_calls；列表 + 右侧抽屉（路由 call-detail 带 id）。 */
import { ref, computed, watch, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useScopeStore } from '@/stores/scope'
import { useUiStore } from '@/stores/ui'
import { fmtTime, num, shortId, fmtDuration, copyText } from '@/utils/format'
import { PageHeading, TgPanel, TgTable, TgBadge, TgSeg, TgPager, TgDrawer, TgKv, TgIcon } from '@/components/ui'
import { usePrices, promptName } from './components/usePrices'

const route = useRoute(), router = useRouter(), scope = useScopeStore(), ui = useUiStore()
const { load: loadPrices, costOf, fmtCost } = usePrices()
onMounted(loadPrices)

const q = route.query
const days = ref(q.days != null ? String(q.days) : '30')
const purpose = ref(q.purpose || ''), status = ref(q.status || ''), model = ref(q.model || ''), by = ref(q.by || '')
const keyword = ref(q.keyword || ''), kw = ref(keyword.value)
const taskId = ref(q.task_id || ''), reqId = ref(q.requirement_id || '')
const project = ref(q.project || '')
const page = ref(+q.page || 1), size = ref(+q.page_size || 50)
watch([purpose, status, model, by, keyword, days, taskId, reqId, project, size], () => { page.value = 1 })
watch([purpose, status, model, by, keyword, days, taskId, reqId, project, page, size], () => router.replace({
  name: route.name, params: route.params,
  query: { purpose: purpose.value || undefined, status: status.value || undefined, model: model.value || undefined, by: by.value || undefined, keyword: keyword.value || undefined, days: days.value !== '30' ? days.value : undefined, task_id: taskId.value || undefined, requirement_id: reqId.value || undefined, project: project.value || undefined, page: page.value > 1 ? page.value : undefined, page_size: size.value !== 50 ? size.value : undefined },
}))

const { data, loading, error, reload } = useLoad(
  () => api.get('/api/v1/ai/calls', { project: scope.project || project.value || undefined, task_id: taskId.value || undefined, requirement_id: reqId.value || undefined, purpose: purpose.value || undefined, model: model.value || undefined, status: status.value || undefined, by: by.value || undefined, keyword: keyword.value || undefined, days: days.value, page: page.value, page_size: size.value }),
  { deps: [() => scope.project, purpose, status, model, by, keyword, days, taskId, reqId, project, page, size] },
)
const items = computed(() => data.value?.items || [])
const pc = computed(() => data.value?.purpose_counts || {})
const sc = computed(() => data.value?.status_counts || {})
const withCur = (list, cur) => [...new Set([...(list || []), ...(cur ? [cur] : [])])]
const cols = [
  { key: 'at', title: '时间', w: '120px' }, { key: 'purpose', title: '用途' }, { key: 'model', title: '模型' }, { key: 'target', title: '项目 / 对象' },
  { key: 'status', title: '状态', w: '90px' }, { key: 'tokens', title: 'token 入 / 出' }, { key: 'elapsed', title: '耗时', w: '90px' }, { key: 'cost', title: '估算费用', w: '90px', align: 'right' }, { key: 'by', title: '操作人', w: '90px' },
]

/* 抽屉：路由 call-detail 的 id */
const openId = computed(() => route.name === 'call-detail' ? route.params.id : '')
const call = ref(null), callLoading = ref(false), chain = ref([])
watch(openId, async id => {
  call.value = null; chain.value = []
  if (!id) return
  callLoading.value = true
  try {
    call.value = await api.get(`/api/v1/ai/calls/${id}`)
    // 重试链：同任务 + 同用途的其它调用（attempts > 1 说明客户端内部已重试过）
    if (call.value.task_id) {
      const r = await api.get('/api/v1/ai/calls', { task_id: call.value.task_id, purpose: call.value.purpose, page_size: 20 })
      chain.value = (r.items || []).filter(x => x.id !== call.value.id)
    }
  } catch (e) { ui.error(e); close() } finally { callLoading.value = false }
}, { immediate: true })
function open(row) { router.push({ name: 'call-detail', params: { id: row.id }, query: route.query }) }
function close() { router.push({ name: 'calls', query: route.query }) }
const idx = computed(() => items.value.findIndex(x => String(x.id) === String(openId.value)))
function step(d) { const n = items.value[idx.value + d]; if (n) open(n) }
const callCost = computed(() => call.value ? costOf(call.value.model, call.value.prompt_tokens, call.value.completion_tokens) : null)
const pvEntries = pv => Object.entries(pv || {})
</script>
<template>
  <PageHeading title="调用日志" :sub="`每次模型调用一行：用途、Prompt 版本、模型、token（含缓存命中 / 推理）、耗时、估算费用、输入输出预览。${data ? `范围内 ${num(data.total)} 条` : ''}；保留 TIANGONG_LOG_RETENTION_DAYS 天后自动清理。`">
    <template #actions><router-link class="btn" :to="{ name: 'usage' }">用量统计</router-link><button type="button" class="btn" @click="reload()"><TgIcon name="refresh" :size="14" /> 刷新</button></template>
  </PageHeading>
  <div v-if="error" class="err-box">{{ error.message }}</div>
  <TgPanel :pad="false">
    <div class="panel-pad filters" style="padding-bottom:10px">
      <TgSeg v-model="days" :items="[['1', '今天'], ['7', '近 7 天'], ['30', '近 30 天'], ['90', '近 90 天'], ['0', '全部']]" />
      <TgSeg v-model="status" :items="[{ key: '', label: '全部' }, { key: 'ok', label: '成功', count: sc.ok || 0 }, { key: 'error', label: '只看失败', count: sc.error || 0 }]" />
      <select v-model="purpose" class="select"><option value="">全部用途</option><option v-for="p in withCur(Object.keys(pc).sort(), purpose)" :key="p" :value="p">{{ p }}（{{ pc[p] || 0 }}）</option></select>
      <select v-model="model" class="select"><option value="">全部模型</option><option v-for="m in withCur(data?.models, model)" :key="m" :value="m">{{ m }}</option></select>
      <select v-model="by" class="select"><option value="">全部操作人</option><option v-for="u in withCur(data?.users, by)" :key="u" :value="u">{{ u }}</option></select>
      <select v-if="!scope.project" v-model="project" class="select"><option value="">全部项目</option><option v-for="p in withCur(data?.projects, project)" :key="p" :value="p">{{ p }}</option></select>
      <input v-model="kw" class="field" style="width:180px" placeholder="关键字：任务 / 需求 ID、错误、Prompt" @keyup.enter="keyword = kw" @blur="keyword = kw" />
      <span v-if="taskId" class="tag">任务 {{ shortId(taskId, 12) }} <a class="link" @click="taskId = ''">×</a></span>
      <span v-if="reqId" class="tag">需求 {{ reqId }} <a class="link" @click="reqId = ''">×</a></span>
    </div>
    <TgTable :cols="cols" :rows="items" row-key="id" :loading="loading" clickable :active-key="openId ? +openId : null" empty="没有调用记录" empty-hint="范围内还没有模型调用，或调用日志已按保留天数清理。" @row-click="open">
      <template #cell-at="{ row }"><span class="nowrap">{{ fmtTime(row.at, true) }}</span></template>
      <template #cell-purpose="{ row }"><b>{{ row.purpose }}</b><div v-if="row.prompt_key" class="sub">{{ promptName(row.prompt_key) }} v{{ row.prompt_version ?? '?' }}</div></template>
      <template #cell-model="{ row }"><span class="small">{{ row.model }}</span><div class="sub">{{ row.provider }}</div></template>
      <template #cell-target="{ row }">
        <span class="small">{{ row.project || '—' }}</span>
        <div class="sub mono">
          <router-link v-if="row.task_id" :to="{ name: 'design-detail', params: { id: row.task_id } }" @click.stop>task {{ shortId(row.task_id) }}</router-link>
          <router-link v-if="row.requirement_id" :to="{ name: 'req-detail', params: { id: row.requirement_id } }" style="margin-left:6px" @click.stop>req {{ shortId(row.requirement_id) }}</router-link>
        </div>
      </template>
      <template #cell-status="{ row }"><TgBadge :status="row.status" /><div v-if="row.status === 'error'" class="tiny ellipsis" style="color:#db5361;max-width:140px" :title="row.error">{{ row.error }}</div></template>
      <template #cell-tokens="{ row }">{{ num(row.prompt_tokens) }} / {{ num(row.completion_tokens) }}<div class="sub">缓存 {{ num(row.cached_tokens || 0) }} · 推理 {{ num(row.reasoning_tokens || 0) }}</div></template>
      <template #cell-elapsed="{ row }">{{ fmtDuration(row.elapsed_ms) }}<span v-if="row.attempts > 1" class="sub"> · {{ row.attempts }} 次</span></template>
      <template #cell-cost="{ row }"><span :title="costOf(row.model, row.prompt_tokens, row.completion_tokens) == null ? '单价表无此模型或无权读取单价' : '按 config/models.yaml 单价估算'">{{ fmtCost(costOf(row.model, row.prompt_tokens, row.completion_tokens)) }}</span></template>
      <template #cell-by="{ row }">{{ row.by || '系统' }}</template>
    </TgTable>
    <TgPager v-model:page="page" v-model:size="size" :total="data?.total || 0" :sizes="[20, 50, 100, 200]" />
  </TgPanel>

  <TgDrawer :model-value="!!openId" :title="call ? `调用记录 #${call.id} · ${call.purpose}` : '调用记录'" :sub="call ? `${call.model} · ${fmtTime(call.at, true)} · ${call.by || '系统'} · ${call.project || '未归属项目'}` : ''" wide @update:model-value="close">
    <template #head-actions>
      <button type="button" class="btn small" :disabled="idx <= 0" @click="step(-1)">上一条</button>
      <button type="button" class="btn small" :disabled="idx < 0 || idx >= items.length - 1" @click="step(1)">下一条</button>
    </template>
    <div v-if="callLoading" class="page-loading"><span class="spinner" /> 加载中…</div>
    <template v-else-if="call">
      <TgKv :pairs="[['耗时 / 重试', `${fmtDuration(call.elapsed_ms)} · 客户端重试 ${Math.max(0, (call.attempts || 1) - 1)} 次`], ['token', `输入 ${num(call.prompt_tokens)}（缓存命中 ${num(call.cached_tokens || 0)}）· 输出 ${num(call.completion_tokens)}（推理 ${num(call.reasoning_tokens || 0)}）`], ['估算费用', fmtCost(callCost)], ['finish_reason', call.finish_reason || '—'], ['字符数', `入 ${num(call.input_chars)} · 出 ${num(call.output_chars)}`]]">
      </TgKv>
      <div class="row" style="margin:-4px 0 10px"><span class="muted tiny">状态：</span><TgBadge :status="call.status" /><span v-if="call.task_id" class="tag"><router-link :to="{ name: 'design-detail', params: { id: call.task_id } }">任务 {{ shortId(call.task_id, 12) }}</router-link></span><span v-if="call.requirement_id" class="tag"><router-link :to="{ name: 'req-detail', params: { id: call.requirement_id } }">需求 {{ call.requirement_id }}</router-link></span></div>
      <div v-if="call.status === 'error'" class="err-box" style="margin-bottom:10px"><b>错误</b>：{{ call.error }}</div>
      <h4>Prompt 版本</h4>
      <div v-if="pvEntries(call.prompt_versions).length" class="inline-list"><router-link v-for="[k, v] in pvEntries(call.prompt_versions)" :key="k" class="tag" :class="{ ai: v > 1 }" :to="{ name: 'prompt-detail', params: { key: k } }">{{ promptName(k) }} v{{ v }}</router-link></div>
      <div v-else class="muted small">未登记 Prompt 版本（未经 prompt_text 取用）</div>
      <h4>重试链 <span class="tiny muted">同任务 · 同用途的其它调用</span></h4>
      <div v-if="chain.length" class="list">
        <div v-for="c in chain" :key="c.id" class="row clickable" @click="open(c)"><span class="mono tiny">#{{ c.id }}</span><span class="small">{{ fmtTime(c.at, true) }}</span><TgBadge :status="c.status" /><span class="small">{{ c.model }}</span><span class="tiny muted">{{ num(c.prompt_tokens) }} / {{ num(c.completion_tokens) }} token · {{ fmtDuration(c.elapsed_ms) }}</span></div>
      </div>
      <div v-else class="muted small">{{ call.task_id ? '没有其它同用途调用' : '未归属任务，无重试链' }}</div>
      <h4>输入（前 3000 字）<a class="link tiny" style="margin-left:8px" @click="copyText(call.input_preview).then(() => ui.toast('已复制'))">复制</a></h4>
      <div class="code-block">{{ call.input_preview || '（无）' }}</div>
      <h4>输出（前 3000 字）<a class="link tiny" style="margin-left:8px" @click="copyText(call.output_preview || '').then(() => ui.toast('已复制'))">复制</a></h4>
      <div class="code-block">{{ call.output_preview || '（无）' }}</div>
    </template>
  </TgDrawer>
</template>

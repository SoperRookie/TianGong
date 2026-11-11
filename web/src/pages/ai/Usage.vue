<script setup>
/* 用量统计与预算（11.13 / 11.9）：按模型 / 用途 / 日汇总调用数、token、估算费用、失败率；纯 CSS 条形图。 */
import { ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { num, pct, fmtDate } from '@/utils/format'
import { PageHeading, TgPanel, TgSeg, TgStat, TgEmpty, TgIcon } from '@/components/ui'
import { usePrices } from './components/usePrices'

const route = useRoute(), router = useRouter(), scope = useScopeStore(), auth = useAuthStore()
const { load: loadPrices, costOf } = usePrices()
const days = ref(route.query.days != null ? String(route.query.days) : '30')
watch(days, v => router.replace({ query: { ...route.query, days: v !== '30' ? v : undefined } }))

const { data, loading, error, reload } = useLoad(
  () => api.get('/api/v1/ai/stats', { days: days.value, project: scope.project || undefined }),
  { deps: [() => scope.project, days] },
)
const byModel = computed(() => [...(data.value?.by_model || [])].sort((a, b) => b.calls - a.calls))
const byPurpose = computed(() => data.value?.by_purpose || [])
const totalCalls = computed(() => byModel.value.reduce((s, m) => s + m.calls, 0))
const totalTokens = computed(() => byModel.value.reduce((s, m) => s + m.tokens, 0))
const totalErrors = computed(() => byPurpose.value.reduce((s, p) => s + p.errors, 0))
const maxModel = (k) => Math.max(1, ...byModel.value.map(m => m[k] || 0))
const maxPurpose = (k) => Math.max(1, ...byPurpose.value.map(p => p[k] || 0))
const usd = v => '$' + (v || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const M = v => (v / 1e6).toFixed(2) + 'M'

/* 按日：后端 stats 没有按日汇总，这里用调用日志抽样（范围内最近最多 1000 条）按日累计；样本不全时标注 */
const daily = ref([]), dailyTotal = ref(0), dailyLoading = ref(false)
async function loadDaily() {
  dailyLoading.value = true
  try {
    await loadPrices()
    const rows = []
    let total = 0
    for (let page = 1; page <= 5; page++) {
      const r = await api.get('/api/v1/ai/calls', { days: days.value, project: scope.project || undefined, page, page_size: 200 })
      total = r.total; rows.push(...(r.items || []))
      if (rows.length >= r.total) break
    }
    dailyTotal.value = total
    const map = {}
    rows.forEach(c => {
      const d = fmtDate(c.at)
      const x = (map[d] ||= { date: d, calls: 0, tokens: 0, errors: 0, cost: 0 })
      x.calls++; x.tokens += (c.prompt_tokens || 0) + (c.completion_tokens || 0); if (c.status === 'error') x.errors++
      x.cost += costOf(c.model, c.prompt_tokens, c.completion_tokens) || 0
    })
    daily.value = Object.values(map).sort((a, b) => a.date.localeCompare(b.date))
  } catch { daily.value = [] } finally { dailyLoading.value = false }
}
watch([days, () => scope.project], loadDaily, { immediate: true })
const maxDaily = computed(() => Math.max(1, ...daily.value.map(d => d.calls)))
const sampled = computed(() => daily.value.reduce((s, d) => s + d.calls, 0))
</script>
<template>
  <PageHeading title="用量统计与预算" sub="按模型 / 用途汇总调用次数、token、估算费用（单价来自 config/models.yaml，美元 / 百万 token；口径为估算，非官方账单）；管理员看当月预算余额。">
    <template #actions><router-link v-if="auth.isAdmin" class="btn" :to="{ name: 'models' }">模型配置</router-link><router-link class="btn" :to="{ name: 'calls' }">调用日志</router-link><button type="button" class="btn" @click="reload()"><TgIcon name="refresh" :size="14" /> 刷新</button></template>
  </PageHeading>
  <div v-if="error" class="err-box">{{ error.message }}</div>
  <div class="row mb"><TgSeg v-model="days" :items="[['7', '近 7 天'], ['30', '近 30 天'], ['90', '近 90 天'], ['0', '全部']]" /><span class="tiny muted">{{ scope.project ? `范围：${scope.label}` : '范围：全部可见项目' }}</span><span v-if="data?.unpriced_models?.length" class="tiny" style="color:#d68a28">以下模型未配置单价，费用未计入：{{ data.unpriced_models.join('、') }}</span></div>
  <div class="stat-grid">
    <TgStat label="调用次数" :value="num(totalCalls)" :foot="`失败 ${num(totalErrors)} · 失败率 ${pct(totalErrors, totalCalls, 1)}`" icon="spark" color="blue" />
    <TgStat label="Token 消耗" :value="M(totalTokens)" :foot="`输入 ${M(byModel.reduce((s, m) => s + m.prompt_tokens, 0))} · 输出 ${M(byModel.reduce((s, m) => s + m.completion_tokens, 0))}`" icon="layers" color="purple" />
    <TgStat label="估算费用" :value="usd(data?.cost_usd)" foot="所选时间范围 · 按当前单价表" icon="chart" color="green" />
    <TgStat v-if="data?.budget" :label="`本月预算余额（${data.budget.month}）`" :value="data.budget.monthly_budget_usd ? usd(data.budget.remaining_usd) : '未设预算'" :foot="`已消费 ${usd(data.budget.spent_usd)}${data.budget.monthly_budget_usd ? ` / 预算 ${usd(data.budget.monthly_budget_usd)}（${Math.round((data.budget.used_ratio || 0) * 100)}%）` : ' · 在「模型配置」设置月预算'}`" icon="alert" :color="data.budget.remaining_usd != null && data.budget.remaining_usd < 0 ? 'red' : 'orange'" />
    <TgStat v-else label="失败调用" :value="num(totalErrors)" foot="按用途统计" icon="alert" color="red" />
  </div>
  <div class="grid-2">
    <TgPanel :title="`按日调用（${daily.length} 天）`" :hint="dailyTotal > sampled ? `按调用日志抽样：范围内 ${num(dailyTotal)} 条，统计最近 ${num(sampled)} 条` : '来自调用日志逐条累计；悬停看 token 与估算费用'">
      <div v-if="dailyLoading" class="page-loading"><span class="spinner" /> 统计中…</div>
      <template v-else-if="daily.length">
        <div class="chart"><div v-for="d in daily" :key="d.date" :style="{ height: Math.max(3, d.calls / maxDaily * 100) + '%', background: d.errors ? '#f0a03d' : undefined }" :title="`${d.date} · ${num(d.calls)} 次（失败 ${d.errors}）· ${M(d.tokens)} token · ${usd(d.cost)}`"><i>{{ d.date.slice(5) }}</i></div></div>
        <div style="height:22px" />
        <div class="tiny muted">橙色为当日有失败调用</div>
      </template>
      <TgEmpty v-else title="范围内没有调用" />
    </TgPanel>
    <TgPanel title="按模型" hint="缓存命中含在输入内按折扣计费；推理 token 含在输出内">
      <div class="table-wrap"><table class="table dense">
        <thead><tr><th>模型</th><th>调用</th><th>输入 / 输出 token</th><th>其中缓存 / 推理</th><th>平均耗时</th><th>估算费用</th></tr></thead>
        <tbody>
          <tr v-for="m in byModel" :key="m.model">
            <td><b>{{ m.model }}</b></td>
            <td>{{ num(m.calls) }}<div class="bar" style="width:70px"><span :style="{ width: m.calls / maxModel('calls') * 100 + '%' }" /></div></td>
            <td>{{ M(m.prompt_tokens) }} / {{ M(m.completion_tokens) }}</td>
            <td class="muted">{{ M(m.cached_tokens || 0) }} / {{ M(m.reasoning_tokens || 0) }}</td>
            <td>{{ m.calls ? Math.round(m.elapsed_ms / m.calls) : 0 }} ms</td>
            <td><template v-if="m.priced">{{ usd(m.cost_usd) }}<div class="bar" style="width:70px"><span :style="{ width: (m.cost_usd || 0) / Math.max(0.0001, data?.cost_usd || 0) * 100 + '%', background: '#23a783' }" /></div></template><span v-else class="muted" title="未配置单价">—</span></td>
          </tr>
          <tr v-if="!byModel.length"><td colspan="6"><TgEmpty :title="loading ? '加载中…' : '范围内没有调用'" /></td></tr>
        </tbody>
      </table></div>
    </TgPanel>
  </div>
  <TgPanel class="mt" title="按用途" hint="用例生成与评审占大头：v2.1.4 起只带本模块章节原文、评审前缀可缓存">
    <div class="table-wrap"><table class="table dense">
      <thead><tr><th>用途</th><th style="width:220px">调用</th><th style="width:100px">失败</th><th style="width:200px">失败率</th></tr></thead>
      <tbody>
        <tr v-for="p in byPurpose" :key="p.purpose">
          <td><b>{{ p.purpose }}</b></td>
          <td><div class="row"><span style="width:60px">{{ num(p.calls) }}</span><div class="bar" style="flex:1"><span :style="{ width: p.calls / maxPurpose('calls') * 100 + '%' }" /></div></div></td>
          <td><span v-if="p.errors" style="color:#db5361;font-weight:700">{{ num(p.errors) }}</span><span v-else>0</span></td>
          <td><div class="row"><span style="width:50px">{{ pct(p.errors, p.calls, 1) }}</span><div class="bar" style="flex:1"><span :style="{ width: (p.calls ? p.errors / p.calls * 100 : 0) + '%', background: '#db5361' }" /></div></div></td>
        </tr>
        <tr v-if="!byPurpose.length"><td colspan="4"><TgEmpty :title="loading ? '加载中…' : '范围内没有调用'" /></td></tr>
      </tbody>
    </table></div>
  </TgPanel>
</template>

<script setup>
/* 我的最近操作（3.17 / 2.17）：GET /audit?user=我&days=&kind=&keyword=&failed_only=&page=&page_size=；工作台 recent_ops（最近 10 条成功操作）作为顶部快览。
   点击行按对象跳转：需求 / 任务 / 计划 / 项目。 */
import { ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useAuthStore } from '@/stores/auth'
import { useScopeStore } from '@/stores/scope'
import { fmtTime, fmtAgo, num } from '@/utils/format'
import { PageHeading, TgPanel, TgTable, TgBadge, TgSeg, TgPager, TgIcon } from '@/components/ui'
const route = useRoute(), router = useRouter(), auth = useAuthStore(), scope = useScopeStore()
const days = ref(String(route.query.days ?? 30)), kind = ref(route.query.kind || ''), kw = ref(route.query.q || ''), failed = ref(route.query.failed === '1'), inScope = ref(route.query.scope === '1')
const page = ref(Number(route.query.page || 1)), size = ref(50)
const DAYS = [['7', '近 7 天'], ['30', '近 30 天'], ['90', '近 90 天'], ['0', '全部']]
watch([days, kind, kw, failed, inScope], () => { page.value = 1 })
watch([days, kind, kw, failed, inScope, page], () => router.replace({ query: { days: days.value === '30' ? undefined : days.value, kind: kind.value || undefined, q: kw.value || undefined, failed: failed.value ? '1' : undefined, scope: inScope.value ? '1' : undefined, page: page.value > 1 ? String(page.value) : undefined } }))
const { data, loading, error, reload } = useLoad(() => api.get('/api/v1/audit', { user: auth.me, days: Number(days.value), kind: kind.value, keyword: kw.value.trim(), failed_only: failed.value || undefined, project: inScope.value ? scope.project : undefined, page: page.value, page_size: size.value }), { deps: [days, kind, () => kw.value.trim(), failed, inScope, page, size, () => inScope.value && scope.project] })
const { data: wb } = useLoad(() => api.get('/api/v1/workbench'))
const kinds = computed(() => { const kc = data.value?.kind_counts || {}, all = data.value?.kinds || {}; return [{ key: '', label: '全部', count: Object.values(kc).reduce((s, n) => s + n, 0) }, ...Object.entries(all).filter(([k]) => kc[k]).map(([k, l]) => ({ key: k, label: l, count: kc[k] }))] })
const cols = [{ key: 'at', title: '时间', w: '130px' }, { key: 'action', title: '动作' }, { key: 'project', title: '项目', w: '140px' }, { key: 'target', title: '对象', w: '150px' }, { key: 'status', title: '结果', w: '80px' }, { key: 'duration_ms', title: '耗时', w: '70px' }, { key: 'detail', title: '明细' }]
/** 按路径推断可跳转的对象 */
function linkOf(a) {
  const p = a.path || ''
  let m
  if ((m = /^\/api\/v1\/requirements\/([^/?]+)/.exec(p))) return { name: 'req-detail', params: { id: decodeURIComponent(m[1]) } }
  if ((m = /^\/api\/v1\/tasks\/([^/?]+)/.exec(p))) return { name: 'design-detail', params: { id: decodeURIComponent(m[1]) } }
  if ((m = /^\/api\/v1\/plans\/([^/?]+)/.exec(p))) return { name: 'plan-detail', params: { id: decodeURIComponent(m[1]) } }
  if ((m = /^\/api\/v1\/projects\/([^/?]+)/.exec(p)) && a.action !== '删除项目') return { name: 'project-detail', params: { name: decodeURIComponent(m[1]) } }
  if (a.kind === 'prompt' && a.target) return { name: 'prompt-detail', params: { key: a.target } }
  if (a.project) return { name: 'project-detail', params: { name: a.project } }
  return null
}
function go(a) { const to = linkOf(a); if (to) router.push(to) }
</script>
<template>
  <PageHeading title="最近操作" :sub="`${auth.displayName} 的写操作记录（来自操作日志）；点击行跳到对象。`">
    <template #actions><button type="button" class="btn" @click="reload()"><TgIcon name="refresh" :size="14" /> 刷新</button><router-link v-if="auth.isAdmin" class="btn" :to="{ name: 'audit' }">全部操作日志 ›</router-link></template>
  </PageHeading>
  <div v-if="error" class="err-box mb">{{ error.message }}</div>
  <TgPanel v-if="(wb?.recent_ops || []).length" class="mb" title="最近 10 条成功操作" hint="工作台聚合接口的 recent_ops">
    <div class="timeline">
      <div v-for="(op, i) in wb.recent_ops" :key="i" class="entry clickable" @click="go(op)"><small>{{ fmtAgo(op.at) }} · {{ op.kind_label || op.kind }}</small>{{ op.action }}<span v-if="op.target" class="muted mono"> · {{ op.target }}</span><span v-if="op.project" class="tag" style="margin-left:6px">{{ op.project }}</span><span v-if="op.detail" class="muted"> · {{ op.detail }}</span></div>
    </div>
  </TgPanel>
  <TgPanel :pad="false">
    <div class="filters panel-pad" style="padding-bottom:10px">
      <TgSeg v-model="days" :items="DAYS" />
      <select v-model="kind" class="select"><option v-for="k in kinds" :key="k.key" :value="k.key">{{ k.label }}（{{ k.count }}）</option></select>
      <input v-model="kw" class="field" style="width:220px" placeholder="搜索动作 / 对象 / 明细 / 项目" />
      <label class="tiny muted row" style="gap:4px;cursor:pointer"><input v-model="failed" type="checkbox" /> 只看失败（≥ 400）</label>
      <label v-if="scope.project" class="tiny muted row" style="gap:4px;cursor:pointer"><input v-model="inScope" type="checkbox" /> 只看「{{ scope.project }}」</label>
      <span class="grow" />
      <span class="muted tiny">共 {{ num(data?.total || 0) }} 条</span>
    </div>
    <TgTable :cols="cols" :rows="data?.items || []" row-key="id" :loading="loading" clickable empty="没有操作记录" @row-click="go">
      <template #cell-at="{ row }"><span :title="fmtTime(row.at, true)">{{ fmtTime(row.at) }}</span></template>
      <template #cell-action="{ row }"><b>{{ row.action }}</b> <span class="tag">{{ row.kind_label || row.kind }}</span><span v-if="row.security" class="badge gray" style="margin-left:4px">安全</span><div class="sub mono">{{ row.method }} {{ row.path }}</div></template>
      <template #cell-project="{ row }">{{ row.project || '—' }}</template>
      <template #cell-target="{ row }"><span class="mono">{{ row.target || '—' }}</span></template>
      <template #cell-status="{ row }"><TgBadge :status="row.status >= 400 ? 'error' : 'ok'" :text="`HTTP ${row.status}`" /></template>
      <template #cell-duration_ms="{ row }"><span class="muted">{{ num(row.duration_ms) }} ms</span></template>
      <template #cell-detail="{ row }"><span class="muted">{{ row.detail || '' }}</span></template>
    </TgTable>
    <TgPager v-model:page="page" v-model:size="size" :total="data?.total || 0" :sizes="[20, 50, 100, 200]" />
  </TgPanel>
</template>

<script setup>
/* 操作日志（2.16～2.18）：按项目 / 类型 / 操作人 / 关键字 / 时间范围 / 只看失败 / 安全日志筛选分页；行点击展开明细。 */
import { ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { fmtTime, num } from '@/utils/format'
import { PageHeading, TgPanel, TgBadge, TgSeg, TgPager, TgEmpty, TgIcon, TgStat } from '@/components/ui'

const route = useRoute(), router = useRouter(), scope = useScopeStore(), auth = useAuthStore()
const q = route.query
const view = ref(q.view || 'all')   // all / failed / security
const kind = ref(q.kind || ''), user = ref(q.user || ''), keyword = ref(q.keyword || ''), kw = ref(keyword.value), days = ref(q.days != null ? String(q.days) : '30'), project = ref(q.project || '')
const page = ref(+q.page || 1), size = ref(+q.page_size || 50)
const expanded = ref(null)
watch([view, kind, user, keyword, days, project, size, () => scope.project], () => { page.value = 1 })
watch([view, kind, user, keyword, days, project, page, size], () => router.replace({ query: { view: view.value !== 'all' ? view.value : undefined, kind: kind.value || undefined, user: user.value || undefined, keyword: keyword.value || undefined, days: days.value !== '30' ? days.value : undefined, project: project.value || undefined, page: page.value > 1 ? page.value : undefined, page_size: size.value !== 50 ? size.value : undefined } }))

const { data, loading, error, reload } = useLoad(
  () => api.get('/api/v1/audit', { project: scope.project || project.value || undefined, kind: kind.value || undefined, user: user.value || undefined, keyword: keyword.value || undefined, days: days.value, security: view.value === 'security' ? true : undefined, failed_only: view.value === 'failed' ? true : undefined, page: page.value, page_size: size.value }),
  { deps: [() => scope.project, view, kind, user, keyword, days, project, page, size] },
)
const items = computed(() => data.value?.items || [])
const KINDS = computed(() => data.value?.kinds || {})
const kc = computed(() => data.value?.kind_counts || {})
const topKinds = computed(() => Object.entries(kc.value).sort((a, b) => b[1] - a[1]).slice(0, 4))
const withCur = (list, cur) => [...new Set([...(list || []), ...(cur ? [cur] : [])])]
</script>
<template>
  <PageHeading title="操作日志" :sub="`写接口按（方法, 路径）映射为中文动作落 audit_log，含操作人、IP、UA、项目、结果、耗时、明细。${data ? `范围内 ${num(data.total)} 条` : ''}；保留 TIANGONG_LOG_RETENTION_DAYS 天（启动及每 24 小时清理，同时清理 AI 调用日志）。${auth.isAdmin ? '' : '非管理员只看所属项目业务日志与自己的操作。'}`">
    <template #actions><button type="button" class="btn" @click="reload()"><TgIcon name="refresh" :size="14" /> 刷新</button></template>
  </PageHeading>
  <div v-if="error" class="err-box">{{ error.message }}</div>
  <div v-if="topKinds.length" class="stat-grid">
    <TgStat v-for="[k, n] in topKinds" :key="k" :label="KINDS[k] || k" :value="num(n)" :foot="kind === k ? '当前筛选' : '点击筛选该类型'" icon="list" :color="kind === k ? 'blue' : 'gray'" @click="kind = kind === k ? '' : k" />
  </div>
  <TgPanel :pad="false">
    <div class="panel-pad filters" style="padding-bottom:10px">
      <TgSeg v-model="view" :items="[['all', '全部'], ['failed', '只看失败'], ...(auth.isAdmin ? [['security', '安全日志']] : [])]" />
      <TgSeg v-model="days" :items="[['1', '今天'], ['7', '近 7 天'], ['30', '近 30 天'], ['90', '近 90 天'], ['0', '全部']]" />
      <select v-model="kind" class="select"><option value="">全部类型</option><option v-for="(l, k) in KINDS" :key="k" :value="k">{{ l }}{{ kc[k] ? `（${kc[k]}）` : '' }}</option></select>
      <select v-model="user" class="select"><option value="">全部操作人</option><option v-for="u in withCur(data?.users, user)" :key="u" :value="u">{{ u }}</option></select>
      <select v-if="!scope.project" v-model="project" class="select"><option value="">全部项目</option><option v-for="p in withCur(data?.projects, project)" :key="p" :value="p">{{ p }}</option></select>
      <input v-model="kw" class="field" style="width:180px" placeholder="动作 / 对象 / 明细 / 路径 / IP" @keyup.enter="keyword = kw" @blur="keyword = kw" />
    </div>
    <div class="table-wrap" :class="{ loading }"><table class="table">
      <thead><tr><th style="width:130px">时间</th><th style="width:110px">操作人</th><th>类型 / 动作</th><th>项目</th><th>对象</th><th>请求</th><th style="width:80px">结果</th><th style="width:70px">耗时</th><th>明细</th></tr></thead>
      <tbody>
        <template v-for="a in items" :key="a.id">
          <tr class="row-click" :class="{ sel: expanded === a.id }" @click="expanded = expanded === a.id ? null : a.id">
            <td class="nowrap">{{ fmtTime(a.at, true) }}</td>
            <td><b>{{ a.user || '—' }}</b><div class="sub mono">{{ a.ip }}</div></td>
            <td><span class="tag" :class="{ ai: a.security }">{{ a.kind_label || KINDS[a.kind] || a.kind }}</span> {{ a.action }}</td>
            <td class="small">{{ a.project || '—' }}</td>
            <td><span class="mono tiny">{{ a.target || '—' }}</span></td>
            <td><span class="mono tiny">{{ a.method }} {{ a.path }}</span></td>
            <td><TgBadge :status="a.status >= 400 ? 'error' : 'ok'" :text="'HTTP ' + a.status" /></td>
            <td class="small">{{ num(a.duration_ms) }} ms</td>
            <td><span class="muted small ellipsis" style="max-width:260px;display:inline-block" :title="a.detail">{{ a.detail || '' }}</span></td>
          </tr>
          <tr v-if="expanded === a.id" class="expand-row"><td colspan="9">
            <dl class="kv"><dt>ID</dt><dd>{{ a.id }}</dd><dt>时间</dt><dd>{{ fmtTime(a.at, true) }}</dd><dt>操作人 / IP</dt><dd>{{ a.user }} · {{ a.ip }}</dd><dt>UA</dt><dd class="mono tiny">{{ a.ua || '—' }}</dd><dt>请求</dt><dd class="mono">{{ a.method }} {{ a.path }}</dd><dt>对象</dt><dd class="mono">{{ a.target || '—' }}</dd><dt>结果</dt><dd>HTTP {{ a.status }} · {{ num(a.duration_ms) }} ms{{ a.security ? ' · 安全日志' : '' }}</dd><dt>明细</dt><dd class="pre" style="max-height:200px">{{ a.detail || '—' }}</dd></dl>
          </td></tr>
        </template>
        <tr v-if="!items.length"><td colspan="9"><TgEmpty :title="loading ? '加载中…' : '没有日志'" /></td></tr>
      </tbody>
    </table></div>
    <TgPager v-model:page="page" v-model:size="size" :total="data?.total || 0" :sizes="[20, 50, 100, 200]" />
  </TgPanel>
</template>
<style scoped>
.expand-row td { background: #f8fafd; padding: 12px 16px; }
.expand-row .kv { grid-template-columns: 90px 1fr; }
</style>

<script setup>
/* 项目报表（3.19）：GET /reports/summary?days=&project=；项目取顶栏工作范围（全局筛选），时间窗口 7 / 30 / 90 / 全部进 URL。
   指标卡、趋势柱图、分布分段条、执行情况、人员统计、AI 质量、项目产出 Top、成员产出；纯留痕统计，不调模型。 */
import { ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useScopeStore } from '@/stores/scope'
import { num } from '@/utils/format'
import SegBar from './components/SegBar.vue'
import HBarList from './components/HBarList.vue'
import TrendChart from './components/TrendChart.vue'
import { PageHeading, TgPanel, TgSeg, TgIcon, TgEmpty } from '@/components/ui'
const route = useRoute(), router = useRouter(), scope = useScopeStore()
const days = ref(['7', '30', '90', '0'].includes(String(route.query.days)) ? String(route.query.days) : '30')
const DAYS = [['7', '近 7 天'], ['30', '近 30 天'], ['90', '近 90 天'], ['0', '全部']]
watch(days, v => router.replace({ query: { ...route.query, days: v === '30' ? undefined : v } }))
const { data: d, loading, error, reload } = useLoad(() => api.get('/api/v1/reports/summary', { days: Number(days.value), project: scope.project || undefined }), { deps: [days, () => scope.project] })
const pct = v => v == null ? '—' : (v * 100).toFixed(1) + '%'
const t = computed(() => d.value?.totals || {}), ex = computed(() => d.value?.execution || {}), q = computed(() => d.value?.ai_quality || null)
const reviewTotal = computed(() => { const a = d.value?.review_actions || {}; return (a.approve || 0) + (a.modify || 0) + (a.reject || 0) + (a.delete || 0) })
const adoptionNote = computed(() => t.value.adoption_rate == null ? '离线终稿口径 · 暂无样本' : `离线终稿口径 · ${t.value.adoption_samples} 个样本 · ${t.value.adoption_rate >= 0.5 ? '达标' : '未达标（线 50%）'}`)
const scopeNote = computed(() => scope.project ? `项目「${scope.project}」` : (scope.line ? `产品线「${scope.line}」下全部可见项目（报表接口按可见项目汇总，不按产品线细分）` : '全部可见项目'))
</script>
<template>
  <PageHeading title="项目报表" :sub="`${scopeNote} · 任务与用例产出、一次通过率、采纳率、审核动作、执行情况、AI 质量；项目概览 KPI 同源。切换顶栏工作范围即切换项目。`">
    <template #actions><TgSeg v-model="days" :items="DAYS" /><button type="button" class="btn" @click="reload()"><TgIcon name="refresh" :size="14" /> 刷新</button></template>
  </PageHeading>
  <div v-if="error" class="err-box mb">{{ error.message }}</div>
  <div v-if="loading && !d" class="page-loading">加载中…</div>
  <template v-else-if="d">
    <div class="stat-grid five">
      <div class="panel stat"><div class="row"><div><div class="stat-label">任务</div><div class="stat-num">{{ num(t.tasks) }}</div></div><div class="stat-icon blue"><TgIcon name="layers" :size="18" /></div></div><div class="stat-foot"><span>AI 定点修改 {{ t.fix_runs || 0 }} 次</span></div></div>
      <div class="panel stat"><div class="row"><div><div class="stat-label">生成用例</div><div class="stat-num">{{ num(t.cases) }}</div></div><div class="stat-icon purple"><TgIcon name="file" :size="18" /></div></div><div class="stat-foot"><span>平均 AI 评审 {{ t.avg_review_rounds ?? '—' }} 轮</span></div></div>
      <div class="panel stat"><div class="row"><div><div class="stat-label">AI 一次通过率</div><div class="stat-num">{{ pct(t.ai_pass_rate) }}</div></div><div class="stat-icon green"><TgIcon name="check" :size="18" /></div></div><div class="stat-foot"><span>评审回环内收敛占比</span></div></div>
      <div class="panel stat"><div class="row"><div><div class="stat-label">完全采纳率</div><div class="stat-num">{{ pct(t.adoption_rate) }}</div></div><div class="stat-icon orange"><TgIcon name="upload" :size="18" /></div></div><div class="stat-foot"><span>{{ adoptionNote }}</span></div></div>
      <div class="panel stat"><div class="row"><div><div class="stat-label">学习规则</div><div class="stat-num">{{ d.rules?.active ?? 0 }} <small class="muted" style="font-size:13px;font-weight:550">生效</small></div></div><div class="stat-icon blue"><TgIcon name="spark" :size="18" /></div></div><div class="stat-foot"><span>候选待确认 {{ d.rules?.candidates ?? 0 }} 条</span><router-link class="link" :to="{ name: 'rules' }">规则库 →</router-link></div></div>
    </div>
    <TgPanel title="用例产出趋势" hint="按任务创建日汇总生成用例数；悬停看日期与任务数"><TrendChart :trend="d.trend || []" /></TgPanel>
    <div class="grid-4 mt">
      <TgPanel title="用例优先级分布"><SegBar :entries="[['P0', d.priority?.P0, 'p0'], ['P1', d.priority?.P1, 'p1'], ['P2', d.priority?.P2, 'p2'], ['P3', d.priority?.P3, 'p3']]" /></TgPanel>
      <TgPanel title="任务状态"><SegBar :entries="[['完成', d.status?.completed, 'green'], ['待确认测试点', d.status?.awaiting_confirmation, 'purple'], ['进行中', (d.status?.running || 0) + (d.status?.queued || 0), 'orange'], ['失败', d.status?.failed, 'red']]" /></TgPanel>
      <TgPanel title="用例审核状态"><SegBar :entries="[['已通过', d.case_states?.approved, 'green'], ['未审核', d.case_states?.pending, 'orange'], ['已驳回', d.case_states?.rejected, 'red']]" /></TgPanel>
      <TgPanel title="人工审核动作" :hint="`合计 ${reviewTotal}`"><SegBar :entries="[['通过', d.review_actions?.approve, 'green'], ['修改', d.review_actions?.modify, 'blue'], ['驳回', d.review_actions?.reject, 'red'], ['删除', d.review_actions?.delete, 'gray']]" /></TgPanel>
    </div>
    <TgPanel class="mt" title="用例执行情况" hint="测试计划执行口径，随时间窗口 / 项目过滤；执行总次数含同轮复测">
      <div class="grid-3 mb">
        <div class="kpi"><div class="stat-label">已执行用例</div><div class="stat-num">{{ num(ex.executed_cases) }}</div><div class="muted tiny">{{ ex.plans || 0 }} 个计划 · {{ ex.runs || 0 }} 个执行轮次</div></div>
        <div class="kpi"><div class="stat-label">执行总次数</div><div class="stat-num">{{ num(ex.executions) }}</div><div class="muted tiny">含同轮复测</div></div>
        <div class="kpi"><div class="stat-label">执行通过率</div><div class="stat-num">{{ pct(ex.pass_rate) }}</div><div class="muted tiny">失败 {{ ex.status?.fail || 0 }} · 阻塞 {{ ex.status?.blocked || 0 }}</div></div>
      </div>
      <div class="grid-2" style="grid-template-columns:1fr 1fr">
        <div><div class="panel-title mb">执行结果分布</div><SegBar :entries="[['通过', ex.status?.pass, 'green'], ['失败', ex.status?.fail, 'red'], ['阻塞', ex.status?.blocked, 'orange'], ['跳过', ex.status?.skipped, 'gray']]" /></div>
        <div><div class="panel-title mb">失败分类分布 <span class="muted tiny" style="font-weight:400">用例问题类是提示词优化信号</span></div><HBarList :rows="ex.fail_reasons || []" label-key="reason" value-key="count" unit="次" /></div>
      </div>
      <template v-if="(ex.people || []).length">
        <div class="panel-title mt mb">执行人员统计 <span class="muted tiny" style="font-weight:400">按实际执行人；代执行计入执行人并单独列出</span></div>
        <table class="table dense">
          <thead><tr><th>执行人</th><th>执行次数</th><th>用例数</th><th>通过</th><th>失败</th><th>阻塞</th><th>跳过</th><th>通过率</th><th>代执行</th></tr></thead>
          <tbody><tr v-for="x in ex.people" :key="x.username"><td><b>{{ x.username }}</b></td><td>{{ x.executions }}</td><td>{{ x.cases }}</td><td>{{ x.pass }}</td><td><b v-if="x.fail" style="color:var(--red)">{{ x.fail }}</b><template v-else>0</template></td><td>{{ x.blocked }}</td><td>{{ x.skipped }}</td><td>{{ pct(x.pass_rate) }}</td><td class="muted">{{ x.proxied || 0 }}</td></tr></tbody>
        </table>
      </template>
    </TgPanel>
    <TgPanel v-if="q" class="mt" title="AI 质量统计" hint="AI 产出 → 人工评审留痕口径；直接通过 = 未经驳回或修改即通过；驳回率 > 30% 标红">
      <div class="grid-2" style="grid-template-columns:1fr 1fr">
        <table v-for="[label, s] in [['测试点', q.point], ['用例', q.case]]" :key="label" class="table dense">
          <thead><tr><th>{{ label }}</th><th>AI 产出</th><th>采纳率</th><th>直接通过率</th><th>修改率</th><th>驳回率</th></tr></thead>
          <tbody><tr><td class="muted">通过 {{ s.approved }} · 修改 {{ s.modified }} · 驳回 {{ s.rejected }} · 删除 {{ s.deleted }}</td><td><b>{{ s.total }}</b></td><td><b>{{ pct(s.adoption_rate) }}</b></td><td>{{ pct(s.direct_pass_rate) }}</td><td>{{ pct(s.modify_rate) }}</td><td><b v-if="s.reject_rate != null && s.reject_rate > 0.3" style="color:var(--red)">{{ pct(s.reject_rate) }}</b><template v-else>{{ pct(s.reject_rate) }}</template></td></tr></tbody>
        </table>
      </div>
      <div class="grid-2 mt" style="grid-template-columns:1fr 1fr">
        <div><div class="panel-title mb">驳回类型分布</div><HBarList :rows="q.reject_types || []" label-key="type" value-key="count" unit="次" /></div>
        <div><div class="panel-title mb">驳回原因 Top</div><HBarList :rows="q.reject_reasons || []" label-key="reason" value-key="count" unit="次" /></div>
      </div>
    </TgPanel>
    <div class="grid-2 mt" style="grid-template-columns:1fr 1fr">
      <TgPanel title="项目产出 Top" hint="按生成用例数倒序，前 10"><HBarList :rows="d.projects || []" label-key="project" value-key="cases" unit="条" /></TgPanel>
      <TgPanel title="成员产出" hint="按任务数倒序，前 10" :pad="false">
        <table class="table dense">
          <thead><tr><th>成员</th><th>任务</th><th>生成用例</th><th>审核动作</th></tr></thead>
          <tbody><tr v-for="c in d.creators || []" :key="c.user"><td><b>{{ c.user }}</b></td><td>{{ c.tasks }}</td><td>{{ c.cases }}</td><td>{{ c.reviews }}</td></tr><tr v-if="!(d.creators || []).length"><td colspan="4"><TgEmpty title="暂无数据" /></td></tr></tbody>
        </table>
      </TgPanel>
    </div>
  </template>
</template>
<style scoped>
.stat-grid.five { grid-template-columns: repeat(5, minmax(0, 1fr)); }
.stat { cursor: default; }
.kpi { border: 1px solid var(--line-2); border-radius: 4px; padding: 12px 14px; }
@media (max-width: 1280px) { .stat-grid.five { grid-template-columns: repeat(3, 1fr); } }
</style>

<script setup>
/* 项目概览（3.7）：用例生命周期管道（来自项目列表行统计）、KPI（reports/summary?days=0&project=）、最近任务、需求状态与待确认事项、进行中的计划、项目信息与快捷入口。 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useAuthStore } from '@/stores/auth'
import { useScopeStore } from '@/stores/scope'
import { fmtAgo, fmtTime, num } from '@/utils/format'
import { roleLabel } from './useProjectActions'
import { TgPanel, TgBadge, TgKv, TgEmpty, TgPrio } from '@/components/ui'
const props = defineProps({ project: { type: String, required: true }, row: { type: Object, default: () => ({}) }, detail: { type: Object, default: null } })
const emit = defineEmits(['tab'])
const router = useRouter(), auth = useAuthStore(), scope = useScopeStore()
const { data: kpi } = useLoad(() => api.get('/api/v1/reports/summary', { days: 0, project: props.project }), { deps: [() => props.project] })
const { data: tasks } = useLoad(() => api.get('/api/v1/tasks', { project: props.project, limit: 6 }), { deps: [() => props.project] })
const { data: reqs } = useLoad(() => api.get('/api/v1/requirements', { project: props.project }), { deps: [() => props.project] })
const { data: plans } = useLoad(() => api.get('/api/v1/plans', { project: props.project }), { deps: [() => props.project] })
const r = computed(() => props.row || {})
const failCnt = computed(() => (r.value.executed || 0) - (r.value.exec_pass || 0))
const stages = computed(() => [
  ['AI 生成', r.value.cases, '拆解确认后生成', ''], ['审核中', r.value.pending, '待人工审核', r.value.pending ? 'warn' : ''],
  ['已驳回', r.value.rejected, '待 AI 定点修改', r.value.rejected ? 'bad' : ''], ['正式用例', r.value.approved, '通过并锁定', r.value.approved ? 'ok' : ''],
  ['已执行', r.value.executed, '任务级旧执行轮次', ''], ['执行通过', r.value.exec_pass, failCnt.value ? `失败 / 阻塞 ${failCnt.value}` : '—', failCnt.value ? 'bad' : (r.value.exec_pass ? 'ok' : '')],
])
const reqStat = computed(() => { const m = {}; (reqs.value?.requirements || []).forEach(q => { m[q.status] = (m[q.status] || 0) + 1 }); return m })
const openQ = computed(() => (reqs.value?.requirements || []).reduce((s, q) => s + (q.open_questions || 0), 0))
const activePlans = computed(() => (plans.value?.plans || []).filter(p => p.status !== 'archived' && p.status !== 'done'))
const ratio = v => v == null ? '—' : (v * 100).toFixed(1) + '%'
const reviewTotal = computed(() => { const a = kpi.value?.review_actions || {}; return (a.approve || 0) + (a.modify || 0) + (a.reject || 0) + (a.delete || 0) })
const canWrite = computed(() => auth.can(props.project, 'requirement.edit') && r.value.status !== 'archived')
const title = t => t.requirement_title || (t.sources || []).join('、') || t.task_id
/** 进入项目内的列表页前先把该项目设为工作范围 */
function go(name, query) { if (scope.project !== props.project) scope.setProject(props.project); router.push({ name, query }) }
</script>
<template>
  <div class="grid-2">
    <div class="stack">
      <TgPanel title="用例生命周期" hint="生成 → 审核 → 正式 → 执行 → 反哺；数据来自项目列表行统计">
        <div class="pipe">
          <div v-for="[lab, n, sub, cls] in stages" :key="lab" class="stage" :class="cls"><div class="lab">{{ lab }}</div><div class="n">{{ num(n) }}</div><div class="sub">{{ sub }}</div></div>
        </div>
        <div class="muted tiny mt">闭环：执行失败回归缺陷修复后复测；人工修改留痕进入「学习规则」反哺下一次生成；需求变更只更新受影响用例并回到审核。</div>
      </TgPanel>
      <div class="stat-grid four" style="margin-bottom:0">
        <div class="panel stat"><div class="stat-label">AI 一次通过率</div><div class="stat-num">{{ ratio(kpi?.totals?.ai_pass_rate) }}</div><div class="stat-foot"><span>平均评审 {{ kpi?.totals?.avg_review_rounds ?? '—' }} 轮</span></div></div>
        <div class="panel stat"><div class="stat-label">完全采纳率</div><div class="stat-num">{{ ratio(kpi?.totals?.adoption_rate) }}</div><div class="stat-foot"><span>离线终稿口径 · {{ kpi?.totals?.adoption_samples ?? 0 }} 样本</span></div></div>
        <div class="panel stat"><div class="stat-label">优先级分布</div><div class="row" style="margin-top:8px;gap:6px"><span v-for="p in ['P0', 'P1', 'P2', 'P3']" :key="p" class="row" style="gap:3px"><TgPrio :p="p" /> <b>{{ kpi?.priority?.[p] ?? 0 }}</b></span></div><div class="stat-foot"><span>全部历史用例</span></div></div>
        <div class="panel stat"><div class="stat-label">人工审核动作</div><div class="stat-num">{{ reviewTotal }}</div><div class="stat-foot"><span>通过 {{ kpi?.review_actions?.approve ?? 0 }} · 修改 {{ kpi?.review_actions?.modify ?? 0 }} · 驳回 {{ kpi?.review_actions?.reject ?? 0 }}</span></div></div>
      </div>
      <TgPanel title="最近任务" :hint="`全部 ${r.tasks || 0} 个`">
        <template #actions><button type="button" class="btn small ghost" @click="go('designs')">设计任务 ›</button></template>
        <table v-if="(tasks?.tasks || []).length" class="table dense">
          <thead><tr><th style="width:100px">状态</th><th>任务</th><th style="width:70px">用例</th><th style="width:90px">创建人</th><th style="width:90px">时间</th></tr></thead>
          <tbody>
            <tr v-for="t in tasks.tasks" :key="t.task_id" class="row-click" @click="router.push({ name: 'design-detail', params: { id: t.task_id } })">
              <td><TgBadge :status="t.status" /></td>
              <td><b>{{ title(t) }}</b><div class="sub mono">{{ t.task_id }}<span v-if="t.error" style="color:var(--red)"> · {{ t.error }}</span></div></td>
              <td>{{ t.case_count ? `${t.case_count} 条` : '—' }}</td><td class="muted">{{ t.created_by || '—' }}</td><td class="muted">{{ fmtAgo(t.created_at) }}</td>
            </tr>
          </tbody>
        </table>
        <TgEmpty v-else title="还没有任务" hint="从需求发起测试设计后在此出现" />
      </TgPanel>
    </div>
    <div class="stack">
      <TgPanel title="项目信息">
        <template #actions><button v-if="auth.can(project, 'project.edit')" type="button" class="btn small ghost" @click="emit('tab', 'settings')">编辑</button></template>
        <TgKv :pairs="[['编码', r.code || '—'], ['产品线', r.business_line || '未归属'], ['负责人', r.owner || '—'], ['我的角色', roleLabel(detail?.my_role || r.my_role) || '—'], ['成员', `${r.member_count ?? 0} 人`], ['版本 / 模块', `${detail?.versions ?? '—'} / ${detail?.modules ?? '—'}`], ['创建', `${fmtTime(r.created_at)} · ${r.created_by || '—'}`], ['更新', `${fmtTime(r.updated_at)} · ${r.updated_by || '—'}`], ['自动沉淀', r.knowledge_auto === false ? '关闭' : '开启'], ['项目画像', r.profile?.active ? `v${r.profile.version} 已生效${r.profile.draft ? ' · 有草稿待确认' : ''}` : (r.profile?.draft ? '有草稿待确认' : '尚未生效')]]" />
        <div v-if="r.description" class="source-block mt" style="white-space:pre-wrap">{{ r.description }}</div>
      </TgPanel>
      <TgPanel title="需求" :hint="`${reqs?.total ?? (reqs?.requirements || []).length} 条`">
        <template #actions><button type="button" class="btn small ghost" @click="go('requirements')">需求列表 ›</button></template>
        <div class="pill-row">
          <TgBadge v-for="(n, k) in reqStat" :key="k" :status="k">{{ reqs?.statuses?.[k] || k }} {{ n }}</TgBadge>
          <TgBadge v-if="openQ" status="open" :text="`待确认事项 ${openQ}`" />
          <span v-if="!Object.keys(reqStat).length" class="muted">暂无需求</span>
        </div>
      </TgPanel>
      <TgPanel title="测试计划" :hint="`${(plans?.plans || []).length} 个 · 进行中 ${activePlans.length}`">
        <template #actions><button type="button" class="btn small ghost" @click="go('plans')">计划列表 ›</button></template>
        <div v-if="activePlans.length" class="list">
          <div v-for="p in activePlans.slice(0, 4)" :key="p.plan_id" class="clickable" @click="router.push({ name: 'plan-detail', params: { id: p.plan_id } })">
            <b>{{ p.name }}</b> <span class="muted tiny">· {{ p.summary?.cases ?? 0 }} 条 · 已分配 {{ p.summary?.assigned ?? 0 }}<template v-if="p.summary?.latest_run?.pass_rate != null"> · 通过率 {{ (p.summary.latest_run.pass_rate * 100).toFixed(0) }}%</template></span>
          </div>
        </div>
        <span v-else class="muted">没有进行中的计划</span>
      </TgPanel>
      <TgPanel v-if="canWrite" title="快捷入口">
        <div class="row">
          <button type="button" class="btn small" @click="go('requirements', { new: 1 })">＋ 新建需求</button>
          <button type="button" class="btn small" @click="go('plans', { new: 1 })">＋ 新建计划</button>
          <button type="button" class="btn small" @click="emit('tab', 'members')">管理成员</button>
          <button type="button" class="btn small" @click="emit('tab', 'modules')">模块树</button>
        </div>
      </TgPanel>
    </div>
  </div>
</template>
<style scoped>
.pipe { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 8px; }
.stage { border: 1px solid #e7edf7; border-radius: 3px; padding: 10px 10px 8px; background: #fafcff; position: relative; }
.stage:not(:last-child):after { content: '›'; position: absolute; right: -8px; top: 36%; color: #b9c8e8; font-size: 16px; }
.stage .lab { font-size: 11.5px; color: #8390a6; }
.stage .n { font-size: 22px; font-weight: 780; margin: 2px 0; color: #1d2a44; }
.stage .sub { font-size: 10.5px; color: #97a2b3; }
.stage.ok { border-color: #bbe9db; background: #f4fffa; } .stage.ok .n { color: var(--green); }
.stage.warn { border-color: #ffe6c3; background: #fff9f0; } .stage.warn .n { color: var(--amber); }
.stage.bad { border-color: #ffd4d8; background: #fff8f8; } .stage.bad .n { color: var(--red); }
</style>

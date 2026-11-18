<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { fmtAgo, fmtTime } from '@/utils/format'
import { PageHeading, TgStat, TgPanel, TgBadge, TgEmpty, TgIcon } from '@/components/ui'
const router = useRouter(), scope = useScopeStore(), auth = useAuthStore()
const { data: wb, loading, error, reload } = useLoad(() => api.get('/api/v1/workbench'))
const inScope = arr => scope.project ? (arr || []).filter(x => !x.project || x.project === scope.project) : (arr || [])
const review = computed(() => inScope(wb.value?.pending_review?.items))
const fix = computed(() => inScope(wb.value?.to_fix?.items))
const execItems = computed(() => inScope(wb.value?.to_execute?.items))
const reqs = computed(() => inScope(wb.value?.pending_requirements?.items))
const ai = computed(() => inScope(wb.value?.my_ai_tasks?.items))
const steps = [['录入需求', 'requirements'], ['附件解析', 'requirements'], ['整理稿核对', 'requirements'], ['AI 分析与确认', 'questions'], ['测试点审核', 'points'], ['用例审核', 'cases'], ['计划与执行', 'plans']]
const title = t => t.requirement_title || (t.sources || []).join('、') || t.task_id
</script>
<template>
  <PageHeading :title="`你好，${auth.displayName}`" :sub="scope.project ? `当前工作范围：${scope.label}。数字只统计这个项目内的待办。` : '当前显示全部可见项目的待办。'">
    <template #actions><button type="button" class="btn" @click="reload()"><TgIcon name="refresh" :size="14" /> 刷新</button><router-link class="btn primary" :to="{ name: 'requirements', query: { new: 1 } }">录入需求</router-link></template>
  </PageHeading>
  <div v-if="error" class="err-box">{{ error.message }}</div>
  <div class="stat-grid four">
    <TgStat label="待我审核" :value="review.length" :foot="`测试点 ${review.filter(x => x.kind === 'point').length} 个任务 · 用例 ${review.filter(x => x.kind === 'case').length} 个任务`" icon="checklist" color="orange" :to="review.length ? { name: review[0].kind === 'point' ? 'points' : 'cases', params: { tid: review[0].task_id } } : null" />
    <TgStat label="被驳回待修改" :value="fix.length" :foot="fix.some(x => x.has_proposal) ? '有 AI 修改提案待确认' : '驳回后 AI 定向修正'" icon="alert" color="red" :to="fix.length ? { name: 'design-detail', params: { id: fix[0].task_id } } : null" />
    <TgStat label="分配给我待执行" :value="wb?.to_execute?.count || 0" foot="测试计划 › 分配给我" icon="play" color="blue" :to="{ name: 'exec' }" />
    <TgStat label="待确认需求" :value="reqs.length" :foot="`待确认事项 ${reqs.reduce((s, r) => s + (r.open_questions || 0), 0)} 条`" icon="message" color="purple" :to="reqs.length ? { name: 'req-detail', params: { id: reqs[0].req_id, step: 4 } } : null" />
  </div>
  <TgPanel title="主线：需求 → 用例 → 执行" hint="点任一步直达对应页面；当前范围内的待办会在该步骤标出。">
    <div class="flow-grid">
      <router-link v-for="([n, to], i) in steps" :key="n" class="flow-card" :class="{ cur: (i === 3 && reqs.length) || (i === 4 && review.some(x => x.kind === 'point')) || (i === 5 && review.some(x => x.kind === 'case')) || (i === 6 && execItems.length) }" :to="{ name: to }"><span class="step">{{ i + 1 }}</span><strong>{{ n }}</strong><small v-if="i === 3 && reqs.length">{{ reqs.length }} 条需求待确认</small><small v-else-if="i === 4 && review.some(x => x.kind === 'point')">{{ review.filter(x => x.kind === 'point').reduce((s, x) => s + x.count, 0) }} 条测试点</small><small v-else-if="i === 5 && review.some(x => x.kind === 'case')">{{ review.filter(x => x.kind === 'case').reduce((s, x) => s + x.count, 0) }} 条用例</small><small v-else-if="i === 6 && execItems.length">{{ wb?.to_execute?.count }} 条待执行</small></router-link>
    </div>
  </TgPanel>
  <div class="grid-2 mt">
    <TgPanel title="待我审核" :hint="`${review.length} 个任务`">
      <template #actions><router-link class="btn small ghost" :to="{ name: 'designs' }">全部任务</router-link></template>
      <div v-for="t in review.slice(0, 6)" :key="t.task_id" class="todo-card clickable" @click="router.push({ name: t.kind === 'point' ? 'points' : 'cases', params: { tid: t.task_id } })">
        <div class="ic" :class="t.kind === 'point' ? 'orange' : 'blue'"><TgIcon :name="t.kind === 'point' ? 'checklist' : 'file'" :size="16" /></div>
        <div class="t"><strong>{{ title(t) }}</strong><small>{{ t.project || '未指定项目' }} · {{ t.kind === 'point' ? '测试点' : '用例' }} {{ t.count }} 条待审 · {{ fmtAgo(t.created_at) }}</small></div>
        <TgBadge :status="t.status" />
      </div>
      <TgEmpty v-if="!review.length && !loading" title="没有待审核的任务" />
    </TgPanel>
    <TgPanel title="被驳回待修改" :hint="`${fix.length} 个任务`">
      <div v-for="t in fix.slice(0, 6)" :key="t.task_id" class="todo-card clickable" @click="router.push({ name: 'design-detail', params: { id: t.task_id } })">
        <div class="ic red"><TgIcon name="alert" :size="16" /></div>
        <div class="t"><strong>{{ title(t) }}</strong><small>{{ t.project || '未指定项目' }} · 测试点 {{ t.points }} / 用例 {{ t.cases }} 条被驳回<template v-if="t.has_proposal"> · AI 修改提案待确认</template></small></div>
        <TgBadge v-if="t.has_proposal" status="proposed" text="有提案" />
      </div>
      <TgEmpty v-if="!fix.length && !loading" title="没有被驳回的条目" />
    </TgPanel>
    <TgPanel title="待确认需求" :hint="`${reqs.length} 条`">
      <template #actions><router-link class="btn small ghost" :to="{ name: 'questions' }">待确认事项</router-link></template>
      <div v-for="r in reqs.slice(0, 6)" :key="r.req_id" class="todo-card clickable" @click="router.push({ name: 'req-detail', params: { id: r.req_id, step: 4 } })">
        <div class="ic purple"><TgIcon name="message" :size="16" /></div>
        <div class="t"><strong>{{ r.title }}</strong><small>{{ r.project }} · {{ r.open_questions }} 条待确认事项 · {{ fmtAgo(r.updated_at) }}</small></div>
      </div>
      <TgEmpty v-if="!reqs.length && !loading" title="没有待确认的需求" />
    </TgPanel>
    <TgPanel title="我的 AI 任务" :hint="wb ? `运行中 ${wb.my_ai_tasks.running} · 待确认 ${wb.my_ai_tasks.awaiting} · 失败 ${wb.my_ai_tasks.failed}` : ''">
      <template #actions><router-link class="btn small ghost" :to="{ name: 'aitasks' }">AI 任务中心</router-link></template>
      <div v-for="t in ai.slice(0, 6)" :key="t.task_id" class="todo-card clickable" @click="router.push({ name: 'design-detail', params: { id: t.task_id } })">
        <div class="ic" :class="t.status === 'failed' ? 'red' : t.status === 'completed' ? 'green' : 'blue'"><TgIcon name="spark" :size="16" /></div>
        <div class="t"><strong>{{ title(t) }}</strong><small>{{ t.project || '未指定项目' }} · 用例 {{ t.case_count }} 条 · {{ fmtTime(t.created_at) }}<span v-if="t.error" class="muted"> · {{ t.error }}</span></small></div>
        <TgBadge :status="t.status" />
      </div>
      <TgEmpty v-if="!ai.length && !loading" title="还没有我发起的 AI 任务" />
    </TgPanel>
  </div>
  <TgPanel class="mt" title="最近操作" hint="我最近 10 条操作（操作日志）">
    <template #actions><router-link class="btn small ghost" :to="{ name: 'dash-recent' }">更多</router-link></template>
    <div class="timeline">
      <div v-for="(op, i) in (wb?.recent_ops || []).slice(0, 10)" :key="i" class="entry"><small>{{ fmtTime(op.at || op.time || op.created_at) }} · {{ op.kind }}</small>{{ op.action }}<span v-if="op.target" class="muted"> · {{ op.target }}</span><span v-if="op.project" class="tag" style="margin-left:6px">{{ op.project }}</span></div>
      <TgEmpty v-if="!(wb?.recent_ops || []).length && !loading" title="还没有操作记录" />
    </div>
  </TgPanel>
</template>

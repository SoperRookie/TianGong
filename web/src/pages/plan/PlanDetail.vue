<script setup>
/* 计划详情：页头（名称 / 状态 / 项目 / 负责人 / 周期 / 主按钮按状态）+ 页签 用例清单 / 候选池 / 分配 / 执行轮次 / 测试报告 / 附件（sub 路由参数） */
import { ref, computed, watch, nextTick, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtTime } from '@/utils/format'
import { PageHeading, TgPanel, TgTabs, TgBadge, TgDropdown, TgEmpty, TgIcon } from '@/components/ui'
import PlanFormDialog from './components/PlanFormDialog.vue'
import RunBar from './components/RunBar.vue'
import ItemsPane from './components/ItemsPane.vue'
import CandidatesPane from './components/CandidatesPane.vue'
import AssignPane from './components/AssignPane.vue'
import RunsPane from './components/RunsPane.vue'
import ReportPane from './components/ReportPane.vue'
import AttachmentsPane from './components/AttachmentsPane.vue'

const route = useRoute(), router = useRouter(), auth = useAuthStore(), ui = useUiStore()
const id = computed(() => String(route.params.id || ''))
const sub = computed(() => String(route.params.sub || 'items'))
const { data: plan, loading, error, reload } = useLoad(() => api.get(`/api/v1/plans/${id.value}`), { deps: [id] })
watch(plan, p => { if (p) ui.setCrumb(p.name) })
watch(() => route.fullPath, () => nextTick(() => { if (plan.value && route.name === 'plan-detail') ui.setCrumb(plan.value.name) }))
onBeforeUnmount(() => ui.setCrumb(''))

const project = computed(() => plan.value?.project || '')
const canManage = computed(() => auth.can(project.value, 'plan.manage'))
const canAssign = computed(() => auth.can(project.value, 'plan.assign'))
const canRun = computed(() => auth.can(project.value, 'exec.run'))
const canAttach = computed(() => auth.can(project.value, 'exec.attach'))
const latest = computed(() => plan.value?.runs?.length ? plan.value.runs[plan.value.runs.length - 1] : null)
const active = computed(() => latest.value && !latest.value.finished_at ? latest.value : null)
const attCount = computed(() => (plan.value?.runs || []).reduce((s, r) => s + (r.attachments || []).length, 0))
const tabs = computed(() => [
  { key: 'items', label: '用例清单', count: plan.value?.items.length || 0 }, { key: 'candidates', label: '候选池' }, { key: 'assign', label: '分配', count: plan.value?.summary?.assigned || 0 },
  { key: 'runs', label: '执行轮次', count: plan.value?.runs.length || 0 }, { key: 'report', label: '测试报告', count: (plan.value?.runs || []).filter(r => r.report).length || null }, { key: 'attachments', label: '附件', count: attCount.value || null },
])
function go(tab, runId) { router.replace({ name: 'plan-detail', params: { id: id.value, sub: tab }, query: runId ? { ...route.query, run: runId } : route.query }) }
const runQuery = computed(() => String(route.query.run || ''))
function pickRun(rid) { if (rid && rid !== runQuery.value) router.replace({ query: { ...route.query, run: rid } }) }

/* 主按钮：无用例 → 加入用例；有用例无进行中轮次 → 开始执行；进行中 → 继续执行 */
const runsPane = ref(null)
const primary = computed(() => {
  const p = plan.value
  if (!p) return null
  if (!p.items.length) return canManage.value ? { label: '＋ 加入用例', act: () => go('candidates') } : null
  if (active.value) return canRun.value ? { label: '继续执行', act: () => router.push({ name: 'exec-panel', params: { id: id.value, rid: active.value.run_id } }) } : null
  if (p.status === 'archived') return null
  return canRun.value ? { label: '开始执行', act: () => { go('runs'); nextTick(() => runsPane.value?.openNew?.()) } } : null
})
const formOpen = ref(false)
async function setStatus(s, label) { try { await api.put(`/api/v1/plans/${id.value}`, { status: s }); ui.toast(`已置为${label}`); reload(true) } catch (e) { ui.error(e) } }
async function remove() {
  if (!await ui.confirm('删除计划？', `「${plan.value.name}」将被删除；有执行轮次的计划不可删。`, { ok: '删除', danger: true })) return
  try { await api.del(`/api/v1/plans/${id.value}`); ui.toast('已删除'); router.replace({ name: 'plans' }) } catch (e) { ui.error(e) }
}
const menu = computed(() => {
  const items = []
  if (canManage.value) {
    items.push({ label: '编辑计划', action: () => { formOpen.value = true } }, { divider: true })
    for (const [k, l] of [['not_started', '未开始'], ['in_progress', '进行中'], ['done', '已完成'], ['archived', '已归档']]) items.push({ label: `置为${l}`, disabled: plan.value?.status === k, action: () => setStatus(k, l) })
  }
  if (auth.isAdmin) items.push({ divider: true }, { label: '删除计划', danger: true, disabled: (plan.value?.runs.length || 0) > 0, hint: plan.value?.runs.length ? '有执行轮次' : '', action: remove })
  return items
})
</script>
<template>
  <PageHeading>
    <template #back><router-link class="back" :to="{ name: 'plans' }"><TgIcon name="back" :size="14" /> 计划列表</router-link></template>
    <template #title>{{ plan?.name || (loading ? '加载中…' : '计划') }}</template>
    <template #badge><TgBadge v-if="plan" :status="plan.status" :text="plan.status_label" /><TgBadge v-if="active" status="running" :text="`${active.name} 执行中`" /></template>
    <template #sub><template v-if="plan"><span class="tag">{{ plan.project }}</span> · 负责人 {{ plan.owner || '—' }} · {{ plan.start_date || '—' }} → {{ plan.end_date || '—' }} · 创建 {{ plan.created_by }} {{ fmtTime(plan.created_at) }} · <span class="mono">{{ plan.plan_id }}</span></template></template>
    <template #actions>
      <button v-if="canManage" type="button" class="btn" @click="formOpen = true"><TgIcon name="edit" :size="14" /> 编辑</button>
      <button v-if="primary" type="button" class="btn primary lg" @click="primary.act">{{ primary.label }}</button>
      <TgDropdown v-if="menu.length" :items="menu" />
    </template>
  </PageHeading>
  <div v-if="error" class="err-box">{{ error.message }}</div>
  <template v-if="plan">
    <div class="stat-grid four" v-if="plan.items.length">
      <div class="panel stat"><div class="stat-label">用例</div><div class="stat-num">{{ plan.items.length }}</div><div class="stat-foot"><span>已分配 {{ plan.summary.assigned }} · 未分配 {{ plan.items.length - plan.summary.assigned }}</span></div></div>
      <div class="panel stat"><div class="stat-label">轮次</div><div class="stat-num">{{ plan.runs.length }}</div><div class="stat-foot"><span>{{ active ? `${active.name} 进行中` : (latest ? `最近 ${latest.name} 已结束` : '还没有开始执行') }}</span></div></div>
      <div class="panel stat"><div class="stat-label">最近一轮已执行</div><div class="stat-num">{{ latest?.summary?.executed || 0 }} <span class="muted small">/ {{ plan.items.length }}</span></div><RunBar v-if="latest" :summary="latest.summary" :total="plan.items.length" text /><div v-else class="stat-foot"><span>—</span></div></div>
      <div class="panel stat"><div class="stat-label">通过率</div><div class="stat-num">{{ latest?.summary?.pass_rate != null ? (latest.summary.pass_rate * 100).toFixed(1) + '%' : '—' }}</div><div class="stat-foot"><span>失败 {{ latest?.summary?.fail || 0 }} · 阻塞 {{ latest?.summary?.blocked || 0 }}</span></div></div>
    </div>
    <TgTabs :items="tabs" :model-value="sub" @update:modelValue="go" />
    <TgPanel>
      <ItemsPane v-if="sub === 'items'" :plan="plan" :can-manage="canManage" @reload="reload(true)" @go="go" />
      <CandidatesPane v-else-if="sub === 'candidates'" :plan="plan" :can-manage="canManage" @reload="reload(true)" />
      <AssignPane v-else-if="sub === 'assign'" :plan="plan" :can-assign="canAssign" @reload="reload(true)" />
      <RunsPane v-else-if="sub === 'runs'" ref="runsPane" :plan="plan" :can-run="canRun" :can-assign="canAssign" @reload="reload(true)" @go="go" />
      <ReportPane v-else-if="sub === 'report'" :plan="plan" :can-run="canRun" :run-id="runQuery" @reload="reload(true)" @pick-run="pickRun" />
      <AttachmentsPane v-else-if="sub === 'attachments'" :plan="plan" :can-attach="canAttach" :run-id="runQuery" @reload="reload(true)" />
    </TgPanel>
  </template>
  <TgPanel v-else-if="!loading && !error"><TgEmpty title="计划不存在" /></TgPanel>
  <PlanFormDialog v-model="formOpen" :plan="plan" @saved="reload(true)" />
</template>
<style scoped>
.stat { cursor: default; }
</style>

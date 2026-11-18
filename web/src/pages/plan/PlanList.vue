<script setup>
/* 计划列表：GET /api/v1/plans（状态 / 执行进度 / 负责人 / 关键字 / 只看我的 / 分页），新建计划，行尾 ⋯ 编辑 / 归档 / 删除 */
import { ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { num, shortId } from '@/utils/format'
import { PageHeading, TgPanel, TgTable, TgBadge, TgSeg, TgDropdown, TgPager, TgStat } from '@/components/ui'
import PlanFormDialog from './components/PlanFormDialog.vue'
import RunBar from './components/RunBar.vue'

const route = useRoute(), router = useRouter(), scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore()
const status = ref(String(route.query.status || '')), runState = ref(String(route.query.run_state || '')), owner = ref(String(route.query.owner || ''))
const kw = ref(String(route.query.keyword || '')), mine = ref(route.query.mine === '1')
const page = ref(+route.query.page || 1), size = ref(+route.query.page_size || 20)
const kwLive = ref(kw.value)
let timer = null
watch(kw, v => { clearTimeout(timer); timer = setTimeout(() => { kwLive.value = v.trim(); page.value = 1 }, 300) })
watch([status, runState, owner, mine, size], () => { page.value = 1 })
const params = computed(() => ({ ...scope.query, status: status.value, run_state: runState.value, owner: owner.value, keyword: kwLive.value, mine: mine.value ? 'true' : '', page: page.value, page_size: size.value }))
const { data, loading, error, reload } = useLoad(() => api.get('/api/v1/plans', params.value), { deps: [params] })
watch(params, p => router.replace({ query: { status: p.status || undefined, run_state: p.run_state || undefined, owner: p.owner || undefined, keyword: p.keyword || undefined, mine: mine.value ? '1' : undefined, page: p.page > 1 ? p.page : undefined, page_size: p.page_size !== 20 ? p.page_size : undefined } }))

const plans = computed(() => data.value?.plans || [])
const counts = computed(() => data.value?.status_counts || {})
const allCount = computed(() => Object.values(counts.value).reduce((s, n) => s + n, 0))
const statusItems = computed(() => [['', '全部', allCount.value], ['not_started', '未开始', counts.value.not_started || 0], ['in_progress', '进行中', counts.value.in_progress || 0], ['done', '已完成', counts.value.done || 0], ['archived', '已归档', counts.value.archived || 0]])
const stats = computed(() => {
  const list = plans.value
  const latest = p => p.summary?.latest_run
  return {
    cases: list.reduce((s, p) => s + (p.summary?.cases || 0), 0), assigned: list.reduce((s, p) => s + (p.summary?.assigned || 0), 0),
    executed: list.reduce((s, p) => s + (latest(p)?.executed || 0), 0), pass: list.reduce((s, p) => s + (latest(p)?.pass || 0), 0), fail: list.reduce((s, p) => s + (latest(p)?.fail || 0), 0),
    myPending: list.reduce((s, p) => s + (p.my_pending || 0), 0), running: list.filter(p => latest(p) && !latest(p).finished_at).length,
  }
})
const cols = [
  { key: 'name', title: '计划' }, { key: 'project', title: '项目', w: '160px' }, { key: 'owner', title: '负责人', w: '90px' }, { key: 'cases', title: '用例', w: '80px' },
  { key: 'progress', title: '最近一轮进度', w: '230px' }, { key: 'status', title: '状态', w: '80px' }, { key: 'dates', title: '周期', w: '190px' }, { key: 'ops', title: '', w: '48px' },
]
const canCreate = computed(() => auth.isAdmin || (scope.project ? auth.can(scope.project, 'plan.manage') : scope.projects.some(p => auth.can(p.project, 'plan.manage'))))
const formOpen = ref(false), editing = ref(null)
function openNew() { editing.value = null; formOpen.value = true }
function openEdit(p) { editing.value = p; formOpen.value = true }
function onSaved(p) { reload(true); if (!editing.value && p?.plan_id) router.push({ name: 'plan-detail', params: { id: p.plan_id } }) }
async function setStatus(p, s, label) {
  try { await api.put(`/api/v1/plans/${p.plan_id}`, { status: s }); ui.toast(`已${label}`); reload(true) } catch (e) { ui.error(e) }
}
async function remove(p) {
  if (!await ui.confirm('删除计划？', `「${p.name}」将被删除，仅系统管理员可删；有执行轮次的计划不可删。`, { ok: '删除', danger: true })) return
  try { await api.del(`/api/v1/plans/${p.plan_id}`); ui.toast('已删除'); reload(true) } catch (e) { ui.error(e) }
}
const latest = p => p.summary?.latest_run || null
function menu(p) {
  const m = auth.can(p.project, 'plan.manage')
  const run = latest(p)
  const items = [{ label: '查看详情', action: () => router.push({ name: 'plan-detail', params: { id: p.plan_id } }) }]
  if (run && !run.finished_at && auth.can(p.project, 'exec.run')) items.push({ label: '进入执行面板', action: () => router.push({ name: 'exec-panel', params: { id: p.plan_id, rid: run.run_id } }) })
  if (m) {
    items.push({ divider: true }, { label: '编辑', action: () => openEdit(p) })
    if (p.status !== 'archived') items.push({ label: '归档', action: () => setStatus(p, 'archived', '归档') })
    else items.push({ label: '取消归档（置为进行中）', action: () => setStatus(p, 'in_progress', '恢复为进行中') })
  }
  if (auth.isAdmin) items.push({ divider: true }, { label: '删除', danger: true, disabled: (p.summary?.runs || 0) > 0, hint: (p.summary?.runs || 0) > 0 ? '有执行轮次' : '', action: () => remove(p) })
  return items
}
</script>
<template>
  <PageHeading title="测试计划" sub="计划只收已通过的正式用例，加入即完整快照并引用版本号；一个计划同时只能有一轮未结束。">
    <template #actions><button v-if="canCreate" type="button" class="btn primary" @click="openNew">＋ 新建计划</button></template>
  </PageHeading>
  <div v-if="error" class="err-box">{{ error.message }}</div>
  <div class="stat-grid four">
    <TgStat label="计划" :value="data?.total ?? '—'" :foot="`${stats.running} 个有进行中的轮次 · ${counts.in_progress || 0} 进行中 · ${counts.not_started || 0} 未开始`" icon="checklist" color="blue" />
    <TgStat label="计划内用例" :value="num(stats.cases)" :foot="`已分配 ${num(stats.assigned)}`" icon="list" color="purple" />
    <TgStat label="最近一轮已执行" :value="num(stats.executed)" :foot="`通过 ${num(stats.pass)} · 失败 ${num(stats.fail)}`" icon="play" color="green" />
    <TgStat label="我的待执行" :value="num(stats.myPending)" foot="分配给我且本轮未执行" icon="user" color="orange" :to="{ name: 'exec' }" />
  </div>
  <TgPanel :pad="false">
    <div class="filters pad">
      <TgSeg v-model="status" :items="statusItems" />
      <select v-model="runState" class="select"><option value="">执行进度：全部</option><option value="none">无轮次</option><option value="running">执行中</option><option value="finished">已结束</option></select>
      <select v-model="owner" class="select"><option value="">负责人：全部</option><option v-for="o in data?.owners || []" :key="o" :value="o">{{ o }}</option></select>
      <input v-model="kw" class="field" style="width:200px" placeholder="搜索计划名 / 项目 / 轮次" />
      <label class="row tiny" style="gap:4px"><input v-model="mine" type="checkbox" /> 只看分配给我的</label>
      <span class="grow" /><span class="small muted">共 {{ data?.total ?? 0 }} 个</span>
    </div>
    <TgTable :cols="cols" :rows="plans" row-key="plan_id" :loading="loading" clickable empty="没有符合条件的计划" @row-click="r => router.push({ name: 'plan-detail', params: { id: r.plan_id } })">
      <template #cell-name="{ row }"><div style="font-weight:650">{{ row.name }}</div><div class="sub"><span class="mono">{{ shortId(row.plan_id) }}</span> · 创建 {{ row.created_by }}<template v-if="row.my_items"> · 我的 {{ row.my_items }} 条（待执行 {{ row.my_pending }}）</template></div></template>
      <template #cell-project="{ row }"><span class="tag">{{ row.project }}</span></template>
      <template #cell-owner="{ row }">{{ row.owner || '—' }}</template>
      <template #cell-cases="{ row }">{{ num(row.summary?.cases || 0) }}<div class="sub">已分配 {{ num(row.summary?.assigned || 0) }}</div></template>
      <template #cell-progress="{ row }">
        <template v-if="latest(row)">
          <div class="row" style="gap:6px;flex-wrap:nowrap"><RunBar :summary="latest(row)" :total="row.summary.cases" mini /><span class="tiny">{{ latest(row).executed }} / {{ row.summary.cases }}</span><TgBadge :status="latest(row).finished_at ? 'finished' : 'running'" :text="latest(row).finished_at ? '已结束' : '执行中'" /></div>
          <div class="sub">{{ latest(row).name }} · 通过 {{ latest(row).pass }} · 失败 {{ latest(row).fail }} · 阻塞 {{ latest(row).blocked }} · 跳过 {{ latest(row).skipped }} · 共 {{ row.summary.runs }} 轮</div>
        </template>
        <span v-else class="muted">— 还没有执行轮次</span>
      </template>
      <template #cell-status="{ row }"><TgBadge :status="row.status" :text="row.status_label" /></template>
      <template #cell-dates="{ row }"><span class="tiny">{{ row.start_date || '—' }} → {{ row.end_date || '—' }}</span></template>
      <template #cell-ops="{ row }"><span @click.stop><TgDropdown icon small label="更多" :items="menu(row)" /></span></template>
    </TgTable>
    <TgPager v-model:page="page" v-model:size="size" :total="data?.total || 0" />
  </TgPanel>
  <PlanFormDialog v-model="formOpen" :plan="editing" :default-project="scope.project" @saved="onSaved" />
</template>
<style scoped>
.filters.pad { padding: 12px 14px 10px; }
</style>

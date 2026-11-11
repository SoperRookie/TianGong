<script setup>
/*
 需求列表：筛选（状态 / 关键字 / 来源类型 / 创建人 / 版本 + 工作范围的模块 / 端）+ 分页；
 阶段列为流程条缩略、待办列直接写数字；行尾「发起设计 / ⋯」；勾选后底部批量条（批量确认分析 / 合并发起设计 / 合并 / 删除 / 恢复）；
 录入需求（含同名附件查重）与需求书写指引。
*/
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, seg } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { useBadgeStore } from '@/stores/badge'
import { fmtAgo, fmtTime } from '@/utils/format'
import { REQ_STATUSES } from '@/utils/status'
import { PageHeading, TgPanel, TgTable, TgBadge, TgSeg, TgDropdown, TgPager, TgDialog, TgMd, TgIcon } from '@/components/ui'
import { REQ_STEPS, reqStage, stepStates, reqTodo } from './components/reqUtil'
import ReqFormDialog from './components/ReqFormDialog.vue'
import MergeDialog from './components/MergeDialog.vue'
import DesignDialog from './components/DesignDialog.vue'
const route = useRoute(), router = useRouter(), scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore(), badge = useBadgeStore()
const q = route.query
const status = ref(q.status || ''), kw = ref(q.kw || ''), kwInput = ref(q.kw || ''), source = ref(q.source || ''), creator = ref(q.creator || ''), version = ref(q.version || '')
const deleted = ref(q.deleted === '1'), page = ref(parseInt(q.page) || 1), size = ref([20, 50, 100, 200].includes(parseInt(q.size)) ? parseInt(q.size) : 20)
const more = ref(!!(q.source || q.creator || q.version))
const selected = ref(new Set())
const newOpen = ref(q.new === '1'), guideOpen = ref(false), guide = ref(null), mergeOpen = ref(false), designOpen = ref(false), editOpen = ref(false), editReq = ref(null)
const versions = ref([])
const moduleId = computed(() => scope.module ? (scope.flatModules.find(m => m.name === scope.module || m.path === scope.module)?.module_id || '') : '')
const { data, loading, error, reload } = useLoad(() => api.get('/api/v1/requirements', { ...scope.query, status: status.value, keyword: kw.value, source_type: source.value, created_by: creator.value, version_id: version.value, module_id: moduleId.value, include_deleted: deleted.value ? 'true' : '', page: page.value, page_size: size.value }),
  { deps: [() => scope.project, () => scope.line, status, kw, source, creator, version, deleted, page, size, moduleId] })
watch([status, kw, source, creator, version, deleted, moduleId, () => scope.project], () => { page.value = 1 })
watch([status, kw, source, creator, version, deleted, page, size], () => router.replace({ query: { ...route.query, status: status.value || undefined, kw: kw.value || undefined, source: source.value || undefined, creator: creator.value || undefined, version: version.value || undefined, deleted: deleted.value ? '1' : undefined, page: page.value > 1 ? page.value : undefined, size: size.value !== 20 ? size.value : undefined, new: undefined } }))
// 外部改了 query（浏览器后退、工作台链接 ?status= / ?new=1）：同步到筛选状态
watch(() => route.query, nq => {
  if (route.name !== 'requirements') return
  const sync = (r, v) => { if ((r.value || '') !== (v || '')) r.value = v || '' }
  sync(status, nq.status); sync(kw, nq.kw); sync(source, nq.source); sync(creator, nq.creator); sync(version, nq.version)
  kwInput.value = kw.value
  if (deleted.value !== (nq.deleted === '1')) deleted.value = nq.deleted === '1'
  const p = parseInt(nq.page) || 1; if (page.value !== p) page.value = p
  if (nq.new === '1') newOpen.value = true
})
watch(() => scope.project, async p => { versions.value = []; version.value = ''; if (p) { try { versions.value = (await api.get(`/api/v1/projects/${seg(p)}/versions`)).versions || [] } catch { /* 无权限 */ } } }, { immediate: true })
/** 端筛选：按整理稿按端汇总里是否含该端（没有整理稿的需求不参与） */
const rows = computed(() => {
  let list = data.value?.requirements || []
  if (scope.terminal) list = list.filter(r => (r.extraction?.terminal_summary?.terminals || []).some(t => t.key === scope.terminal))
  return list
})
watch(data, d => {
  if (!d) return
  const counts = {}
  d.requirements.forEach(r => { if (r.module_path) { const name = r.module_path.split('/').pop(); counts[name] = (counts[name] || 0) + 1; counts[r.module_path] = (counts[r.module_path] || 0) + 1 } })
  scope.moduleCounts = counts
  const alive = new Set(d.requirements.map(r => r.req_id)); selected.value = new Set([...selected.value].filter(x => alive.has(x)))
})
onBeforeUnmount(() => { scope.moduleCounts = null })
const canEdit = computed(() => scope.project ? auth.can(scope.project, 'requirement.edit') : (auth.isAdmin || (auth.user?.projects || []).some(p => auth.can(p.project, 'requirement.edit'))))
const statusItems = computed(() => REQ_STATUSES.map(([k, l]) => [k, l, k ? (data.value?.status_counts?.[k] || 0) : undefined]))
const filtering = computed(() => status.value || kw.value || source.value || creator.value || version.value || scope.module || scope.terminal)
const picked = computed(() => rows.value.filter(r => selected.value.has(r.req_id)))
const cols = [{ key: 'title', title: '需求' }, { key: 'status', title: '状态', w: '96px' }, { key: 'stage', title: '阶段', w: '150px' }, { key: 'todo', title: '待办' }, { key: 'created_by', title: '创建人', w: '90px' }, { key: 'updated_at', title: '更新', w: '100px' }, { key: 'ops', title: '操作', w: '140px' }]
const open = (r, step) => router.push({ name: 'req-detail', params: { id: r.req_id, step: step || reqStage(r) } })
function clearFilters() { status.value = ''; kw.value = ''; kwInput.value = ''; source.value = ''; creator.value = ''; version.value = ''; scope.setModule(''); scope.setTerminal('') }
async function openGuide() {
  guideOpen.value = true
  if (!guide.value) { try { guide.value = await api.get('/api/v1/requirements/writing-guide') } catch (e) { ui.error(e) } }
}
function downloadGuide() {
  const blob = new Blob([guide.value?.markdown || ''], { type: 'text/markdown;charset=utf-8' })
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = guide.value?.filename || '需求文档编写规范.md'; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000)
}
async function batchConfirm() {
  const project = scope.project || null, label = project ? `项目「${project}」` : '全部项目'
  if (!project && !auth.isAdmin) return ui.toast('未选择项目时只有系统管理员可对全部项目批量确认', { kind: 'warn' })
  try {
    const pv = await api.get('/api/v1/requirements/analysis/batch-confirm', { project })
    if (!pv.pending) return ui.toast(`${label}没有待确认的分析结果（已确认 ${pv.confirmed}，未分析 ${pv.no_analysis}${pv.review_pending ? `，复核质疑未处理 ${pv.review_pending}` : ''}）`, { kind: 'warn' })
    const names = pv.pending_titles.slice(0, 8).join('、') + (pv.pending > 8 ? ` 等 ${pv.pending} 条` : '')
    if (!await ui.confirm('批量确认分析结果', `${label}有 ${pv.pending} 条需求的分析结果待确认：${names}。确认后沉淀到各自项目知识库（后台执行）；已确认 ${pv.confirmed}、未分析 ${pv.no_analysis}${pv.review_pending ? `、复核质疑未处理 ${pv.review_pending}` : ''} 会跳过。`, { ok: '确认并沉淀' })) return
    const x = await api.post('/api/v1/requirements/analysis/batch-confirm', { project })
    ui.toast(`已确认 ${x.confirmed.length} 条，正在沉淀到知识库；跳过 ${x.skipped.length} 条`); reload(true); badge.refresh(true)
  } catch (e) { ui.error(e) }
}
function multiDesign() {
  const ps = [...new Set(picked.value.map(r => r.project))]
  if (ps.length > 1) return ui.toast(`所选需求须属于同一项目（当前涉及：${ps.join('、')}）`, { kind: 'warn' })
  if (picked.value.some(r => r.deleted_at)) return ui.toast('已删除的需求不能发起设计', { kind: 'warn' })
  designOpen.value = true
}
function multiMerge() {
  const ps = [...new Set(picked.value.map(r => r.project))]
  if (ps.length > 1) return ui.toast('合并的需求须属于同一项目', { kind: 'warn' })
  mergeOpen.value = true
}
async function removeMany(list) {
  const n = list.reduce((s, r) => s + (r.task_count || 0), 0)
  if (!await ui.confirm(`删除 ${list.length} 条需求？`, `${n ? `其中已发起 ${n} 个测试设计任务，删除后任务与用例保留，仅失去来源追溯；` : ''}需求进入回收站，可恢复。`, { ok: '删除', danger: true })) return
  try { for (const r of list) await api.del(`/api/v1/requirements/${r.req_id}${r.task_count ? '?force=true' : ''}`); ui.toast(`已删除 ${list.length} 条，可在回收站恢复`); selected.value = new Set(); reload(true); badge.refresh(true) } catch (e) { ui.error(e); reload(true) }
}
async function restoreMany(list) {
  try { for (const r of list) await api.post(`/api/v1/requirements/${r.req_id}/restore`); ui.toast(`已恢复 ${list.length} 条`); selected.value = new Set(); reload(true) } catch (e) { ui.error(e); reload(true) }
}
async function setStatus(r, st) { try { await api.put(`/api/v1/requirements/${r.req_id}`, { status: st }); ui.toast({ done: '已标记完成', archived: '已归档', designing: '已恢复为设计中' }[st]); reload(true) } catch (e) { ui.error(e) } }
const rowMenu = r => {
  const edit = auth.can(r.project, 'requirement.edit'), canDel = auth.isAdmin || (auth.accessMode === 'members' && edit)
  if (r.deleted_at) return [{ label: '查看', action: () => open(r) }, ...(edit && !r.merged_into ? [{ label: '恢复', action: () => restoreMany([r]) }] : [])]
  return [
    { label: '查看详情', action: () => open(r) },
    ...(edit ? [{ label: '编辑', action: () => { editReq.value = r; editOpen.value = true } }, { label: '合并到…', action: () => { selected.value = new Set([r.req_id]); mergeOpen.value = true } }] : []),
    ...(edit && r.status === 'designing' ? [{ label: '标记完成', action: () => setStatus(r, 'done') }] : []),
    ...(edit ? (r.status !== 'archived' ? [{ label: '归档', action: () => setStatus(r, 'archived') }] : [{ label: '恢复为设计中', action: () => setStatus(r, 'designing') }]) : []),
    ...(canDel ? [{ divider: true }, { label: '删除', action: () => removeMany([r]), danger: true }] : []),
  ]
}
const headMenu = computed(() => [
  { label: '需求书写指引', action: openGuide },
  ...(canEdit.value ? [{ label: '批量确认分析结果', action: batchConfirm, hint: scope.project ? '本项目' : '全部项目' }] : []),
  { divider: true },
  { label: deleted.value ? '隐藏已删除' : '显示已删除（回收站）', action: () => { deleted.value = !deleted.value } },
  ...(filtering.value ? [{ label: '清除筛选', action: clearFilters }] : []),
])
</script>
<template>
  <PageHeading title="需求列表" :sub="scope.project ? `${scope.label} · 行点进详情，一条需求的原文、整理稿、分析、待确认、测试设计都在详情的流程条里。` : '全部可见项目；选择工作范围后可录入需求与批量确认。'">
    <template #actions>
      <button v-if="canEdit" type="button" class="btn primary" @click="newOpen = true"><TgIcon name="upload" :size="14" /> 录入需求</button>
      <TgDropdown :items="headMenu" />
    </template>
  </PageHeading>
  <div v-if="error" class="err-box">{{ error.message }}</div>
  <TgPanel :pad="false">
    <div class="filters" style="padding:12px 14px">
      <TgSeg v-model="status" :items="statusItems" />
      <input v-model="kwInput" class="field" style="width:240px" placeholder="搜索标题 / 原文 / 附件 / 待确认事项" @keydown.enter="kw = kwInput.trim()" @change="kw = kwInput.trim()" />
      <a class="link small" @click="more = !more">{{ more ? '收起筛选' : '更多筛选 ▾' }}</a>
      <template v-if="more">
        <select v-model="source" class="select"><option value="">全部来源</option><option v-for="(l, k) in data?.source_types || {}" :key="k" :value="k">{{ l }}</option></select>
        <select v-model="creator" class="select"><option value="">全部创建人</option><option v-for="u in data?.creators || []" :key="u" :value="u">{{ u }}</option></select>
        <select v-if="versions.length" v-model="version" class="select"><option value="">全部版本</option><option v-for="v in versions" :key="v.version_id" :value="v.version_id">{{ v.name }}</option></select>
      </template>
      <span class="grow" />
      <span class="small muted">共 {{ data?.total ?? 0 }} 条<template v-if="scope.terminal"> · 端筛选后 {{ rows.length }} 条</template><template v-if="deleted"> · 含回收站</template><a v-if="filtering" class="link" style="margin-left:8px" @click="clearFilters">清除筛选</a></span>
    </div>
    <TgTable :cols="cols" :rows="rows" row-key="req_id" :loading="loading" clickable selectable v-model:selected="selected" :empty="filtering ? '没有符合筛选条件的需求' : '暂无需求'" :empty-hint="canEdit && !filtering ? '点击「录入需求」粘贴原文或上传文件' : ''" @row-click="r => open(r)">
      <template #cell-title="{ row }">
        <div style="font-weight:650;color:#2b3b5b">{{ row.title }} <TgBadge v-if="row.deleted_at" status="error" text="已删除" /><span v-if="row.merged_into" class="tag">已合并</span></div>
        <div class="sub">{{ row.req_id }} · {{ row.project }}<template v-if="row.version_name"> · {{ row.version_name }}</template><template v-if="row.module_path"> · {{ row.module_path }}</template> · {{ row.source_label }}<template v-if="row.attachments.length"> · {{ row.attachments.length }} 附件</template></div>
      </template>
      <template #cell-status="{ row }"><TgBadge :status="row.status" :text="row.status_label" /></template>
      <template #cell-stage="{ row }">
        <span class="stage-dots" :title="stepStates(row).map((s, i) => `${i + 1} ${REQ_STEPS[i]}：${s.state === 'done' ? '完成' : s.state === 'warn' ? '待处理' : '未开始'}`).join('\n')">
          <i v-for="(s, i) in stepStates(row)" :key="i" :class="[s.state, { cur: s.cur }]" @click.stop="open(row, i + 1)">{{ i + 1 }}</i>
        </span>
      </template>
      <template #cell-todo="{ row }"><span class="small" :style="row.blocking_questions || row.parse_failed ? 'color:var(--amber)' : ''">{{ reqTodo(row) }}</span></template>
      <template #cell-created_by="{ row }">{{ row.created_by || '—' }}</template>
      <template #cell-updated_at="{ row }"><span class="tiny muted" :title="fmtTime(row.updated_at)">{{ fmtAgo(row.updated_at) }}</span></template>
      <template #cell-ops="{ row }">
        <span class="row nowrap" @click.stop>
          <a v-if="!row.deleted_at && auth.can(row.project, 'point.ai') && (row.status === 'analyzed' || row.status === 'confirmed')" class="link" @click="selected = new Set([row.req_id]); designOpen = true">发起设计</a>
          <a v-else class="link" @click="open(row)">查看 →</a>
          <TgDropdown icon label="更多" :items="rowMenu(row)" small />
        </span>
      </template>
    </TgTable>
    <TgPager v-model:page="page" v-model:size="size" :total="data?.total || 0" />
  </TgPanel>
  <!-- 底部批量条 -->
  <div v-if="selected.size" class="batch">
    <span>已选 {{ selected.size }} 条</span>
    <button v-if="canEdit" type="button" class="btn" @click="batchConfirm" title="对当前项目范围内已分析未确认的分析结果一次确认并沉淀">批量确认分析结果</button>
    <button v-if="picked.some(r => !r.deleted_at)" type="button" class="btn" @click="multiDesign">合并发起测试设计</button>
    <button v-if="picked.length >= 1 && picked.every(r => !r.deleted_at)" type="button" class="btn" @click="multiMerge">合并到…</button>
    <button v-if="picked.some(r => !r.deleted_at)" type="button" class="btn" @click="removeMany(picked.filter(r => !r.deleted_at))">删除</button>
    <button v-if="picked.some(r => r.deleted_at && !r.merged_into)" type="button" class="btn" @click="restoreMany(picked.filter(r => r.deleted_at && !r.merged_into))">恢复</button>
    <span class="x" @click="selected = new Set()">✕</span>
  </div>

  <ReqFormDialog v-model="newOpen" @created="r => router.push({ name: 'req-detail', params: { id: r.req_id, step: r.attachments?.length ? 1 : 3 } })" @replaced="r => router.push({ name: 'req-detail', params: { id: r.req_id, step: 1 } })" />
  <ReqFormDialog v-model="editOpen" :req="editReq" @saved="reload(true)" />
  <MergeDialog v-model="mergeOpen" :sources="picked" @done="into => { selected = new Set(); router.push({ name: 'req-detail', params: { id: into } }) }" />
  <DesignDialog v-model="designOpen" :reqs="picked" @done="() => { selected = new Set(); reload(true) }" />
  <TgDialog v-model="guideOpen" title="需求书写指引" no-foot wide>
    <div class="row" style="margin-bottom:10px"><span class="tiny muted">给产品 / 需求方的写法规范与模板：规则逐条编号、数值方向写死、状态流转成表、角色权限单独一节、异常分支明写、修订记录</span><button type="button" class="btn small right" :disabled="!guide" @click="downloadGuide"><TgIcon name="download" :size="13" /> 下载 Markdown</button></div>
    <div v-if="!guide" class="page-loading"><span class="spinner" /> 加载中…</div>
    <TgMd v-else :text="guide.markdown" style="max-height:60vh" />
  </TgDialog>
</template>
<style scoped>
.stage-dots { display: inline-flex; gap: 3px; }
.stage-dots i { font-style: normal; width: 18px; height: 18px; border-radius: 50%; display: grid; place-items: center; font-size: 10px; font-weight: 700; background: #edf0f8; color: #9aa6ba; cursor: pointer; }
.stage-dots i.done { background: #d6f3e8; color: #138b64; }
.stage-dots i.warn { background: #fff3e1; color: #d68a28; }
.stage-dots i.cur { box-shadow: 0 0 0 2px #4f7dff inset; color: #285be0; background: #f2f6ff; }
</style>

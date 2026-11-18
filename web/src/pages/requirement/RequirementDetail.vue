<script setup>
/*
 需求详情 · 流程条：7 步（原文与附件 / 整理稿 / AI 分析 / 待确认 / 发起设计 / 任务进度 / 计划与执行），URL 带步骤号；
 右侧固定原文与元信息；当前步高亮且只有一个主按钮；附件解析 / 提取进行中每 3 秒自动刷新。
*/
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { useBadgeStore } from '@/stores/badge'
import { fmtTime, fmtAgo, num } from '@/utils/format'
import { PageHeading, TgBadge, TgPanel, TgKv, TgDropdown, TgIcon, TgEmpty } from '@/components/ui'
import { REQ_STEPS, reqStage, stepStates } from './components/reqUtil'
import StepRaw from './components/StepRaw.vue'
import StepExtraction from './components/StepExtraction.vue'
import StepAnalysis from './components/StepAnalysis.vue'
import StepQuestions from './components/StepQuestions.vue'
import StepDesign from './components/StepDesign.vue'
import StepTasks from './components/StepTasks.vue'
import StepPlans from './components/StepPlans.vue'
import ReqFormDialog from './components/ReqFormDialog.vue'
import MergeDialog from './components/MergeDialog.vue'
import DesignDialog from './components/DesignDialog.vue'
const route = useRoute(), router = useRouter(), auth = useAuthStore(), ui = useUiStore(), badge = useBadgeStore()
const r = ref(null), error = ref(null), loading = ref(false)
const tasks = ref([])
const editOpen = ref(false), mergeOpen = ref(false), designOpen = ref(false)
const rawOpen = ref(true)
const id = computed(() => String(route.params.id || ''))
const step = computed(() => { const s = parseInt(route.params.step); return s >= 1 && s <= 7 ? s : (r.value ? reqStage(r.value) : 1) })
let timer = null, seq = 0
async function load(silent = false) {
  const my = ++seq
  if (!silent) loading.value = true
  try {
    const d = await api.get(`/api/v1/requirements/${id.value}`)
    if (my !== seq) return
    r.value = d; error.value = null
    ui.setCrumb(d.title)
    if (d.tasks?.length) loadTasks(d.project)
    clearTimeout(timer)
    if (d.parsing || d.extraction_status === 'running') timer = setTimeout(() => load(true), 3000)
  } catch (e) { if (my === seq) error.value = e } finally { if (my === seq) loading.value = false }
}
async function loadTasks(project) {
  try { const d = await api.get('/api/v1/tasks', { project, limit: 500 }); tasks.value = (d.tasks || []).filter(t => (r.value?.tasks || []).includes(t.task_id)) } catch { tasks.value = [] }
}
onMounted(load)
onBeforeUnmount(() => { clearTimeout(timer); ui.setCrumb('') })
watch(id, () => load())
const reload = () => { load(true); badge.refresh(true) }
const go = s => router.replace({ name: 'req-detail', params: { id: id.value, step: s } })

const canEdit = computed(() => r.value && auth.can(r.value.project, 'requirement.edit'))
const canAI = computed(() => r.value && auth.can(r.value.project, 'requirement.ai'))
const canDesign = computed(() => r.value && auth.can(r.value.project, 'point.ai'))
const canDelete = computed(() => auth.isAdmin || (auth.accessMode === 'members' && canEdit.value))
const must = computed(() => r.value?.blocking_questions || 0)
const steps = computed(() => {
  const x = r.value; if (!x) return []
  const st = stepStates(x), t = x.trace || {}, tot = t.totals || {}, qn = (x.questions || []).length, atts = x.attachments || []
  const m = x.extraction?.meta || {}
  const subs = [
    atts.length ? `${atts.length} 附件${x.parsing ? ' · 解析中' : x.parse_failed ? ` · ${x.parse_failed} 失败` : ' ✓'}` : (x.raw_text ? '文本原文' : '—'),
    x.extraction ? `${m.checked || 0} 条 · ${x.extraction_status === 'outdated' ? '已过期' : x.extraction.reviewed ? '已审核' : `${m.unverified || 0} ⚠`}` : (x.extraction_status === 'running' ? '提取中' : x.extraction_wanted ? '待提取' : '无需'),
    x.analysis ? `11 项 · ${x.analysis_meta?.verification ? `${x.analysis_meta.verification.verified}/${x.analysis_meta.verification.checked} 核验` : ''}${x.analysis_confirmed && !x.analysis_confirmed_stale ? ' · 已确认' : ''}` : '未分析',
    qn ? (must.value ? `${must.value} 必须 · 待处理` : `${qn} 项${x.open_questions ? ` · ${x.open_questions} 建议待办` : ' ✓'}`) : '—',
    x.task_count ? `${x.task_count} 次设计` : '未发起',
    tot.points || tot.cases ? `${num(tot.points)} 点 · ${num(tot.cases)} 例` : (x.task_count ? '进行中' : '—'),
    tot.in_plan ? `${tot.in_plan} 入计划 · ${tot.executed} 已执行` : '—',
  ]
  return REQ_STEPS.map((name, i) => ({ name, sub: subs[i], ...st[i] }))
})
/** 唯一主按钮：随阶段变化 */
const mainBtn = computed(() => {
  const x = r.value; if (!x) return null
  if (x.status === 'archived') return null
  if (x.parsing) return { label: '附件解析中…', disabled: true }
  if ((x.extraction_status === 'none' && x.extraction_wanted) || x.extraction_status === 'outdated') return canAI.value ? { label: x.extraction_status === 'outdated' ? '重新提取整理稿' : '提取整理稿', step: 2 } : null
  if (!x.analysis || x.analysis_outdated) return canAI.value ? { label: x.analysis ? '重新 AI 分析' : 'AI 需求分析', step: 3 } : null
  if (must.value) return { label: `去确认 ${must.value} 项必须确认`, step: 4 }
  if (x.task_count) return { label: '查看任务进度', step: 6 }
  return canDesign.value ? { label: '发起测试设计', action: () => { designOpen.value = true } } : null
})
function runMain() { const b = mainBtn.value; if (!b || b.disabled) return; b.action ? b.action() : go(b.step) }
async function setStatus(status) {
  try { await api.put(`/api/v1/requirements/${id.value}`, { status }); ui.toast({ done: '已标记完成', archived: '已归档', designing: '已恢复为设计中' }[status]); reload() } catch (e) { ui.error(e) }
}
async function remove() {
  const x = r.value, n = x.task_count || 0
  if (!await ui.confirm(`删除需求「${x.title}」？`, n ? `该需求已发起 ${n} 个测试设计任务。删除后任务与用例保留，仅失去来源追溯；需求进入回收站可恢复。` : '需求进入回收站，可在列表「显示已删除」后恢复。', { ok: '删除', danger: true })) return
  try { await api.del(`/api/v1/requirements/${id.value}${n ? '?force=true' : ''}`); ui.toast('需求已删除，可在回收站恢复', { undo: async () => { await api.post(`/api/v1/requirements/${id.value}/restore`); load() } }); router.push({ name: 'requirements' }) } catch (e) { ui.error(e) }
}
async function restore() { try { await api.post(`/api/v1/requirements/${id.value}/restore`); ui.toast('需求已恢复'); reload() } catch (e) { ui.error(e) } }
const more = computed(() => {
  const x = r.value; if (!x || !canEdit.value) return []
  if (x.deleted_at) return [{ label: '恢复需求', action: restore }]
  return [
    { label: '编辑需求', action: () => { editOpen.value = true } },
    ...(x.status === 'designing' ? [{ label: '标记完成', action: () => setStatus('done') }] : []),
    ...(x.status !== 'archived' ? [{ label: '合并到…', action: () => { mergeOpen.value = true } }, { label: '归档', action: () => setStatus('archived') }] : [{ label: '恢复为设计中', action: () => setStatus('designing') }]),
    ...(canDesign.value && x.status !== 'archived' ? [{ divider: true }, { label: '发起测试设计', action: () => { designOpen.value = true }, disabled: !!must.value, hint: must.value ? `${must.value} 项必须确认` : '' }] : []),
    ...(canDelete.value ? [{ divider: true }, { label: '删除需求', action: remove, danger: true }] : []),
  ]
})
</script>
<template>
  <PageHeading v-if="r" :title="r.title">
    <template #back><router-link class="back" :to="{ name: 'requirements' }"><TgIcon name="back" :size="14" /> 需求列表 <span class="sep">/</span> {{ r.req_id }} · 需求详情</router-link></template>
    <template #badge><TgBadge :status="r.status" :text="r.status_label" /><TgBadge v-if="r.deleted_at" status="error" text="已删除" /></template>
    <template #sub>{{ r.project }}<template v-if="r.version_name"> · {{ r.version_name }}</template><template v-if="r.module_path"> · {{ r.module_path }}</template> · {{ r.source_label }} · 创建 {{ r.created_by }} {{ fmtTime(r.created_at) }} · 更新 {{ r.updated_by }} {{ fmtAgo(r.updated_at) }}</template>
    <template #actions>
      <button v-if="mainBtn" type="button" class="btn primary lg" :disabled="mainBtn.disabled" @click="runMain">{{ mainBtn.label }}</button>
      <span v-if="must && mainBtn && !mainBtn.step" class="tiny" style="color:var(--amber)">必须级事项未清零</span>
      <TgDropdown v-if="more.length" label="更多" :items="more" />
    </template>
  </PageHeading>
  <div v-if="error" class="err-box">{{ error.message }} <router-link class="link" :to="{ name: 'requirements' }">返回需求列表</router-link></div>
  <div v-else-if="!r" class="page-loading"><span class="spinner" /> 加载中…</div>
  <template v-else>
    <div v-if="must" class="attention" style="margin-bottom:10px"><TgIcon name="alert" :size="15" /> <span>还有 <b>{{ must }} 项「必须确认」</b>未处理，发起测试设计前需逐条确认；建议级可带假设继续。AI 已按「原文 › 历史结论 › 行业惯例」给出推荐答案。</span><button type="button" class="btn small" @click="go(4)">去确认</button></div>
    <div v-if="r.deleted_at" class="attention" style="margin-bottom:10px">该需求已删除（{{ r.deleted_by }} {{ fmtTime(r.deleted_at) }}）<template v-if="r.merged_into">，已合并到 <router-link class="link" :to="{ name: 'req-detail', params: { id: r.merged_into } }">{{ r.merged_into }}</router-link></template></div>
    <div class="flowbar">
      <div v-for="(s, i) in steps" :key="i" class="fstep" :class="[s.state, { sel: step === i + 1 }]" @click="go(i + 1)"><span>{{ s.state === 'done' && step !== i + 1 ? '✓' : i + 1 }}</span><b>{{ s.name }}</b><small>{{ s.sub }}</small></div>
    </div>
    <div class="req-split">
      <div class="panel panel-pad" :class="{ 'is-loading': loading }">
        <StepRaw v-if="step === 1" :r="r" :can-edit="canEdit && !r.deleted_at" @reload="reload" />
        <StepExtraction v-else-if="step === 2" :r="r" :can-edit="canEdit && !r.deleted_at" :can-ai="canAI && !r.deleted_at" @reload="reload" />
        <StepAnalysis v-else-if="step === 3" :r="r" :can-edit="canEdit && !r.deleted_at" :can-ai="canAI && !r.deleted_at" @reload="reload" @go="go" />
        <StepQuestions v-else-if="step === 4" :r="r" :can-edit="canEdit && !r.deleted_at" :can-ai="canAI && !r.deleted_at" @reload="reload" />
        <StepDesign v-else-if="step === 5" :r="r" :can-design="canDesign && !r.deleted_at" :tasks="tasks" @reload="reload" @go="go" />
        <StepTasks v-else-if="step === 6" :r="r" :tasks="tasks" />
        <StepPlans v-else :r="r" />
      </div>
      <div class="stack">
        <TgPanel :hint="r.extraction ? `整理稿 ${num(r.extraction.meta?.chars || 0)} 字 · 源材料 ${num(r.extraction.meta?.source_chars || 0)} 字` : (r.raw_text ? `${num(r.raw_text.length)} 字` : '')">
          <template #title>需求原文 <a class="link tiny" @click="rawOpen = !rawOpen">{{ rawOpen ? '折叠' : '展开' }}</a></template>
          <template v-if="rawOpen">
            <div v-if="r.raw_text" class="source-block rail-text">{{ r.raw_text }}</div>
            <div v-else-if="r.extraction?.text" class="md rail-text" style="white-space:pre-wrap">{{ r.extraction.text.slice(0, 2500) }}</div>
            <div v-else class="tiny muted">附件解析文本见第 ① 步「解析全文」</div>
            <div v-if="r.description" class="source-block mt"><div class="source-label">人工补充</div><div style="white-space:pre-wrap">{{ r.description }}</div></div>
          </template>
        </TgPanel>
        <TgPanel title="元信息">
          <TgKv :pairs="[['项目', r.project], ['版本', r.version_name || '—'], ['模块', r.module_path || '—'], ['来源', `${r.source_label}${r.attachments.length ? ` · ${r.attachments.length} 附件` : ''}`], ['状态', r.status_label], ['创建', `${r.created_by || ''} ${fmtTime(r.created_at)}`], ['更新', `${r.updated_by || ''} ${fmtTime(r.updated_at)}`], ['编号', r.req_id]]" />
          <div v-if="canEdit && !r.deleted_at" class="row mt"><button type="button" class="btn small" @click="editOpen = true"><TgIcon name="edit" :size="13" /> 编辑</button><button v-if="canDelete" type="button" class="btn small danger" @click="remove"><TgIcon name="trash" :size="13" /> 删除</button></div>
          <div v-else-if="canEdit && r.deleted_at" class="row mt"><button type="button" class="btn small" @click="restore">恢复</button></div>
        </TgPanel>
        <TgPanel title="追溯" hint="需求 → 测试点 → 用例 → 计划 → 执行">
          <TgKv :pairs="[['附件', r.attachments.length ? r.attachments.map(a => a.filename || a.source_url).join('、') : '无'], ['设计任务', r.task_count ? `${r.task_count} 个` : '未发起'], ['测试点', r.trace?.totals?.points ? `${r.trace.totals.points} 条 · 通过 ${r.trace.totals.points_approved}` : '—'], ['用例', r.trace?.totals?.cases ? `${r.trace.totals.cases} 条 · 正式 ${r.trace.totals.cases_approved}` : '—'], ['执行', r.trace?.totals?.executed ? `${r.trace.totals.executed} 条 · 通过 ${r.trace.totals.exec_pass}` : '—']]" />
          <div v-if="(r.merged_from || []).length" class="tiny muted mt">合并自：{{ r.merged_from.map(x => x.title || x.req_id || x).join('、') }}</div>
        </TgPanel>
      </div>
    </div>
    <ReqFormDialog v-model="editOpen" :req="r" @saved="reload" />
    <MergeDialog v-model="mergeOpen" :sources="[r]" @done="into => router.push({ name: 'req-detail', params: { id: into } })" />
    <DesignDialog v-model="designOpen" :reqs="[r]" @done="reload" />
  </template>
</template>
<style scoped>
.rail-text { max-height: 320px; overflow: auto; white-space: pre-wrap; }
.is-loading { opacity: .85; }
.btn.disabled { pointer-events: none; opacity: .5; }
</style>

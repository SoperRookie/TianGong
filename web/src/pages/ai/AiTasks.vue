<script setup>
/* AI 任务中心（11.11）：全部生成 / 设计任务的类型、状态、8 节点流水线、失败归类、部分成功、Prompt 版本与「继续完成」。 */
import { ref, computed, watch, reactive } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { useBadgeStore } from '@/stores/badge'
import { fmtTime, fmtAgo, num, shortId, fmtDuration } from '@/utils/format'
import { TASK_STATUSES } from '@/utils/status'
import { PageHeading, TgPanel, TgBadge, TgSeg, TgStat, TgEmpty, TgIcon, TgKv } from '@/components/ui'
import TaskPipeline from './components/TaskPipeline.vue'
import { promptName, errorKindLabel, errorKindHint, resumeLabel, resumeHint, retryToast, PROGRESS_TEXT } from './components/usePrices'

const route = useRoute(), router = useRouter(), scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore(), badge = useBadgeStore()
const status = ref(route.query.status || '')
const kind = ref(route.query.kind || '')
const mine = ref(route.query.mine === '1')
const expanded = ref(route.query.task || '')
watch([status, kind, mine, expanded], () => router.replace({ query: { ...route.query, status: status.value || undefined, kind: kind.value || undefined, mine: mine.value ? '1' : undefined, task: expanded.value || undefined } }))

const { data, loading, error, reload } = useLoad(
  () => api.get('/api/v1/ai/tasks', { project: scope.project || undefined, status: status.value || undefined, mine: mine.value || undefined, limit: 300 }),
  { deps: [() => scope.project, status, mine] },
)
const tasks = computed(() => (data.value?.tasks || []).filter(t => !kind.value || t.kind === kind.value))
const kinds = computed(() => [...new Set((data.value?.tasks || []).map(t => t.kind))])
const counts = computed(() => data.value?.counts || {})
const segItems = computed(() => TASK_STATUSES.map(([k, l]) => ({ key: k, label: l, count: k ? (counts.value[k] || 0) : undefined })))
const title = t => t.requirement_title || (t.sources || []).join('、') || t.task_id

/* 展开行：按需加载任务详情（流水线数字、trace、进度明细） */
const details = reactive({})
const detailLoading = reactive({})
async function toggle(t) {
  expanded.value = expanded.value === t.task_id ? '' : t.task_id
  if (expanded.value) loadDetail(t.task_id)
}
async function loadDetail(id, force = false) {
  if (details[id] && !force) return
  detailLoading[id] = true
  try { details[id] = await api.get(`/api/v1/tasks/${id}`) } catch (e) { ui.error(e) } finally { detailLoading[id] = false }
}
if (expanded.value) loadDetail(expanded.value)

const traceOf = id => {
  const d = details[id]; if (!d) return []
  return [...(((d.analysis || {}).trace) || []), ...(((d.result || {}).trace) || [])]
}
const pvEntries = pv => Object.entries(pv || {})

/* 执行中任务轮询 */
let timer = null
watch(data, () => {
  clearTimeout(timer)
  if ((data.value?.tasks || []).some(t => t.status === 'queued' || t.status === 'running')) timer = setTimeout(() => { reload(true); if (expanded.value) loadDetail(expanded.value, true) }, 8000)
})

async function retry(t) {
  if (!auth.can(t.project, 'point.ai')) return
  try {
    const r = await api.post(`/api/v1/tasks/${t.task_id}/retry`, {})
    ui.toast(retryToast(r)); reload(true); badge.refresh?.(true)
    if (expanded.value === t.task_id) loadDetail(t.task_id, true)
  } catch (e) { ui.error(e) }
}
async function cancel(t) {
  const d = details[t.task_id]
  const orphan = d && d.progress_detail && d.progress_detail.job_active === false
  const ok = await ui.confirm(orphan ? '标记失败？' : '取消任务？', orphan ? '该任务没有后台执行句柄（孤儿任务），将直接标记为失败，之后可「继续完成」。' : '中断后台执行，任务标记为失败并留痕；已完成的拆解与模块保留在检查点，可继续完成。', { ok: orphan ? '标记失败' : '取消任务', danger: true })
  if (!ok) return
  try {
    await api.post(`/api/v1/tasks/${t.task_id}/cancel`, {}, { query: orphan ? { force: true } : undefined })
    ui.toast('已取消'); reload(true); if (expanded.value === t.task_id) loadDetail(t.task_id, true)
  } catch (e) { ui.error(e) }
}
const progressPct = t => t.status === 'completed' ? 100 : t.status === 'awaiting_confirmation' ? 75 : t.status === 'running' ? ({ parsing: 10, analyzing: 25, generating_reviewing: 85, exporting: 95 }[t.progress] || 30) : t.status === 'queued' ? 2 : 0
</script>
<template>
  <PageHeading title="AI 任务" :sub="`${scope.project ? scope.label : '全部可见项目'} · 每次 AI 任务记录模型、Prompt 版本、输入输出与发起人；显示需求名而非哈希 ID。`">
    <template #actions>
      <router-link class="btn" :to="{ name: 'calls' }">调用日志</router-link>
      <router-link class="btn" :to="{ name: 'usage' }">用量统计</router-link>
      <button type="button" class="btn" @click="reload()"><TgIcon name="refresh" :size="14" /> 刷新</button>
    </template>
  </PageHeading>
  <div v-if="error" class="err-box">{{ error.message }}</div>
  <div class="stat-grid">
    <TgStat label="执行中 / 排队" :value="`${counts.running || 0} / ${counts.queued || 0}`" foot="任务并发闸门 TIANGONG_TASK_MAX_CONCURRENCY，超出排队" icon="spark" color="blue" />
    <TgStat label="等待人工" :value="counts.awaiting_confirmation || 0" foot="拆解完成，待审核测试点后生成用例" icon="checklist" color="orange" />
    <TgStat label="失败" :value="counts.failed || 0" :foot="`可继续完成 ${tasks.filter(t => t.retryable).length} 个（restore / fill / resume / retry）`" icon="alert" color="red" />
    <TgStat label="已完成" :value="counts.completed || 0" :foot="`用例 ${num(tasks.filter(t => t.status === 'completed').reduce((s, t) => s + (t.case_count || 0), 0))} 条`" icon="check" color="green" />
  </div>
  <TgPanel :pad="false">
    <div class="panel-pad filters" style="padding-bottom:10px">
      <TgSeg v-model="status" :items="segItems" />
      <select v-model="kind" class="select"><option value="">全部类型</option><option v-for="k in kinds" :key="k" :value="k">{{ k }}</option></select>
      <label class="row small"><input v-model="mine" type="checkbox" /> 只看我发起的</label>
      <span class="grow" /><span class="small muted">{{ tasks.length }} 条</span>
    </div>
    <div class="table-wrap" :class="{ loading }">
      <table class="table">
        <thead><tr><th>需求 / 任务</th><th style="width:90px">类型</th><th style="width:110px">阶段</th><th style="width:200px">进度</th><th>结果 / 部分成功</th><th style="width:110px">模型</th><th>Prompt 版本</th><th style="width:150px">发起</th><th style="width:150px">操作</th></tr></thead>
        <tbody>
          <template v-for="t in tasks" :key="t.task_id">
            <tr class="row-click" :class="{ sel: expanded === t.task_id }" @click="toggle(t)">
              <td>
                <div style="font-weight:650">{{ title(t) }}</div>
                <div class="sub">{{ t.project || '未指定项目' }} · <span class="mono">{{ shortId(t.task_id, 12) }}</span><template v-if="t.requirement_id"> · <router-link :to="{ name: 'req-detail', params: { id: t.requirement_id } }" @click.stop>需求</router-link></template></div>
              </td>
              <td><span class="tag">{{ t.kind }}</span></td>
              <td><TgBadge :status="t.status" /><div v-if="t.progress && t.status === 'running'" class="sub">{{ PROGRESS_TEXT[t.progress] || t.progress }} <span class="spinner" /></div></td>
              <td>
                <template v-if="t.status === 'failed'"><div class="sub" style="color:#db5361"><b>{{ errorKindLabel(t.error_kind) }}</b> · {{ (t.error || '').slice(0, 60) }}</div></template>
                <template v-else><div class="bar"><span :style="{ width: progressPct(t) + '%', background: t.status === 'completed' ? '#23a783' : t.status === 'awaiting_confirmation' ? '#f0a03d' : undefined }" /></div><div class="sub">{{ t.status === 'awaiting_confirmation' ? '6 / 8 · 等待人工审核测试点' : t.status === 'completed' ? '8 / 8' : t.status === 'queued' ? '排队中' : `${PROGRESS_TEXT[t.progress] || '运行中'}` }}</div></template>
              </td>
              <td>
                <template v-if="t.status === 'completed'"><b>{{ num(t.case_count) }}</b> 条用例<div v-if="t.partial.length" class="sub"><span class="badge orange">部分成功</span> {{ t.partial.join('；') }}</div></template>
                <template v-else-if="t.status === 'failed'"><span v-if="t.resume?.analysis_kept || t.resume?.kept_modules?.length" class="badge orange">拆解{{ t.resume.kept_modules?.length ? ` + ${t.resume.kept_modules.length} 模块` : '' }}已保存</span><span v-else class="muted">—</span></template>
                <span v-else class="muted">—</span>
                <div v-if="t.retry_count" class="sub">已重试 {{ t.retry_count }} 次</div>
              </td>
              <td class="small">{{ t.model || '默认' }}</td>
              <td>
                <span v-if="!pvEntries(t.prompt_versions).length" class="muted">—</span>
                <span v-else class="inline-list"><router-link v-for="[k, v] in pvEntries(t.prompt_versions).slice(0, 3)" :key="k" class="tag" :class="{ ai: v > 1 }" :to="{ name: 'prompt-detail', params: { key: k } }" :title="k" @click.stop>{{ promptName(k) }} v{{ v }}</router-link><span v-if="pvEntries(t.prompt_versions).length > 3" class="tiny muted">+{{ pvEntries(t.prompt_versions).length - 3 }}</span></span>
              </td>
              <td class="small">{{ t.created_by || '—' }}<div class="sub" :title="fmtTime(t.created_at)">{{ fmtAgo(t.created_at) }}</div></td>
              <td @click.stop>
                <span class="row">
                  <router-link class="btn small" :to="{ name: 'design-detail', params: { id: t.task_id } }">概览</router-link>
                  <router-link v-if="t.status === 'awaiting_confirmation'" class="btn small primary" :to="{ name: 'points', params: { tid: t.task_id } }">去审核</router-link>
                  <button v-else-if="t.retryable && auth.can(t.project, 'point.ai')" type="button" class="btn small primary" @click="retry(t)">{{ resumeLabel(t) }}</button>
                  <button v-else-if="(t.status === 'queued' || t.status === 'running') && auth.can(t.project, 'point.ai')" type="button" class="btn small danger" @click="cancel(t)">取消</button>
                </span>
              </td>
            </tr>
            <tr v-if="expanded === t.task_id" class="expand-row">
              <td colspan="9">
                <div class="expand">
                  <div class="row"><strong>{{ title(t) }}</strong> <TgBadge :status="t.status" /><span class="tiny muted">{{ t.project || '未指定项目' }} · 发起 {{ t.created_by }} {{ fmtTime(t.created_at) }} · 重试 {{ t.retry_count || 0 }} 次</span><span class="right spinner" v-if="detailLoading[t.task_id]" /></div>
                  <TaskPipeline :task="t" :detail="details[t.task_id]" />
                  <div class="row" style="margin-top:6px">
                    <span class="tag">{{ t.model || '默认模型' }}</span>
                    <span v-if="details[t.task_id]?.context?.reviewer_model" class="tag">评审 {{ details[t.task_id].context.reviewer_model }}</span>
                    <span v-if="details[t.task_id]?.context?.template_id" class="tag">{{ details[t.task_id].context.template_id }}</span>
                    <span class="tag">trace {{ traceOf(t.task_id).length }}</span>
                    <span v-if="pvEntries(t.prompt_versions).length" class="tiny muted">Prompt：{{ pvEntries(t.prompt_versions).map(([k, v]) => `${promptName(k)} v${v}`).join(' · ') }}</span>
                  </div>
                  <div v-if="t.error" class="attention" style="margin-top:8px"><TgIcon name="alert" :size="14" /> <span><b>{{ errorKindLabel(t.error_kind) }}</b> · {{ t.error }}<div v-if="errorKindHint(t.error_kind) || resumeHint(t)" class="tiny">{{ errorKindHint(t.error_kind) }}<template v-if="resumeHint(t)"> {{ resumeHint(t) }}</template></div></span><button v-if="t.retryable && auth.can(t.project, 'point.ai')" type="button" class="btn small" @click="retry(t)">{{ resumeLabel(t) }}</button></div>
                  <div v-if="t.status === 'completed' && t.partial.length" class="attention" style="margin-top:8px"><TgIcon name="info" :size="14" /> 部分成功：{{ t.partial.join('；') }}<button v-if="t.retryable" type="button" class="btn small" @click="retry(t)">{{ resumeLabel(t) }}</button></div>
                  <div v-if="details[t.task_id]?.progress_detail" class="information" style="margin-top:8px"><TgIcon name="clock" :size="14" /> <span>进度明细：{{ details[t.task_id].progress_detail.job_active ? '后台执行中' : '无后台句柄（孤儿任务，可取消后继续完成）' }} · 拆解 {{ details[t.task_id].progress_detail.analysis_done ? '已落盘' : '未完成' }} · 已完成模块 {{ details[t.task_id].progress_detail.modules_done }} · 检查点 {{ details[t.task_id].progress_detail.checkpoint_at ? fmtTime(details[t.task_id].progress_detail.checkpoint_at, true) : '—' }}</span></div>
                  <div class="information" style="margin-top:8px"><TgIcon name="shield" :size="14" /> 「人工审核测试点」与「评审」是门禁节点：生成完成不等于流程完成；数据库里的审核状态是最终事实来源。</div>
                  <div v-if="traceOf(t.task_id).length" style="margin-top:10px">
                    <div class="panel-title" style="margin-bottom:6px">调用链路 trace（{{ traceOf(t.task_id).length }}）</div>
                    <table class="table dense"><thead><tr><th style="width:40px">#</th><th>Agent</th><th>模块 / 分片</th><th>模型</th><th>动作 / 结果</th><th style="width:90px">耗时</th></tr></thead>
                      <tbody><tr v-for="(x, i) in traceOf(t.task_id).slice(0, 60)" :key="i"><td class="muted">{{ i + 1 }}</td><td><b>{{ x.agent || '—' }}</b></td><td>{{ x.module || (x.chunk != null ? `分片 ${x.chunk}` : '—') }}</td><td class="small">{{ x.model || x.model_name || '—' }}</td><td class="small"><span v-if="x.error" style="color:#db5361">{{ x.error }}</span><template v-else>{{ x.action || '' }}{{ x.round != null ? ` 第 ${x.round} 轮` : '' }}{{ x.added != null ? ` 新增 ${x.added}` : '' }}{{ x.passed != null ? (x.passed ? ' 通过' : ' 未收敛') : '' }}</template></td><td class="small">{{ x.elapsed_ms ? fmtDuration(x.elapsed_ms) : '—' }}</td></tr></tbody></table>
                    <div v-if="traceOf(t.task_id).length > 60" class="tiny muted">只显示前 60 条，完整链路见任务概览 › 调用链路</div>
                  </div>
                  <TgKv v-if="details[t.task_id]" style="margin-top:10px" :pairs="[['知识注入', `${(details[t.task_id].knowledge || []).length} 片`], ['记忆 / 规则', `${(details[t.task_id].memories || []).length} / ${(details[t.task_id].rules || []).length}`], ['修订', `${(details[t.task_id].revisions || []).length} 次`], ['编辑占用', details[t.task_id].editing?.by ? `${details[t.task_id].editing.by} 正在编辑` : '无人编辑']]" />
                  <div class="row" style="margin-top:10px">
                    <router-link class="btn small" :to="{ name: 'design-detail', params: { id: t.task_id } }">任务概览</router-link>
                    <router-link v-if="t.status === 'awaiting_confirmation'" class="btn small primary" :to="{ name: 'points', params: { tid: t.task_id } }">去审核测试点</router-link>
                    <router-link v-if="t.case_count" class="btn small" :to="{ name: 'cases', params: { tid: t.task_id } }">用例审核</router-link>
                    <router-link class="btn small" :to="{ name: 'calls', query: { task_id: t.task_id } }">本任务调用日志</router-link>
                  </div>
                </div>
              </td>
            </tr>
          </template>
          <tr v-if="!tasks.length"><td colspan="9"><TgEmpty :title="loading ? '加载中…' : '暂无 AI 任务'" hint="在需求详情「发起测试设计」或设计任务页创建后，这里按状态列出。" /></td></tr>
        </tbody>
      </table>
    </div>
  </TgPanel>
</template>
<style scoped>
.expand-row td { background: #f8fafd; padding: 12px 16px; }
.expand { font-size: 12.5px; }
.expand .pipeline { margin-top: 10px; }
</style>

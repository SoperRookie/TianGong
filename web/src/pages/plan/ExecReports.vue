<script setup>
/* 执行报告：当前工作范围内各计划各轮次的报告汇总（遍历 plans 与 runs），可生成 / 查看 / 下载，链接到计划详情的报告页签 */
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtTime } from '@/utils/format'
import { PageHeading, TgPanel, TgTable, TgBadge, TgSeg, TgIcon } from '@/components/ui'
import RunBar from './components/RunBar.vue'
const router = useRouter(), scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore()
const only = ref(''), kw = ref('')
const { data, loading, error, reload } = useLoad(async () => {
  const list = (await api.get('/api/v1/plans', { ...scope.query, page: 0 })).plans || []
  const withRuns = list.filter(p => (p.summary?.runs || 0) > 0).slice(0, 60)
  const details = await Promise.all(withRuns.map(p => api.get(`/api/v1/plans/${p.plan_id}`).catch(() => null)))
  const runs = []
  details.forEach(d => { if (d) d.runs.forEach((r, i) => runs.push({ key: `${d.plan_id}:${r.run_id}`, plan: d, run: r, round: i + 1 })) })
  runs.sort((a, b) => (a.run.finished_at ? 1 : 0) - (b.run.finished_at ? 1 : 0) || String(b.run.started_at).localeCompare(String(a.run.started_at)))
  return { runs, plans: list.length }
}, { deps: [() => scope.project, () => scope.line] })
const rows = computed(() => (data.value?.runs || []).filter(x => {
  if (only.value === 'has' && !x.run.report) return false
  if (only.value === 'none' && x.run.report) return false
  if (only.value === 'running' && x.run.finished_at) return false
  if (kw.value && !`${x.plan.name} ${x.plan.project} ${x.run.name}`.toLowerCase().includes(kw.value.trim().toLowerCase())) return false
  return true
}))
const cols = [{ key: 'plan', title: '计划' }, { key: 'run', title: '轮次', w: '150px' }, { key: 'exec', title: '执行', w: '200px' }, { key: 'counts', title: '通过 / 失败 / 阻塞 / 跳过', w: '170px' }, { key: 'report', title: '报告', w: '200px' }, { key: 'att', title: '附件', w: '60px' }, { key: 'ops', title: '操作', w: '170px' }]
const color = c => c === '可发布' ? 'green' : c === '不可发布' ? 'red' : 'orange'
const open = x => router.push({ name: 'plan-detail', params: { id: x.plan.plan_id, sub: 'report' }, query: { run: x.run.run_id } })
const busy = ref('')
async function generate(x) {
  if (x.run.report && !await ui.confirm('重新生成报告？', `「${x.plan.name} · ${x.run.name}」已有报告（${x.run.report.conclusion}），重新生成会覆盖。`)) return
  busy.value = x.key
  try { await api.post(`/api/v1/plans/${x.plan.plan_id}/runs/${x.run.run_id}/report`, {}); ui.toast('报告已生成'); reload(true) } catch (e) { ui.error(e) } finally { busy.value = '' }
}
async function download(x) { try { await api.download(`/api/v1/plans/${x.plan.plan_id}/runs/${x.run.run_id}/report.md`) } catch (e) { ui.error(e) } }
</script>
<template>
  <PageHeading title="执行报告" :sub="`每个执行轮次可生成一份测试报告（结论 / 判据 / 执行数据 / 失败与缺陷 / 风险 / 遗留 / 建议 / Markdown）；轮次未结束生成阶段性报告。${scope.project ? `当前范围：${scope.label}。` : ''}`">
    <template #actions><button type="button" class="btn" @click="reload()"><TgIcon name="refresh" :size="14" /> 刷新</button></template>
  </PageHeading>
  <div v-if="error" class="err-box">{{ error.message }}</div>
  <TgPanel :pad="false">
    <div class="filters" style="padding:12px 14px 10px">
      <TgSeg v-model="only" :items="[['', '全部轮次', data?.runs.length], ['running', '进行中'], ['has', '已有报告', data?.runs.filter(x => x.run.report).length], ['none', '未生成']]" />
      <input v-model="kw" class="field" style="width:200px" placeholder="搜索计划 / 项目 / 轮次" /><span class="grow" /><span class="small muted">{{ rows.length }} 个轮次</span>
    </div>
    <TgTable :cols="cols" :rows="rows" row-key="key" :loading="loading" clickable empty="当前范围内还没有执行轮次" @row-click="open">
      <template #cell-plan="{ row }"><div style="font-weight:650">{{ row.plan.name }}</div><div class="sub">{{ row.plan.project }} · 负责人 {{ row.plan.owner || '—' }}</div></template>
      <template #cell-run="{ row }">{{ row.run.name }} <TgBadge :status="row.run.finished_at ? 'finished' : 'running'" :text="row.run.finished_at ? '已结束' : '进行中'" /><div class="sub">第 {{ row.round }} 轮 · {{ fmtTime(row.run.started_at) }}</div></template>
      <template #cell-exec="{ row }"><div class="row" style="flex-wrap:nowrap;gap:6px"><RunBar :summary="row.run.summary" :total="row.plan.items.length" mini /><span class="tiny">{{ row.run.summary?.executed || 0 }} / {{ row.plan.items.length }}</span></div></template>
      <template #cell-counts="{ row }"><span class="ok">{{ row.run.summary?.pass || 0 }}</span> / <span style="color:#db5361">{{ row.run.summary?.fail || 0 }}</span> / {{ row.run.summary?.blocked || 0 }} / {{ row.run.summary?.skipped || 0 }}</template>
      <template #cell-report="{ row }"><template v-if="row.run.report"><TgBadge :color="color(row.run.report.conclusion)" :text="row.run.report.conclusion" /><span v-if="row.run.report.interim" class="tag" style="margin-left:4px">阶段性</span><div class="sub">{{ row.run.report.by }} · {{ fmtTime(row.run.report.at) }}</div></template><span v-else class="muted">未生成</span></template>
      <template #cell-att="{ row }">{{ (row.run.attachments || []).length || '—' }}</template>
      <template #cell-ops="{ row }">
        <span @click.stop>
          <span class="link" @click="open(row)">{{ row.run.report ? '查看' : '详情' }}</span>
          <span v-if="auth.can(row.plan.project, 'exec.run') && row.run.summary?.executed" class="link" style="margin-left:8px" @click="generate(row)">{{ busy === row.key ? '生成中…' : (row.run.report ? '重新生成' : '生成报告') }}</span>
          <span v-if="row.run.report" class="link" style="margin-left:8px" @click="download(row)">下载 .md</span>
        </span>
      </template>
    </TgTable>
    <div class="table-footer"><span>{{ data?.runs.length || 0 }} 个轮次 · 来自 {{ data?.plans || 0 }} 个计划 · POST /plans/{id}/runs/{rid}/report · GET …/report.md</span></div>
  </TgPanel>
</template>

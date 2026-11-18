<script setup>
/* 概览页签：产出统计、按模块 / 按端、任务信息（来源需求 / 模型 / Prompt 版本 / 编辑占用 / 关联计划）、质量自检、知识 / 记忆 / 规则注入、下一步。 */
import { ref, computed, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { useScopeStore } from '@/stores/scope'
import { fmtTime, num, pct } from '@/utils/format'
import { terminalLabel } from '@/utils/terminals'
import { TgPanel, TgStat, TgKv, TgBadge, TgTable, TgDialog, TgField, TgEmpty, TgIcon } from '@/components/ui'
import { taskTitle, isManual, allPoints, pointStats, caseStats } from './taskUtils'
const props = defineProps({ task: { type: Object, required: true } })
const emit = defineEmits(['reload'])
const router = useRouter(), auth = useAuthStore(), ui = useUiStore(), scope = useScopeStore()
const t = computed(() => props.task)
const ctx = computed(() => t.value.context || {})
const project = computed(() => ctx.value.project || '')
const ps = computed(() => pointStats(t.value)), cs = computed(() => caseStats(t.value))
const running = computed(() => ['queued', 'running'].includes(t.value.status))
const moduleRows = computed(() => {
  const rows = []
  const pm = (t.value.analysis || {}).test_points || []
  if (pm.length) for (const m of pm) {
    const pts = (m.points || []).filter(p => typeof p === 'object')
    const terms = [...new Set(pts.map(p => p.terminal).filter(Boolean))]
    rows.push({ module: m.module, terminal: terms.map(terminalLabel).join(' / '), count: pts.length, approved: pts.filter(p => p.status === 'approved').length, cases: ((t.value.result || {}).cases || []).filter(c => c.module === m.module).length })
  } else {
    const by = {}
    for (const c of (t.value.result || {}).cases || []) { const x = by[c.module] || (by[c.module] = { module: c.module, count: 0, approved: 0, cases: 0, terminal: terminalLabel((t.value.case_terminals || {})[c.uid]) }); x.cases++; x.count++; if ((t.value.case_reviews || {})[c.uid]?.status === 'approved') x.approved++ }
    rows.push(...Object.values(by))
  }
  return rows
})
const reqLinks = computed(() => {
  const ids = ctx.value.requirement_ids || (ctx.value.requirement_id ? [ctx.value.requirement_id] : [])
  const titles = ctx.value.requirement_titles || (ctx.value.requirement_title ? [ctx.value.requirement_title] : [])
  return ids.map((id, i) => ({ id, title: titles[i] || id, deleted: (ctx.value.deleted_requirement_ids || []).includes(id) }))
})
const quality = computed(() => t.value.quality || null)
const qPairs = computed(() => { const q = quality.value; if (!q) return []; return Object.entries(q).filter(([k, v]) => !k.startsWith('_') && k !== 'note' && typeof v !== 'object').map(([k, v]) => [k, k.includes('覆盖度') && v != null ? `${v}%` : (v ?? '—')]) })
const dupCases = computed(() => (t.value.case_dup_pairs || []).length ? t.value.case_dup_pairs : ((quality.value || {})['疑似重复用例对'] || []))
const unresolved = computed(() => (t.value.result || {}).unresolved || [])
const editing = computed(() => t.value.editing || [])
const promptNames = { analyst: '需求拆解', requirement_analysis: '需求分析', generator: '用例生成', reviewer: '用例评审', fix_instruction: '定点修正指令', gap_check: '覆盖查漏', dup_judge: '语义查重', point_fix: '测试点修改', case_fix: '用例修改', requirement_diff: '需求变更', point_supplement: '测试点补充', point_extend: '经验延伸', point_combine: '组合延伸', point_review: '测试点评审', e2e_scenarios: '端到端场景', terminal_classify: '章节归端', case_dup_judge: '用例查重' }

// 关联计划
const plans = ref(null)
async function loadPlans() { plans.value = null; if (!cs.value.total || running.value) return; try { plans.value = (await api.get('/api/v1/plans', { task_id: t.value.task_id })).plans || [] } catch { plans.value = [] } }
watch(() => t.value.task_id, loadPlans, { immediate: true })

// 下一步
const next = computed(() => {
  const out = []
  const tv = t.value
  if (running.value) return out
  if (tv.status === 'awaiting_confirmation') out.push({ cls: 'orange', text: '测试点拆解待确认', btn: '去审核测试点', to: { name: 'points', params: { tid: tv.task_id } } })
  else if (ps.value.pending || ps.value.rejected) out.push({ cls: 'orange', text: `${ps.value.pending + ps.value.rejected} 个测试点待处理`, btn: '去审核', to: { name: 'points', params: { tid: tv.task_id } } })
  if ((tv.points_pending_generation || []).length) out.push({ cls: 'blue', text: `${tv.points_pending_generation.length} 条已通过的测试点尚未生成用例`, btn: '去生成', to: { name: 'points', params: { tid: tv.task_id } } })
  const bs = (tv.blind_spots || []).filter(b => b.status === 'open').length
  if (bs && tv.status !== 'failed') out.push({ cls: 'purple', text: `${bs} 条需求盲区待处置`, btn: '去处置', to: { name: 'points', params: { tid: tv.task_id }, query: { sub: 'blind' } } })
  const dp = (tv.dup_pairs || []).filter(p => p.status === 'open').length
  if (dp && tv.status !== 'failed') out.push({ cls: 'purple', text: `${dp} 组疑似重复测试点待处置`, btn: '去处置', to: { name: 'points', params: { tid: tv.task_id }, query: { sub: 'dup' } } })
  if (tv.pending_fix) out.push({ cls: 'orange', text: `AI 修改提案待确认（${tv.pending_fix.kind === 'cases' ? '用例' : '测试点'}）`, btn: '去确认', to: { name: tv.pending_fix.kind === 'cases' ? 'cases' : 'points', params: { tid: tv.task_id } } })
  if (cs.value.rejected && !tv.pending_fix) out.push({ cls: 'red', text: `${cs.value.rejected} 条用例已驳回，可让 AI 定点修改`, btn: '去处理', to: { name: 'cases', params: { tid: tv.task_id } } })
  if (cs.value.pending) out.push({ cls: 'orange', text: `${cs.value.pending} 条用例待审核`, btn: '去审核', to: { name: 'cases', params: { tid: tv.task_id } } })
  if (cs.value.total && cs.value.approved === cs.value.total) out.push({ cls: 'green', text: '全部用例已通过，可加入测试计划安排执行', btn: '去测试计划', to: { name: 'plans' } })
  return out
})

// 编辑占用解除（管理员）
async function release(e) {
  try { await api.post(`/api/v1/tasks/${t.value.task_id}/editing`, { kind: e.kind, entity_id: e.entity_id, action: 'force_release' }); ui.toast('已解除占用'); emit('reload') } catch (err) { ui.error(err) }
}
// 归属项目（管理员，遗留任务）
const projDlg = ref({ open: false, project: '', busy: false })
async function saveProject() {
  projDlg.value.busy = true
  try { const r = await api.put(`/api/v1/tasks/${t.value.task_id}/project`, { project: projDlg.value.project }); ui.toast(`已归属「${projDlg.value.project}」${r.requirement_migrated ? '，需求实体已建' : ''}`); projDlg.value.open = false; scope.load(true); emit('reload') } catch (e) { ui.error(e) } finally { projDlg.value.busy = false }
}
const infoPairs = computed(() => {
  const tv = t.value, c = ctx.value
  const pairs = [
    ['项目', project.value || '（未指定）'],
    ['路径', isManual(tv) ? '人工用例集（不经 AI）' : c.confirm_points ? '先审核测试点再生成' : '直接生成'],
    ['模型', isManual(tv) ? '—' : `${c.model || '默认模型'} / 评审 ${c.reviewer_model || '同生成模型'}`],
    ['模板', c.template_id || '—'],
    ['知识空间', c.knowledge_space || project.value || '—'],
    ['创建', `${tv.created_by || '—'} · ${fmtTime(tv.created_at, true)}`],
  ]
  if (c.retry_count) pairs.push(['继续 / 重试', `${c.retry_count} 次 · 最近 ${fmtTime(c.retried_at)}`])
  if (c.confirm_terminal) pairs.push(['按端确认', terminalLabel(c.confirm_terminal)])
  if (c.module_id) pairs.push(['模块', c.module_path || c.module_id])
  if (c.version_id) pairs.push(['版本', c.version_name || c.version_id])
  return pairs
})
</script>
<template>
  <div v-if="next.length" class="row" style="margin:12px 0 2px">
    <span v-for="(n, i) in next" :key="i" class="next-chip" :class="n.cls">{{ n.text }} <router-link class="btn small ghost" :to="n.to">{{ n.btn }} →</router-link></span>
  </div>
  <div class="grid-2 mt">
    <div class="stack">
      <TgPanel title="产出" :hint="isManual(t) ? '人工用例集：用例来自人工新增 / 导入' : `${(t.result || {}).review_rounds ?? 0} 轮评审${(t.result || {}).chunks > 1 ? ` · ${t.result.chunks} 分片` : ''}${(t.result || {}).passed === false ? ' · 含未通过项' : ''}`">
        <div class="grid-4">
          <TgStat label="模块" :value="num(moduleRows.length)" />
          <TgStat label="测试点" :value="num(ps.total)" :foot="ps.total ? `通过 ${ps.approved} · 待审 ${ps.pending} · 驳回 ${ps.rejected}` : '无独立测试点环节'" />
          <TgStat label="用例" :value="num(cs.total)" :foot="cs.total ? `正式 ${cs.approved} · 待审 ${cs.pending} · 驳回 ${cs.rejected}` : '等待生成'" />
          <TgStat label="盲区 / 重复" :value="`${(t.blind_spots || []).length} / ${(t.dup_pairs || []).length}`" :foot="`未处置 ${(t.blind_spots || []).filter(b => b.status === 'open').length} / ${(t.dup_pairs || []).filter(p => p.status === 'open').length}`" />
        </div>
        <div v-if="cs.total" class="row small" style="margin-top:12px">
          <span class="muted">优先级：</span><span v-for="p in ['P0', 'P1', 'P2', 'P3']" :key="p"><span class="badge" :class="p.toLowerCase()">{{ p }}</span> {{ cs[p] }}</span>
          <span class="muted" style="margin-left:12px">审核进度：</span><div class="minibar" :title="`${cs.approved} / ${cs.total}`"><span :style="{ width: pct(cs.approved, cs.total) }"></span></div> {{ pct(cs.approved, cs.total) }}
        </div>
        <TgTable v-if="moduleRows.length" class="mt" :cols="[{ key: 'module', title: '模块' }, { key: 'terminal', title: '端', w: '140px' }, { key: 'count', title: ps.total ? '测试点' : '用例', w: '80px', align: 'right' }, { key: 'approved', title: '已通过', w: '160px' }, ...(ps.total ? [{ key: 'cases', title: '用例', w: '70px', align: 'right' }] : [])]" :rows="moduleRows" row-key="module" dense>
          <template #cell-terminal="{ row }"><span v-if="row.terminal" class="tag">{{ row.terminal }}</span><span v-else class="muted">—</span></template>
          <template #cell-approved="{ row }"><span class="row nowrap"><div class="minibar"><span :style="{ width: pct(row.approved, row.count) }"></span></div> {{ row.approved }} / {{ row.count }}</span></template>
        </TgTable>
        <div v-if="(t.point_terminals || []).length" class="row small mt">
          <span class="muted">测试点按端：</span><span v-for="x in t.point_terminals" :key="x.key" class="tag">{{ x.label }} {{ x.total }}（通过 {{ x.approved }} · 待审 {{ x.pending }}<template v-if="x.pending_generation"> · 待生成 {{ x.pending_generation }}</template>）</span>
        </div>
      </TgPanel>

      <TgPanel title="质量自检" hint="_quality_report：评分仅供参考，不作为自动通过依据">
        <TgEmpty v-if="!quality" title="任务未完成，暂无质量摘要" />
        <template v-else>
          <TgKv :pairs="qPairs" />
          <div v-if="unresolved.length" class="mt">
            <div class="panel-title small">未解决评审问题（{{ unresolved.length }}）</div>
            <div class="list"><div v-for="(u, i) in unresolved.slice(0, 10)" :key="i"><span class="tag">评审</span> <b class="mono">{{ u.case_id }}</b> {{ u.problem }}</div></div>
          </div>
          <div v-if="dupCases.length" class="mt">
            <div class="panel-title small">疑似重复用例对（{{ dupCases.length }}）<span class="tiny muted">· 文字初筛 + 语义裁判，处置在用例审核页</span></div>
            <div class="list"><div v-for="(p, i) in dupCases.slice(0, 10)" :key="i"><span class="tag">{{ p.verdict || p.status || '重复' }}</span> <span class="mono">{{ p.a }}</span>「{{ p.a_title }}」≈ <span class="mono">{{ p.b }}</span>「{{ p.b_title }}」<span class="muted">（{{ typeof p.similarity === 'number' && p.similarity <= 1 ? (p.similarity * 100).toFixed(0) + '%' : p.similarity }}）</span></div></div>
          </div>
        </template>
      </TgPanel>

      <TgPanel title="知识注入 · 记忆 · 规则快照" hint="知识配额按 用例 : 文档 : 规则 = 5 : 3 : 2，拆解与生成各一次快照；规则 / 记忆按需求相关度挑选注入生成节点">
        <div class="grid-3">
          <div>
            <div class="panel-title small">知识 {{ (t.knowledge || []).length }} 片</div>
            <div v-if="(t.knowledge || []).length" class="list">
              <div v-for="(k, i) in t.knowledge.slice(0, 12)" :key="i"><span class="tag">{{ k.category || k.kind || '知识' }}</span> {{ k.source || k.title || k.doc || '' }}<span v-if="k.score != null" class="tiny muted"> · {{ Number(k.score).toFixed(3) }}</span><div v-if="k.text || k.content" class="tiny muted ellipsis">{{ k.text || k.content }}</div></div>
            </div>
            <div v-else class="tiny muted">本任务未注入知识</div>
          </div>
          <div>
            <div class="panel-title small">记忆 {{ (t.memories || []).length }} 条</div>
            <div v-if="(t.memories || []).length" class="list"><div v-for="(m, i) in t.memories" :key="i"><span class="tag">{{ m.kind || m.scope || '记忆' }}</span> {{ m.content || m.text }}</div></div>
            <div v-else class="tiny muted">未注入记忆</div>
          </div>
          <div>
            <div class="panel-title small">规则 {{ (t.rules || []).length }} 条</div>
            <div v-if="(t.rules || []).length" class="list"><div v-for="(r, i) in t.rules" :key="i"><span class="tag">{{ r.scope || '规则' }}</span> {{ r.content || r.text }}</div></div>
            <div v-else class="tiny muted">未注入规则</div>
          </div>
        </div>
        <div v-if="(t.reuse_hints || []).length" class="mt">
          <div class="panel-title small">历史用例复用提示</div>
          <div v-for="(h, i) in t.reuse_hints.slice(0, 5)" :key="i" class="source-block" style="margin-top:6px">{{ h.hint || h.text || h }}</div>
        </div>
      </TgPanel>
    </div>

    <div class="stack">
      <TgPanel title="任务信息">
        <template #actions><button v-if="!project && auth.isAdmin" type="button" class="btn small" @click="projDlg = { open: true, project: scope.projects.find(p => p.status !== 'archived')?.project || '', busy: false }">归属项目</button></template>
        <TgKv :pairs="[]">
          <dt>来源需求</dt><dd>
            <template v-if="reqLinks.length"><div v-for="r in reqLinks" :key="r.id"><router-link v-if="!r.deleted" :to="{ name: 'req-detail', params: { id: r.id } }">{{ r.title }}</router-link><span v-else class="muted">{{ r.title }}（需求已删除）</span></div></template>
            <template v-else>{{ (t.sources || []).map(s => s === 'text' ? '粘贴文本' : s).join('、') || '—' }}<span class="tiny muted">（直接上传）</span></template>
          </dd>
          <template v-for="([k, v]) in infoPairs" :key="k"><dt>{{ k }}</dt><dd>{{ v }}</dd></template>
          <dt>Prompt 版本</dt><dd>
            <div v-if="Object.keys(t.prompt_versions || {}).length" class="pill-row"><router-link v-for="(v, k) in t.prompt_versions" :key="k" class="tag" :to="{ name: 'prompt-detail', params: { key: k } }" :title="k">{{ promptNames[k] || k }} v{{ v }}</router-link></div>
            <span v-else class="muted">—</span>
          </dd>
          <dt>编辑占用</dt><dd>
            <template v-if="editing.length"><div v-for="e in editing" :key="e.kind + e.entity_id" class="row small"><TgIcon name="lock" :size="12" /> {{ e.by }} 正在编辑{{ e.kind === 'point' ? '测试点' : '用例' }} <span class="mono">{{ e.entity_id }}</span> <span class="tiny muted">{{ fmtTime(e.at) }}</span><button v-if="auth.isAdmin" type="button" class="btn small ghost" @click="release(e)">解除</button></div></template>
            <span v-else class="muted">无人编辑 · 300 秒无动作自动过期，不阻塞保存</span>
          </dd>
          <dt>关联计划</dt><dd>
            <template v-if="plans && plans.length"><div v-for="p in plans" :key="p.plan_id"><router-link :to="{ name: 'plan-detail', params: { id: p.plan_id } }">{{ p.name }}</router-link> <TgBadge :status="p.status" /> <span class="tiny muted">本任务 {{ p.task_cases }} 条<template v-if="p.summary?.latest_run"> · {{ p.summary.latest_run.executed }}/{{ p.summary.latest_run.total }} 已执行</template></span></div></template>
            <span v-else-if="plans" class="muted">未加入计划</span>
            <span v-else class="muted">{{ cs.total && !running ? '加载中…' : '等待用例通过' }}</span>
          </dd>
        </TgKv>
      </TgPanel>
      <TgPanel v-if="(t.revisions || []).length || t.offline_review || t.requirement_diff || (t.fix_log || []).length" title="留痕摘要">
        <div class="list">
          <div v-if="(t.revisions || []).length">对话修订 {{ t.revisions.length }} 轮 <router-link class="link small" :to="{ name: 'design-detail', params: { id: t.task_id, sub: 'revise' } }">查看</router-link></div>
          <div v-if="t.offline_review">离线终稿 {{ t.offline_review.applied ? '已按终稿覆盖' : '已对照' }} · 采纳率 {{ ((t.offline_review.stats?.adoption_rate || 0) * 100).toFixed(1) }}% <router-link class="link small" :to="{ name: 'design-detail', params: { id: t.task_id, sub: 'final' } }">查看</router-link></div>
          <div v-if="t.requirement_diff">需求变更分析 {{ t.requirement_diff.applied ? '已应用' : '待应用' }} · {{ (t.requirement_diff.changes || []).length }} 项变更 <router-link class="link small" :to="{ name: 'design-detail', params: { id: t.task_id, sub: 'reqdiff' } }">查看</router-link></div>
          <div v-if="(t.fix_log || []).length">AI 定点修改 {{ t.fix_log.length }} 次 <router-link class="link small" :to="{ name: 'design-detail', params: { id: t.task_id, sub: 'revise' } }">查看</router-link></div>
        </div>
      </TgPanel>
    </div>
  </div>
  <TgDialog v-model="projDlg.open" title="归属项目" ok="保存" :busy="projDlg.busy" :ok-disabled="!projDlg.project" @ok="saveProject">
    <TgField label="项目" required><select v-model="projDlg.project" class="select"><option v-for="p in scope.projects.filter(p => p.status !== 'archived')" :key="p.project" :value="p.project">{{ p.project }}</option></select></TgField>
    <div class="tiny muted">归属后自动建需求实体并回填关联，迁移期生成的执行计划一并改挂；已归属的任务不允许改挂。</div>
  </TgDialog>
</template>
<style scoped>
.next-chip { display: inline-flex; align-items: center; gap: 6px; border: 1px solid #e3e9f3; border-radius: 3px; padding: 4px 6px 4px 10px; font-size: 12px; background: #fff; }
.next-chip.orange { border-color: #ffe6c3; background: #fff6e9; color: #9a5a1e; }
.next-chip.red { border-color: #ffd4d8; background: #fff8f8; color: #c0303c; }
.next-chip.green { border-color: #bbe9db; background: #f1fbf7; color: #148f6e; }
.next-chip.purple { border-color: #e3d9fb; background: #f7f3ff; color: #8261d3; }
.next-chip.blue { border-color: #bdd0ff; background: #eef3ff; color: #3d68db; }
.panel-title.small { margin-bottom: 6px; }
</style>

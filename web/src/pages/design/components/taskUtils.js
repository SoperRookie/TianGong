/* 设计任务页共用的纯函数：标题、进度文案、流水线节点、统计。 */
import { terminalLabel } from '@/utils/terminals'

/** 任务主标题：需求名 › 人工用例集名 › 来源文件 › 任务 ID */
export function taskTitle(t) {
  if (!t) return ''
  const ctx = t.context || {}
  const titles = ctx.requirement_titles || []
  const src = (t.sources || []).filter(s => s !== 'text').join('、')
  return t.requirement_title || ctx.requirement_title || titles[0] || src || ((t.sources || []).includes('text') ? '粘贴文本' : t.task_id)
}
export const isManual = t => !!(t?.context || {}).manual

export const PROGRESS_TEXT = {
  queued: '排队中', parsing: '解析材料', analyzing: '需求分析与拆解', quality_check: '查漏 / 查重',
  generating_reviewing: '生成与评审', exporting: '导出产物',
}
export const progressText = p => PROGRESS_TEXT[p] || p || ''

export const ERROR_KIND_HINT = {
  quota: '模型欠费或配额用尽：请充值，或在「模型配置」切换默认模型后继续',
  auth: '模型密钥无效或未配置：请检查密钥配置后继续',
  model: '模型暂时不可用：稍后继续，或切换默认模型',
  cancelled: '任务已取消，可从断点继续',
}
export function resumeLabel(t) {
  const m = (t?.resume || {}).mode
  return m === 'fill' ? '补全失败模块' : m === 'resume' ? '继续完成' : m === 'restore' ? '恢复已完成结果' : '重试'
}
export function resumeHint(t) {
  const r = t?.resume || {}
  const parts = []
  if (t?.error_kind && ERROR_KIND_HINT[t.error_kind] && !(t.error_kind === 'cancelled' && t.status === 'completed')) parts.push(ERROR_KIND_HINT[t.error_kind])
  if (r.mode === 'resume') parts.push(`已完成${r.analysis_kept ? '拆解' : ''}${(r.kept_modules || []).length ? `与 ${r.kept_modules.length} 个模块（${r.kept_modules.join('、')}）` : ''}，继续时不重做`)
  else if (r.mode === 'fill') parts.push('只重新生成失败的模块，其余用例与审核状态不变')
  else if (r.mode === 'restore') parts.push('已完成任务上的增量生成被中断：恢复后已生成的用例与审核原样，新通过的测试点在测试点审核页再点生成')
  else if (r.mode === 'retry') parts.push('没有可复用的检查点，将沿用原需求、模型、模板整体重跑')
  return parts.join('；')
}
export function retryToast(r) {
  return r.mode === 'fill' ? `已开始补全 ${(r.failed_modules || []).length} 个模块`
    : r.mode === 'resume' ? `已从断点继续（保留 ${(r.kept_modules || []).length} 个已完成模块）`
    : r.mode === 'restore' ? `已恢复为已完成${r.pending_points ? `，${r.pending_points} 条新通过的测试点待生成` : ''}`
    : '已重新提交，后台执行中'
}

/** 展平测试点：[{...point, module}] */
export function allPoints(t) {
  const out = []
  for (const m of (t?.analysis || {}).test_points || []) for (const p of m.points || []) {
    if (typeof p === 'string') out.push({ point: p, tp_id: '', status: 'pending', module: m.module, legacy: true })
    else out.push({ ...p, module: m.module })
  }
  return out
}
export function pointStats(t) {
  const s = { total: 0, approved: 0, rejected: 0, pending: 0 }
  for (const p of allPoints(t)) { s.total++; s[p.status === 'approved' || p.status === 'rejected' ? p.status : 'pending']++ }
  return s
}
export function caseStats(t) {
  const s = { total: 0, approved: 0, rejected: 0, pending: 0, locked: 0, P0: 0, P1: 0, P2: 0, P3: 0 }
  const rv = t?.case_reviews || {}
  for (const c of (t?.result || {}).cases || []) {
    s.total++
    const st = (rv[c.uid] || {}).status || 'pending'
    s[st === 'approved' || st === 'rejected' ? st : 'pending']++
    if ((rv[c.uid] || {}).locked) s.locked++
    if (s[c.priority] != null) s[c.priority]++
  }
  return s
}
export const caseState = (t, c) => (t?.case_reviews || {})[c.uid] || { status: 'pending' }

/**
 * 8 节点流水线（设计方案 §4.7 / 原型 pipelineNodes）：返回 [{ name, sub, state: done|cur|todo|skip }]
 */
export function pipelineNodes(t) {
  const ctx = t.context || {}
  const ps = pointStats(t), cs = caseStats(t)
  const pts = allPoints(t)
  const modules = ((t.analysis || {}).test_points || []).length
  const derived = pts.filter(p => p.source === 'derived').length
  const combined = pts.filter(p => p.source === 'combined').length
  const blind = (t.blind_spots || []).length
  const dup = (t.dup_pairs || []).length
  const rounds = (t.result || {}).review_rounds
  const confirm = !!ctx.confirm_points
  const names = [
    ['解析材料', (t.sources || []).length ? `${t.sources.length} 份材料` : '需求实体'],
    ['拆解测试点', ps.total ? `${modules} 模块 · ${ps.total} 条` : '—'],
    ['经验延伸', derived ? `${derived} 条` : (ps.total ? '无新增' : '—')],
    ['查漏 / 组合', ps.total ? `盲区 ${blind} · 组合 ${combined}` : '—'],
    ['查重 / AI 评审', ps.total ? `疑似重复 ${dup}` : '—'],
    ['人工审核测试点', confirm ? (ps.total ? `待审 ${ps.pending} · 通过 ${ps.approved}` : '—') : '直接生成（跳过）'],
    ['生成用例', cs.total ? `${cs.total} 条` : '—'],
    ['评审 ≤ 3 轮', cs.total ? `${rounds != null ? rounds + ' 轮 · ' : ''}正式 ${cs.approved}` : '—'],
  ]
  let cur
  const st = t.status, pg = t.progress
  if (st === 'completed') cur = 9
  else if (st === 'awaiting_confirmation') cur = pg === 'quality_check' ? 4 : 6
  else if (st === 'queued') cur = 1
  else if (st === 'running') cur = pg === 'parsing' ? 1 : pg === 'analyzing' ? 2 : pg === 'quality_check' ? 4 : pg === 'exporting' ? 8 : 7
  else if (st === 'failed') cur = cs.total ? 8 : ps.total ? 7 : (t.sources || []).length ? 2 : 1
  else cur = 1   // cancelled 等
  return names.map(([name, sub], i) => {
    const n = i + 1
    let state = n < cur ? 'done' : n === cur ? 'cur' : 'todo'
    if (n === 6 && !confirm) state = cur > 6 ? 'done' : 'skip'
    if (st === 'failed' && n === cur) state = 'fail'
    return { name, sub, state }
  })
}

/** 用例按端计数：[{ key, label, n }] */
export function caseTerminalRows(t) {
  const ct = t.case_terminals || {}
  const cases = (t.result || {}).cases || []
  const terms = t.terminals && t.terminals.length ? t.terminals : []
  const rows = terms.map(x => ({ ...x, n: cases.filter(c => (ct[c.uid] || 'other') === x.key).length })).filter(x => x.n)
  return rows.map(x => ({ ...x, label: x.label || terminalLabel(x.key) }))
}

/** 模块树计数：每个模块名只计 1 次，记在树上最深的匹配节点（按 name 对 ModuleTree 的 cnt）；对不上的按原名记（计入总数）。 */
export function moduleCounts(names, flatModules = []) {
  const counts = {}
  for (const m of names) {
    if (!m) continue
    const hits = flatModules.filter(n => moduleMatch(m, n.name) || moduleMatch(m, n.path))
    const node = hits.sort((a, b) => (b.depth || 0) - (a.depth || 0))[0]
    const key = node ? node.name : m
    counts[key] = (counts[key] || 0) + 1
  }
  return counts
}
/** 模块筛选匹配：树节点名 / 路径对整名或任一段 */
export function moduleMatch(m, sel) {
  if (!sel) return true
  if (!m) return false
  if (m === sel || m.includes(sel)) return true
  const parts = String(m).split(/\s*[\/\-›>]\s*/).map(s => s.trim())
  return parts.includes(sel) || parts.join('/') === sel || sel.split('/').every(s => parts.includes(s))
}

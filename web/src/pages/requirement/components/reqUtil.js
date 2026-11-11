/* 需求域页面共用：阶段推算、待办摘要、模型 / 模板清单缓存、附件内容告警确认。 */
import { ref } from 'vue'
import { api } from '@/api/client'
import { useUiStore } from '@/stores/ui'

/** 需求流程条 7 步 */
export const REQ_STEPS = ['原文与附件', '整理稿', 'AI 分析', '待确认', '发起设计', '任务进度', '计划与执行']

/** 11 个分析项中有原文依据要求的四个事实项 */
export const AN_EVIDENCE_KEYS = ['rules', 'boundaries', 'state_changes', 'permissions']

/** 分析条目规整（旧数据可能是纯字符串） */
export const anItem = x => (x && typeof x === 'object')
  ? { text: x.text || '', quote: x.quote || '', verified: x.verified, review: x.review || null, manual: !!x.manual }
  : { text: String(x ?? ''), quote: '', verified: null, review: null, manual: false }

/** 当前应停留的步骤（列表与详情通用；详情带 trace 时可判到 6 / 7） */
export function reqStage(r) {
  if (!r) return 1
  const t = r.trace
  if (t && (t.coverage?.in_plan || t.coverage?.executed)) return 7
  if (t && t.coverage?.has_cases) return 6
  if (r.task_count || (r.tasks || []).length) return 6
  if (r.analysis_meta || r.analysis) return r.blocking_questions || r.open_questions ? 4 : 5
  if (r.extraction_status === 'current' || r.extraction) return 3
  return r.extraction_wanted ? 2 : 1
}

/** 每一步的完成态：done / warn / todo（列表阶段缩略与详情流程条共用） */
export function stepStates(r) {
  const stage = reqStage(r)
  const qn = typeof r.questions === 'number' ? r.questions : (r.questions || []).length
  const must = r.blocking_questions || 0
  const atts = r.attachments || []
  const hasTasks = !!(r.task_count || (r.tasks || []).length)
  const t = r.trace
  return [
    atts.length || r.raw_text || r.source_type ? 'done' : 'todo',
    r.extraction_status === 'current' || r.extraction ? (r.extraction_status === 'outdated' ? 'warn' : 'done') : (r.extraction_wanted ? 'warn' : 'todo'),
    r.analysis_meta || r.analysis ? (r.analysis_outdated ? 'warn' : 'done') : 'todo',
    qn ? (must ? 'warn' : 'done') : (r.analysis_meta || r.analysis ? 'done' : 'todo'),
    hasTasks ? 'done' : 'todo',
    t ? (t.coverage?.has_cases ? 'done' : (hasTasks ? 'warn' : 'todo')) : (hasTasks ? 'done' : 'todo'),
    t && (t.coverage?.in_plan || t.coverage?.executed) ? 'done' : (r.status === 'done' ? 'done' : 'todo'),
  ].map((s, i) => ({ state: s, cur: i + 1 === stage }))
}

/** 列表「待办」列：直接写数字 */
export function reqTodo(r) {
  const bits = []
  const qn = typeof r.questions === 'number' ? r.questions : (r.questions || []).length
  const must = r.blocking_questions || 0, open = r.open_questions || 0
  if (open) bits.push(`待确认 ${must} 必须 / ${open - must} 建议`)
  else if (qn) bits.push(`${qn} 事项已处理`)
  if (r.parsing) bits.push(`${r.parsing} 附件解析中`)
  if (r.parse_failed) bits.push(`${r.parse_failed} 附件解析失败`)
  if (r.extraction_status === 'outdated') bits.push('整理稿已过期')
  else if (r.extraction_status === 'none' && r.extraction_wanted) bits.push('待提取整理稿')
  if (r.review_pending) bits.push(`复核质疑 ${r.review_pending}`)
  if ((r.analysis_meta || r.analysis) && !r.analysis_confirmed) bits.push('分析未确认')
  else if (r.analysis_confirmed_stale) bits.push('已确认旧版本')
  if (r.analysis_outdated) bits.push('分析已过期')
  return bits.join(' · ') || '—'
}

const _models = ref(null)
let _modelsP = null
/** 模型清单（全局缓存一次）：{ default_model, models[{name,is_default}], requirement_extract_model } */
export function useModels() {
  if (!_modelsP) _modelsP = api.get('/api/v1/models').then(d => { _models.value = d; return d }).catch(() => { _models.value = { models: [], default_model: '' } })
  return _models
}

const _templates = ref(null)
let _templatesP = null
/** 用例模板清单（全局缓存一次）：{ default_id, templates[{template_id,name,is_default}] } */
export function useTemplates() {
  if (!_templatesP) _templatesP = api.get('/api/v1/templates').then(d => { _templates.value = d; return d }).catch(() => { _templates.value = { templates: [], default_id: '' } })
  return _templates
}

/**
 * 附件内容疑似非需求文本时后端 409 拦截：人看过告警确认后带 ignore_warnings 重试。
 * fn(ignore) 返回 Promise；用户取消时抛出 message 为「已取消」的错误。
 */
export async function withContentWarning(fn) {
  const ui = useUiStore()
  try { return await fn(false) } catch (e) {
    if (!/疑似非需求文本/.test(e?.message || '')) throw e
    const ok = await ui.confirm('附件内容疑似不是需求', (e.message || '').replace(/。确认要按这些内容继续.*$/, '') + '。仍要按这些内容继续吗？（会消耗大量模型调用，结果大概率不可用）', { ok: '仍然继续', danger: true })
    if (!ok) throw new Error('已取消')
    return await fn(true)
  }
}

/** 待确认事项结论框的预填值：历史结论参考 › AI 推荐 › 已有结论 */
export const suggestedAnswer = q => q?.suggested?.answer || q?.suggested_answer?.answer || ''

/** 事项来源标签 */
export function questionKindTag(q) {
  if (q.kind === 'unverified') return { text: '原文核验降级', cls: 'ai' }
  if (q.kind === 'review') return { text: '复核补充', cls: 'ai' }
  if (q.source === 'manual') return { text: '人工', cls: 'manual' }
  return { text: 'AI', cls: 'ai' }
}

/** 需求与底稿解析器名（展示用） */
export function parserName(a) {
  const f = a.filename || ''
  if (a.source_url) return '网页抓取'
  if (/\.zip$/i.test(f)) return 'Axure 原型包 / 压缩包'
  if (/\.(docx?|pdf|pptx?|xlsx?|xls|ppt|doc)$/i.test(f)) return 'Office / PDF'
  if (/\.(png|jpe?g|gif|bmp|webp)$/i.test(f)) return '图片理解'
  if (/\.(eml|msg)$/i.test(f)) return '邮件'
  if (/\.(html?|htm)$/i.test(f)) return '网页'
  return '文本'
}

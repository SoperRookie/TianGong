/* 模型单价表（美元 / 百万 token）：来自 GET /api/v1/models/config（仅管理员可读），用于前端估算调用费用。
   非管理员拿不到单价，costOf 返回 null，页面显示「—」。全局缓存一次。 */
import { ref } from 'vue'
import { api } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

let cache = null
let pending = null

export function usePrices() {
  const prices = ref(cache || {})
  async function load() {
    const auth = useAuthStore()
    if (!auth.isAdmin) return prices.value
    if (cache) { prices.value = cache; return cache }
    if (!pending) {
      pending = api.get('/api/v1/models/config').then(r => {
        const map = {}
        ;(r.models || []).forEach(m => { map[m.name] = { input: m.input_price || 0, output: m.output_price || 0 } })
        cache = map
        return map
      }).catch(() => { cache = {}; return cache })
    }
    prices.value = await pending
    return prices.value
  }
  /** 估算费用（美元）；单价表无此模型或单价为 0 时返回 null */
  const costOf = (model, promptTokens, completionTokens) => {
    const p = prices.value[model]
    if (!p || (!p.input && !p.output)) return null
    return (promptTokens || 0) / 1e6 * p.input + (completionTokens || 0) / 1e6 * p.output
  }
  const fmtCost = v => v == null ? '—' : '$' + (v < 0.01 && v > 0 ? v.toFixed(4) : v.toFixed(2))
  return { prices, load, costOf, fmtCost }
}

/** Prompt key → 中文名（与旧前端一致，调用日志 / 任务列表展示用） */
export const PROMPT_NAMES = {
  analyst: '需求拆解', requirement_analysis: '需求分析', generator: '用例生成', reviewer: '用例评审', fix_instruction: '定点修正指令',
  gap_check: '覆盖查漏', dup_judge: '语义查重', point_fix: '测试点修改', case_fix: '用例修改', requirement_diff: '需求变更', learning: '修改习惯学习',
  point_supplement: '测试点补充', point_compare: '人工与 AI 测试点对比', vision_image: '图片理解', e2e_scenarios: '端到端场景', terminal_classify: '章节归端',
  requirement_analysis_review: '需求分析复核', analysis_judge: '评测裁判', knowledge_cases_block: '注入·历史用例', knowledge_refs_block: '注入·需求规则',
  rules_block: '注入·规则', memory_block: '注入·记忆', point_extend: '经验延伸', point_combine: '组合延伸', requirement_outline: '长需求大纲',
  blind_spot_verify: '盲区复核', requirement_extract: '需求提取', requirement_extract_gap: '提取补漏', requirement_extract_crosscheck: '提取交叉核对',
}
export const promptName = k => PROMPT_NAMES[k] || k

/** 失败原因分类（与 app/api/routes.py _error_kind 同口径） */
export const ERROR_KINDS = {
  quota: ['欠费 / 配额', '模型欠费或配额用尽：请充值，或在「模型配置」切换默认模型后继续'],
  auth: ['密钥', '模型密钥无效或未配置：请检查 .env 中的密钥后继续'],
  model: ['模型故障', '模型暂时不可用（限流 / 超时 / 全链路失败）：稍后继续，或切换默认模型'],
  cancelled: ['已取消', '任务已取消，可从断点继续'],
  other: ['其他', ''],
}
export const errorKindLabel = k => (ERROR_KINDS[k] || ERROR_KINDS.other)[0]
export const errorKindHint = k => (ERROR_KINDS[k] || ERROR_KINDS.other)[1]

/** 「继续完成」按钮文案（按 resume.mode） */
export function resumeLabel(t) {
  const m = (t.resume || {}).mode
  return m === 'fill' ? '补全失败模块' : m === 'resume' ? '继续完成' : m === 'restore' ? '恢复已完成结果' : '重试'
}
export function resumeHint(t) {
  const r = t.resume || {}
  if (r.mode === 'resume') return `已完成${r.analysis_kept ? '拆解' : ''}${r.kept_modules?.length ? `与 ${r.kept_modules.length} 个模块（${r.kept_modules.join('、')}）` : ''}，继续时不重做`
  if (r.mode === 'fill') return `只重新生成失败的 ${r.failed_modules?.length || 0} 个模块，其余用例与审核状态不变`
  if (r.mode === 'restore') return '已完成任务上的增量生成被中断：恢复后已生成的用例与审核原样，新通过的测试点在「测试点审核」页再点生成'
  return ''
}
export function retryToast(r) {
  return r.mode === 'fill' ? `已开始补全 ${(r.failed_modules || []).length} 个模块`
    : r.mode === 'resume' ? `已从断点继续（保留 ${(r.kept_modules || []).length} 个已完成模块）`
    : r.mode === 'restore' ? `已恢复为已完成${r.pending_points ? `，${r.pending_points} 条新通过的测试点待生成` : ''}`
    : '已重新提交，后台执行中'
}
export const PROGRESS_TEXT = { parsing: '解析材料', analyzing: '拆解测试点', generating_reviewing: '生成与评审用例', exporting: '导出产物' }

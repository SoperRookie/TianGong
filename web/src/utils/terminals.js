/* 已知的端（与 app/terminals.py 内置一致）。一份需求实际有哪些端由提取时从文档里识别：清单里没有的按文档叫法新建，
   名称即 key（如「荷官端」），所以 terminalLabel 认不出的 key 原样显示；任务 / 整理稿的完整端清单以接口返回的 terminals / terminal_options 为准。 */
export const TERMINALS = [
  { key: 'h5', label: 'H5' },
  { key: 'game_admin', label: '游戏后台管理' },
  { key: 'merchant_admin', label: '商户后台' },
  { key: 'common', label: '通用' },
  { key: 'e2e', label: '端到端' },
  { key: 'other', label: '其他' },
]
export const BUSINESS_TERMINALS = TERMINALS.slice(0, 3)
export const terminalLabel = (key) => (TERMINALS.find(t => t.key === key) || {}).label || key || '其他'
const FIXED = ['common', 'e2e', 'other']
/** 端的展示顺序：已知业务端 → 文档自有的端（出现先后）→ 通用 / 端到端 / 其他。keys 为实际出现的端。 */
export function orderTerminalKeys(keys) {
  const seen = [...new Set((keys || []).filter(Boolean))]
  const known = TERMINALS.map(t => t.key)
  return [...known.filter(k => seen.includes(k) && !FIXED.includes(k)), ...seen.filter(k => !known.includes(k)), ...FIXED.filter(k => seen.includes(k))]
}
/** 任务里可选的端：接口返回的 task.terminals（含文档自有的端），没有就用已知清单。 */
export const taskTerminals = (task) => (task && task.terminals && task.terminals.length ? task.terminals : TERMINALS)

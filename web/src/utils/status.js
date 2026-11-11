/* 状态 → [文字, 颜色]；颜色只用 green / orange / red / blue / purple / gray 六种。 */
export const STATUS = {
  // 任务
  completed: ['已完成', 'green'], awaiting_confirmation: ['待确认测试点', 'orange'], failed: ['失败', 'red'], running: ['执行中', 'blue'], queued: ['排队中', 'gray'], cancelled: ['已取消', 'gray'],
  // 需求
  draft: ['草稿', 'gray'], analyzed: ['已分析', 'green'], pending_confirm: ['待确认', 'orange'], confirmed: ['已确认', 'green'], designing: ['设计中', 'blue'], done: ['已完成', 'green'], archived: ['已归档', 'gray'],
  // 项目 / 版本
  active: ['进行中', 'green'], paused: ['暂停', 'orange'], not_started: ['未开始', 'gray'], in_progress: ['进行中', 'blue'],
  // 评审
  approved: ['已通过', 'green'], pending: ['待审核', 'orange'], rejected: ['已驳回', 'red'], modified: ['已修改', 'blue'],
  // 执行
  pass: ['通过', 'green'], fail: ['失败', 'red'], blocked: ['阻塞', 'orange'], skipped: ['跳过', 'gray'], untested: ['未执行', 'gray'],
  // 计划
  planning: ['计划中', 'gray'], finished: ['已结束', 'green'], closed: ['已结束', 'green'],
  // 规则 / 依赖 / 记忆
  candidate: ['候选', 'orange'], ignored: ['已忽略', 'gray'], proposed: ['待确认', 'orange'], confirmed_edge: ['已确认', 'green'],
  // AI 调用 / 日志
  ok: ['成功', 'green'], error: ['失败', 'red'], success: ['成功', 'green'], partial: ['部分成功', 'orange'],
  // 待确认事项
  must: ['必须确认', 'orange'], suggest: ['建议确认', 'gray'], open: ['未确认', 'orange'], answered: ['已回答', 'green'], adopted: ['已采纳', 'green'], resolved: ['已确认', 'green'],
  // 底稿
  in: ['已收录', 'green'], ex: ['不收录', 'gray'], todo: ['待处理', 'orange'], review: ['待复核', 'purple'],
  // Prompt 版本
  inactive: ['未生效', 'gray'],
  // 用户
  enabled: ['启用', 'green'], disabled: ['禁用', 'gray'],
}
export function statusOf(key, fallback) {
  const s = STATUS[key]
  return s ? { text: s[0], color: s[1] } : { text: fallback || key || '—', color: 'gray' }
}
export const PRIORITIES = ['P0', 'P1', 'P2', 'P3']
export const TASK_STATUSES = [['', '全部'], ['queued', '排队中'], ['running', '执行中'], ['awaiting_confirmation', '待确认测试点'], ['completed', '已完成'], ['failed', '失败'], ['cancelled', '已取消']]
export const REQ_STATUSES = [['', '全部'], ['draft', '草稿'], ['analyzed', '已分析'], ['pending_confirm', '待确认'], ['confirmed', '已确认'], ['designing', '设计中'], ['done', '已完成'], ['archived', '已归档']]

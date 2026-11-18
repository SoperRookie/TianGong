/* 审核工作台会话：本次已处理计数、用时、底部状态条文案、撤销栈。审核流程内不弹 toast，只改这里。 */
import { ref, onBeforeUnmount } from 'vue'

export function useReviewSession() {
  const processed = ref(0)
  const elapsed = ref('00:00')
  const msg = ref('')            // 状态条文案
  const msgKind = ref('')        // '' | 'err' | 'warn'
  const history = ref([])        // [{ label, undo: async fn, id }]
  const startedAt = Date.now()
  const timer = setInterval(() => {
    const s = Math.floor((Date.now() - startedAt) / 1000)
    const p = n => String(n).padStart(2, '0')
    elapsed.value = s >= 3600 ? `${Math.floor(s / 3600)}:${p(Math.floor(s % 3600 / 60))}:${p(s % 60)}` : `${p(Math.floor(s / 60))}:${p(s % 60)}`
  }, 1000)
  onBeforeUnmount(() => clearInterval(timer))

  /** 写状态条：note('已通过 TP003')；note(msg, 'err') 标红 */
  function note(text, kind = '') { msg.value = text || ''; msgKind.value = kind }
  /** 记一次已处理，并登记撤销动作 */
  function done(label, undo, id) {
    processed.value += 1
    if (undo) history.value.push({ label, undo, id })
  }
  async function undoLast() {
    const h = history.value.pop()
    if (!h) { note('没有可撤销的操作', 'warn'); return null }
    try {
      await h.undo()
      processed.value = Math.max(0, processed.value - 1)
      note(`已撤销：${h.label}`)
      return h
    } catch (e) {
      note(`撤销失败：${e.message}`, 'err')
      return null
    }
  }
  return { processed, elapsed, msg, msgKind, history, note, done, undoLast }
}

/** 列表里「当前条之后的下一条待审」，没有则从头找；都没有返回 null */
export function nextPendingId(list, currentId, idKey, isPending) {
  const i = list.findIndex(x => x[idKey] === currentId)
  const after = list.slice(i + 1).find(isPending) || list.slice(0, Math.max(i, 0)).find(isPending)
  return after ? after[idKey] : null
}

/** 来源文字（与旧前端 srcText 同口径） */
export const SOURCE_TEXT = {
  ai: 'AI 拆解', gap: '查漏新增', supplement: 'AI 补充', ai_fix: '修改新增', derived: '经验延伸', combined: '组合延伸',
  manual: '人工', blind_spot: '盲区转入', e2e: '端到端', import: '导入', copy: '副本', final: '终稿',
}
export const sourceText = s => SOURCE_TEXT[s] || s || ''
/** 测试点维度（app/tasks/points.py DIMENSIONS） */
export const DIMENSIONS = ['正常流程', '异常流程', '输入校验', '边界值', '状态转换', '权限', '数据', '重复操作', '中断', '网络']
/** 测试点归端（与 app/terminals.py point_terminal 同口径） */
export const pointTerminal = p => p.terminal || (p.source === 'e2e' ? 'e2e' : 'other')

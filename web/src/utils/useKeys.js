import { onMounted, onBeforeUnmount } from 'vue'
const typing = e => {
  const t = e.target
  return t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)
}
/**
 * 页面级快捷键：useKeys({ j: next, k: prev, a: approve, 'shift+r': x, 'mod+enter': submit, escape: close })
 * 输入框内不触发（mod+enter / escape 除外）。返回解绑函数。
 */
export function useKeys(map, { enabled = () => true } = {}) {
  const handler = (e) => {
    if (!enabled()) return
    const key = e.key.length === 1 ? e.key.toLowerCase() : e.key.toLowerCase()
    const combo = (e.metaKey || e.ctrlKey ? 'mod+' : '') + (e.shiftKey && e.key.length > 1 ? 'shift+' : '') + (e.altKey ? 'alt+' : '') + (key === ' ' ? 'space' : key)
    const fn = map[combo] || (e.key === '?' ? map['?'] : null)
    if (!fn) return
    const allowInInput = combo.startsWith('mod+') || combo === 'escape'
    if (typing(e) && !allowInInput) return
    if (e.metaKey || e.ctrlKey) { if (!combo.startsWith('mod+')) return }
    e.preventDefault()
    fn(e)
  }
  onMounted(() => window.addEventListener('keydown', handler))
  onBeforeUnmount(() => window.removeEventListener('keydown', handler))
  return () => window.removeEventListener('keydown', handler)
}

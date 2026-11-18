/* 时间：后端一律 UTC ISO，按浏览器时区展示。 */
export function fmtTime(iso, withSeconds = false) {
  if (!iso) return ''
  const d = new Date(iso); if (isNaN(d)) return String(iso)
  const p = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}${withSeconds ? ':' + p(d.getSeconds()) : ''}`
}
export const fmtDate = iso => fmtTime(iso).slice(0, 10)
export const fmtShort = iso => fmtTime(iso).slice(5)
export function fmtAgo(iso) {
  if (!iso) return ''
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return '刚刚'
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} 天前`
  return fmtDate(iso)
}
export const fmtSize = n => n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : n >= 1024 ? (n / 1024).toFixed(1) + ' KB' : (n || 0) + ' B'
export const num = n => (n ?? 0).toLocaleString('zh-CN')
export const pct = (a, b, digits = 0) => b ? (a / b * 100).toFixed(digits) + '%' : '—'
export const shortId = (id, n = 8) => String(id || '').slice(0, n)
export const fmtDuration = ms => ms == null ? '' : ms < 1000 ? `${ms} ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.floor(ms / 60000)} 分 ${Math.round((ms % 60000) / 1000)} 秒`
export const fmtMoney = v => v == null ? '—' : '$' + Number(v).toFixed(v < 1 ? 4 : 2)
/** 轻量 markdown → HTML（标题 / 列表 / 粗体 / 代码 / 段落），内容先转义。 */
export const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]))
export function mdLite(s) {
  return esc(s || '')
    .replace(/^#### (.*)$/gm, '<h4>$1</h4>').replace(/^### (.*)$/gm, '<h3>$1</h3>').replace(/^## (.*)$/gm, '<h2>$1</h2>').replace(/^# (.*)$/gm, '<h1>$1</h1>')
    .replace(/^\s*[-•*] (.*)$/gm, '<li>$1</li>').replace(/^\s*(\d+)\. (.*)$/gm, '<li>$1. $2</li>')
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\n{2,}/g, '<p></p>').replace(/\n/g, '<br>')
}
export function copyText(text) {
  if (navigator.clipboard) return navigator.clipboard.writeText(text)
  const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select()
  try { document.execCommand('copy') } finally { ta.remove() }
  return Promise.resolve()
}

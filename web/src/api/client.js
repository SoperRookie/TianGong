/* 接口客户端：Bearer Token、JSON、401/403 统一处理、文件下载（令牌走请求头不进 URL）。 */
import { useAuthStore } from '@/stores/auth'

export class ApiError extends Error {
  constructor(message, status, data) { super(message); this.status = status; this.data = data || {} }
}

function qs(query) {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(query || {})) {
    if (v === undefined || v === null || v === '') continue
    p.set(k, String(v))
  }
  const s = p.toString()
  return s ? '?' + s : ''
}

/** 路径段编码：项目名可含「/」，后端 KeepEncodedSlashMiddleware 保留 %2F 不解码。 */
export const seg = (s) => encodeURIComponent(String(s ?? ''))

export async function request(path, { method = 'GET', body, query, headers = {}, raw = false, signal } = {}) {
  const auth = useAuthStore()
  const url = path + qs(query)
  const h = { ...headers }
  if (auth.token) h.Authorization = 'Bearer ' + auth.token
  let payload = body
  if (body !== undefined && body !== null && !(body instanceof FormData)) {
    h['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }
  const resp = await fetch(url, { method, headers: h, body: payload, signal })
  if (raw) return resp
  const data = await resp.json().catch(() => ({}))
  if (resp.status === 401 && !path.endsWith('/auth/login')) {
    auth.sessionExpired()
    throw new ApiError(data.detail || '请先登录', 401, data)
  }
  if (resp.status === 403 && data.must_change_password) {
    auth.requirePasswordChange()
    throw new ApiError(data.detail || '须先修改初始口令', 403, data)
  }
  if (!resp.ok) throw new ApiError(data.detail || resp.statusText || `HTTP ${resp.status}`, resp.status, data)
  return data
}

export const api = {
  get: (path, query, opts) => request(path, { query, ...opts }),
  post: (path, body, opts) => request(path, { method: 'POST', body, ...opts }),
  put: (path, body, opts) => request(path, { method: 'PUT', body, ...opts }),
  del: (path, body, opts) => request(path, { method: 'DELETE', body, ...opts }),
  /** multipart 上传：传 FormData */
  upload: (path, formData, opts) => request(path, { method: 'POST', body: formData, ...opts }),
  /** 下载：Authorization 头 + blob，文件名优先取响应头 */
  async download(path, filename, query) {
    const resp = await request(path, { query, raw: true })
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}))
      throw new ApiError(d.detail || resp.statusText, resp.status, d)
    }
    const cd = resp.headers.get('Content-Disposition') || ''
    const m = /filename\*=UTF-8''([^;]+)/i.exec(cd) || /filename="?([^";]+)"?/i.exec(cd)
    const name = filename || (m ? decodeURIComponent(m[1]) : 'download')
    const blob = await resp.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = name; document.body.appendChild(a); a.click()
    setTimeout(() => { URL.revokeObjectURL(url); a.remove() }, 1000)
    return name
  },
  /** 取文本（如 report.md） */
  async text(path, query) {
    const resp = await request(path, { query, raw: true })
    if (!resp.ok) { const d = await resp.json().catch(() => ({})); throw new ApiError(d.detail || resp.statusText, resp.status, d) }
    return resp.text()
  },
}
export default api

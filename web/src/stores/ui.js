/* 全局 UI：toast（带撤销）、确认对话框（Promise）、说明抽屉、全局搜索、面包屑附加段。 */
import { defineStore } from 'pinia'

let toastTimer = null, dialogResolve = null, toastSeq = 0

export const useUiStore = defineStore('ui', {
  state: () => ({
    toastItem: null,    // {id,msg,kind:'ok'|'err'|'warn'|'info',undo:fn|null}
    dialog: null,       // {title,body,ok,cancel,danger}
    help: false,
    search: false,
    profile: false,
    crumb: '',          // 页面可设置的位置条附加段（如需求标题）
    busy: 0,
  }),
  actions: {
    toast(msg, opts = {}) {
      const kind = typeof opts === 'string' ? opts : (opts.kind || 'ok')
      const undo = typeof opts === 'object' ? (opts.undo || null) : null
      this.toastItem = { id: ++toastSeq, msg, kind, undo }
      clearTimeout(toastTimer)
      toastTimer = setTimeout(() => { this.toastItem = null }, opts.duration || (undo ? 8000 : 4000))
    },
    error(e) {
      const msg = e?.message || String(e)
      this.toast(msg, { kind: 'err', duration: 6000 })
    },
    async undoToast() {
      const t = this.toastItem
      this.toastItem = null
      if (t?.undo) { try { await t.undo() } catch (e) { this.error(e) } }
    },
    /** await ui.confirm('删除？', '不可恢复', { ok:'删除', danger:true }) → boolean */
    confirm(title, body = '', opts = {}) {
      return new Promise(resolve => {
        dialogResolve = resolve
        this.dialog = { title, body, ok: opts.ok || '确定', cancel: opts.cancel || '取消', danger: !!opts.danger }
      })
    },
    closeDialog(result) {
      this.dialog = null
      if (dialogResolve) { dialogResolve(!!result); dialogResolve = null }
    },
    setCrumb(s) { this.crumb = s || '' },
  },
})

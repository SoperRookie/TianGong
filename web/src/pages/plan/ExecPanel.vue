<script setup>
/* 执行面板：左列表按模块分组（筛选 待执行 / 通过 / 失败 / 阻塞 / 跳过 + 只看我的）、右侧用例卡 + 结果按钮 P/F/B/S，失败须分类 + 原因（可传截图），提交 POST runs/{rid}/results；AI 归因；底部 sticky 状态条；j/k 移动 */
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { useKeys } from '@/utils/useKeys'
import { fmtTime, fmtSize } from '@/utils/format'
import { FAIL_REASONS, EXEC_STATUSES } from '@/utils/consts'
import { TgBadge, TgPrio, TgSeg, TgEmpty, TgIcon } from '@/components/ui'
import CaseSteps from './components/CaseSteps.vue'
import TriageBox from './components/TriageBox.vue'

const route = useRoute(), router = useRouter(), auth = useAuthStore(), ui = useUiStore()
const id = computed(() => String(route.params.id || '')), rid = computed(() => String(route.params.rid || ''))
const base = computed(() => `/api/v1/plans/${id.value}/runs/${rid.value}`)
const { data: plan, loading, error, reload } = useLoad(() => api.get(`/api/v1/plans/${id.value}`), { deps: [id] })
const run = computed(() => (plan.value?.runs || []).find(r => r.run_id === rid.value) || null)
const results = ref({}), summary = ref(null), attachments = ref([])
watch(run, r => { if (r) { results.value = { ...(r.results || {}) }; summary.value = r.summary; attachments.value = [...(r.attachments || [])] } })
watch(plan, p => { if (p) ui.setCrumb(`${p.name} / ${run.value?.name || ''}`) })
onBeforeUnmount(() => ui.setCrumb(''))
const project = computed(() => plan.value?.project || '')
const canRun = computed(() => auth.can(project.value, 'exec.run'))
const canAttach = computed(() => auth.can(project.value, 'exec.attach'))
const canProxy = computed(() => ['project_admin', 'test_lead'].includes(auth.roleIn(project.value)))
const active = computed(() => !!run.value && !run.value.finished_at)
const items = computed(() => plan.value?.items || [])
const total = computed(() => items.value.length)
const done = computed(() => Object.values(results.value).filter(r => EXEC_STATUSES.some(([k]) => k === r.status)).length)
const counts = computed(() => { const c = { pass: 0, fail: 0, blocked: 0, skipped: 0 }; Object.values(results.value).forEach(r => { if (c[r.status] != null) c[r.status]++ }); return c })
const passRate = computed(() => done.value ? (100 * counts.value.pass / done.value).toFixed(1) + '%' : '—')

/* 筛选与分组 */
const filter = ref(String(route.query.f || '')), mine = ref(route.query.mine !== '0')
const segItems = computed(() => [['', '全部', total.value], ['todo', '待执行', total.value - done.value], ['pass', '通过', counts.value.pass], ['fail', '失败', counts.value.fail], ['blocked', '阻塞', counts.value.blocked], ['skipped', '跳过', counts.value.skipped]])
const isMine = i => !i.assignee || i.assignee === auth.me
const list = computed(() => items.value.filter(i => {
  const r = results.value[i.item_id]
  if (mine.value && !isMine(i)) return false
  if (filter.value === 'todo') return !r
  if (filter.value) return r?.status === filter.value
  return true
}))
const groups = computed(() => { const g = new Map(); list.value.forEach(i => { const k = i.module || '（无模块）'; if (!g.has(k)) g.set(k, []); g.get(k).push(i) }); return [...g.entries()] })
watch([filter, mine], ([f, m]) => router.replace({ query: { ...route.query, f: f || undefined, mine: m ? undefined : '0' } }))

/* 选中项（进 query sel） */
const selId = ref(String(route.query.sel || ''))
const sel = computed(() => items.value.find(i => i.item_id === selId.value) || null)
const selResult = computed(() => sel.value ? results.value[sel.value.item_id] || null : null)
watch(list, l => { if (!l.length) return; if (!selId.value || !l.some(i => i.item_id === selId.value)) selId.value = (l.find(i => !results.value[i.item_id]) || l[0]).item_id }, { immediate: true })
watch(selId, v => { if (v !== String(route.query.sel || '')) router.replace({ query: { ...route.query, sel: v || undefined } }); failOpen.value = false; blockOpen.value = false; triageOpen.value = !!(results.value[v]?.triage); nextTick(() => document.querySelector(`[data-item="${v}"]`)?.scrollIntoView({ block: 'nearest' })) })
function move(d) { const l = list.value; if (!l.length) return; const i = l.findIndex(x => x.item_id === selId.value); selId.value = l[Math.max(0, Math.min(l.length - 1, i + d))].item_id }
function nextTodo() { const l = list.value, i = l.findIndex(x => x.item_id === selId.value); const nx = l.slice(i + 1).find(x => !results.value[x.item_id]) || l.find(x => !results.value[x.item_id]); if (nx) selId.value = nx.item_id }

/* 能否对当前用例打结果 */
const lock = computed(() => {
  if (!sel.value) return ''
  if (!active.value) return '本轮已结束，不能再记录结果；归因与生成报告仍可做。'
  if (!canRun.value) return '没有执行权限。'
  const a = sel.value.assignee
  if (a && a !== auth.me && !canProxy.value) return `已分配给 ${a}，只能由本人执行（负责人可代执行）。`
  return ''
})
const proxyTag = computed(() => sel.value?.assignee && sel.value.assignee !== auth.me && canProxy.value)

/* 结果提交 */
const failOpen = ref(false), blockOpen = ref(false), triageOpen = ref(false), submitting = ref(false)
const fail = ref({ reason: '系统缺陷', step: 0, note: '', files: [] })
const block = ref({ note: '' })
const lastMsg = ref('')
const STATUS_LABEL = Object.fromEntries(EXEC_STATUSES)
async function submit(status, note = '', reason = '') {
  if (!sel.value || lock.value || submitting.value) return
  const item = sel.value
  submitting.value = true
  try {
    const r = await api.post(`${base.value}/results`, { items: [{ item_id: item.item_id, status, note, reason }] })
    results.value = r.results; summary.value = r.summary
    if (!item.assignee) item.assignee = auth.me
    if (status === 'fail' && fail.value.files.length) await uploadFiles(fail.value.files, item.item_id)
    lastMsg.value = `已记录 ${item.case_id}：${STATUS_LABEL[status]}${r.results[item.item_id]?.on_behalf_of ? '（代执行）' : ''}`
    failOpen.value = false; blockOpen.value = false
    fail.value = { reason: '系统缺陷', step: 0, note: '', files: [] }; block.value = { note: '' }
    if (status === 'fail') triageOpen.value = true
    else nextTodo()
  } catch (e) { ui.error(e) } finally { submitting.value = false }
}
function onPass() { submit('pass') }
function onSkip() { submit('skipped') }
function onFail() { if (lock.value) return; blockOpen.value = false; failOpen.value = true; nextTick(() => document.getElementById('fail-note')?.focus()) }
function onBlock() { if (lock.value) return; failOpen.value = false; blockOpen.value = true; nextTick(() => document.getElementById('block-note')?.focus()) }
function confirmFail() {
  const f = fail.value
  if (!f.note.trim()) return ui.toast('失败须填写原因 / 缺陷号', 'warn')
  if (!FAIL_REASONS.includes(f.reason)) return ui.toast('请选择失败分类', 'warn')
  submit('fail', (f.step ? `【第 ${f.step} 步】` : '') + f.note.trim(), f.reason)
}
function confirmBlock() { if (!block.value.note.trim()) return ui.toast('阻塞须填写原因', 'warn'); submit('blocked', block.value.note.trim()) }
useKeys({
  p: onPass, f: onFail, b: onBlock, s: onSkip, j: () => move(1), k: () => move(-1),
  escape: () => { if (failOpen.value || blockOpen.value) { failOpen.value = false; blockOpen.value = false } else if (triageOpen.value) triageOpen.value = false },
  'mod+enter': () => { if (failOpen.value) confirmFail(); else if (blockOpen.value) confirmBlock() },
})

/* 附件（失败截图 / 单条附件） */
const fileInput = ref(null)
async function uploadFiles(files, itemId) {
  const list = [...(files || [])].filter(f => f && f.name)
  if (!list.length) return
  const fd = new FormData(); list.forEach(f => fd.append('files', f)); if (itemId) fd.append('item_id', itemId)
  try { const r = await api.upload(`${base.value}/attachments`, fd); attachments.value.push(...(r.attachments || [])); ui.toast(`已上传 ${r.count} 个附件`) } catch (e) { ui.error(e) }
  if (fileInput.value) fileInput.value.value = ''
}
const selAtts = computed(() => sel.value ? attachments.value.filter(a => a.item_id === sel.value.item_id) : [])
async function download(a) { try { await api.download(`/api/v1/plans/${id.value}/attachments/${a.att_id}`, a.filename) } catch (e) { ui.error(e) } }
function onTriaged(newResults) { results.value = { ...results.value, ...newResults } }

/* 结束本轮 / 用时 */
async function finish() {
  if (!await ui.confirm('结束本轮？', `已执行 ${done.value} / ${total.value}，结束后不能再记录结果 / 上传附件 / 采纳归因。`, { ok: '结束本轮', danger: true })) return
  try { await api.post(`${base.value}/finish`, {}); ui.toast('本轮已结束'); reload(true) } catch (e) { ui.error(e) }
}
const t0 = Date.now(), now = ref(Date.now())
let tick = null
onMounted(() => { tick = setInterval(() => { now.value = Date.now() }, 1000) })
onBeforeUnmount(() => clearInterval(tick))
const elapsed = computed(() => { const s = Math.floor((now.value - t0) / 1000); return s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分 ${s % 60} 秒` })
const rCls = s => ({ pass: 'pass', fail: 'fail', blocked: 'block', skipped: 'skip' })[s] || 'todo'
</script>
<template>
  <router-link class="back" :to="{ name: 'plan-detail', params: { id, sub: 'runs' } }"><TgIcon name="back" :size="14" /> {{ plan?.name || '计划' }} <span class="sep">/</span> {{ run?.name || '' }}</router-link>
  <div v-if="error" class="err-box">{{ error.message }}</div>
  <TgEmpty v-else-if="!loading && plan && !run" title="没有这个执行轮次" />
  <template v-if="plan && run">
    <div class="review-head">
      <h1 style="margin:0;font-size:19px;font-weight:780">执行面板 <TgBadge :status="active ? 'running' : 'finished'" :text="active ? '进行中' : '已结束'" /></h1>
      <div class="stats"><span>执行人 <b>{{ run.executor || auth.me }}</b></span><span>进度 <b>{{ done }} / {{ total }}</b></span><span>通过 <b class="ok">{{ counts.pass }}</b></span><span>失败 <b style="color:#db5361">{{ counts.fail }}</b></span><span>阻塞 <b>{{ counts.blocked }}</b></span><span>跳过 <b>{{ counts.skipped }}</b></span></div>
      <div class="right">
        <router-link class="btn" :to="{ name: 'plan-detail', params: { id, sub: 'attachments' }, query: { run: rid } }">整轮附件 ({{ attachments.length }})</router-link>
        <router-link class="btn" :class="{ disabled: !done }" :to="{ name: 'plan-detail', params: { id, sub: 'report' }, query: { run: rid } }">{{ run.report ? '查看报告' : '生成测试报告' }}</router-link>
        <button v-if="active && canRun" type="button" class="btn primary lg" @click="finish">结束本轮</button>
      </div>
    </div>
    <div class="workbench" style="grid-template-columns:330px minmax(0,1fr)">
      <div class="panel list-pane">
        <div class="list-tools">
          <TgSeg v-model="filter" :items="segItems" />
          <label class="row tiny" style="gap:4px"><input v-model="mine" type="checkbox" /> 只看分配给我 / 未分配的</label>
        </div>
        <div class="list-scroll">
          <template v-for="[mod, arr] in groups" :key="mod">
            <div class="grp"><span class="ellipsis" :title="mod">{{ mod }}</span><span>{{ arr.filter(i => results[i.item_id]).length }} / {{ arr.length }}</span></div>
            <div v-for="i in arr" :key="i.item_id" class="item exec-item" :class="{ sel: i.item_id === selId }" :data-item="i.item_id" @click="selId = i.item_id">
              <span class="r" :class="rCls(results[i.item_id]?.status)">{{ results[i.item_id] ? STATUS_LABEL[results[i.item_id].status] : '待执行' }}</span>
              <TgPrio :p="i.priority" /><span class="ttl" :title="i.title">{{ i.title }}</span>
              <span v-if="i.assignee && i.assignee !== auth.me" class="tiny muted">{{ i.assignee }}</span>
            </div>
          </template>
          <TgEmpty v-if="!list.length" title="没有符合筛选的用例" />
        </div>
      </div>
      <div class="panel panel-pad detail-pane">
        <template v-if="sel">
          <div class="detail-top">
            <span class="id">{{ sel.case_id }}</span><TgPrio :p="sel.priority" /><span class="tag" :title="sel.module">{{ (sel.module || '无模块').slice(0, 30) }}</span>
            <TgBadge v-if="selResult" :status="selResult.status" /><span v-else class="badge gray">待执行</span>
            <span class="meta">快照 v{{ sel.version_no }} · 执行人 {{ sel.assignee || '未分配（执行即认领）' }}<template v-if="selResult"> · {{ selResult.by }} {{ fmtTime(selResult.at) }}<template v-if="selResult.on_behalf_of"> · 代 {{ selResult.on_behalf_of }} 执行</template> · 复测 {{ selResult.history?.length || 0 }}</template></span>
          </div>
          <div class="detail-title">{{ sel.title }}</div>
          <CaseSteps :snapshot="sel.snapshot" :result="selResult" :failed-step="selResult?.triage?.failed_step || 0" />
          <div v-if="selResult && (selResult.status === 'fail' || selResult.status === 'blocked')" class="attention" style="margin-top:10px">
            <TgIcon name="alert" :size="14" /><div style="flex:1"><b>{{ selResult.status === 'fail' ? '失败' : '阻塞' }}：</b><span v-if="selResult.reason" class="tag">{{ selResult.reason }}</span> {{ selResult.note || '（没填）' }}<template v-if="selResult.triage"> · AI 归因：<b>{{ selResult.triage.category }}</b>（置信度 {{ selResult.triage.confidence }}）<span v-if="selResult.triage.adopted" class="tag">已采纳</span></template></div>
            <button v-if="selResult.status === 'fail'" type="button" class="btn small" @click="triageOpen = !triageOpen">{{ triageOpen ? '收起归因' : (selResult.triage ? '查看归因' : 'AI 归因') }}</button>
          </div>
          <div v-if="selResult?.note && selResult.status !== 'fail' && selResult.status !== 'blocked'" class="tiny muted" style="margin-top:8px">说明：{{ selResult.note }}</div>
          <div v-if="lock" class="information" style="margin-top:12px">{{ lock }}</div>
          <div v-else class="detail-actions result-row">
            <button type="button" class="btn success solid" :disabled="submitting" @click="onPass"><kbd>P</kbd> 通过</button>
            <button type="button" class="btn danger" :disabled="submitting" @click="onFail"><kbd>F</kbd> 失败</button>
            <button type="button" class="btn" :disabled="submitting" @click="onBlock"><kbd>B</kbd> 阻塞</button>
            <button type="button" class="btn" :disabled="submitting" @click="onSkip"><kbd>S</kbd> 跳过</button>
            <template v-if="canAttach"><button type="button" class="btn" @click="fileInput?.click()"><TgIcon name="upload" :size="14" /> 附件</button><input ref="fileInput" type="file" multiple hidden @change="uploadFiles($event.target.files, sel.item_id)" /></template>
            <span class="ai">{{ proxyTag ? `代 ${sel.assignee} 执行 · ` : '' }}记录后自动跳下一条 · 同轮重复执行旧记录进复测历史</span>
          </div>
          <div v-if="failOpen" class="fail-form">
            <div><div class="tiny" style="font-weight:700;margin-bottom:5px">失败分类（必填）</div><select v-model="fail.reason" class="select" style="width:100%"><option v-for="r in FAIL_REASONS" :key="r">{{ r }}</option></select></div>
            <div><div class="tiny" style="font-weight:700;margin-bottom:5px">失败步骤</div><select v-model="fail.step" class="select" style="width:100%"><option :value="0">未指定</option><option v-for="(s, i) in (sel.snapshot?.steps || [])" :key="i" :value="i + 1">第 {{ i + 1 }} 步</option></select></div>
            <div class="full"><textarea id="fail-note" v-model="fail.note" class="field" rows="3" placeholder="原因 / 缺陷号（必填）：实际结果是什么" /></div>
            <div class="full drop" @dragover.prevent @drop.prevent="fail.files = [...fail.files, ...$event.dataTransfer.files]">
              <label style="cursor:pointer"><TgIcon name="upload" :size="14" /> 拖放或选择截图 · 记录失败后自动上传并关联本条 · AI 归因会读取截图 <input type="file" multiple hidden accept="image/*,.log,.txt,.mp4,.zip" @change="fail.files = [...fail.files, ...$event.target.files]" /></label>
              <div v-if="fail.files.length" class="tiny" style="margin-top:4px">{{ fail.files.map(f => f.name).join('、') }} <span class="link" @click.stop="fail.files = []">清空</span></div>
            </div>
            <div class="full row" style="justify-content:flex-end"><button type="button" class="btn small" @click="failOpen = false">取消 <kbd>Esc</kbd></button><button type="button" class="btn danger small" :disabled="submitting" @click="confirmFail">记录失败 <kbd>⌘⏎</kbd></button></div>
          </div>
          <div v-if="blockOpen" class="fail-form" style="border-color:#ffe6c3;background:#fff9f1">
            <div class="full"><div class="tiny" style="font-weight:700;margin-bottom:5px">阻塞原因（必填）</div><textarea id="block-note" v-model="block.note" class="field" rows="2" placeholder="环境 / 依赖 / 数据等阻塞原因，或缺陷号" /></div>
            <div class="full row" style="justify-content:flex-end"><button type="button" class="btn small" @click="blockOpen = false">取消 <kbd>Esc</kbd></button><button type="button" class="btn small primary" :disabled="submitting" @click="confirmBlock">记录阻塞并跳下一条 <kbd>⌘⏎</kbd></button></div>
          </div>
          <TriageBox v-if="triageOpen && selResult?.status === 'fail'" :plan-id="id" :run-id="rid" :item="sel" :result="selResult" :active="active" :can-run="canRun" @updated="onTriaged" @close="triageOpen = false" />
          <div v-if="selAtts.length" class="mt"><div class="tiny" style="font-weight:700;margin-bottom:4px">本条附件</div><div class="row"><span v-for="a in selAtts" :key="a.att_id" class="tag link" @click="download(a)">{{ a.filename }} · {{ fmtSize(a.size) }}</span></div></div>
          <div v-if="selResult?.history?.length" class="mt"><div class="tiny" style="font-weight:700;margin-bottom:4px">复测历史</div><div class="list"><div v-for="(h, i) in selResult.history" :key="i"><TgBadge :status="h.status" /> {{ h.by }} · {{ fmtTime(h.at) }}<span v-if="h.reason" class="tag" style="margin-left:4px">{{ h.reason }}</span><span v-if="h.note" class="muted"> · {{ h.note }}</span></div></div></div>
        </template>
        <TgEmpty v-else title="没有用例" hint="调整左侧筛选，或回计划加入用例。" />
      </div>
    </div>
    <div class="statusbar">
      <span>已执行 <b>{{ done }}</b> / {{ total }}</span><span>通过率 <b>{{ passRate }}</b></span><span>用时 {{ elapsed }}</span>
      <span v-if="lastMsg" class="ok">{{ lastMsg }}</span>
      <span class="kbd-hint"><kbd>j</kbd><kbd>k</kbd> 移动 · <kbd>P</kbd><kbd>F</kbd><kbd>B</kbd><kbd>S</kbd> 结果 · <kbd>⌘⏎</kbd> 提交失败 / 阻塞 · <kbd>Esc</kbd> 收起</span>
      <span class="undo" @click="nextTodo">跳到下一条待执行</span>
    </div>
  </template>
</template>
<style scoped>
.btn.disabled { pointer-events: none; opacity: .5; }
.list-tools .seg { flex-wrap: wrap; }
.grp .ellipsis { max-width: 220px; }
.detail-top .meta { flex-basis: 100%; margin-left: 0; }
</style>

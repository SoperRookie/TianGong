<script setup>
/*
 ② 整理稿：提取 / 重新提取（POST /extract）、预览 / 逐条审核（POST /extraction/items）、按端汇总（端从文档里识别）与章节归端 / 新建端 / 重新识别端（POST /extraction/terminals、/extraction/terminals/detect）、
 底稿核对（LedgerPanel）、整篇改写（PUT /extraction）、完成审核（POST /extraction/review-done）、历史版本（GET history、history/{index}、POST restore）、删除（DELETE /extraction）。
*/
import { ref, computed, watch } from 'vue'
import { api } from '@/api/client'
import { useUiStore } from '@/stores/ui'
import { fmtTime, fmtAgo, num, pct } from '@/utils/format'
import { TgBadge, TgDrawer, TgEmpty, TgMd, TgStat } from '@/components/ui'
import { useModels, withContentWarning } from './reqUtil'
import ExtractionOutline from './ExtractionOutline.vue'
import LedgerPanel from './LedgerPanel.vue'
const props = defineProps({ r: { type: Object, required: true }, canEdit: Boolean, canAi: Boolean })
const emit = defineEmits(['reload'])
const ui = useUiStore(), models = useModels()
const model = ref(''), extracting = ref(false)
const view = ref('preview')   // preview / all / unverified / gap / forced / doubt / draft / reviewed / ledger
const editing = ref(false), editText = ref('')
const termOpen = ref(true)
const histOpen = ref(false), hist = ref(null), histSel = ref(''), histView = ref(null)
const ledgerKey = ref(0)
const ex = computed(() => props.r.extraction), m = computed(() => ex.value?.meta || {}), st = computed(() => props.r.extraction_status)
const ls = computed(() => ex.value?.ledger?.stats || null), li = computed(() => ex.value?.ledger?.image || null)
const gap = computed(() => m.value.gap || null)
const items = computed(() => (ex.value?.modules || []).flatMap(x => x.items || []))
const counts = computed(() => ({ all: items.value.length, unverified: items.value.filter(it => it.verified === false && !it.review).length, gap: items.value.filter(it => it.gap && !it.forced).length, forced: items.value.filter(it => it.forced).length, doubt: items.value.filter(it => it.doubt).length, draft: items.value.filter(it => it.draft).length, reviewed: items.value.filter(it => it.review).length }))
const filters = computed(() => [['preview', '预览'], ['all', `逐条审核 ${counts.value.all}`], ['unverified', `⚠ 待核对 ${counts.value.unverified}`], ['gap', `补漏 ${counts.value.gap}`], ['forced', `兜底 ${counts.value.forced}`], ['doubt', `疑点 ${counts.value.doubt}`], ['draft', `疑似草稿 ${counts.value.draft}`], ['reviewed', `已处理 ${counts.value.reviewed}`], ['ledger', `底稿核对${ls.value && (ls.value.todo || ls.value.review_open) ? ` ${(ls.value.todo || 0) + (ls.value.review_open || 0)}` : ''}`]])
const HINT = { preview: '整理稿正文预览（页面 › 区块 › 条目）；要核对通过 / 修改 / 删除条目切到「逐条审核」', ledger: '底稿 = 产品原文的每一句，每句的去向：收录 / 排除（原因与判定方）/ 待处理。要人看的只有「需复核」和「待处理」；待处理为 0、需复核都看过才能完成审核' }
const hintDefault = '⚠ = 没在材料里逐字找到，请对照原文核对通过、改成原句或删除；〔补漏〕= 模型判定是需求后补进的原句；〔兜底〕= 模型没判定去向、程序按原句收进来的；〔疑点〕= 原文本身的问题，原句照抄不改；〔疑似草稿〕= 画布上与主体不相连的旧版本区块'
const ts = computed(() => ex.value?.terminal_summary)
const termEdits = computed(() => ex.value?.terminal_edits || {}), termReasons = computed(() => ex.value?.terminal_reasons || {}), termNotes = computed(() => ex.value?.terminal_notes || {})
const bizTermCount = computed(() => (ts.value?.terminals || []).filter(t => !['common', 'other'].includes(t.key)).length)
const newTermFor = ref(''), newTermName = ref(''), detecting = ref(false)
watch(() => props.r.req_id, () => { view.value = 'preview'; editing.value = false; histOpen.value = false })

async function extract() {
  extracting.value = true
  try {
    const x = await withContentWarning(ignore => api.post(`/api/v1/requirements/${props.r.req_id}/extract`, { model: model.value || null, ignore_warnings: ignore }))
    const mm = x.extraction?.meta || {}, g = mm.gap || {}, l = mm.ledger || {}
    ui.toast(`提取完成：${mm.modules || 0} 个模块 ${mm.checked || 0} 条${g.added ? `，补漏 +${g.added}` : ''}${g.forced ? `，兜底 ${g.forced}` : ''}${l.total ? `；底稿 ${l.total} 句全部有去向` : ''}${mm.unverified ? `，${mm.unverified} 条未逐字找到请核对` : ''}`, { kind: mm.unverified || g.forced ? 'warn' : 'ok' })
    view.value = 'preview'; editing.value = false; emit('reload')
  } catch (e) { if (e.message !== '已取消') ui.error(e); emit('reload') } finally { extracting.value = false }
}
async function review({ item_id, action, text }) {
  if (action === 'delete' && !await ui.confirm('删除这条摘录？', '删除后整理稿正文同步更新，已有分析结果会标为基于旧输入。', { ok: '删除', danger: true })) return
  try {
    const x = await api.post(`/api/v1/requirements/${props.r.req_id}/extraction/items`, { item_id, action, text: text ?? null })
    if (action === 'edit') { const it = (x.extraction?.modules || []).flatMap(mm => mm.items).find(y => y.id === item_id); ui.toast(it && it.verified === false ? '已修改，但改后的文字仍没在材料里逐字找到' : '已修改，逐字核验通过', { kind: it && it.verified === false ? 'warn' : 'ok' }) }
    else ui.toast(action === 'ok' ? '已核对通过' : '已删除')
    ledgerKey.value++; emit('reload')
  } catch (e) { ui.error(e) }
}
async function setTerminal(section, terminal) {
  if (terminal === '__new__') { newTermFor.value = section; newTermName.value = ''; return }   // 新建端：就地填名称
  try { await api.post(`/api/v1/requirements/${props.r.req_id}/extraction/terminals`, { section, terminal }); ui.toast(`「${section}」已归到新的端`); newTermFor.value = ''; emit('reload') } catch (e) { ui.error(e) }
}
function submitNewTerminal() {
  const name = newTermName.value.trim()
  if (!name) return ui.toast('请填端的名称', { kind: 'warn' })
  setTerminal(newTermFor.value, name)
}
async function detectTerminals() {
  detecting.value = true
  try {
    const x = await api.post(`/api/v1/requirements/${props.r.req_id}/extraction/terminals/detect`, { model: model.value || null })
    ui.toast(`已重新识别：${(x.extraction?.terminal_summary?.terminals || []).map(t => t.label).join(' / ') || '没有识别出端'}`)
    emit('reload')
  } catch (e) { ui.error(e) } finally { detecting.value = false }
}
function startEdit() { editText.value = ex.value?.text || ''; editing.value = true }
async function saveText() {
  try { await api.put(`/api/v1/requirements/${props.r.req_id}/extraction`, { text: editText.value }); ui.toast('整理稿已保存'); editing.value = false; ledgerKey.value++; emit('reload') } catch (e) { ui.error(e) }
}
async function reviewDone() {
  try { await api.post(`/api/v1/requirements/${props.r.req_id}/extraction/review-done`, {}); ui.toast('整理稿已标记为已审核'); emit('reload') } catch (e) { ui.error(e) }
}
async function removeExtraction() {
  if (!await ui.confirm('删除整理稿？', 'AI 分析与测试设计将退回使用原始材料（原文 + 附件解析文本）；开着自动提取时下次分析会再提一次。', { ok: '删除', danger: true })) return
  try { await api.del(`/api/v1/requirements/${props.r.req_id}/extraction`); ui.toast('整理稿已删除'); view.value = 'preview'; emit('reload') } catch (e) { ui.error(e) }
}
async function openHistory() {
  histOpen.value = true; histSel.value = ''; histView.value = null
  try { hist.value = await api.get(`/api/v1/requirements/${props.r.req_id}/extraction/history`) } catch (e) { ui.error(e) }
}
async function viewVersion(idx) {
  histView.value = null
  if (idx === '') return
  try { histView.value = await api.get(`/api/v1/requirements/${props.r.req_id}/extraction/history/${idx}`) } catch (e) { ui.error(e) }
}
async function restore(idx) {
  if (!await ui.confirm('恢复历史版本？', '当前整理稿会进入历史，恢复的版本成为当前版；已有分析结果会标为基于旧输入。', { ok: '恢复' })) return
  try { await api.post(`/api/v1/requirements/${props.r.req_id}/extraction/restore`, { index: idx }); ui.toast('已恢复'); histOpen.value = false; emit('reload') } catch (e) { ui.error(e) }
}
const brief = v => `${fmtTime(v.at)} · ${v.model || (v.auto ? '自动' : v.by || '')} · ${v.checked || 0} 条${v.unverified ? ` · ⚠ ${v.unverified}` : ''}${v.edited ? ' · 整篇改写' : ''}${v.reviewed ? ' · 已审核' : ''}${v.restored_from ? ' · 恢复版' : ''}${v.rev ? ` · 改动 ${v.rev} 次` : ''}`
</script>
<template>
  <div class="panel-head">
    <div><div class="panel-title">② 整理稿</div><div class="panel-hint">从全部材料逐字摘录，按页面 › 区块归整；四层流水线（提取 → 交叉核对 → 补漏 → 底稿核验）。AI 分析与测试设计以整理稿为准，改动后需重新分析。</div></div>
    <div class="row">
      <template v-if="canAi">
        <select v-model="model" class="select small" style="width:150px" title="提取模型：逐字摘录、输出量大，低档模型会压成摘要"><option value="">默认（{{ models?.requirement_extract_model || models?.default_model || '未配置' }}）</option><option v-for="x in models?.models || []" :key="x.name" :value="x.name">{{ x.name }}</option></select>
        <button type="button" class="btn small" :class="{ primary: !ex }" :disabled="extracting || st === 'running' || r.parsing" @click="extract"><span v-if="extracting || st === 'running'" class="spinner" /> {{ extracting || st === 'running' ? '提取中' : ex ? '重新提取' : '提取整理稿' }}</button>
      </template>
      <template v-if="ex && st !== 'running'">
        <button type="button" class="btn small" @click="openHistory">历史版本{{ r.extraction_history_count ? `（${r.extraction_history_count}）` : '' }}</button>
        <button v-if="canEdit && !editing" type="button" class="btn small" @click="startEdit" title="整篇改写后逐条结构失效，不能再逐条审核">整篇改写</button>
        <button v-if="canEdit && ex.modules && !ex.reviewed" type="button" class="btn small primary" @click="reviewDone" title="⚠ 条目都处理过、底稿没有待处理句、需复核都看过才能标已审核">完成审核</button>
        <button v-if="canEdit" type="button" class="btn small danger" @click="removeExtraction">删除</button>
      </template>
    </div>
  </div>
  <div v-if="st === 'running' || extracting" class="information" style="margin-bottom:10px"><span class="spinner" /> 正在从原文与附件提取需求内容（分层提取 → 核验 → 自动补漏 → 底稿核验），可离开页面，完成后自动刷新</div>
  <div v-if="r.extraction_error" class="err-box" style="margin-bottom:10px">上次提取失败：{{ r.extraction_error }}</div>
  <div v-if="st === 'outdated'" class="attention" style="margin-bottom:10px">⚠ 提取之后附件有追加或重新解析，整理稿基于旧材料：AI 分析与测试设计会先重新提取（或点「重新提取」）</div>
  <div v-else-if="st === 'none' && r.extraction_wanted" class="attention" style="margin-bottom:10px">有已解析的附件但还没有整理稿{{ r.content_warnings?.length ? '（附件有告警：疑似非需求文本 / 图片识图失败，自动提取已跳过）' : '' }}：点「提取整理稿」，或直接 AI 分析时会先自动提取</div>
  <div v-else-if="st === 'none' && !ex" class="information" style="margin-bottom:10px">没有附件的需求直接用原文分析，不需要整理稿；原文很长或夹杂无关内容时也可以提取</div>

  <template v-if="ex">
    <div class="grid-4" style="margin-bottom:12px">
      <TgStat label="逐字核验" :value="`${num(m.verified || 0)}/${num(m.checked || 0)}`" :foot="`未逐字找到 ${m.unverified || 0} · 分片 ${m.chunks || 1} · 模块 ${m.modules || 0}`" />
      <TgStat label="底稿去向" :value="ls ? `${num(ls.in)} 收录` : '—'" :foot="ls ? `排除 ${ls.ex}（模型 ${ls.ex_by?.model || 0} / 程序 ${ls.ex_by?.auto || 0} / 人工 ${ls.ex_by?.user || 0}）· 待处理 ${ls.todo}` : '未核算'" />
      <TgStat label="补漏" :value="gap ? `+${gap.added || 0}` : '—'" :foot="gap?.after?.lines ? `覆盖 ${gap.before?.covered ?? '—'} → ${gap.after.covered} / ${gap.after.lines} 行（${pct(gap.after.covered, gap.after.lines)}）· ${gap.rounds || 0} 轮${gap.forced ? ` · 兜底 ${gap.forced}` : ''}` : ''" />
      <TgStat label="需复核 / 疑点" :value="`${ls?.review_open || 0} / ${m.crosscheck?.doubts ?? counts.doubt}`" :foot="`需复核未看 ${ls?.review_open || 0}${li?.total ? ` · 图中文字 ${li.total} 句（收录 ${li.in}）` : ''}`" />
    </div>
    <div class="row" style="margin-bottom:8px">
      <span class="tag">{{ m.model || '—' }}</span><span class="tag">{{ m.auto ? '自动触发' : (m.by || '手动') }}</span><span class="tag">{{ fmtTime(m.at) }}</span>
      <span class="tiny muted">材料 {{ num(m.source_chars || 0) }} 字 → 整理稿 {{ num(m.chars || 0) }} 字</span>
      <TgBadge v-if="ex.reviewed" status="approved" :text="`已审核 ${ex.reviewed.by || ''} ${fmtAgo(ex.reviewed.at)}`" />
      <span v-if="ex.edited" class="tiny muted">整篇改写 {{ ex.edited.by }} {{ fmtAgo(ex.edited.at) }}</span>
      <span v-if="m.restored_from" class="tiny muted">从历史版本恢复</span>
      <span v-if="(m.dropped || []).length" class="tiny muted">已剔除：{{ m.dropped.slice(0, 5).join('、') }}{{ m.dropped.length > 5 ? '…' : '' }}</span>
    </div>

    <!-- 按端汇总：端由模型在提取时从文档里识别 -->
    <details v-if="ts?.terminals?.length" :open="termOpen" class="term-fold" @toggle="e => termOpen = e.target.open">
      <summary>按端汇总 · {{ bizTermCount }} 个端 · 总需求 {{ ts.total.items }} 条（{{ ts.total.sections }} 个章节 / {{ ts.total.pages }} 页 / {{ ts.total.blocks }} 区块）：{{ ts.terminals.map(t => `${t.label} ${t.items}`).join(' · ') }}</summary>
      <div class="row tiny muted" style="margin:6px 0;align-items:center">
        <span style="flex:1;min-width:260px">这份需求有哪些端由模型在提取完成时从文档里识别（不用配置），再按整理稿顶层章节（文件夹 / 页面）归进去，拿不准的按章节名关键词兜底；判错的在右侧改，也可以选「＋ 新的端…」自己起一个。发起测试设计时测试点按这里的端分列表、按端单独生成用例与导出。<template v-if="ex.terminal_detected">上次重新识别：{{ ex.terminal_detected.by || '' }} {{ fmtAgo(ex.terminal_detected.at) }}</template></span>
        <button v-if="canAi" type="button" class="btn small" :disabled="detecting" title="让模型按现有整理稿重新识别端并归类（一次调用，不重新提取）；人工改过的章节保持不动" @click="detectTerminals">{{ detecting ? '识别中…' : '重新识别端' }}</button>
      </div>
      <table class="table dense">
        <thead><tr><th style="width:150px">端</th><th style="width:170px">规模</th><th>章节 › 页面（条目数）</th></tr></thead>
        <tbody>
          <tr v-for="tm in ts.terminals" :key="tm.key">
            <td><b>{{ tm.label }}</b><span v-if="tm.custom" class="tag ai" style="margin-left:4px" title="端清单里没有、按这份文档的叫法识别出来的端">文档</span><div v-if="termNotes[tm.key]" class="tiny muted">{{ termNotes[tm.key] }}</div></td>
            <td class="tiny">{{ tm.pages }} 页 · {{ tm.blocks }} 区块 · <b>{{ tm.items }}</b> 条</td>
            <td>
              <div v-for="sec in tm.sections" :key="sec.key" class="row" style="padding:2px 0">
                <span :title="termReasons[sec.key] ? '判定依据：' + termReasons[sec.key] : ''">{{ sec.no ? sec.no + '. ' : '' }}{{ sec.key }}<span v-if="termEdits[sec.key]" class="tag manual" style="margin-left:4px" :title="`${termEdits[sec.key].by || ''} ${fmtAgo(termEdits[sec.key].at)}`">人工</span></span>
                <span class="tiny muted">{{ sec.group ? sec.pages.map(p => `${p.title}（${p.items}）`).join('、') : (sec.pages[0] ? sec.pages[0].items + ' 条' : '') }}</span>
                <select v-if="canEdit" class="select small" style="width:120px" :value="sec.terminal" title="改这个章节所属的端（之后发起的测试设计按改后的端归测试点）" @change="setTerminal(sec.key, $event.target.value)"><option v-for="o in ex.terminal_options || []" :key="o.key" :value="o.key">{{ o.label }}</option><option value="__new__">＋ 新的端…</option></select>
                <template v-if="canEdit && newTermFor === sec.key">
                  <input v-model="newTermName" class="input small" style="width:150px" maxlength="16" placeholder="端的名称，如 荷官端" @keyup.enter="submitNewTerminal" />
                  <button type="button" class="btn small primary" @click="submitNewTerminal">确定</button>
                  <button type="button" class="btn small" @click="newTermFor = ''; emit('reload')">取消</button>
                </template>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </details>

    <!-- 整篇改写 -->
    <template v-if="editing">
      <textarea v-model="editText" class="field" rows="24" style="font-family:inherit;line-height:1.6" />
      <div class="row" style="margin-top:6px"><button type="button" class="btn small primary" @click="saveText">保存整理稿</button><button type="button" class="btn small" @click="editing = false">取消</button><span class="tiny muted">整篇改写后逐条结构失效；保存后已有分析结果会标为基于旧输入，需重新分析</span></div>
    </template>
    <!-- 没有逐条结构：只能整篇查看 -->
    <template v-else-if="!ex.modules">
      <div class="tiny muted" style="margin-bottom:6px">这份整理稿没有逐条结构（整篇改写过或旧版本提取），只能整篇查看 / 改写；重新提取后可逐条审核</div>
      <TgMd :text="ex.text" style="max-height:640px" />
    </template>
    <template v-else>
      <div class="row" style="margin-bottom:8px">
        <div class="seg"><button v-for="[k, t] in filters" :key="k" type="button" :class="{ on: view === k }" @click="view = k">{{ t }}</button></div>
      </div>
      <div class="tiny muted" style="margin-bottom:8px">{{ HINT[view] || hintDefault }}</div>
      <LedgerPanel v-if="view === 'ledger'" :key="ledgerKey" :r="r" :can-edit="canEdit" @changed="emit('reload')" />
      <TgMd v-else-if="view === 'preview'" :text="ex.text" style="max-height:640px" />
      <ExtractionOutline v-else :ex="ex" :filter="view" :can-edit="canEdit" @review="review">
        <template #badge="{ it }">
          <span v-if="it.verified === false" style="color:var(--amber)" title="未在材料中逐字找到">⚠ </span>
          <span v-if="it.forced" class="badge red" title="模型没判定去向，程序按原句收进来的；请核对：留下、改归模块，或在底稿核对里排除">兜底</span>
          <span v-else-if="it.gap" class="badge orange" title="模型判定是需求后补进的原句">补漏</span>
          <span v-if="it.manual" class="badge green" :title="it.inherited ? `沿用需求「${it.inherited.title || ''}」底稿核对时的人工收录` : '底稿核对里人工按原句收录'">人工收录{{ it.inherited ? '·沿用' : '' }}</span>
          <span v-if="it.draft" class="badge red" title="画布上与页面主体不相连、与正式内容重复却有出入的区块（旧版本 / 备用稿）：已照抄收录，用例不要照它写">疑似草稿</span>
          <span v-if="it.review" class="badge" :class="it.review.status === 'ok' ? 'green' : 'blue'" :title="`${it.review.by || ''} ${fmtAgo(it.review.at)}`">{{ it.review.status === 'ok' ? '已核对' : it.review.status === 'included' ? '人工收录' : '已修改' }}</span>
        </template>
        <template #ops="{ it, edit }">
          <template v-if="canEdit">
            <button v-if="it.verified === false && !it.review" type="button" class="btn ghost small" title="核对通过：原文确有此意" @click="review({ item_id: it.id, action: 'ok' })">✓</button>
            <button type="button" class="btn ghost small" title="修改（改成材料里的原句会重新核验）" @click="edit()">改</button>
            <button type="button" class="btn ghost small danger" title="删除此条" @click="review({ item_id: it.id, action: 'delete' })">删</button>
          </template>
        </template>
      </ExtractionOutline>
      <div class="kbd-hint mt">逐条审核：<kbd>✓</kbd> 核对通过 · <kbd>改</kbd> 修改 · <kbd>删</kbd> 删除单条摘录；人工改过的条目记入已处理（{{ counts.reviewed }} 条）</div>
    </template>
  </template>
  <TgEmpty v-else-if="st !== 'running' && !extracting" title="还没有整理稿" :hint="r.extraction_wanted ? '附件解析完成后自动提取；也可手动点击「提取整理稿」' : '纯文本需求可直接进入 AI 分析；原文很长或夹杂无关内容时可提取整理稿'" />

  <!-- 历史版本 -->
  <TgDrawer v-model="histOpen" title="整理稿历史版本" sub="最近 10 版留档；选一个版本看与当前版的逐行差异，可恢复为当前" wide>
    <div v-if="!hist" class="page-loading"><span class="spinner" /> 加载中…</div>
    <template v-else>
      <div class="tiny muted" style="margin-bottom:8px"><b>当前版</b> {{ hist.current ? brief(hist.current) : '—' }}</div>
      <TgEmpty v-if="!hist.history.length" title="没有历史版本" />
      <template v-else>
        <div class="row">
          <select v-model="histSel" class="select" style="flex:1" @change="viewVersion(histSel)"><option value="">选择历史版本查看差异…</option><option v-for="v in hist.history" :key="v.index" :value="String(v.index)">历史 {{ v.index + 1 }}：{{ brief(v) }}</option></select>
          <button v-if="histView && canEdit" type="button" class="btn small primary" @click="restore(histView.index)">恢复此版本为当前</button>
        </div>
        <template v-if="histView">
          <div class="tiny muted" style="margin:8px 0">与当前版相比：当前多出 <b style="color:var(--green)">{{ histView.diff.added.length }}</b> 行，历史多出 <b style="color:var(--red)">{{ histView.diff.removed.length }}</b> 行</div>
          <div class="form-grid">
            <div><div class="tiny muted">当前有、历史没有</div><pre class="pre"><template v-for="(l, i) in histView.diff.added" :key="i"><span class="diff-add">+ {{ l }}</span>
</template><template v-if="!histView.diff.added.length">（无）</template></pre></div>
            <div><div class="tiny muted">历史有、当前没有</div><pre class="pre"><template v-for="(l, i) in histView.diff.removed" :key="i"><span class="diff-del">- {{ l }}</span>
</template><template v-if="!histView.diff.removed.length">（无）</template></pre></div>
          </div>
          <details class="mt"><summary class="tiny muted" style="cursor:pointer">历史版本全文</summary><pre class="pre">{{ histView.text }}</pre></details>
        </template>
      </template>
    </template>
  </TgDrawer>
</template>
<style scoped>
.term-fold { border: 1px solid var(--line); border-radius: 4px; padding: 8px 12px; margin-bottom: 12px; }
.term-fold summary { cursor: pointer; font-weight: 650; color: #455368; }
</style>

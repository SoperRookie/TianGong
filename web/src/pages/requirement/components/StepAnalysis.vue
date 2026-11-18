<script setup>
/*
 ③ AI 分析：POST /analyze（模型 / 复核模型 / 独立复核）；11 项按 4 组展示；原文核验与复核标记（analysis_meta.verification / review）；
 人工改删补 / 采纳改正 / 原条目没问题（POST /analysis/items）；确认并沉淀（POST /analysis/confirm）/ 取消确认（DELETE）；加入评测集（POST /analysis/eval-sample）；历史结果回看。
*/
import { ref, computed, watch } from 'vue'
import { api } from '@/api/client'
import { useUiStore } from '@/stores/ui'
import { fmtTime, fmtAgo } from '@/utils/format'
import { TgBadge, TgEmpty, TgTabs } from '@/components/ui'
import { useModels, withContentWarning, anItem, AN_EVIDENCE_KEYS } from './reqUtil'
const props = defineProps({ r: { type: Object, required: true }, canEdit: Boolean, canAi: Boolean })
const emit = defineEmits(['reload', 'go'])
const ui = useUiStore(), models = useModels()
const model = ref(''), reviewer = ref(''), review = ref(true), analyzing = ref(false)
const hist = ref(-1)   // -1 当前；否则 analysis_history 下标
const group = ref('a'), key = ref('features'), kw = ref('')
const editing = ref(null), editText = ref(''), adding = ref(false), addText = ref('')
const GROUPS = [['a', '功能 / 前置 / 流程', ['features', 'preconditions', 'normal_flows', 'exception_flows']], ['b', '规则 / 边界 / 状态', ['rules', 'boundaries', 'state_changes']], ['c', '权限 / 依赖 / 风险', ['permissions', 'dependencies', 'risks']]]
const labels = computed(() => props.r.analysis_labels || {})
const history = computed(() => props.r.analysis_history || [])
const viewingOld = computed(() => hist.value >= 0 && hist.value < history.value.length)
const an = computed(() => viewingOld.value ? history.value[hist.value].analysis : props.r.analysis)
const meta = computed(() => viewingOld.value ? (history.value[hist.value].meta || {}) : (props.r.analysis_meta || {}))
const v = computed(() => meta.value.verification || null), rv = computed(() => meta.value.review || null)
const c = computed(() => props.r.analysis_confirmed), pending = computed(() => props.r.review_pending || 0)
const openN = computed(() => (props.r.questions || []).filter(q => q.status === 'open' && (q.level || 'must') === 'must').length)
const count = k => (an.value?.[k] || []).length
const flagged = k => (an.value?.[k] || []).filter(x => { const it = anItem(x); return it.review && (it.review.verdict === 'wrong' || it.review.verdict === 'unsupported') }).length
const total = computed(() => Object.keys(labels.value).filter(k => k !== 'open_questions').reduce((n, k) => n + count(k), 0))
const groupTabs = computed(() => GROUPS.map(([k, label, keys]) => ({ key: k, label, count: keys.reduce((n, x) => n + count(x), 0) })).concat([{ key: 'q', label: '待确认事项', count: openN.value || count('open_questions') }]))
const keyTabs = computed(() => (GROUPS.find(g => g[0] === group.value)?.[2] || []).map(k => ({ key: k, label: (labels.value[k] || k) + (flagged(k) ? ' ⚠' : ''), count: count(k) })))
watch(group, g => { const keys = GROUPS.find(x => x[0] === g)?.[2]; if (keys && !keys.includes(key.value)) key.value = keys[0]; kw.value = '' })
const editable = computed(() => props.canEdit && !viewingOld.value)
const items = computed(() => (an.value?.[key.value] || []).map((x, i) => ({ i, n: i + 1, ...anItem(x) })).filter(x => !kw.value.trim() || x.text.toLowerCase().includes(kw.value.trim().toLowerCase()) || x.quote.toLowerCase().includes(kw.value.trim().toLowerCase())))

async function analyze() {
  analyzing.value = true
  try {
    const x = await withContentWarning(ignore => api.post(`/api/v1/requirements/${props.r.req_id}/analyze`, { model: model.value || null, reviewer_model: reviewer.value || null, review: review.value, ignore_warnings: ignore }))
    ui.toast(x.open_questions ? `分析完成，${x.open_questions} 项待确认事项需要确认` : '分析完成，无待确认事项', { kind: x.open_questions ? 'warn' : 'ok' })
    hist.value = -1; emit('reload'); if (x.blocking_questions) emit('go', 4)
  } catch (e) { if (e.message !== '已取消') ui.error(e) } finally { analyzing.value = false }
}
async function op(body) {
  try { await api.post(`/api/v1/requirements/${props.r.req_id}/analysis/items`, body); editing.value = null; adding.value = false; addText.value = ''; emit('reload') } catch (e) { ui.error(e) }
}
async function del(it) { if (!await ui.confirm('删除分析条目', '删除后不可恢复（重新分析会重新生成）。确定？', { ok: '删除', danger: true })) return; op({ key: key.value, action: 'delete', index: it.i }) }
function startEdit(it) { editing.value = it.i; editText.value = it.text }
function saveEdit(it) { if (!editText.value.trim() || editText.value === it.text) { editing.value = null; return } op({ key: key.value, action: 'edit', index: it.i, text: editText.value.trim() }) }
async function confirm(undo) {
  if (undo && !await ui.confirm('取消确认', '取消后，知识库中由这份分析沉淀的文档会一并移除。确定？', { ok: '取消确认', danger: true })) return
  try {
    if (undo) await api.del(`/api/v1/requirements/${props.r.req_id}/analysis/confirm`); else await api.post(`/api/v1/requirements/${props.r.req_id}/analysis/confirm`, {})
    ui.toast(undo ? '已取消确认，知识库中的沉淀已移除' : '已确认，分析结果正在沉淀到项目知识库'); emit('reload')
  } catch (e) { ui.error(e) }
}
async function evalSample() {
  if (!await ui.confirm(props.r.eval_sample ? '更新评测集标准答案' : '加入评测集', '把当前分析结果（人工过目 / 修改后）作为这份需求的标准答案存入需求分析评测集；以后每次改 Prompt、换模型，管理员可在「需求分析评测」页回归评测。确定？')) return
  try { await api.post(`/api/v1/requirements/${props.r.req_id}/analysis/eval-sample`, {}); ui.toast('已存入评测集'); emit('reload') } catch (e) { ui.error(e) }
}
</script>
<template>
  <div class="panel-head">
    <div><div class="panel-title">③ AI 分析结果</div><div class="panel-hint">11 项结构化输出 → 原文核验（四个事实项无依据的降级为待确认）→ 独立复核（理解错 / 无依据 / 遗漏）→ 推荐答案。人工可逐条改 / 删 / 补，修改留痕进评测集与学习规则。</div></div>
    <div class="row">
      <template v-if="canAi">
        <select v-model="model" class="select small" style="width:140px" title="分析模型"><option value="">默认（{{ models?.default_model || '未配置' }}）</option><option v-for="x in models?.models || []" :key="x.name" :value="x.name">{{ x.name }}</option></select>
        <select v-model="reviewer" class="select small" style="width:150px" title="独立复核模型：建议与分析不同"><option value="">复核：同分析模型</option><option v-for="x in models?.models || []" :key="x.name" :value="x.name">{{ x.name }}</option></select>
        <label class="row tiny"><input type="checkbox" v-model="review" /> 独立复核</label>
        <button type="button" class="btn small" :class="{ primary: !r.analysis }" :disabled="analyzing || r.parsing || r.extraction_status === 'running'" @click="analyze"><span v-if="analyzing" class="spinner" /> {{ analyzing ? (r.extraction_wanted && r.extraction_status !== 'current' ? '先提取再分析' : '分析中') : r.analysis ? '重新分析' : 'AI 需求分析' }}</button>
      </template>
      <template v-if="r.analysis && !viewingOld && canEdit">
        <button type="button" class="btn small" :disabled="!!pending" @click="evalSample" title="把当前分析结果作为这份需求的标准答案存入评测集">{{ r.eval_sample ? '更新评测集' : '加入评测集' }}</button>
        <template v-if="c && !r.analysis_confirmed_stale"><TgBadge status="approved" text="已确认并沉淀" /><button type="button" class="btn small" @click="confirm(true)">取消确认</button></template>
        <template v-else><button type="button" class="btn success solid small" :disabled="!!pending" @click="confirm(false)" title="rules / boundaries / state_changes / permissions → 业务规则库，其余 → 需求文档库；人工改过的自动入评测集">确认并沉淀</button><button v-if="c" type="button" class="btn small" @click="confirm(true)">取消确认</button></template>
      </template>
    </div>
  </div>
  <div class="tiny muted" style="margin-bottom:8px">
    <template v-if="r.extraction_status === 'current'"><b>分析输入：需求整理稿</b>（{{ r.extraction?.meta?.chars || 0 }} 字{{ r.extraction?.edited ? '，人工修改过' : '' }}）+ 人工补充。<a class="link" @click="emit('go', 2)">查看整理稿 →</a></template>
    <template v-else-if="r.extraction_wanted"><b>分析输入：原始材料</b>（原文 + 附件解析文本）。{{ r.extraction_status === 'running' ? '需求提取进行中，完成后分析将以整理稿为准。' : r.extraction_status === 'outdated' ? '整理稿已过期，点分析时会先重新提取。' : '点分析时会先自动提取需求整理稿。' }}</template>
    <template v-else><b>分析输入：需求原文</b> + 人工补充。</template>
  </div>
  <TgEmpty v-if="!an" title="还没有分析结果" :hint="canAi ? '整理稿确认后点击「AI 需求分析」：11 项结构化输出 → 原文核验 → 独立复核 → 推荐答案' : 'AI 只整理需求已写明的内容，每条带原文依据并逐条程序核验'" />
  <template v-else>
    <div class="row" style="margin-bottom:10px">
      <select v-if="history.length" v-model.number="hist" class="select small" style="width:250px"><option :value="-1">当前结果（{{ fmtTime(r.analysis_meta?.at) }}）</option><option v-for="(h, i) in [...history].reverse()" :key="i" :value="history.length - 1 - i">历史 {{ history.length - i }}：{{ fmtTime(h.meta?.at) }} · {{ h.meta?.model || '' }}</option></select>
      <span v-if="viewingOld" class="badge orange">正在查看历史结果</span>
      <span class="tiny muted">{{ meta.model || '' }} · {{ meta.chunks > 1 ? `${meta.chunks} 片 · ` : '' }}{{ fmtTime(meta.at) }} · {{ meta.by || '自动' }}<template v-if="meta.knowledge"> · 知识注入 {{ meta.knowledge }} 片</template><template v-if="history.length"> · 历史 {{ history.length }} 次</template><template v-if="r.analysis_edit_count"> · 人工修改 {{ r.analysis_edit_count }} 处</template></span>
    </div>
    <div class="row" style="margin-bottom:10px">
      <TgBadge v-if="v" :status="v.unverified ? 'partial' : 'ok'" :text="`原文核验 ${v.verified}/${v.checked}`" />
      <TgBadge v-if="v?.demoted" status="pending_confirm" :text="`降级为待确认 ${v.demoted}`" />
      <TgBadge v-if="rv" :status="rv.error ? 'error' : rv.skipped ? 'skipped' : 'ok'" :text="rv.error ? `独立复核失败：${rv.error}` : rv.skipped ? `独立复核跳过：${rv.skipped}` : `独立复核（${rv.model || ''}）理解错 ${rv.wrong} · 无依据 ${rv.unsupported} · 补充 ${rv.added}${rv.unverified_added ? ` · 补充未通过核验 ${rv.unverified_added}` : ''}`" />
      <TgBadge v-else status="skipped" text="未做独立复核" />
      <span class="tiny muted right">四个事实项（规则 / 边界 / 状态 / 权限）无原文依据的条目会被移出并降为「必须确认」事项</span>
    </div>
    <div v-if="!viewingOld && r.analysis_outdated" class="attention" style="margin-bottom:10px">⚠ 分析之后附件或整理稿有变化，当前结果基于旧输入，建议重新分析</div>
    <div v-if="!viewingOld" class="information" style="margin-bottom:10px">
      <template v-if="pending"><TgBadge status="rejected" :text="`复核质疑 ${pending} 条未处理`" /> 逐条采纳改正 / 手动修改 / 删除 / 标记没问题后才能确认与沉淀</template>
      <template v-else-if="!c"><TgBadge status="pending" text="未确认 · 未沉淀到知识库" /> 请过目各分析项（可逐条改 / 删 / 补），确认无误后沉淀到知识库</template>
      <template v-else-if="r.analysis_confirmed_stale"><TgBadge status="candidate" text="已确认旧版本" /> 知识库中是 {{ c.by || '' }} {{ fmtAgo(c.at) }} 确认的那一版，之后重新分析或人工修改过，需再次确认</template>
      <template v-else><TgBadge status="approved" text="已确认 · 已沉淀到知识库" /> {{ c.by || '' }} · {{ fmtAgo(c.at) }}</template>
      <span v-if="r.eval_sample" class="tiny" :title="`标准答案更新于 ${fmtTime(r.eval_sample.updated_at)}`">· 已在评测集</span>
    </div>
    <div v-if="openN" class="attention" style="margin-bottom:10px">分析产出 {{ openN }} 项「必须确认」的待确认事项，确认后才能发起测试设计。<button type="button" class="btn small" @click="emit('go', 4)">去确认</button></div>

    <TgTabs v-model="group" :items="groupTabs" />
    <template v-if="group === 'q'">
      <div class="list"><div v-for="(q, i) in an.open_questions || []" :key="i"><b class="mono">{{ i + 1 }}</b> {{ typeof q === 'string' ? q : q.question }}</div></div>
      <TgEmpty v-if="!(an.open_questions || []).length" title="没有待确认事项" />
      <div class="mt"><button type="button" class="btn small" @click="emit('go', 4)">到第 ④ 步处理待确认事项 →</button></div>
    </template>
    <template v-else>
      <div class="row" style="margin-bottom:8px">
        <div class="seg"><button v-for="t in keyTabs" :key="t.key" type="button" :class="{ on: key === t.key }" @click="key = t.key">{{ t.label }} {{ t.count }}</button></div>
        <span v-if="AN_EVIDENCE_KEYS.includes(key)" class="tiny muted">事实项：每条须有原文依据，带入测试设计与业务规则库</span>
        <input v-model="kw" class="field small right" style="width:200px" placeholder="在本项内筛选…" />
      </div>
      <div v-if="items.length" class="list">
        <div v-for="it in items" :key="it.i">
          <div class="row" style="align-items:flex-start">
            <b class="mono" style="min-width:24px">{{ it.n }}</b>
            <div style="flex:1;min-width:0">
              <template v-if="editing === it.i">
                <textarea v-model="editText" class="field" rows="2" />
                <div class="row" style="margin-top:4px"><button type="button" class="btn small primary" @click="saveEdit(it)">保存</button><button type="button" class="btn small" @click="editing = null">取消</button><span class="tiny muted">以需求原文为准修改（修改会留痕，进评测集与学习规则）</span></div>
              </template>
              <template v-else>
                <span style="white-space:pre-wrap">{{ it.text }}</span>
                <span v-if="it.manual" class="tag manual" style="margin-left:6px">人工</span>
                <span v-if="it.review?.verdict === 'added'" class="tag ai" style="margin-left:6px" title="独立复核认为首轮分析遗漏，已通过原文核验后补入">复核补充</span>
                <div v-if="it.quote" class="tiny" style="color:#4165a0;margin-top:3px"><span v-if="it.verified === true" class="ok">✓ 原文依据</span><span v-else class="badge orange">⚠ 依据与原文不符</span>：「{{ it.quote }}」</div>
                <div v-else-if="AN_EVIDENCE_KEYS.includes(key) && !it.manual" class="tiny muted" style="margin-top:3px">未给出原文依据</div>
                <div v-if="it.review && it.review.verdict !== 'added'" class="review-box" :class="it.review.verdict === 'wrong' ? 'wrong' : 'unsupported'">
                  <b>复核：{{ it.review.verdict === 'wrong' ? '理解错' : '无依据' }}</b> {{ it.review.note || '' }}
                  <div v-if="it.review.verdict === 'wrong' && it.review.fix">建议改为：<b>{{ it.review.fix }}</b></div>
                  <div v-if="editable" class="row" style="margin-top:4px">
                    <button v-if="it.review.verdict === 'wrong' && it.review.fix" type="button" class="btn small primary" @click="op({ key, action: 'accept_fix', index: it.i })">采纳改正</button>
                    <button type="button" class="btn small" @click="startEdit(it)">手动修改</button>
                    <button v-if="it.review.verdict !== 'wrong'" type="button" class="btn small danger" @click="del(it)">删除</button>
                    <button type="button" class="btn small" @click="op({ key, action: 'dismiss_review', index: it.i })">原条目没问题</button>
                  </div>
                </div>
              </template>
            </div>
            <span v-if="editable && editing !== it.i" class="row tiny nowrap"><a class="link" @click="startEdit(it)">改</a><a class="link" style="color:var(--red)" @click="del(it)">删</a></span>
          </div>
        </div>
      </div>
      <TgEmpty v-else :title="kw ? `没有包含「${kw}」的条目` : 'AI 没有从需求中整理出这一项的内容'" />
      <div v-if="editable" class="mt">
        <template v-if="adding">
          <textarea v-model="addText" class="field" rows="2" placeholder="一条一个事实，写清数值、单位与方向" />
          <div class="row" style="margin-top:4px"><button type="button" class="btn small primary" :disabled="!addText.trim()" @click="op({ key, action: 'add', text: addText.trim() })">添加</button><button type="button" class="btn small" @click="adding = false">取消</button></div>
        </template>
        <button v-else type="button" class="btn small" @click="adding = true">＋ 人工补充一条{{ labels[key] }}</button>
        <span class="tiny muted" style="margin-left:8px">修改即新版本，已确认的变为「已确认旧版本」；共 {{ total }} 条分析要点</span>
      </div>
    </template>
  </template>
</template>
<style scoped>
.review-box { margin-top: 6px; padding: 6px 10px; border-radius: 4px; border: 1px solid var(--amber); font-size: 12px; }
.review-box.wrong { border-color: var(--red); } .review-box.wrong > b { color: var(--red); } .review-box.unsupported > b { color: var(--amber); }
</style>

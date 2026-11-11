<script setup>
/* 项目画像（9.16–9.18）：GET /projects/{name}/profile；提炼 POST .../profile/extract（调模型）；确认 POST .../profile/confirm（可带编辑后的 profile）；
   丢弃 DELETE .../profile/draft；「编辑生效版本」把 active 当草稿打开，确认即生成新版本。六段：background 文本、glossary [{term, meaning}]、其余为列表。 */
import { ref, computed, watch } from 'vue'
import { api, seg } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useAuthStore } from '@/stores/auth'
import { useScopeStore } from '@/stores/scope'
import { useUiStore } from '@/stores/ui'
import { fmtAgo, fmtTime } from '@/utils/format'
import { TgPanel, TgBadge, TgEmpty, TgIcon } from '@/components/ui'
import ProfileSection from './ProfileSection.vue'
const props = defineProps({ project: { type: String, required: true } })
const emit = defineEmits(['changed', 'tab'])
const auth = useAuthStore(), scope = useScopeStore(), ui = useUiStore()
const { data: d, loading, error, reload } = useLoad(() => api.get(`/api/v1/projects/${seg(props.project)}/profile`), { deps: [() => props.project] })
const canAi = computed(() => auth.can(props.project, 'point.ai'))
const sections = computed(() => Object.entries(d.value?.sections || {}))
const extracting = ref(false), confirming = ref(false), editingActive = ref(false), showRendered = ref(false)
const edit = ref({})   // 编辑中的文本：{ key: string }

/** 画像 → 可编辑文本（术语「术语：释义」逐行，列表按行） */
function toText(pf) {
  const out = {}
  for (const [k] of sections.value) {
    if (k === 'background') out[k] = pf?.background || ''
    else if (k === 'glossary') out[k] = (pf?.glossary || []).map(g => `${g.term}：${g.meaning}`).join('\n')
    else out[k] = (pf?.[k] || []).join('\n')
  }
  return out
}
/** 文本 → 画像结构 */
function collect() {
  const out = {}
  for (const [k] of sections.value) {
    const v = edit.value[k] || ''
    if (k === 'background') out[k] = v.trim()
    else if (k === 'glossary') out[k] = v.split('\n').map(x => x.trim()).filter(Boolean).map(x => { const [t, ...m] = x.replace(':', '：').split('：'); return { term: t.trim(), meaning: m.join('：').trim() } })
    else out[k] = v.split('\n').map(x => x.replace(/^[-•\s]+/, '').trim()).filter(Boolean)
  }
  return out
}
const draftEditable = computed(() => !!d.value?.draft && d.value.can_confirm)
function startDraftEdit() { edit.value = toText(d.value.draft) }
function startActiveEdit() { editingActive.value = true; edit.value = toText(d.value.active) }
const rows = v => Math.min(12, Math.max(3, String(v || '').split('\n').length + 1))

async function extract() {
  const mode = d.value?.draft ? '重新提炼（覆盖当前草稿）' : d.value?.has_active ? 'AI 增量提炼（在生效版本上增补修正）' : 'AI 提炼画像'
  if (!await ui.confirm(`${mode}？`, '会把项目描述、模块树、需求、已确认测试点与评审通过用例发给模型，生成一版草稿等待确认。', { ok: '开始提炼' })) return
  extracting.value = true
  try { d.value = await api.post(`/api/v1/projects/${seg(props.project)}/profile/extract`, {}); ui.toast('草稿已生成，请检查后确认生效'); edit.value = toText(d.value.draft); emit('changed') }
  catch (e) { ui.error(e) } finally { extracting.value = false }
}
async function confirmProfile(withEdits) {
  confirming.value = true
  try {
    d.value = await api.post(`/api/v1/projects/${seg(props.project)}/profile/confirm`, withEdits ? { profile: collect() } : {})
    editingActive.value = false
    ui.toast(`项目画像 v${d.value.version} 已生效，后续 AI 环节将带着它理解业务`)
    scope.load(true); emit('changed')
  } catch (e) { ui.error(e) } finally { confirming.value = false }
}
async function discard() {
  if (!await ui.confirm('丢弃草稿？', '生效版本不受影响，之后可重新提炼。', { ok: '丢弃', danger: true })) return
  try { d.value = await api.del(`/api/v1/projects/${seg(props.project)}/profile/draft`); ui.toast('已丢弃草稿'); scope.load(true); emit('changed') } catch (e) { ui.error(e) }
}
// 草稿到达 / 变化时初始化编辑文本（可编辑者直接改 textarea）
watch(() => [draftEditable.value, d.value?.draft_meta?.at], ([ok]) => { if (ok) startDraftEdit() }, { immediate: true })
</script>
<template>
  <div v-if="error" class="err-box mb">{{ error.message }}</div>
  <div class="information mb"><TgIcon name="info" :size="14" /><span>项目画像是对这个业务的结构化认知（背景 / 角色 / 流程 / 术语 / 规则 / 易错点）。AI 从本项目已沉淀的需求、已确认测试点与评审通过的用例中提炼成草稿，<b>管理员确认后生效</b>，之后每次拆解、生成、查漏、评审、需求分析都会带着它（2500 字预算，14 处 Prompt）。<template v-if="d && d.knowledge_auto">项目每新增 3 个任务会自动提炼一版新草稿等待确认。</template><template v-else-if="d"><span style="color:var(--amber)">本项目已关闭自动沉淀，画像只能手动提炼。</span></template></span></div>
  <div class="grid-2">
    <div class="stack">
      <TgPanel>
        <template #title>项目画像 <TgBadge v-if="d?.has_active" status="approved" :text="`v${d.version} 已生效`" /><TgBadge v-else status="pending" text="尚未生效" /><span v-if="d?.has_active" class="muted tiny" style="font-weight:400;margin-left:6px">{{ d.confirmed_by || '' }} · {{ fmtAgo(d.confirmed_at) }}</span></template>
        <template #actions>
          <button v-if="canAi" type="button" class="btn small" :disabled="extracting" @click="extract"><TgIcon name="spark" :size="13" /> {{ extracting ? '提炼中…' : (d?.draft ? '重新提炼' : d?.has_active ? 'AI 增量提炼' : 'AI 提炼画像') }}</button>
          <button v-if="d?.has_active && d.can_confirm && !d.draft && !editingActive" type="button" class="btn small" @click="startActiveEdit">编辑生效版本</button>
        </template>
        <div v-if="loading && !d" class="muted">加载中…</div>
        <template v-else-if="d">
          <!-- 待确认草稿 -->
          <div v-if="d.draft" class="draft">
            <div class="row mb"><b>待确认的 AI 草稿</b><span class="muted tiny">{{ d.draft_meta?.trigger === 'auto' ? '自动提炼' : '手动提炼' }} · {{ d.draft_meta?.model || '' }} · {{ fmtAgo(d.draft_meta?.at) }} · 素材：需求 {{ d.draft_meta?.requirements || 0 }} / 任务 {{ d.draft_meta?.tasks || 0 }} / 测试点 {{ d.draft_meta?.points || 0 }} / 用例 {{ d.draft_meta?.cases || 0 }}</span>
              <span class="right row">
                <template v-if="d.can_confirm"><button type="button" class="btn small success solid" :disabled="confirming" @click="confirmProfile(true)">{{ confirming ? '处理中…' : '确认生效' }}</button><button type="button" class="btn small" @click="discard">丢弃草稿</button></template>
                <span v-else class="muted tiny">等待管理员确认</span>
              </span></div>
            <template v-if="draftEditable">
              <div class="muted tiny mb">可直接修改下面的内容后再确认；确认后立即对本项目所有 AI 环节生效。</div>
              <label v-for="[k, label] in sections" :key="k" class="frow"><span class="flabel">{{ label }} <span class="muted" style="font-weight:400">{{ k === 'background' ? '' : k === 'glossary' ? '（每行一个：术语：释义）' : '（每行一条）' }}</span></span><span class="fctl"><textarea v-model="edit[k]" class="field" :rows="rows(edit[k])" /></span></label>
            </template>
            <template v-else><div v-for="[k, label] in sections" :key="k" class="sec"><div class="sec-t">{{ label }}</div><ProfileSection :k="k" :pf="d.draft" /></div></template>
          </div>
          <!-- 编辑生效版本 -->
          <div v-if="editingActive" class="draft active-edit">
            <div class="row mb"><b>编辑生效版本</b><span class="muted tiny">保存即生成 v{{ d.version + 1 }}</span><span class="right row"><button type="button" class="btn small primary" :disabled="confirming" @click="confirmProfile(true)">保存并生效</button><button type="button" class="btn small" @click="editingActive = false">取消</button></span></div>
            <label v-for="[k, label] in sections" :key="k" class="frow"><span class="flabel">{{ label }}</span><span class="fctl"><textarea v-model="edit[k]" class="field" :rows="rows(edit[k])" /></span></label>
          </div>
          <!-- 生效版本 -->
          <template v-else-if="d.has_active">
            <div v-if="d.draft" class="panel-title mt mb">当前生效版本 v{{ d.version }}</div>
            <div v-for="[k, label] in sections" :key="k" class="sec"><div class="sec-t">{{ label }}</div><ProfileSection :k="k" :pf="d.active" /></div>
          </template>
          <TgEmpty v-else-if="!d.draft" title="还没有项目画像" :hint="(canAi ? '点击「AI 提炼画像」从本项目的需求与用例中生成草稿。' : '') + (d.auto_due ? '已有素材，下一个任务完成后也会自动提炼。' : '')" />
        </template>
      </TgPanel>
    </div>
    <div class="stack">
      <TgPanel title="注入预览" :hint="d?.rendered ? `${d.rendered.length} 字 · 按 2500 字预算渲染，经 ContextVar 注入拆解 / 延伸 / 生成 / 评审 / 查漏 / 需求分析等 14 处 Prompt` : '尚无可注入的画像文本'">
        <template #actions><button v-if="d?.rendered" type="button" class="btn small ghost" @click="showRendered = !showRendered">{{ showRendered ? '收起' : '展开' }}</button></template>
        <div v-if="d?.rendered && showRendered" class="pre">{{ d.rendered }}</div>
        <div v-else-if="d?.rendered" class="muted small ellipsis">{{ d.rendered.slice(0, 120) }}…</div>
      </TgPanel>
      <TgPanel title="版本历史" :hint="d?.history?.length ? `最近 ${d.history.length} 条` : '无历史'">
        <div v-if="d?.history?.length" class="list"><div v-for="h in d.history.slice().reverse()" :key="h.version + h.at"><b>v{{ h.version }}</b> <span class="muted">· {{ h.by || '—' }} · {{ fmtTime(h.at) }} · {{ h.source === 'manual' ? '人工编辑' : 'AI 草稿确认' }}</span></div></div>
      </TgPanel>
      <TgPanel title="存量回溯" hint="把上线前的历史任务 / 需求补进知识库，完成后自动提炼一版画像草稿">
        <button type="button" class="btn small" @click="emit('tab', 'knowledge')">去知识回溯 ›</button>
      </TgPanel>
    </div>
  </div>
</template>
<style scoped>
.draft { border: 1px solid #ffe6c3; background: #fffdf8; border-radius: 4px; padding: 12px 14px; margin-bottom: 12px; }
.draft.active-edit { border-color: var(--blue-line); background: #f8fbff; }
.sec { margin-top: 12px; }
.sec-t { font-size: 12px; font-weight: 700; color: #455368; margin-bottom: 4px; }
</style>

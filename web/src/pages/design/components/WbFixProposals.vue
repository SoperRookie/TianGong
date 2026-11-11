<script setup>
/* AI 修改提案（pending_fix）逐项接受 / 拒绝 + 最近一次落地结果（fix_log 末条）的修改前 → 修改后。测试点与用例共用。 */
import { ref, watch, computed } from 'vue'
import { fmtTime } from '@/utils/format'
import { TgEmpty } from '@/components/ui'
const props = defineProps({ pf: { type: Object, default: null }, lastFix: { type: Object, default: null }, busy: Boolean, canConfirm: Boolean })
const emit = defineEmits(['confirm'])
const accepted = ref(new Set())
watch(() => props.pf, pf => { accepted.value = new Set((pf?.proposals || []).map(p => p.proposal_id)) }, { immediate: true })
const kind = computed(() => props.pf?.kind || props.lastFix?.kind || 'points')
function toggle(id) { const s = new Set(accepted.value); s.has(id) ? s.delete(id) : s.add(id); accepted.value = s }
function all(on) { accepted.value = new Set(on ? (props.pf?.proposals || []).map(p => p.proposal_id) : []) }
function apply(rejectAll) {
  const decisions = rejectAll ? [] : (props.pf?.proposals || []).map(p => ({ proposal_id: p.proposal_id, decision: accepted.value.has(p.proposal_id) ? 'accept' : 'reject' }))
  emit('confirm', decisions)
}
const label = p => p.tp_id || p.case_id || '新增'
const ACT = { split: '拆分', delete: '删除', add: '新增', modify: '修改' }
</script>
<template>
  <div class="fp">
    <template v-if="pf && pf.proposals?.length">
      <div class="panel-title">AI 修改提案 · 待确认（{{ pf.proposals.length }} 条）</div>
      <div class="information fp-info">AI 修改不直接覆盖：勾选要接受的提案，接受项生成新版本并回到待审核；未勾选项视为拒绝（驳回状态保留，可继续 AI 优化或人工编辑）。{{ pf.by }} 发起于 {{ fmtTime(pf.at) }}<span v-if="pf.model_name"> · {{ pf.model_name }}</span></div>
      <label v-for="p in pf.proposals" :key="p.proposal_id" class="fp-row">
        <input type="checkbox" :checked="accepted.has(p.proposal_id)" @change="toggle(p.proposal_id)" />
        <div class="fp-body">
          <div class="row fp-meta"><span class="mono fp-id">{{ label(p) }}</span><span class="badge blue">{{ ACT[p.action] || p.action }}</span><span v-if="p.module" class="tag">{{ p.module }}</span><span v-if="p.comment_type" class="tag">{{ p.comment_type }}</span><span class="tiny muted">{{ p.note }}</span></div>
          <template v-if="kind === 'points'">
            <div v-if="p.action === 'split'"><div class="diff-del">{{ p.before }}</div><div v-for="(x, i) in (p.after || [])" :key="i" class="diff-add">拆分 → {{ x }}</div></div>
            <div v-else-if="p.action === 'delete'" class="diff-del">{{ p.before }}（建议删除）</div>
            <div v-else-if="p.action === 'add'" class="diff-add">新增：{{ p.after }}</div>
            <div v-else><span class="diff-del">{{ p.before }}</span> → <span class="diff-add">{{ p.after }}</span></div>
          </template>
          <template v-else>
            <div v-if="p.action === 'delete'" class="diff-del">建议删除该用例</div>
            <div v-else-if="p.action === 'add'" class="diff-add">新增：{{ (p.after || {}).title }}</div>
            <template v-else><div v-for="(c, i) in (p.changes || [])" :key="i" class="fp-change"><b>{{ c.field }}</b>：<span class="diff-del pre-line">{{ c.before }}</span> → <span class="diff-add pre-line">{{ c.after }}</span></div></template>
          </template>
        </div>
      </label>
      <div v-if="pf.invalid?.length" class="tiny muted fp-invalid">AI 另有 {{ pf.invalid.length }} 条输出无效已丢弃：{{ pf.invalid.slice(0, 3).map(x => `${x.case_id || x.tp_id || ''} ${x.problem || ''}`).join('；') }}</div>
      <div class="row fp-foot">
        <button type="button" class="btn primary" :disabled="busy || !canConfirm" @click="apply(false)">应用所选（{{ accepted.size }}）</button>
        <button type="button" class="btn" :disabled="busy || !canConfirm" @click="apply(true)">全部拒绝</button>
        <span class="link tiny" @click="all(true)">全选</span><span class="link tiny" @click="all(false)">全不选</span>
      </div>
    </template>
    <TgEmpty v-else title="没有待确认的 AI 修改提案" hint="驳回若干条后点「AI 修改驳回项」，AI 只改被驳回项并产出提案，在这里逐项接受。" />
    <template v-if="lastFix">
      <div class="panel-title fp-last">最近一次 AI 定点修改结果 <span class="tiny muted">{{ lastFix.by }} · {{ fmtTime(lastFix.at) }} · 拒绝 {{ lastFix.rejected_proposals || 0 }} 条</span></div>
      <div v-if="!(lastFix.diff || []).length && !(lastFix.added || []).length" class="tiny muted">全部提案被拒绝，未产生改动。</div>
      <div v-for="(x, i) in (lastFix.diff || [])" :key="i" class="fp-diff">
        <span class="mono fp-id">{{ x.tp_id || x.case_id }}</span> <span class="tag" v-if="x.comment_type">{{ x.comment_type }}</span>
        <template v-if="lastFix.kind === 'points'">
          <div v-if="x.action === 'split'"><span class="diff-del">{{ x.before }}</span><div v-for="(a, j) in (x.after || [])" :key="j" class="diff-add">+ {{ a }}</div></div>
          <div v-else-if="x.action === 'delete'"><span class="diff-del">{{ x.before }}</span>（已删除）</div>
          <div v-else><span class="diff-del">{{ x.before }}</span><div class="diff-add">{{ x.after }}</div></div>
        </template>
        <template v-else>
          <template v-if="x.action === 'modify'"><div v-for="(c, j) in (x.changes || [])" :key="j"><b>{{ c.field }}</b>：<span class="diff-del pre-line">{{ c.before }}</span> → <span class="diff-add pre-line">{{ c.after }}</span></div></template>
          <div v-else-if="x.action === 'delete'" class="diff-del">{{ x.case_id }} 已删除</div>
          <div v-else class="diff-add">新增 {{ x.case_id }}：{{ (x.after || {}).title }}</div>
        </template>
        <div v-if="x.note" class="tiny muted">{{ x.note }}</div>
      </div>
      <div v-if="(lastFix.added || []).length" class="inline-list"><span v-for="(a, i) in lastFix.added" :key="i" class="tag">新增 {{ a.tp_id }}：{{ a.point }}</span></div>
    </template>
  </div>
</template>
<style scoped>
.fp-info { margin: 8px 0 10px; }
.fp-row { display: flex; gap: 10px; align-items: flex-start; padding: 10px 12px; border: 1px solid var(--line); border-radius: 3px; margin-bottom: 8px; cursor: pointer; font-size: 12.5px; }
.fp-row input { margin-top: 3px; }
.fp-body { flex: 1; min-width: 0; }
.fp-meta { margin-bottom: 4px; }
.fp-id { font-weight: 700; color: #2b3b5b; }
.fp-change { margin-top: 3px; }
.pre-line { white-space: pre-line; }
.fp-invalid { margin: 4px 0 8px; }
.fp-foot { margin-top: 10px; }
.fp-last { margin-top: 18px; }
.fp-diff { border: 1px solid var(--line); border-radius: 3px; padding: 8px 12px; margin-bottom: 6px; font-size: 12.5px; }
</style>

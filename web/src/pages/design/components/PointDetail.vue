<script setup>
/* 测试点详情卡：编号 / 模块 / 维度 / 端 / 状态 / 版本 / 来源；描述；需求来源 ref 与依据；AI 评审意见；驳回信息；评审记录；
   文字按钮带快捷键（A 通过 · R 驳回 · E 修改 · D 删除 · 解锁）；驳回表单与就地编辑在卡内展开。 */
import { ref, computed, watch } from 'vue'
import { fmtTime } from '@/utils/format'
import { terminalLabel, TERMINALS } from '@/utils/terminals'
import { TgBadge } from '@/components/ui'
import WbRejectForm from './WbRejectForm.vue'
import { DIMENSIONS, sourceText, pointTerminal } from './WbSession'
const props = defineProps({
  p: { type: Object, required: true }, log: { type: Array, default: () => [] }, holder: { type: Object, default: null },
  mode: { type: String, default: '' },          // '' | 'reject' | 'edit'
  canReview: Boolean, canEdit: Boolean, canDelete: Boolean, canAi: Boolean, busy: Boolean, generated: Boolean, pendingGeneration: Boolean,
  model: { type: String, default: '' },
  terminals: { type: Array, default: () => TERMINALS },   // 任务的端清单（含文档自有的端）
})
const emit = defineEmits(['approve', 'reject', 'edit', 'save', 'cancel', 'delete', 'unlock', 'fix'])
const form = ref({ point: '', dimension: '', ref: '', terminal: '' })
const editEl = ref(null)
watch(() => [props.mode, props.p.tp_id], () => {
  if (props.mode === 'edit') {
    form.value = { point: props.p.point, dimension: props.p.dimension || '', ref: props.p.ref || '', terminal: pointTerminal(props.p) }
    setTimeout(() => editEl.value?.focus(), 0)
  }
}, { immediate: true })
const statusText = computed(() => props.p.status === 'approved' ? '已通过 · 已锁定' : props.p.status === 'rejected' ? '已驳回' : '待审核')
const rv = computed(() => props.p.ai_review)
const canFixBySuggest = computed(() => rv.value && rv.value.fix && !['UI 外观', '超出需求', '维度不符'].includes(rv.value.verdict))
function save() {
  const text = form.value.point.trim()
  if (!text) return
  emit('save', { point: text, dimension: form.value.dimension, ref: form.value.ref.trim(), terminal: form.value.terminal, base_version: props.p.version || 1 })
}
function onEditKey(e) { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); save() } if (e.key === 'Escape') emit('cancel') }
const lastReviewer = computed(() => { for (let i = props.log.length - 1; i >= 0; i--) { const e = props.log[i]; if (['approve', 'reject', 'unlock'].includes(e.action)) return e } return null })
</script>
<template>
  <div class="detail-top">
    <span class="id">{{ p.tp_id }}</span>
    <span class="tag" :title="p.module">{{ p.module }}</span>
    <span v-if="p.dimension" class="badge blue">{{ p.dimension }}</span>
    <span class="tag">{{ terminalLabel(pointTerminal(p)) }}</span>
    <TgBadge :status="p.status || 'pending'" :text="statusText" />
    <span v-if="p.version > 1" class="tag">v{{ p.version }}</span>
    <span v-if="generated" class="badge green">已生成用例</span>
    <span v-else-if="pendingGeneration" class="badge orange">待生成用例</span>
    <span class="meta">{{ sourceText(p.source || 'ai') }}<template v-if="model && (p.source || 'ai') === 'ai'"> · {{ model }}</template></span>
  </div>
  <div v-if="holder" class="attention pd-holder">{{ holder.by }} 正在编辑此测试点（{{ fmtTime(holder.at) }}），注意保存冲突（版本号乐观锁会拦截覆盖）</div>

  <template v-if="mode === 'edit'">
    <div class="pd-edit" @keydown="onEditKey">
      <textarea ref="editEl" v-model="form.point" class="field pd-ta" rows="3" placeholder="测试点描述" />
      <div class="pd-edit-row">
        <select v-model="form.dimension" class="select"><option value="">维度：不指定</option><option v-for="d in DIMENSIONS" :key="d" :value="d">{{ d }}</option></select>
        <select v-model="form.terminal" class="select"><option v-for="t in terminals" :key="t.key" :value="t.key">端：{{ t.label }}</option></select>
        <input v-model="form.ref" class="field" placeholder="需求来源：页面或章节 › 需求点（可空）" />
      </div>
      <div class="row pd-edit-foot">
        <button type="button" class="btn small" @click="emit('cancel')">取消 <kbd>Esc</kbd></button>
        <button type="button" class="btn primary small" :disabled="busy || !form.point.trim()" @click="save">保存（版本 +1，回到待审核）<kbd>⌘⏎</kbd></button>
        <span class="tiny muted">基于 v{{ p.version || 1 }}；他人已改过则保存被拒（409），需重新加载</span>
      </div>
    </div>
  </template>
  <div v-else class="detail-title">{{ p.point }}</div>

  <dl class="kv">
    <dt>需求来源</dt><dd><b v-if="p.ref">{{ p.ref }}</b><span v-else-if="p.basis" class="muted">经验 / 组合延伸，无原文引用</span><span v-else class="badge orange">无需求依据 · 待核对</span></dd>
    <dt v-if="p.basis">依据</dt><dd v-if="p.basis">{{ p.basis }}<span v-if="p.combo_from?.length" class="tiny muted"> · 由 {{ p.combo_from.join('、') }} 组合</span></dd>
    <dt v-if="(p.warnings || []).length">提示</dt><dd v-if="(p.warnings || []).length"><span v-for="w in p.warnings" :key="w" class="badge orange">{{ w }}</span></dd>
    <dt v-if="rv">AI 评审</dt><dd v-if="rv"><span class="badge purple">{{ rv.verdict }}</span><span v-if="rv.unverified" class="tag">引文未核验</span> {{ rv.fix || rv.note }}<div v-if="rv.quote" class="tiny muted">原文：「{{ rv.quote }}」</div><span v-if="canFixBySuggest && canEdit" class="link tiny" @click="emit('edit', rv.fix)">按建议修改</span></dd>
    <dt v-if="p.status === 'rejected'">驳回意见</dt><dd v-if="p.status === 'rejected'"><span v-for="t in (p.reject_types || [])" :key="t" class="chip on pd-chip">{{ t }}</span> {{ p.comment }}<div v-if="p.fix_request" class="tiny">修改要求：{{ p.fix_request }}</div><div v-if="p.fix_note" class="tiny muted">备注：{{ p.fix_note }}</div><div class="tiny muted">范围：{{ p.fix_scope || '当前项' }}<template v-if="p.reject_count"> · 驳回 {{ p.reject_count }} 次</template></div><div v-if="p.reject_count >= 2" class="attention pd-hint">已连续 {{ p.reject_count }} 次未通过审核，建议检查需求是否有歧义、是否需要人工直接修改或补充需求信息<button v-if="canAi" type="button" class="btn small" @click="emit('fix')">AI 修改驳回项</button></div></dd>
    <dt>记录</dt><dd><template v-if="log.length"><span v-for="(l, i) in log" :key="i" class="tag pd-log" :title="l.comment || ''">{{ { approve: '通过', reject: '驳回', modify: '修改', unlock: '解锁', restore: '恢复版本', restore_bin: '回收站恢复', delete: '删除' }[l.action] || l.action }} · {{ l.by }} {{ fmtTime(l.at) }}</span></template><span v-else class="muted">无审核记录</span><span v-if="lastReviewer && p.status !== 'pending'" class="tiny muted"> · 最近由 {{ lastReviewer.by }} {{ lastReviewer.action === 'approve' ? '通过（锁定）' : lastReviewer.action === 'reject' ? '驳回' : '解锁' }}</span></dd>
  </dl>

  <div class="detail-actions" v-if="mode !== 'edit'">
    <template v-if="p.status === 'approved'">
      <button v-if="canReview" type="button" class="btn" :disabled="busy" @click="emit('unlock')">解锁（回待审核）</button>
    </template>
    <template v-else>
      <button v-if="canReview" type="button" class="btn success solid" :disabled="busy" @click="emit('approve')"><kbd>A</kbd> 通过并锁定</button>
      <button v-if="canReview" type="button" class="btn danger" :class="{ on: mode === 'reject' }" :disabled="busy" @click="mode === 'reject' ? emit('cancel') : emit('reject')"><kbd>R</kbd> 驳回</button>
    </template>
    <button v-if="canEdit" type="button" class="btn" :disabled="busy" @click="emit('edit')"><kbd>E</kbd> 修改</button>
    <button v-if="canDelete" type="button" class="btn" :disabled="busy" @click="emit('delete')"><kbd>D</kbd> 删除</button>
    <span class="ai">AI 评审：{{ rv ? rv.verdict : (p.warnings || []).length ? '有提示' : '无硬伤' }}</span>
  </div>
  <WbRejectForm v-if="mode === 'reject'" kind="point" :label="`测试点 ${p.tp_id}`" :busy="busy" @submit="v => emit('reject', v)" @cancel="emit('cancel')" />
</template>
<style scoped>
.pd-holder { margin: 10px 0; }
.pd-edit { margin: 10px 0 12px; }
.pd-ta { width: 100%; font-size: 14px; font-weight: 600; }
.pd-edit-row { display: grid; grid-template-columns: 160px 170px 1fr; gap: 8px; margin-top: 8px; }
.pd-edit-row .field, .pd-edit-row .select { width: 100%; }
.pd-edit-foot { margin-top: 8px; }
.pd-chip { cursor: default; margin-right: 4px; }
.pd-hint { margin-top: 6px; }
.pd-log { margin: 0 4px 4px 0; }
.btn.danger.on { background: #ffebee; }
</style>

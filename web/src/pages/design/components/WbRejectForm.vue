<script setup>
/* 结构化驳回表单（在详情区展开）：驳回类型多选必填 + 原因必填 + 修改要求 / 备注 + 修改范围；用例另有字段级 / 步骤级定位。⌘⏎ 提交，Esc 取消。 */
import { ref, computed, onMounted } from 'vue'
import { REJECT_TYPES, FIX_SCOPES, CASE_FIELD_LABELS } from '@/utils/consts'
import { useKeys } from '@/utils/useKeys'
const props = defineProps({
  kind: { type: String, default: 'point' },      // point / case
  label: { type: String, default: '' },           // 「TP003」或「3 条 · 统一驳回意见」
  stepMax: { type: Number, default: 0 },          // 用例步骤数（批量为 0：不支持步骤级定位）
  scope: { type: String, default: '当前项' },
  busy: Boolean,
})
const emit = defineEmits(['submit', 'cancel'])
const types = ref(new Set()), fields = ref(new Set())
const comment = ref(''), fixRequest = ref(''), fixNote = ref(''), fixScope = ref(props.scope), steps = ref(''), err = ref('')
const reasonEl = ref(null)
const isCase = computed(() => props.kind === 'case')
const flip = (set, v) => { const s = new Set(set); s.has(v) ? s.delete(v) : s.add(v); return s }
const toggleType = v => { types.value = flip(types.value, v) }
const toggleField = v => { fields.value = flip(fields.value, v) }
function submit() {
  err.value = ''
  if (!types.value.size) { err.value = '请选择驳回类型（可多选）'; return }
  if (!comment.value.trim()) { err.value = '驳回原因不能为空'; reasonEl.value?.focus(); return }
  const out = { comment: comment.value.trim(), reject_types: [...types.value], fix_request: fixRequest.value.trim(), fix_note: fixNote.value.trim(), fix_scope: fixScope.value }
  if (isCase.value) {
    out.fields = [...fields.value]
    const nums = steps.value.split(/[,，\s]+/).filter(Boolean).map(Number)
    if (nums.some(n => !Number.isInteger(n) || n < 1 || n > props.stepMax)) { err.value = `指定步骤须为 1~${props.stepMax} 的序号`; return }
    out.steps = nums
  }
  emit('submit', out)
}
useKeys({ 'mod+enter': submit, escape: () => emit('cancel') })
onMounted(() => reasonEl.value?.focus())
</script>
<template>
  <div class="reject">
    <div class="small rj-head">驳回 {{ label }} <span class="muted">· 结构化驳回，AI 按类型定向修正</span></div>
    <div class="chips"><span v-for="t in REJECT_TYPES" :key="t" class="chip" :class="{ on: types.has(t) }" @click="toggleType(t)">{{ t }}</span></div>
    <div class="rj-grid">
      <input ref="reasonEl" v-model="comment" class="field" placeholder="驳回原因（必填）" />
      <input v-model="fixRequest" class="field" placeholder="修改要求（告诉 AI 应该怎么改，可选）" />
      <input v-model="fixNote" class="field" placeholder="修改备注（可选）" />
      <select v-model="fixScope" class="select"><option v-for="s in FIX_SCOPES" :key="s" :value="s">范围：{{ s }}</option></select>
    </div>
    <template v-if="isCase">
      <div class="tiny muted rj-sub">字段级定位（只允许 AI 改这些字段，可选）</div>
      <div class="chips"><span v-for="([v, l]) in CASE_FIELD_LABELS" :key="v" class="chip" :class="{ on: fields.has(v) }" @click="toggleField(v)">{{ l }}</span></div>
      <input v-model="steps" class="field" :disabled="!stepMax" :placeholder="stepMax ? `步骤级定位：只允许 AI 改第 N 步，1~${stepMax}，如 2,3（可选）` : '批量驳回不支持步骤级定位'" />
    </template>
    <div v-if="err" class="tiny rj-err">{{ err }}</div>
    <div class="row rj-foot">
      <button type="button" class="btn small" @click="emit('cancel')">取消 <kbd>Esc</kbd></button>
      <button type="button" class="btn danger small" :disabled="busy" @click="submit">确认驳回 <kbd>⌘⏎</kbd></button>
    </div>
  </div>
</template>
<style scoped>
.rj-head { font-weight: 700; color: #2b3b5b; }
.rj-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.rj-grid .field, .rj-grid .select { width: 100%; }
.rj-sub { margin-top: 8px; }
.rj-err { color: var(--red); margin-top: 6px; }
.rj-foot { margin-top: 10px; justify-content: flex-end; }
.reject .field { margin-top: 0; }
</style>

<script setup>
/* 对话框：<TgDialog v-model="open" title="…" ok="保存" :danger="false" :busy="saving" @ok="save">表单</TgDialog>；⌘⏎ 确定，Esc 取消 */
import { watch, onBeforeUnmount, nextTick, ref } from 'vue'
const props = defineProps({ modelValue: Boolean, title: { type: String, default: '' }, ok: { type: String, default: '确定' }, cancel: { type: String, default: '取消' }, danger: Boolean, busy: Boolean, wide: Boolean, noFoot: Boolean, okDisabled: Boolean })
const emit = defineEmits(['update:modelValue', 'ok', 'close'])
function close() { emit('update:modelValue', false); emit('close') }
const onKey = e => {
  if (!props.modelValue) return
  if (e.key === 'Escape') { e.stopPropagation(); close() }
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter' && !props.busy && !props.okDisabled) { e.preventDefault(); emit('ok') }
}
const box = ref(null)
watch(() => props.modelValue, v => {
  v ? window.addEventListener('keydown', onKey) : window.removeEventListener('keydown', onKey)
  // 打开后自动聚焦第一个 [autofocus]，否则第一个输入控件
  if (v) nextTick(() => { const el = box.value?.querySelector('[autofocus], input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=file]), textarea, select'); el && el.focus() })
}, { immediate: true })
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>
<template>
  <Teleport to="body">
    <div v-if="modelValue" class="dialog-mask" @click.self="close">
      <div ref="box" class="dialog" :class="{ wide }" role="dialog" :aria-label="title">
        <div class="dialog-head"><slot name="title">{{ title }}</slot><button type="button" class="drawer-x" aria-label="关闭" @click="close">×</button></div>
        <div class="dialog-body"><slot /></div>
        <div v-if="!noFoot" class="dialog-foot">
          <slot name="foot-left" />
          <button type="button" class="btn" @click="close">{{ cancel }} <kbd>Esc</kbd></button>
          <button type="button" class="btn" :class="danger ? 'danger' : 'primary'" :disabled="busy || okDisabled" @click="$emit('ok')">{{ busy ? '处理中…' : ok }} <kbd>⌘⏎</kbd></button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

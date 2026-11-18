<script setup>
import { watch, onBeforeUnmount } from 'vue'
const props = defineProps({ modelValue: Boolean, title: { type: String, default: '' }, sub: { type: String, default: '' }, wide: Boolean, width: { type: String, default: '' } })
const emit = defineEmits(['update:modelValue', 'close'])
function close() { emit('update:modelValue', false); emit('close') }
const onKey = e => { if (e.key === 'Escape' && props.modelValue) { e.stopPropagation(); close() } }
watch(() => props.modelValue, v => { v ? window.addEventListener('keydown', onKey) : window.removeEventListener('keydown', onKey) }, { immediate: true })
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>
<template>
  <Teleport to="body">
    <template v-if="modelValue">
      <div class="drawer-mask" @click="close" />
      <aside class="drawer" :style="width ? { width } : (wide ? { width: 'min(820px,100%)' } : null)" role="dialog" :aria-label="title">
        <div class="drawer-head">
          <div><strong><slot name="title">{{ title }}</slot></strong><div v-if="sub || $slots.sub" class="tiny muted"><slot name="sub">{{ sub }}</slot></div></div>
          <div class="row">
            <slot name="head-actions" />
            <button type="button" class="drawer-x" aria-label="关闭" @click="close">×</button>
          </div>
        </div>
        <div class="drawer-body"><slot /></div>
        <div v-if="$slots.foot" class="drawer-foot"><slot name="foot" /></div>
      </aside>
    </template>
  </Teleport>
</template>

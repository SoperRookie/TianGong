<script setup>
defineProps({ title: { type: String, default: '' }, hint: { type: String, default: '' }, pad: { type: Boolean, default: true } })
</script>
<template>
  <div class="panel" :class="{ 'panel-pad': pad }">
    <div v-if="title || $slots.actions || $slots.title" class="panel-head">
      <div>
        <div class="panel-title"><slot name="title">{{ title }}</slot></div>
        <div v-if="hint || $slots.hint" class="panel-hint"><slot name="hint">{{ hint }}</slot></div>
      </div>
      <div v-if="$slots.actions" class="row"><slot name="actions" /></div>
    </div>
    <slot />
  </div>
</template>

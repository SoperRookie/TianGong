<script setup>
/* items: [{ key, label, count? }]；v-model 为当前 key */
defineProps({ items: { type: Array, required: true }, modelValue: { type: String, default: '' } })
defineEmits(['update:modelValue'])
</script>
<template>
  <div class="tabs">
    <button v-for="t in items" :key="t.key" type="button" class="tab" :class="{ active: modelValue === t.key }" @click="$emit('update:modelValue', t.key)">
      {{ t.label }}<span v-if="t.count !== undefined && t.count !== null" class="n">{{ t.count }}</span>
    </button>
  </div>
</template>

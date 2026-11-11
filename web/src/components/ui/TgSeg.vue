<script setup>
/* 分段控件：items [{ key, label, count? }] 或 [[key,label]]；v-model 当前 key */
import { computed } from 'vue'
const props = defineProps({ items: { type: Array, required: true }, modelValue: { type: String, default: '' } })
defineEmits(['update:modelValue'])
const list = computed(() => props.items.map(i => Array.isArray(i) ? { key: i[0], label: i[1], count: i[2] } : i))
</script>
<template>
  <div class="seg">
    <button v-for="t in list" :key="t.key" type="button" :class="{ on: modelValue === t.key }" @click="$emit('update:modelValue', t.key)">
      {{ t.label }}<template v-if="t.count !== undefined && t.count !== null"> {{ t.count }}</template>
    </button>
  </div>
</template>

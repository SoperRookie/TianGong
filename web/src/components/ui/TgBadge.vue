<script setup>
import { computed } from 'vue'
import { statusOf } from '@/utils/status'
const props = defineProps({ status: { type: String, default: '' }, text: { type: String, default: '' }, color: { type: String, default: '' } })
const s = computed(() => statusOf(props.status, props.text))
</script>
<template>
  <span class="badge" :class="color || s.color"><slot>{{ text || s.text }}</slot></span>
</template>

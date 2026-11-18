<script setup>
import { computed } from 'vue'
import { mdLite } from '@/utils/format'
const props = defineProps({ text: { type: String, default: '' } })
const html = computed(() => mdLite(props.text))
</script>
<template><div class="md" v-html="html" /></template>

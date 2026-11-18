<script setup>
defineProps({ p: { type: String, default: '' } })
</script>
<template>
  <span class="badge" :class="(p || 'p2').toLowerCase()">{{ p || '—' }}</span>
</template>

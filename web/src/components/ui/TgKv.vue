<script setup>
/* 键值对：pairs [[label, value]] */
defineProps({ pairs: { type: Array, default: () => [] } })
</script>
<template>
  <dl class="kv"><template v-for="([k, v], i) in pairs" :key="i"><dt>{{ k }}</dt><dd>{{ v ?? '—' }}</dd></template><slot /></dl>
</template>

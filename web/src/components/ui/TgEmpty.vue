<script setup>
defineProps({ title: { type: String, default: '暂无数据' }, hint: { type: String, default: '' } })
</script>
<template>
  <div class="empty"><strong>{{ title }}</strong><div v-if="hint || $slots.default" class="tiny muted"><slot>{{ hint }}</slot></div><div v-if="$slots.action" class="row" style="margin-top:10px;justify-content:center"><slot name="action" /></div></div>
</template>

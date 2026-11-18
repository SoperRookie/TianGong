<script setup>
import TgIcon from './TgIcon.vue'
defineProps({ label: String, value: [String, Number], foot: { type: String, default: '' }, icon: { type: String, default: '' }, color: { type: String, default: 'blue' }, to: { type: [String, Object], default: null } })
</script>
<template>
  <component :is="to ? 'router-link' : 'div'" :to="to" class="panel stat">
    <div class="row">
      <div><div class="stat-label">{{ label }}</div><div class="stat-num">{{ value ?? '—' }}</div></div>
      <div v-if="icon" class="stat-icon" :class="color"><TgIcon :name="icon" :size="18" /></div>
    </div>
    <div v-if="foot || to" class="stat-foot"><span>{{ foot }}</span><span v-if="to" class="link">去处理 →</span></div>
  </component>
</template>

<script setup>
/* 页头：标题 · 徽标 · 说明；右侧 #actions（主按钮 + 更多）；#back 返回链接 */
defineProps({ title: { type: String, default: '' }, sub: { type: String, default: '' } })
</script>
<template>
  <div v-if="$slots.back" class="back"><slot name="back" /></div>
  <div class="page-heading">
    <div>
      <h1><slot name="title">{{ title }}</slot> <slot name="badge" /></h1>
      <p v-if="sub || $slots.sub"><slot name="sub">{{ sub }}</slot></p>
    </div>
    <div v-if="$slots.actions" class="actions"><slot name="actions" /></div>
  </div>
</template>

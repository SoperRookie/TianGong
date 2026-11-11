<script setup>
/* 表单行：<TgField label="名称" hint="…" required><input class="field" v-model="x"></TgField> */
defineProps({ label: { type: String, default: '' }, hint: { type: String, default: '' }, required: Boolean, inline: Boolean })
</script>
<template>
  <label class="frow" :class="{ inline }">
    <span v-if="label" class="flabel">{{ label }}<i v-if="required" class="req">*</i></span>
    <span class="fctl"><slot /><small v-if="hint" class="muted">{{ hint }}</small></span>
  </label>
</template>

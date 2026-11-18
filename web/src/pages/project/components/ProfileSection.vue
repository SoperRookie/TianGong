<script setup>
/* 只读画像分段：background 文本 / glossary 术语表 / 其余为列表 */
defineProps({ k: { type: String, required: true }, pf: { type: Object, default: () => ({}) } })
</script>
<template>
  <template v-if="k === 'background'"><div v-if="pf?.background" style="white-space:pre-wrap">{{ pf.background }}</div><div v-else class="muted">—</div></template>
  <template v-else-if="k === 'glossary'">
    <table v-if="(pf?.glossary || []).length" class="table dense"><tbody><tr v-for="g in pf.glossary" :key="g.term"><td style="width:180px"><b>{{ g.term }}</b></td><td>{{ g.meaning }}</td></tr></tbody></table>
    <div v-else class="muted">—</div>
  </template>
  <template v-else>
    <ul v-if="(pf?.[k] || []).length" class="steps-mini" style="margin:4px 0 0 18px;padding:0"><li v-for="(x, i) in pf[k]" :key="i">{{ x }}</li></ul>
    <div v-else class="muted">—</div>
  </template>
</template>

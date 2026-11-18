<script setup>
/* 分页：page / size v-model，total 总数 */
import { computed } from 'vue'
const props = defineProps({ page: { type: Number, default: 1 }, size: { type: Number, default: 20 }, total: { type: Number, default: 0 }, sizes: { type: Array, default: () => [20, 50, 100, 200] } })
const emit = defineEmits(['update:page', 'update:size'])
const pages = computed(() => Math.max(1, Math.ceil(props.total / props.size)))
</script>
<template>
  <div class="table-footer">
    <span class="muted">共 {{ total }} 条 · 第 {{ page }} / {{ pages }} 页</span>
    <span class="row">
      <select class="select small" :value="size" @change="emit('update:size', +$event.target.value); emit('update:page', 1)"><option v-for="s in sizes" :key="s" :value="s">{{ s }} 条 / 页</option></select>
      <button type="button" class="btn small" :disabled="page <= 1" @click="emit('update:page', page - 1)">上一页</button>
      <button type="button" class="btn small" :disabled="page >= pages" @click="emit('update:page', page + 1)">下一页</button>
    </span>
  </div>
</template>

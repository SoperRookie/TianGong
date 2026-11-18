<script setup>
/* 审核工作台公共壳：返回链接 · 页头（标题 / 徽标 / 统计 / 右侧动作）· 提示条 · 左列表右详情 · 底部 sticky 状态条 · 批量条。
   列表 / 详情通过 #list / #detail 插槽填充；页签型视图（盲区 / 重复 / 提案）可用 #workbench 整体替换两栏。 */
import { TgBadge } from '@/components/ui'
defineProps({
  title: { type: String, default: '' }, badge: { type: String, default: '' },
  backTo: { type: Object, default: null }, backLabel: { type: String, default: '' }, taskStatus: { type: String, default: '' },
  stats: { type: Array, default: () => [] },     // [[label, value]]
  processed: { type: Number, default: 0 }, elapsed: { type: String, default: '00:00' },
  msg: { type: String, default: '' }, msgKind: { type: String, default: '' },
  keys: { type: Array, default: () => [] },      // [[key, label]]
  canUndo: Boolean, checked: { type: Number, default: 0 }, checkedLabel: { type: String, default: '条' },
})
const emit = defineEmits(['undo', 'clear'])
</script>
<template>
  <router-link v-if="backTo" class="back" :to="backTo">← 任务概览 <span class="sep">/</span> {{ backLabel }} <TgBadge v-if="taskStatus" :status="taskStatus" /></router-link>
  <div class="review-head">
    <h1 class="wb-title">{{ title }} <span v-if="badge" class="badge blue">{{ badge }}</span></h1>
    <div class="stats"><span v-for="([k, v], i) in stats" :key="i">{{ k }} <b>{{ v }}</b></span></div>
    <div class="right"><slot name="actions" /></div>
  </div>
  <slot name="banner" />
  <slot name="workbench">
    <div class="workbench">
      <div class="panel list-pane"><slot name="list" /></div>
      <div class="panel panel-pad detail-pane"><slot name="detail" /></div>
    </div>
  </slot>
  <div class="statusbar">
    <span>本次已处理 <b>{{ processed }}</b></span>
    <span>用时 {{ elapsed }}</span>
    <span class="kbd-hint"><template v-for="([k, l], i) in keys" :key="i"><kbd>{{ k }}</kbd> {{ l }}<template v-if="i < keys.length - 1"> · </template></template></span>
    <span v-if="msg" class="wb-msg" :class="msgKind">{{ msg }}</span>
    <span class="undo" :class="{ off: !canUndo }" @click="emit('undo')">撤销上一步 <kbd>u</kbd></span>
  </div>
  <div v-if="checked" class="batch">已选 {{ checked }} {{ checkedLabel }}<slot name="batch" /><span class="x" @click="emit('clear')">✕</span></div>
</template>
<style scoped>
.wb-title { margin: 0; font-size: 19px; font-weight: 780; }
.wb-msg { color: #2b3b5b; font-weight: 600; max-width: 420px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wb-msg.err { color: var(--red); }
.wb-msg.warn { color: var(--amber); }
.undo.off { color: #aab4c4; cursor: default; }
.statusbar kbd { border: 1px solid #d9dfe8; border-radius: 3px; padding: 0 4px; font-family: inherit; background: #fff; color: #5c697b; font-size: 11px; }
</style>

<script setup>
import { useUiStore } from '@/stores/ui'
const ui = useUiStore()
</script>
<template>
  <Teleport to="body">
    <Transition name="fade">
      <div v-if="ui.toastItem" class="toast" :class="ui.toastItem.kind" role="status">{{ ui.toastItem.msg }}<a v-if="ui.toastItem.undo" @click="ui.undoToast()">撤销</a><a v-else class="x" @click="ui.toastItem = null">×</a></div>
    </Transition>
  </Teleport>
</template>

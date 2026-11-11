<script setup>
import { useUiStore } from '@/stores/ui'
import { TgDialog } from '@/components/ui'
const ui = useUiStore()
</script>
<template>
  <TgDialog v-if="ui.dialog" :model-value="true" :title="ui.dialog.title" :ok="ui.dialog.ok" :cancel="ui.dialog.cancel" :danger="ui.dialog.danger" @update:model-value="ui.closeDialog(false)" @ok="ui.closeDialog(true)">
    <div style="white-space:pre-wrap">{{ ui.dialog.body }}</div>
  </TgDialog>
</template>

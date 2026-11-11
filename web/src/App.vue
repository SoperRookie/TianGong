<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import AppShell from '@/components/shell/AppShell.vue'
const route = useRoute()
const bare = computed(() => route.meta.bare)
</script>

<template>
  <router-view v-if="bare" />
  <AppShell v-else />
</template>

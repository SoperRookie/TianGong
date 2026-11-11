<script setup>
import { TgIcon } from '@/components/ui'
defineProps({ group: Object, section: String })
</script>
<template>
  <nav class="sub-nav" aria-label="二级导航">
    <div class="sub-crumb"><TgIcon name="folder" :size="14" /> {{ group.name }} <i>/</i></div>
    <div class="sub-links">
      <router-link v-for="[k, n] in group.pages" :key="k" :to="{ name: k }" :class="{ active: section === k }" custom v-slot="{ navigate }"><button type="button" :class="{ active: section === k }" @click="navigate">{{ n }}</button></router-link>
    </div>
  </nav>
</template>

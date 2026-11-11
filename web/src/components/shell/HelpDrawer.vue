<script setup>
/* 「本页交互说明」抽屉：目标 / 规则 / 交互 / 验收 + 快捷键 + 本页覆盖的功能项。内容来自 src/specs.js。 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useUiStore } from '@/stores/ui'
import { SPECS, FEATURE_MAP } from '@/specs'
import features from '@/data/features.json'
import { sectionOf } from '@/nav'
import { TgDrawer } from '@/components/ui'
const ui = useUiStore(), route = useRoute()
const key = computed(() => (SPECS[route.name] ? route.name : sectionOf(route.name)))
const spec = computed(() => SPECS[key.value] || SPECS.default)
const feats = computed(() => { const nos = FEATURE_MAP[route.name] || FEATURE_MAP[sectionOf(route.name)] || []; return features.filter(f => nos.includes(f.no)) })
</script>
<template>
  <TgDrawer :model-value="ui.help" title="本页交互说明" :sub="spec.title || ''" @update:model-value="ui.help = $event">
    <div class="goal">{{ spec.goal }}</div>
    <h4>规则</h4><ol><li v-for="(x, i) in spec.rules" :key="i">{{ x }}</li></ol>
    <h4>交互</h4><ol><li v-for="(x, i) in spec.inter" :key="i">{{ x }}</li></ol>
    <h4>验收</h4><ol><li v-for="(x, i) in spec.accept" :key="i">{{ x }}</li></ol>
    <h4 v-if="spec.keys && spec.keys.length">快捷键</h4>
    <div v-if="spec.keys && spec.keys.length" class="kbd-hint"><span v-for="[k, d] in spec.keys" :key="k"><kbd>{{ k }}</kbd> {{ d }}</span></div>
    <h4>全局快捷键</h4>
    <div class="kbd-hint"><span><kbd>⌘K</kbd> 全局搜索</span><span><kbd>?</kbd> 本说明</span><span><kbd>Esc</kbd> 关闭抽屉 / 对话框</span><span><kbd>⌘⏎</kbd> 对话框确定</span></div>
    <template v-if="feats.length">
      <h4>本页覆盖的功能项（{{ feats.length }}）</h4>
      <div class="list"><div v-for="f in feats" :key="f.no"><b class="mono">{{ f.no }}</b> {{ f.name }} <span class="muted tiny">{{ f.entry }}</span></div></div>
    </template>
  </TgDrawer>
</template>

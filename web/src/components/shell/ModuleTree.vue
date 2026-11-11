<script setup>
/* 左侧模块树：第一层端（seg），第二层项目模块树（最多 5 级）；筛选写入 scope.module / scope.terminal。 */
import { computed, ref } from 'vue'
import { useScopeStore } from '@/stores/scope'
import { TERMINALS } from '@/utils/terminals'
import { TgIcon } from '@/components/ui'
const scope = useScopeStore()
const collapsed = ref(new Set())
const counts = computed(() => scope.moduleCounts || {})
const total = computed(() => Object.values(counts.value).reduce((s, n) => s + n, 0))
function toggle(id) { const s = new Set(collapsed.value); s.has(id) ? s.delete(id) : s.add(id); collapsed.value = s }
function cnt(n) { const c = counts.value; if (!scope.moduleCounts) return null; return c[n.name] ?? c[n.path] ?? 0 }
const flat = computed(() => {
  const out = []
  const walk = (nodes, depth, hiddenParent) => (nodes || []).forEach(n => {
    const path = n.path || n.name
    if (!hiddenParent) out.push({ ...n, depth, path, has: !!(n.children && n.children.length) })
    walk(n.children, depth + 1, hiddenParent || collapsed.value.has(n.module_id))
  })
  walk(scope.modules.tree, 0, false)
  return out
})
</script>
<template>
  <aside class="scoped-sidebar">
    <div class="side-topline"><TgIcon name="layers" :size="15" /> 业务模块筛选 <span class="badge blue">按端</span></div>
    <div class="side-hint">{{ scope.project || '未选择项目' }} · {{ scope.flatModules.length }} 个模块 <router-link :to="{ name: 'modules' }">管理模块</router-link></div>
    <div class="tree-group">端</div>
    <div class="seg side-seg">
      <button type="button" :class="{ on: !scope.terminal }" @click="scope.setTerminal('')">全部端</button>
      <button v-for="t in TERMINALS.slice(0, 3)" :key="t.key" type="button" :class="{ on: scope.terminal === t.key }" @click="scope.setTerminal(t.key)">{{ t.label }}</button>
    </div>
    <div class="seg side-seg">
      <button v-for="t in TERMINALS.slice(3)" :key="t.key" type="button" :class="{ on: scope.terminal === t.key }" @click="scope.setTerminal(t.key)">{{ t.label }}</button>
    </div>
    <div class="tree-group">模块</div>
    <button type="button" class="module-choice" :class="{ chosen: !scope.module }" @click="scope.setModule('')"><TgIcon name="folder" :size="14" /> 全部模块 <span v-if="scope.moduleCounts" class="cnt">{{ total }}</span></button>
    <div v-for="n in flat" :key="n.module_id" class="tree-item" :class="'l' + Math.min(n.depth + 1, 5)" :style="{ paddingLeft: (n.depth * 12) + 'px' }">
      <span class="tw" @click="n.has && toggle(n.module_id)">{{ n.has ? (collapsed.has(n.module_id) ? '▸' : '▾') : '·' }}</span>
      <button type="button" class="module-choice" :class="{ chosen: scope.module === n.name }" :title="n.path" @click="scope.setModule(scope.module === n.name ? '' : n.name)">{{ n.name }}<span v-if="cnt(n) !== null" class="cnt">{{ cnt(n) }}</span></button>
    </div>
    <div v-if="!flat.length" class="muted tiny" style="padding:8px 14px">项目还没有模块，到「项目 › 模块与端」添加。</div>
    <div class="side-foot"><div class="pill"><TgIcon name="shield" :size="13" /> AI 生成内容必须人工审核</div>模块树来自项目「模块与端」页（最多 5 级，逻辑删除可恢复）；端与模块筛选只作用于需求、待确认事项、设计任务、测试点 / 用例与用例库。</div>
  </aside>
</template>

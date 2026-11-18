<script setup>
import { ref, watch, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useUiStore } from '@/stores/ui'
import { api } from '@/api/client'
import { TgBadge } from '@/components/ui'
const ui = useUiStore(), router = useRouter()
const q = ref(''), groups = ref([]), loading = ref(false), input = ref(null), active = ref(0)
let timer = null, seq = 0
watch(() => ui.search, v => { if (v) { q.value = ''; groups.value = []; nextTick(() => input.value?.focus()) } })
watch(q, v => { clearTimeout(timer); timer = setTimeout(run, 220) })
async function run() {
  const kw = q.value.trim(); if (!kw) { groups.value = []; return }
  const my = ++seq; loading.value = true
  try { const r = await api.get('/api/v1/search', { q: kw, limit: 6 }); if (my === seq) { groups.value = r.groups || []; active.value = 0 } }
  catch (e) { ui.error(e) } finally { if (my === seq) loading.value = false }
}
function toOf(kind, it) {
  switch (kind) {
    case 'requirement': return { name: 'req-detail', params: { id: it.req_id } }
    case 'task': return { name: 'design-detail', params: { id: it.task_id } }
    case 'point': return { name: 'points', params: { tid: it.task_id }, query: { sel: it.tp_id } }
    case 'case': return { name: 'cases', params: { tid: it.task_id }, query: { sel: it.case_id } }
    case 'plan': return { name: 'plan-detail', params: { id: it.plan_id } }
    case 'knowledge': return { name: 'knowledge', query: { doc: it.doc_id } }
    default: return '/'
  }
}
function go(kind, it) { ui.search = false; router.push(toOf(kind, it)) }
function flat() { return groups.value.flatMap(g => g.items.map(it => [g.kind, it])) }
function onKey(e) {
  const f = flat()
  if (e.key === 'ArrowDown') { active.value = Math.min(f.length - 1, active.value + 1); e.preventDefault() }
  else if (e.key === 'ArrowUp') { active.value = Math.max(0, active.value - 1); e.preventDefault() }
  else if (e.key === 'Enter' && f[active.value]) go(...f[active.value])
}
function idx(gi, ii) { let n = 0; for (let i = 0; i < gi; i++) n += groups.value[i].items.length; return n + ii }
const title = (kind, it) => ({ requirement: it.title, task: it.requirement_title || (it.sources || []).join('、') || it.task_id, point: it.point, case: it.title, plan: it.name, knowledge: it.source }[kind])
const id = (kind, it) => ({ requirement: it.req_id, task: String(it.task_id).slice(0, 8), point: it.tp_id, case: it.case_id, plan: it.plan_id, knowledge: String(it.doc_id).slice(0, 8) }[kind])
</script>
<template>
  <Teleport to="body">
    <div v-if="ui.search" class="search-overlay" @click.self="ui.search = false">
      <div class="search-box">
        <input ref="input" v-model="q" placeholder="全局搜索：需求 / 任务 / 测试点 / 用例 / 测试计划 / 知识文档" @keydown="onKey" />
        <template v-if="q.trim()">
          <template v-for="(g, gi) in groups" :key="g.kind">
            <div class="grp"><span>{{ g.label }}</span><span>{{ g.total }}</span></div>
            <div v-for="(it, ii) in g.items" :key="ii" class="hit" :class="{ on: idx(gi, ii) === active }" @click="go(g.kind, it)">
              <span class="mono">{{ id(g.kind, it) }}</span>{{ title(g.kind, it) }}
              <span class="muted right">{{ it.project || it.space || '' }} <TgBadge v-if="it.status || it.review" :status="it.status || it.review" /></span>
            </div>
          </template>
          <div v-if="!groups.length && !loading" class="empty">没有匹配结果</div>
          <div v-if="loading" class="empty tiny">搜索中…</div>
        </template>
        <div v-else class="empty tiny">输入关键字，↑↓ 选择，回车跳转；Esc 关闭</div>
      </div>
    </div>
  </Teleport>
</template>

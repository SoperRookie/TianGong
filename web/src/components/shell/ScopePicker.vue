<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useScopeStore } from '@/stores/scope'
import { useBadgeStore } from '@/stores/badge'
import { TgIcon, TgBadge } from '@/components/ui'
const emit = defineEmits(['close'])
const scope = useScopeStore(), badge = useBadgeStore()
const kw = ref(''), line = ref(scope.line)
const root = ref(null)
const lines = computed(() => ['', ...scope.lines])
const list = computed(() => scope.projects.filter(p => (!line.value || p.business_line === line.value) && (!kw.value || p.project.toLowerCase().includes(kw.value.toLowerCase()) || (p.code || '').toLowerCase().includes(kw.value.toLowerCase()))))
function pick(p) { scope.setProject(p.project); badge.refresh(true); emit('close') }
function pickLine(l) { line.value = l }
function all() { scope.setLine(line.value); emit('close') }
const onDoc = e => { if (root.value && !root.value.contains(e.target) && !e.target.closest('.scope')) emit('close') }
onMounted(() => { setTimeout(() => document.addEventListener('click', onDoc)); scope.load() })
onBeforeUnmount(() => document.removeEventListener('click', onDoc))
</script>
<template>
  <div ref="root" class="scope-pop">
    <div class="scope-lines">
      <div class="scope-h">产品线</div>
      <button v-for="l in lines" :key="l" type="button" class="scope-line" :class="{ on: line === l }" @click="pickLine(l)">{{ l || '全部产品线' }}<span class="muted">{{ l ? scope.projects.filter(p => p.business_line === l).length : scope.projects.length }}</span></button>
    </div>
    <div class="scope-projects">
      <div class="row" style="margin-bottom:8px"><input v-model="kw" class="field" placeholder="搜索项目名 / 代号" autofocus /><button type="button" class="btn small" @click="all">{{ line ? '产品线内全部项目' : '全部项目' }}</button></div>
      <div class="scope-list">
        <button v-for="p in list" :key="p.project" type="button" class="scope-item" :class="{ on: p.project === scope.project }" @click="pick(p)">
          <TgIcon v-if="p.favorite" name="star" :size="12" /><span class="nm">{{ p.project }}</span><span class="muted tiny">{{ p.business_line || '未分产品线' }}</span><TgBadge :status="p.status" /><span class="muted tiny">{{ p.cases }} 用例</span>
        </button>
        <div v-if="!list.length" class="muted tiny" style="padding:12px">没有匹配项目</div>
      </div>
    </div>
  </div>
</template>

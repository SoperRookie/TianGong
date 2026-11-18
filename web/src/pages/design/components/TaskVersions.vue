<script setup>
/* 版本链（7.13 / 需求 10 章）：按用例 / 测试点查看完整版本链（来源、修改人、时间、原因、字段差异），恢复为追加 manual 新版。 */
import { ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtTime } from '@/utils/format'
import { TgPanel, TgBadge, TgPrio, TgEmpty, TgSeg } from '@/components/ui'
import { allPoints, caseState } from './taskUtils'
const props = defineProps({ task: { type: Object, required: true } })
const emit = defineEmits(['reload'])
const route = useRoute(), router = useRouter(), auth = useAuthStore(), ui = useUiStore()
const kind = ref(route.query.kind === 'point' ? 'point' : 'case')
const entity = ref(route.query.entity || '')
const kw = ref('')
const versions = ref(null), loading = ref(false)
const canEdit = computed(() => auth.can((props.task.context || {}).project, 'case.edit'))
const cases = computed(() => (props.task.result || {}).cases || [])
const points = computed(() => allPoints(props.task).filter(p => p.tp_id))
const list = computed(() => {
  const k = kw.value.trim().toLowerCase()
  const rows = kind.value === 'case' ? cases.value.map(c => ({ id: c.uid, code: c.case_id, title: c.title, module: c.module, version: c.version || 1, status: caseState(props.task, c).status }))
    : points.value.map(p => ({ id: p.tp_id, code: p.tp_id, title: p.point, module: p.module, version: p.version || 1, status: p.status || 'pending' }))
  return k ? rows.filter(r => `${r.code} ${r.title} ${r.module}`.toLowerCase().includes(k)) : rows
})
const current = computed(() => list.value.find(r => r.id === entity.value) || null)
async function load() {
  versions.value = null
  if (!entity.value) return
  loading.value = true
  try { versions.value = (await api.get(`/api/v1/tasks/${props.task.task_id}/versions`, { kind: kind.value, entity_id: entity.value })).versions || [] } catch (e) { ui.error(e) } finally { loading.value = false }
}
watch([kind, entity], () => { router.replace({ query: { ...route.query, kind: kind.value, entity: entity.value || undefined } }); load() }, { immediate: true })
watch(kind, () => { entity.value = '' })
const total = computed(() => (versions.value || []).length)
async function restore(v) {
  if (!await ui.confirm('恢复历史版本？', `确定恢复到 v${v.version_no}？将基于该版追加一条人工新版并回到待审核，历史不会被覆盖。`, { ok: '恢复此版' })) return
  try { await api.post(`/api/v1/tasks/${props.task.task_id}/versions/restore`, { kind: kind.value, entity_id: entity.value, version_no: v.version_no }); ui.toast(`已恢复到 v${v.version_no}（追加新版，待重新审核）`); emit('reload'); load() } catch (e) { ui.error(e) }
}
</script>
<template>
  <div class="grid-2 mt" style="grid-template-columns:minmax(280px,1fr) minmax(0,1.6fr)">
    <TgPanel :pad="false">
      <div class="list-tools"><div class="row"><TgSeg v-model="kind" :items="[['case', `用例 ${cases.length}`], ['point', `测试点 ${points.length}`]]" /><input v-model="kw" class="field small" style="flex:1" placeholder="搜索编号 / 标题" /></div></div>
      <div class="list-scroll" style="max-height:70vh">
        <TgEmpty v-if="!list.length" :title="kind === 'case' ? '没有用例' : '没有测试点'" />
        <div v-for="r in list" :key="r.id" class="item" :class="{ sel: r.id === entity, ok: r.status === 'approved', rej: r.status === 'rejected' }" @click="entity = r.id">
          <span class="st"></span><span class="id">{{ r.code }}</span><span class="ttl" :title="r.title">{{ r.title }}</span><span class="tiny muted">v{{ r.version }}</span>
        </div>
      </div>
    </TgPanel>
    <TgPanel :title="current ? `${current.code} · ${current.title}` : '版本链'" :hint="current ? `${current.module} · 当前 v${current.version} · ${total} 个版本` : '左侧选一条用例 / 测试点'">
      <template #actions><TgBadge v-if="current" :status="current.status" /></template>
      <TgEmpty v-if="!entity" title="请选择一条用例或测试点" />
      <div v-else-if="loading" class="page-loading"><span class="spinner"></span> 加载中…</div>
      <TgEmpty v-else-if="versions && !versions.length" title="还没有版本记录" hint="存量任务在首次改动时补记首版" />
      <div v-else class="stack">
        <div v-for="v in (versions || []).slice().reverse()" :key="v.version_no" class="source-block">
          <div class="row"><b>v{{ v.version_no }}</b><span class="tag">{{ v.source_label || v.source }}</span><span class="tiny muted">{{ v.created_by || '' }} · {{ fmtTime(v.created_at) }}</span><span v-if="v.reason" class="tiny muted">· {{ v.reason }}</span>
            <button v-if="canEdit && v.version_no < total" type="button" class="btn small right" @click="restore(v)">恢复此版</button><span v-else-if="v.version_no === total" class="badge green right">当前</span></div>
          <template v-if="kind === 'case'">
            <div style="margin-top:6px"><b>{{ v.content?.title }}</b> <TgPrio :p="v.content?.priority" /> <span class="tag">{{ v.content?.module }}</span></div>
            <div v-if="v.content?.precondition" class="tiny muted">前置：{{ v.content.precondition }}</div>
            <ol class="steps-mini tiny muted" style="padding-left:18px;margin:4px 0"><li v-for="(s, i) in v.content?.steps || []" :key="i">{{ s.action }} → {{ s.expected }}</li></ol>
          </template>
          <div v-else style="margin-top:6px">{{ v.content?.point }} <span v-if="v.content?.dimension" class="tag">{{ v.content.dimension }}</span></div>
          <div v-if="(v.changes || []).length" class="tiny" style="margin-top:6px"><div v-for="(ch, i) in v.changes" :key="i"><b>{{ ch.field }}</b>：<span class="diff-del">{{ ch.before }}</span> → <span class="diff-add">{{ ch.after }}</span></div></div>
        </div>
      </div>
    </TgPanel>
  </div>
</template>

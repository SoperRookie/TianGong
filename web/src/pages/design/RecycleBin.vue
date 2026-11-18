<script setup>
/* 项目回收站（7.12）：GET /api/v1/recycle-bin?project= 聚合项目下全部任务的逻辑删除条目，按任务分组；恢复 / 永久删除走任务级接口。 */
import { ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtTime } from '@/utils/format'
import { PageHeading, TgPanel, TgTable, TgSeg, TgEmpty, TgBadge, TgIcon } from '@/components/ui'
import { taskTitle } from './components/taskUtils'
const route = useRoute(), router = useRouter(), scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore()
const kind = ref(route.query.kind || ''), kw = ref(route.query.kw || '')
const { data, loading, error, reload } = useLoad(() => scope.project ? api.get('/api/v1/recycle-bin', { project: scope.project }) : Promise.resolve({ items: [] }), { deps: [() => scope.project] })
const { data: tasksData, reload: reloadTasks } = useLoad(() => scope.project ? api.get('/api/v1/tasks', { project: scope.project, limit: 5000 }) : Promise.resolve({ tasks: [] }), { deps: [() => scope.project] })
const taskMap = computed(() => Object.fromEntries((tasksData.value?.tasks || []).map(t => [t.task_id, t])))
const items = computed(() => data.value?.items || [])
const filtered = computed(() => { const k = kw.value.trim().toLowerCase(); return items.value.filter(i => (!kind.value || i.kind === kind.value) && (!k || `${i.entity_id} ${i.label} ${i.module || ''}`.toLowerCase().includes(k))) })
const groups = computed(() => {
  const by = {}
  for (const it of filtered.value) (by[it.task_id] || (by[it.task_id] = [])).push(it)
  return Object.entries(by).map(([task_id, list]) => ({ task_id, task: taskMap.value[task_id], items: list }))
})
watch([kind, kw], () => router.replace({ query: { kind: kind.value || undefined, kw: kw.value || undefined } }))
const canEdit = computed(() => auth.can(scope.project, 'case.edit'))
async function restore(it) {
  try { const r = await api.post(`/api/v1/tasks/${it.task_id}/recycle-bin/restore`, { item_id: it.id }); ui.toast(`已恢复 ${r.restored || it.entity_id} 回待审核（版本 +1，来源 manual）`); reload(true); reloadTasks(true) } catch (e) { ui.error(e) }
}
async function purge(it) {
  if (!await ui.confirm('永久删除？', `「${it.label}」将从回收站抹掉快照，不可恢复（版本历史仍留档）。仅系统管理员。`, { ok: '永久删除', danger: true })) return
  try { await api.del(`/api/v1/tasks/${it.task_id}/recycle-bin/${it.id}`); ui.toast('已永久删除'); reload(true) } catch (e) { ui.error(e) }
}
const cols = [{ key: 'kind', title: '类型', w: '80px' }, { key: 'entity_id', title: '编号', w: '110px' }, { key: 'label', title: '内容' }, { key: 'module', title: '模块', w: '170px' }, { key: 'deleted', title: '删除人 / 时间', w: '190px' }, { key: 'ops', title: '', w: '150px' }]
</script>
<template>
  <PageHeading title="回收站" :sub="scope.project ? `${scope.project} · 逻辑删除的测试点与用例快照（用例 ${items.filter(i => i.kind === 'case').length} · 测试点 ${items.filter(i => i.kind === 'point').length}）；恢复回待审核并记 manual 版本；管理员可永久删除。需求回收站见需求列表「已删除」，任务恢复见设计任务「已删除」。` : '请先在顶栏选择一个项目'">
    <template #actions><button type="button" class="btn" @click="reload()"><TgIcon name="refresh" :size="14" /> 刷新</button></template>
  </PageHeading>
  <div v-if="error" class="err-box">{{ error.message }}</div>
  <TgPanel :pad="false">
    <div class="filters" style="padding:12px 14px 10px">
      <TgSeg v-model="kind" :items="[['', `全部 ${items.length}`], ['case', `用例 ${items.filter(i => i.kind === 'case').length}`], ['point', `测试点 ${items.filter(i => i.kind === 'point').length}`]]" />
      <input v-model="kw" class="field" style="width:220px" placeholder="搜索编号 / 内容 / 模块" />
      <span class="tiny muted">按任务分组 · {{ groups.length }} 个任务</span>
    </div>
    <TgEmpty v-if="!scope.project" title="未选择项目" hint="回收站按项目聚合，请在顶栏工作范围选择项目。" />
    <TgEmpty v-else-if="!loading && !filtered.length" :title="items.length ? '没有符合筛选的条目' : '回收站是空的'" />
    <div v-else-if="loading && !items.length" class="page-loading"><span class="spinner"></span> 加载中…</div>
    <div v-for="g in groups" :key="g.task_id">
      <div class="grp" style="position:static">
        <span><router-link :to="{ name: 'design-detail', params: { id: g.task_id } }" class="link">{{ g.task ? taskTitle(g.task) : g.task_id }}</router-link> <span class="mono muted">{{ g.task_id }}</span> <TgBadge v-if="g.task" :status="g.task.status" /></span>
        <span>{{ g.items.length }} 条 · <router-link :to="{ name: 'design-detail', params: { id: g.task_id, sub: 'recycle' } }" class="link">任务回收站</router-link></span>
      </div>
      <TgTable :cols="cols" :rows="g.items" row-key="id" dense>
        <template #cell-kind="{ row }"><span class="tag">{{ row.kind === 'case' ? '用例' : '测试点' }}</span></template>
        <template #cell-entity_id="{ row }"><span class="mono">{{ row.entity_id }}</span></template>
        <template #cell-module="{ row }">{{ row.module || '—' }}</template>
        <template #cell-deleted="{ row }">{{ row.deleted_by }} · {{ fmtTime(row.deleted_at) }}</template>
        <template #cell-ops="{ row }"><div class="row nowrap"><button v-if="canEdit" type="button" class="btn small" @click="restore(row)">恢复</button><button v-if="auth.isAdmin" type="button" class="btn small danger" @click="purge(row)">永久删除</button></div></template>
      </TgTable>
    </div>
  </TgPanel>
</template>

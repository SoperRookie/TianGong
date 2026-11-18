<script setup>
/* 任务回收站（7.12）：被删除的测试点 / 用例快照，恢复回待审核并记 manual 版本；管理员可永久删除。 */
import { ref, computed, watch } from 'vue'
import { api } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtTime } from '@/utils/format'
import { TgPanel, TgTable, TgSeg } from '@/components/ui'
const props = defineProps({ task: { type: Object, required: true } })
const emit = defineEmits(['reload'])
const auth = useAuthStore(), ui = useUiStore()
const items = ref([]), loading = ref(false), kind = ref('')
const canEdit = computed(() => auth.can((props.task.context || {}).project, 'case.edit'))
async function load() { loading.value = true; try { items.value = (await api.get(`/api/v1/tasks/${props.task.task_id}/recycle-bin`)).items || [] } catch (e) { ui.error(e) } finally { loading.value = false } }
watch(() => props.task.task_id, load, { immediate: true })
const rows = computed(() => items.value.filter(i => !kind.value || i.kind === kind.value))
async function restore(it) {
  try { const r = await api.post(`/api/v1/tasks/${props.task.task_id}/recycle-bin/restore`, { item_id: it.id }); ui.toast(`已恢复 ${r.restored || it.entity_id} 回待审核（版本 +1）`); load(); emit('reload') } catch (e) { ui.error(e) }
}
async function purge(it) {
  if (!await ui.confirm('永久删除？', `「${it.label}」将从回收站抹掉快照，不可恢复（版本历史仍留档）。`, { ok: '永久删除', danger: true })) return
  try { await api.del(`/api/v1/tasks/${props.task.task_id}/recycle-bin/${it.id}`); ui.toast('已永久删除'); load() } catch (e) { ui.error(e) }
}
</script>
<template>
  <TgPanel class="mt" :pad="false">
    <div class="filters" style="padding:12px 14px 10px"><TgSeg v-model="kind" :items="[['', `全部 ${items.length}`], ['case', `用例 ${items.filter(i => i.kind === 'case').length}`], ['point', `测试点 ${items.filter(i => i.kind === 'point').length}`]]" /><span class="tiny muted">恢复回待审核并记 manual 版本；永久删除仅管理员</span></div>
    <TgTable :cols="[{ key: 'kind', title: '类型', w: '80px' }, { key: 'entity_id', title: '编号', w: '110px' }, { key: 'label', title: '内容' }, { key: 'module', title: '模块', w: '160px' }, { key: 'deleted', title: '删除人 / 时间', w: '190px' }, { key: 'ops', title: '', w: '150px' }]" :rows="rows" row-key="id" :loading="loading" empty="回收站是空的">
      <template #cell-kind="{ row }"><span class="tag">{{ row.kind === 'case' ? '用例' : '测试点' }}</span></template>
      <template #cell-entity_id="{ row }"><span class="mono">{{ row.entity_id }}</span></template>
      <template #cell-module="{ row }">{{ row.module || '—' }}</template>
      <template #cell-deleted="{ row }">{{ row.deleted_by }} · {{ fmtTime(row.deleted_at) }}</template>
      <template #cell-ops="{ row }"><div class="row nowrap"><button v-if="canEdit" type="button" class="btn small" @click="restore(row)">恢复</button><button v-if="auth.isAdmin" type="button" class="btn small danger" @click="purge(row)">永久删除</button></div></template>
    </TgTable>
  </TgPanel>
</template>

<script setup>
/* 合并需求：把 sources（一条或多条）并入同项目另一条需求；POST /requirements/{id}/merge {into}，逐条串行 */
import { ref, computed, watch } from 'vue'
import { api } from '@/api/client'
import { useUiStore } from '@/stores/ui'
import { fmtTime } from '@/utils/format'
import { TgDialog, TgField } from '@/components/ui'
const props = defineProps({ modelValue: Boolean, sources: { type: Array, default: () => [] } })
const emit = defineEmits(['update:modelValue', 'done'])
const ui = useUiStore()
const list = ref([]), into = ref(''), busy = ref(false)
const project = computed(() => props.sources[0]?.project || '')
const srcIds = computed(() => new Set(props.sources.map(s => s.req_id)))
watch(() => props.modelValue, async v => {
  if (!v) return
  into.value = ''; list.value = []
  if (!project.value) return
  try {
    const d = await api.get('/api/v1/requirements', { project: project.value })
    const titles = new Set(props.sources.map(s => s.title))
    const all = (d.requirements || []).filter(x => !srcIds.value.has(x.req_id) && x.status !== 'archived' && !x.deleted_at)
    list.value = [...all.filter(x => titles.has(x.title)), ...all.filter(x => !titles.has(x.title))]
    into.value = list.value[0]?.req_id || ''
  } catch (e) { ui.error(e) }
})
async function submit() {
  if (!into.value) return
  if (!await ui.confirm('合并需求', `${props.sources.length} 条需求将并入目标需求：任务、附件、待确认事项归到目标，本需求进入回收站。继续？`, { ok: '合并', danger: true })) return
  busy.value = true
  try {
    let r = null
    for (const s of props.sources) r = await api.post(`/api/v1/requirements/${s.req_id}/merge`, { into: into.value })
    ui.toast(`已合并到「${r?.title || into.value}」`)
    emit('update:modelValue', false); emit('done', into.value)
  } catch (e) { ui.error(e) } finally { busy.value = false }
}
</script>
<template>
  <TgDialog :model-value="modelValue" @update:model-value="v => emit('update:modelValue', v)" title="合并到…" ok="合并" danger :busy="busy" :ok-disabled="!into" @ok="submit">
    <div class="information" style="margin-bottom:10px">同一份文档重复导入时，把本需求并入另一条：任务 / 附件 / 待确认事项并入目标，人工补充以「【合并自 标题】」段追加；同名需求排在前面。</div>
    <div class="tiny muted" style="margin-bottom:8px">来源：{{ sources.map(s => s.title).join('、') }}</div>
    <TgField label="目标需求" required>
      <select v-model="into" class="select">
        <option v-for="x in list" :key="x.req_id" :value="x.req_id">{{ x.title }} · {{ x.task_count }} 个任务 · {{ fmtTime(x.created_at) }}{{ sources.some(s => s.title === x.title) ? '（同名）' : '' }}</option>
      </select>
    </TgField>
    <div v-if="!list.length" class="tiny muted">该项目没有其他可合并的需求</div>
  </TgDialog>
</template>

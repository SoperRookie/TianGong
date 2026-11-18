<script setup>
/* 附件：POST runs/{rid}/attachments multipart 上传（可关联用例），GET /plans/{id}/attachments/{att_id} 下载 */
import { ref, computed, watch } from 'vue'
import { api } from '@/api/client'
import { useUiStore } from '@/stores/ui'
import { fmtTime, fmtSize } from '@/utils/format'
import { TgTable, TgEmpty, TgIcon } from '@/components/ui'
const props = defineProps({ plan: { type: Object, required: true }, canAttach: Boolean, runId: { type: String, default: '' } })
const emit = defineEmits(['reload'])
const ui = useUiStore()
const base = computed(() => `/api/v1/plans/${props.plan.plan_id}`)
const runs = computed(() => props.plan.runs || [])
const rid = ref(props.runId || (runs.value.length ? runs.value[runs.value.length - 1].run_id : ''))
watch(() => props.runId, v => { if (v) rid.value = v })
const run = computed(() => runs.value.find(r => r.run_id === rid.value) || null)
const itemId = ref(''), uploading = ref(false), fileInput = ref(null), dragging = ref(false)
const itemOf = id => props.plan.items.find(i => i.item_id === id)
const rows = computed(() => (run.value?.attachments || []).map(a => ({ ...a, item: a.item_id ? itemOf(a.item_id) : null })).reverse())
const cols = [{ key: 'filename', title: '文件' }, { key: 'kind', title: '类型', w: '70px' }, { key: 'size', title: '大小', w: '80px' }, { key: 'item', title: '关联用例', w: '220px' }, { key: 'by', title: '上传人 / 时间', w: '170px' }]
const KIND = { image: '图片', video: '视频', log: '日志', archive: '压缩包', doc: '文档', other: '其他' }
async function upload(files) {
  const list = [...(files || [])].filter(f => f && f.name)
  if (!list.length || !run.value) return
  const fd = new FormData()
  list.forEach(f => fd.append('files', f))
  if (itemId.value) fd.append('item_id', itemId.value)
  uploading.value = true
  try { const r = await api.upload(`${base.value}/runs/${rid.value}/attachments`, fd); ui.toast(`已上传 ${r.count} 个附件`); emit('reload') } catch (e) { ui.error(e) } finally { uploading.value = false; if (fileInput.value) fileInput.value.value = '' }
}
function onDrop(e) { dragging.value = false; upload(e.dataTransfer?.files) }
async function download(a) { try { await api.download(`${base.value}/attachments/${a.att_id}`, a.filename) } catch (e) { ui.error(e) } }
const canUpload = computed(() => props.canAttach && run.value && !run.value.finished_at && props.plan.status !== 'archived')
</script>
<template>
  <TgEmpty v-if="!runs.length" title="还没有执行轮次" hint="附件挂在执行轮次上；开始一轮执行后可上传。" />
  <template v-else>
    <div class="row" style="margin-bottom:10px">
      <select v-model="rid" class="select"><option v-for="r in runs" :key="r.run_id" :value="r.run_id">{{ r.name }} · {{ r.finished_at ? '已结束' : '进行中' }} · 附件 {{ (r.attachments || []).length }}</option></select>
      <template v-if="canUpload">
        <select v-model="itemId" class="select" style="max-width:320px"><option value="">整轮附件（不关联用例）</option><option v-for="i in plan.items" :key="i.item_id" :value="i.item_id">{{ i.case_id }} {{ i.title }}</option></select>
        <button type="button" class="btn primary small" :disabled="uploading" @click="fileInput?.click()"><TgIcon name="upload" :size="14" /> {{ uploading ? '上传中…' : '上传附件' }}</button>
        <input ref="fileInput" type="file" multiple hidden @change="upload($event.target.files)" />
      </template>
      <span v-else-if="run?.finished_at" class="tiny muted">本轮已结束，不可再上传</span>
    </div>
    <div v-if="canUpload" class="drop" :class="{ on: dragging }" @dragover.prevent="dragging = true" @dragleave="dragging = false" @drop.prevent="onDrop"><TgIcon name="upload" :size="14" /> 拖放文件到这里 · 多文件 · 不限格式 · 单文件 ≤ 200MB · 图片类会被失败归因读取</div>
    <TgTable class="mt" :cols="cols" :rows="rows" row-key="att_id" dense empty="本轮还没有附件">
      <template #cell-filename="{ row }"><span class="link" @click="download(row)">{{ row.filename }}</span></template>
      <template #cell-kind="{ row }"><span class="tag">{{ KIND[row.kind] || row.kind }}</span></template>
      <template #cell-size="{ row }">{{ fmtSize(row.size) }}</template>
      <template #cell-item="{ row }"><template v-if="row.item"><span class="mono">{{ row.item.case_id }}</span><div class="sub">{{ row.item.title }}</div></template><span v-else class="muted">整轮</span></template>
      <template #cell-by="{ row }">{{ row.by }}<div class="sub">{{ fmtTime(row.at) }}</div></template>
    </TgTable>
  </template>
</template>
<style scoped>
.drop.on { background: #eef3ff; border-color: #4f7dff; }
</style>

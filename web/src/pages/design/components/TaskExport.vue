<script setup>
/* 导出（5.20 / 5.21）：用例 xlsx / csv / xmind（可只导正式、紧凑脑图、按端），测试点 xlsx / xmind（可只导已通过、按端）。 */
import { ref, computed } from 'vue'
import { api } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { TgPanel, TgTable, TgBadge, TgEmpty } from '@/components/ui'
import { caseStats, pointStats, caseTerminalRows } from './taskUtils'
const props = defineProps({ task: { type: Object, required: true } })
const auth = useAuthStore(), ui = useUiStore()
const approvedOnly = ref(false), compact = ref(false), busy = ref('')
const project = computed(() => (props.task.context || {}).project)
const canExport = computed(() => auth.can(project.value, 'case.export'))
const cs = computed(() => caseStats(props.task)), ps = computed(() => pointStats(props.task))
const termRows = computed(() => caseTerminalRows(props.task))
const pointTerms = computed(() => props.task.point_terminals || [])
const FMT = [{ fmt: 'xlsx', desc: '按用例模板列导出；优先级着色、防公式注入' }, { fmt: 'csv', desc: '同 xlsx 内容，UTF-8 BOM' }, { fmt: 'xmind', desc: 'XMind ZEN 格式；可紧凑布局（只到用例一层，步骤放备注）' }]
async function dlCases(fmt, terminal) {
  const key = fmt + (terminal || ''); busy.value = key
  const label = terminal ? '-' + (termRows.value.find(x => x.key === terminal)?.label || terminal) : ''
  try { await api.download(`/api/v1/tasks/${props.task.task_id}/files/${fmt}`, `${approvedOnly.value ? '正式用例' : '测试用例'}${label}.${fmt}`, { scope: approvedOnly.value ? 'approved' : 'all', layout: compact.value && fmt === 'xmind' ? 'compact' : '', terminal: terminal || '' }); ui.toast('已开始下载') }
  catch (e) { ui.error(e) } finally { busy.value = '' }
}
async function dlPoints(fmt, terminal) {
  const key = 'p' + fmt + (terminal || ''); busy.value = key
  const label = terminal ? '-' + (pointTerms.value.find(x => x.key === terminal)?.label || terminal) : ''
  try { await api.download(`/api/v1/tasks/${props.task.task_id}/points/export`, `${approvedOnly.value ? '正式测试点' : '测试点'}${label}.${fmt}`, { fmt, scope: approvedOnly.value ? 'approved' : 'all', terminal: terminal || '' }); ui.toast('已开始下载') }
  catch (e) { ui.error(e) } finally { busy.value = '' }
}
</script>
<template>
  <div v-if="!canExport" class="attention mt">没有 case.export 权限，不能导出。</div>
  <template v-else>
    <TgPanel class="mt" title="导出用例" hint="GET /api/v1/tasks/{id}/files/{fmt} · 用例内容（含审核状态）指纹变了就重新生成，与页面一致">
      <template #actions>
        <label class="row small"><input v-model="approvedOnly" type="checkbox" /> 只导出正式用例（{{ cs.approved }} 条）</label>
        <label class="row small"><input v-model="compact" type="checkbox" /> XMind 紧凑布局</label>
      </template>
      <TgEmpty v-if="!cs.total" title="任务还没有用例" />
      <template v-else>
        <TgTable :cols="[{ key: 'fmt', title: '格式', w: '90px' }, { key: 'desc', title: '说明' }, { key: 'state', title: '内容指纹', w: '170px' }, { key: 'op', title: '', w: '100px' }]" :rows="FMT" row-key="fmt" dense>
          <template #cell-fmt="{ row }"><b>{{ row.fmt.toUpperCase() }}</b></template>
          <template #cell-state><TgBadge v-if="task.files_dirty || !task.files_hash" status="warn" text="已变化，下载时重新生成" color="orange" /><TgBadge v-else status="ok" text="最新" /></template>
          <template #cell-op="{ row }"><button type="button" class="btn small" :disabled="!!busy" @click="dlCases(row.fmt)">{{ busy === row.fmt ? '生成中…' : '下载' }}</button></template>
        </TgTable>
        <div v-if="termRows.length > 1 || (termRows.length === 1 && termRows[0].key !== 'other')" class="mt">
          <div class="panel-title">按端单独导出</div>
          <div class="tiny muted" style="margin:4px 0 8px">按来源测试点归端；文件名带端名，即时生成不缓存。</div>
          <div class="row" v-for="x in termRows" :key="x.key" style="margin-bottom:6px"><span class="tag">{{ x.label }}</span><span class="small">{{ x.n }} 条</span><button v-for="f in ['xlsx', 'csv', 'xmind']" :key="f" type="button" class="btn small" :disabled="!!busy" @click="dlCases(f, x.key)">{{ f }}</button></div>
        </div>
      </template>
    </TgPanel>
    <TgPanel class="mt" title="导出测试点" hint="GET /api/v1/tasks/{id}/points/export · Excel 12 列（含端、来源、审核意见、覆盖用例）或与页面脑图同结构的 XMind">
      <TgEmpty v-if="!ps.total" title="任务没有测试点拆解结果" />
      <template v-else>
        <div class="row"><span class="small">全部端 · {{ ps.total }} 条{{ approvedOnly ? `（只导已通过 ${ps.approved}）` : '' }}</span><button type="button" class="btn small" :disabled="!!busy" @click="dlPoints('xlsx')">导出 xlsx</button><button type="button" class="btn small" :disabled="!!busy" @click="dlPoints('xmind')">导出脑图</button></div>
        <div v-for="x in pointTerms" :key="x.key" class="row" style="margin-top:6px"><span class="tag">{{ x.label }}</span><span class="small">{{ x.total }} 条 · 通过 {{ x.approved }}</span><button type="button" class="btn small" :disabled="!!busy" @click="dlPoints('xlsx', x.key)">xlsx</button><button type="button" class="btn small" :disabled="!!busy" @click="dlPoints('xmind', x.key)">脑图</button></div>
      </template>
    </TgPanel>
  </template>
</template>

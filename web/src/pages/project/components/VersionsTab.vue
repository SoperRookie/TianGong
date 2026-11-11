<script setup>
/* 项目版本（3.9）：GET/POST /projects/{name}/versions、PUT/DELETE .../versions/{id}；被需求引用的版本不可删（后端 400）。 */
import { ref, computed } from 'vue'
import { api, seg } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtAgo } from '@/utils/format'
import { VERSION_STATUSES } from '@/utils/consts'
import { useProjectActions } from './useProjectActions'
import { TgPanel, TgTable, TgBadge, TgDialog, TgField, TgIcon } from '@/components/ui'
const props = defineProps({ project: { type: String, required: true } })
const emit = defineEmits(['changed'])
const auth = useAuthStore(), ui = useUiStore()
const { canDelete } = useProjectActions()
const { data, loading, error, reload } = useLoad(() => api.get(`/api/v1/projects/${seg(props.project)}/versions`), { deps: [() => props.project] })
const manage = computed(() => auth.can(props.project, 'version.manage'))
const cols = [{ key: 'name', title: '版本', w: '140px' }, { key: 'code', title: '编码', w: '90px' }, { key: 'description', title: '描述' }, { key: 'start_date', title: '开始', w: '100px' }, { key: 'planned_end', title: '计划结束', w: '100px' }, { key: 'actual_end', title: '实际结束', w: '100px' }, { key: 'status', title: '状态', w: '80px' }, { key: 'created', title: '创建', w: '120px' }, { key: 'ops', title: '', w: '100px' }]
const dlg = ref(false), editing = ref(null), form = ref({}), saving = ref(false)
const blank = () => ({ name: '', code: '', description: '', start_date: '', planned_end: '', actual_end: '', status: 'not_started' })
function open(v) { editing.value = v || null; form.value = v ? { ...blank(), ...v } : blank(); dlg.value = true }
async function save() {
  const f = form.value
  if (!f.name.trim()) return ui.toast('版本名称不能为空', 'warn')
  const body = { name: f.name.trim(), code: (f.code || '').trim(), description: (f.description || '').trim(), start_date: f.start_date || '', planned_end: f.planned_end || '', actual_end: f.actual_end || '', status: f.status }
  saving.value = true
  const base = `/api/v1/projects/${seg(props.project)}/versions`
  try {
    if (editing.value) await api.put(`${base}/${editing.value.version_id}`, body); else await api.post(base, body)
    dlg.value = false; ui.toast(editing.value ? '版本已保存' : `版本「${body.name}」已创建`)
    await reload(true); emit('changed')
  } catch (e) { ui.error(e) } finally { saving.value = false }
}
async function remove(v) {
  if (!await ui.confirm('删除版本？', `「${v.name}」删除后不可恢复；仍被需求引用时会被拒绝。`, { ok: '删除', danger: true })) return
  try { await api.del(`/api/v1/projects/${seg(props.project)}/versions/${v.version_id}`); ui.toast('版本已删除'); await reload(true); emit('changed') } catch (e) { ui.error(e) }
}
</script>
<template>
  <div v-if="error" class="err-box mb">{{ error.message }}</div>
  <TgPanel title="项目版本" :hint="`${(data?.versions || []).length} 个 · 名称 / 编码 / 日期 / 状态；需求可关联版本，被引用的版本不可删`" :pad="false">
    <template #actions><button v-if="manage" type="button" class="btn small primary" @click="open(null)"><TgIcon name="plus" :size="13" /> 新建版本</button></template>
    <TgTable :cols="cols" :rows="data?.versions || []" row-key="version_id" :loading="loading" empty="暂无版本" empty-hint="新建版本后，需求录入时可选择所属版本">
      <template #cell-name="{ row }"><b>{{ row.name }}</b></template>
      <template #cell-code="{ row }"><span class="mono">{{ row.code || '—' }}</span></template>
      <template #cell-description="{ row }"><span class="muted">{{ row.description || '—' }}</span></template>
      <template #cell-start_date="{ row }">{{ row.start_date || '—' }}</template>
      <template #cell-planned_end="{ row }">{{ row.planned_end || '—' }}</template>
      <template #cell-actual_end="{ row }">{{ row.actual_end || '—' }}</template>
      <template #cell-status="{ row }"><TgBadge :status="row.status" :text="data?.statuses?.[row.status]" /></template>
      <template #cell-created="{ row }"><span class="muted">{{ row.created_by || '—' }} · {{ fmtAgo(row.created_at) }}</span></template>
      <template #cell-ops="{ row }">
        <span v-if="manage" class="row" style="gap:2px;flex-wrap:nowrap">
          <button type="button" class="btn ghost small" @click="open(row)">编辑</button>
          <button v-if="canDelete" type="button" class="btn ghost small" style="color:var(--red)" @click="remove(row)">删除</button>
        </span>
      </template>
    </TgTable>
  </TgPanel>
  <TgDialog v-model="dlg" :title="editing ? `编辑版本 · ${editing.name}` : '新建版本'" :ok="editing ? '保存' : '创建'" :busy="saving" @ok="save">
    <div class="form-grid">
      <TgField label="版本名称" required hint="同项目内唯一，≤ 60 字"><input v-model="form.name" class="field" maxlength="60" autofocus /></TgField>
      <TgField label="编码"><input v-model="form.code" class="field" placeholder="如 1.0" /></TgField>
      <TgField label="开始日期"><input v-model="form.start_date" type="date" class="field" /></TgField>
      <TgField label="计划结束"><input v-model="form.planned_end" type="date" class="field" /></TgField>
      <TgField label="实际结束"><input v-model="form.actual_end" type="date" class="field" /></TgField>
      <TgField label="状态"><select v-model="form.status" class="select"><option v-for="[k, l] in VERSION_STATUSES" :key="k" :value="k">{{ l }}</option></select></TgField>
    </div>
    <TgField label="描述"><textarea v-model="form.description" class="field" rows="2" placeholder="版本范围说明" /></TgField>
  </TgDialog>
</template>

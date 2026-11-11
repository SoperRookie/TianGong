<script setup>
/* 存量迁移报告与批量归属（3.20 / 3.21，仅系统管理员）：GET /admin/migration；单条 PUT /tasks/{id}/project；批量 POST /admin/migration/assign。 */
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useScopeStore } from '@/stores/scope'
import { useUiStore } from '@/stores/ui'
import { fmtTime, num } from '@/utils/format'
import { UNASSIGNED } from './useProjectActions'
import { PageHeading, TgPanel, TgTable, TgBadge, TgDialog, TgField, TgKv } from '@/components/ui'
const router = useRouter(), scope = useScopeStore(), ui = useUiStore()
const { data: r, loading, error, reload } = useLoad(() => api.get('/api/v1/admin/migration'))
const selected = ref(new Set()), dlg = ref(false), target = ref(''), busy = ref(false), single = ref('')
const candidates = computed(() => scope.projects.filter(p => p.project !== UNASSIGNED && !p.builtin && p.status !== 'archived').map(p => p.project))
const cols = [{ key: 'task_id', title: '任务 ID', w: '130px' }, { key: 'sources', title: '来源' }, { key: 'created_by', title: '创建人', w: '90px' }, { key: 'cases', title: '用例数', w: '70px' }, { key: 'created_at', title: '创建时间', w: '130px' }, { key: 'ops', title: '', w: '100px' }]
function openAssign(taskId) { single.value = taskId || ''; target.value = scope.project && candidates.value.includes(scope.project) ? scope.project : (candidates.value[0] || ''); dlg.value = true }
async function doAssign() {
  if (!target.value) return ui.toast('请选择目标项目', 'warn')
  busy.value = true
  try {
    if (single.value) {
      const x = await api.put(`/api/v1/tasks/${single.value}/project`, { project: target.value })
      ui.toast(`已归属「${target.value}」${x.requirement_migrated ? '，需求实体已建' : ''}${x.plans_moved ? '，迁移计划已改挂' : ''}`)
    } else {
      const x = await api.post('/api/v1/admin/migration/assign', { task_ids: [...selected.value], project: target.value })
      ui.toast(`已归属 ${x.assigned.length} 个到「${target.value}」${x.failed.length ? `，${x.failed.length} 个失败：${x.failed.map(f => f.reason).slice(0, 2).join('；')}` : ''}`, x.failed.length ? 'warn' : 'ok')
    }
    dlg.value = false; selected.value = new Set()
    await Promise.all([reload(true), scope.load(true)])
  } catch (e) { ui.error(e) } finally { busy.value = false }
}
</script>
<template>
  <div v-if="error" class="err-box mb">{{ error.message }}</div>
  <TgPanel title="存量迁移报告" hint="任务归属 / 需求实体化 / 执行轮次迁移的完成度；归属项目后需求与计划自动跟随">
    <template #actions><TgBadge v-if="r" :status="r.complete ? 'completed' : 'pending'" :text="r.complete ? '已完成' : '待处理'" /><button type="button" class="btn small" @click="reload()">刷新</button></template>
    <TgKv v-if="r" :pairs="[['任务', `${num(r.tasks_total)} 个：已归属 ${num(r.tasks_with_project)}，未归属 ${num(r.tasks_unassigned)}`], ['需求实体化', `已实体化 ${num(r.requirements_migrated)} 条，待实体化 ${r.tasks_without_requirement.length} 个任务`], ['执行轮次', `已迁入计划 ${num(r.plans_migrated)} 个，待迁 ${r.tasks_with_legacy_executions.length} 个任务`], ['未指定的计划', `${num(r.plans_unassigned)} 个`]]" />
    <div v-else-if="loading" class="muted">加载中…</div>
  </TgPanel>
  <TgPanel class="mt" title="未归属项目的任务" :hint="r ? `${r.unassigned.length} 个 · 勾选后批量归属，或逐条归属` : ''" :pad="false">
    <template #actions><button type="button" class="btn small primary" :disabled="!selected.size" @click="openAssign('')">批量归属所选（{{ selected.size }}）</button></template>
    <TgTable :cols="cols" :rows="r?.unassigned || []" row-key="task_id" :loading="loading" selectable v-model:selected="selected" clickable empty="没有未归属的任务" @row-click="t => router.push({ name: 'design-detail', params: { id: t.task_id } })">
      <template #cell-task_id="{ row }"><span class="mono">{{ row.task_id }}</span></template>
      <template #cell-sources="{ row }">{{ (row.sources || []).join('、') || '—' }}</template>
      <template #cell-created_at="{ row }">{{ fmtTime(row.created_at) }}</template>
      <template #cell-ops="{ row }"><button type="button" class="btn small ghost" @click.stop="openAssign(row.task_id)">归属项目</button></template>
    </TgTable>
  </TgPanel>
  <TgDialog v-model="dlg" :title="single ? `归属任务 ${single}` : `批量归属 ${selected.size} 个任务`" ok="归属" :busy="busy" @ok="doAssign">
    <TgField label="目标项目" required hint="只列未归档项目；已归属的任务不允许改挂">
      <select v-model="target" class="select"><option v-for="n in candidates" :key="n" :value="n">{{ n }}</option></select>
    </TgField>
  </TgDialog>
</template>

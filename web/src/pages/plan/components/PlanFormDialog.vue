<script setup>
/* 新建 / 编辑计划：名称 · 项目 · 负责人 · 周期（编辑时可改状态）；POST /plans 或 PUT /plans/{id} */
import { ref, computed, watch } from 'vue'
import { api, seg } from '@/api/client'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { TgDialog, TgField } from '@/components/ui'
const props = defineProps({ modelValue: Boolean, plan: { type: Object, default: null }, defaultProject: { type: String, default: '' } })
const emit = defineEmits(['update:modelValue', 'saved'])
const scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore()
const PLAN_STATUSES = [['not_started', '未开始'], ['in_progress', '进行中'], ['done', '已完成'], ['archived', '已归档']]
const form = ref({ name: '', project: '', owner: '', start_date: '', end_date: '', status: 'not_started' })
const saving = ref(false), members = ref([])
const projects = computed(() => scope.projects.filter(p => p.status !== 'archived' && auth.can(p.project, 'plan.manage')))
const isEdit = computed(() => !!props.plan)
watch(() => props.modelValue, v => {
  if (!v) return
  const p = props.plan
  form.value = p ? { name: p.name, project: p.project, owner: p.owner || '', start_date: p.start_date || '', end_date: p.end_date || '', status: p.status }
    : { name: '', project: props.defaultProject || scope.project || projects.value[0]?.project || '', owner: auth.me, start_date: '', end_date: '', status: 'not_started' }
})
watch(() => [props.modelValue, form.value.project], async ([open, project]) => {
  members.value = []
  if (!open || !project) return
  try { members.value = (await api.get(`/api/v1/projects/${seg(project)}/members`)).members || [] } catch { /* 无权限时不给候选 */ }
}, { immediate: true })
async function save() {
  const f = form.value
  if (!f.name.trim()) return ui.toast('请填写计划名称', 'warn')
  if (!f.project) return ui.toast('请选择项目', 'warn')
  saving.value = true
  try {
    let plan
    if (isEdit.value) plan = await api.put(`/api/v1/plans/${props.plan.plan_id}`, { name: f.name.trim(), owner: f.owner.trim(), start_date: f.start_date, end_date: f.end_date, status: f.status })
    else plan = await api.post('/api/v1/plans', { name: f.name.trim(), project: f.project, owner: f.owner.trim(), start_date: f.start_date, end_date: f.end_date })
    ui.toast(isEdit.value ? '计划已保存' : '计划已创建')
    emit('saved', plan); emit('update:modelValue', false)
  } catch (e) { ui.error(e) } finally { saving.value = false }
}
</script>
<template>
  <TgDialog :model-value="modelValue" :title="isEdit ? '编辑计划' : '新建计划'" :ok="isEdit ? '保存' : '创建'" :busy="saving" @update:modelValue="v => emit('update:modelValue', v)" @ok="save">
    <TgField label="计划名称" required><input v-model="form.name" class="field" placeholder="如：v2.2 回归测试" autofocus /></TgField>
    <TgField label="项目" required :hint="isEdit ? '项目创建后不可更改' : '只列出我有计划管理权限的项目'">
      <select v-model="form.project" class="select" :disabled="isEdit">
        <option v-if="isEdit" :value="form.project">{{ form.project }}</option>
        <option v-for="p in projects" :key="p.project" :value="p.project">{{ p.business_line ? p.business_line + ' / ' : '' }}{{ p.project }}</option>
      </select>
    </TgField>
    <TgField label="负责人" hint="默认本人；可从项目成员中选">
      <input v-model="form.owner" class="field" list="plan-owner-list" placeholder="用户名" />
      <datalist id="plan-owner-list"><option v-for="m in members" :key="m.username" :value="m.username">{{ m.name || m.username }} · {{ m.role_label }}</option></datalist>
    </TgField>
    <div class="form-grid">
      <TgField label="开始日期"><input v-model="form.start_date" type="date" class="field" /></TgField>
      <TgField label="结束日期"><input v-model="form.end_date" type="date" class="field" /></TgField>
    </div>
    <TgField v-if="isEdit" label="状态" hint="人工可任意切换；结束轮次时若全部执行过会自动置为已完成">
      <select v-model="form.status" class="select"><option v-for="[k, l] in PLAN_STATUSES" :key="k" :value="k">{{ l }}</option></select>
    </TgField>
  </TgDialog>
</template>

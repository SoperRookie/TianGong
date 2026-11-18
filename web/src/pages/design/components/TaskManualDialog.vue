<script setup>
/* 人工用例集（5.16）：不经 AI 的空任务容器 → POST /api/v1/projects/{name}/manual-task */
import { ref, computed, watch, onMounted } from 'vue'
import { api, seg } from '@/api/client'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { TgDialog, TgField, TgIcon } from '@/components/ui'
const props = defineProps({ modelValue: Boolean })
const emit = defineEmits(['update:modelValue', 'created'])
const scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore()
const project = ref(scope.project || ''), title = ref(''), templateId = ref(''), busy = ref(false), templates = ref([])
const projects = computed(() => scope.projects.filter(p => p.status !== 'archived' && auth.can(p.project, 'case.edit')))
onMounted(async () => { try { const t = await api.get('/api/v1/templates'); templates.value = t.templates || []; templateId.value = t.default_id || '' } catch { /* 非关键 */ } })
watch(() => props.modelValue, v => { if (v) { project.value = scope.project || projects.value[0]?.project || ''; title.value = '' } })
async function submit() {
  if (!project.value) return ui.toast('请选择项目', 'warn')
  busy.value = true
  try {
    const r = await api.post(`/api/v1/projects/${seg(project.value)}/manual-task`, { title: title.value.trim() || '人工用例集', template_id: templateId.value || null })
    emit('update:modelValue', false); emit('created', r)
  } catch (e) { ui.error(e) } finally { busy.value = false }
}
</script>
<template>
  <TgDialog :model-value="modelValue" title="新建人工用例集" ok="创建" :busy="busy" :ok-disabled="!project" @update:modelValue="$emit('update:modelValue', $event)" @ok="submit">
    <TgField label="项目" required><select v-model="project" class="select"><option v-for="p in projects" :key="p.project" :value="p.project">{{ p.project }}</option></select></TgField>
    <TgField label="名称"><input v-model="title" class="field" placeholder="例：人工用例集：11-02 需求" maxlength="80" @keydown.enter="submit" /></TgField>
    <TgField label="用例模板"><select v-model="templateId" class="select"><option v-for="t in templates" :key="t.template_id" :value="t.template_id">{{ t.name }}</option></select></TgField>
    <div class="information"><TgIcon name="info" :size="13" /> <span>不经 AI 的空任务容器，承接人工新增与 Excel 导入；用例以草稿进入评审流，同样走审核与锁定。</span></div>
  </TgDialog>
</template>

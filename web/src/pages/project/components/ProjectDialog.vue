<script setup>
/* 新建 / 编辑项目对话框：名称 · 编码 · 产品线（可就地新建）· 负责人 · 状态（编辑时）· 描述 · 自动沉淀开关。
   创建接口不收 knowledge_auto，未勾选时创建成功后再 PUT 一次；改名由后端联动全部引用。 */
import { ref, watch, computed } from 'vue'
import { api, seg } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useScopeStore } from '@/stores/scope'
import { useUiStore } from '@/stores/ui'
import { PROJECT_STATUSES } from '@/utils/consts'
import { TgDialog, TgField } from '@/components/ui'
const props = defineProps({ modelValue: Boolean, project: { type: Object, default: null }, presetLine: { type: String, default: '' } })
const emit = defineEmits(['update:modelValue', 'saved'])
const auth = useAuthStore(), scope = useScopeStore(), ui = useUiStore()
const NEW_LINE = '__new__'
const form = ref({}), lines = ref([]), users = ref([]), saving = ref(false), newLine = ref('')
const editing = computed(() => !!props.project)
async function loadOptions() {
  try { lines.value = (await api.get('/api/v1/business-lines')).business_lines || [] } catch { lines.value = [] }
  try { users.value = (await api.get('/api/v1/auth/users/lookup')).users || [] } catch { users.value = [] }
}
watch(() => props.modelValue, v => {
  if (!v) return
  const p = props.project
  form.value = {
    name: p?.project || '', code: p?.code || '', business_line: p?.business_line || props.presetLine || '',
    owner: p ? (p.owner || '') : auth.me, status: p?.status || 'active', description: p?.description || '',
    knowledge_auto: p ? p.knowledge_auto !== false : true,
  }
  newLine.value = ''
  loadOptions()
}, { immediate: true })
async function save() {
  const f = form.value
  const name = f.name.trim()
  if (!name) return ui.toast('项目名不能为空', 'warn')
  const line = f.business_line === NEW_LINE ? newLine.value.trim() : f.business_line
  if (f.business_line === NEW_LINE && !line) return ui.toast('请输入新产品线名称', 'warn')
  saving.value = true
  try {
    if (editing.value) {
      await api.put(`/api/v1/projects/${seg(props.project.project)}`, {
        name, description: f.description.trim(), code: f.code.trim(), owner: f.owner.trim(),
        business_line: line, status: f.status, knowledge_auto: !!f.knowledge_auto,
      })
      // 改名且正是当前工作范围：同步 localStorage 与 store
      if (scope.project === props.project.project && name !== props.project.project) scope.setProject(name)
      ui.toast(name !== props.project.project ? `已保存，项目改名为「${name}」并联动全部引用` : '项目已保存')
    } else {
      await api.post('/api/v1/projects', { name, description: f.description.trim(), code: f.code.trim(), owner: f.owner.trim(), business_line: line })
      if (!f.knowledge_auto) await api.put(`/api/v1/projects/${seg(name)}`, { knowledge_auto: false })
      ui.toast(`项目「${name}」已创建，你与负责人已成为项目管理员`)
    }
    await Promise.all([scope.load(true), auth.fetchMe()])
    emit('update:modelValue', false)
    emit('saved', name)
  } catch (e) { ui.error(e) } finally { saving.value = false }
}
</script>
<template>
  <TgDialog :model-value="modelValue" :title="editing ? `编辑项目 · ${project.project}` : '新建项目'" :ok="editing ? '保存' : '创建'" :busy="saving" @update:model-value="$emit('update:modelValue', $event)" @ok="save">
    <div class="form-grid">
      <TgField label="项目名称" required :hint="editing ? '改名会联动更新任务 / 计划 / 需求 / 知识空间等全部引用' : '名称为自然键，最长 100 字，不含尖括号、引号与反斜杠'"><input v-model="form.name" class="field" maxlength="100" placeholder="如：连赢活动10/25" autofocus /></TgField>
      <TgField label="项目编码" hint="全局唯一，≤ 50 字，可空"><input v-model="form.code" class="field" maxlength="50" placeholder="如：LY" /></TgField>
      <TgField label="产品线" hint="同产品线项目共享历史用例复用提示；按产品线模式下决定可见范围">
        <select v-model="form.business_line" class="select">
          <option value="">未归属产品线</option>
          <option v-for="l in lines" :key="l.name" :value="l.name">{{ l.name }}<template v-if="l.code"> · {{ l.code }}</template></option>
          <option v-if="form.business_line && !lines.some(l => l.name === form.business_line) && form.business_line !== NEW_LINE" :value="form.business_line">{{ form.business_line }}</option>
          <option :value="NEW_LINE">＋ 新建产品线…</option>
        </select>
        <input v-if="form.business_line === NEW_LINE" v-model="newLine" class="field" maxlength="100" placeholder="新产品线名称（保存时自动登记）" style="margin-top:6px" />
      </TgField>
      <TgField label="负责人" hint="须是已存在用户；自动成为项目管理员">
        <input v-model="form.owner" class="field" list="tg-owner-users" placeholder="用户名" />
        <datalist id="tg-owner-users"><option v-for="u in users" :key="u.username" :value="u.username">{{ u.name || u.username }}</option></datalist>
      </TgField>
      <TgField v-if="editing" label="状态" hint="归档后只读；归档项目在此保持「已归档」提交会被拒绝，请先改为进行中">
        <select v-model="form.status" class="select"><option v-for="[k, l] in PROJECT_STATUSES" :key="k" :value="k">{{ l }}</option></select>
      </TgField>
    </div>
    <TgField label="描述"><textarea v-model="form.description" class="field" rows="3" placeholder="项目范围、端、关键背景（会作为项目画像的一部分注入 AI）" /></TgField>
    <label class="row tiny" style="cursor:pointer"><input v-model="form.knowledge_auto" type="checkbox" /> 自动沉淀知识与项目画像草稿 <span class="muted">（需求原文 / 通过的测试点 / 通过的用例 / 终稿 / 已确认分析自动入项目知识库；关闭后画像只能手动提炼、回溯按钮禁用）</span></label>
  </TgDialog>
</template>

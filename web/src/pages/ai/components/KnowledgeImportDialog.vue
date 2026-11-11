<script setup>
/* 手工导入知识（9.2 / 9.3 / 9.4）：文档（文件或文本）/ 历史用例表 / 历史缺陷表；三层范围：公共（仅管理员）/ 项目 / 模块。 */
import { ref, reactive, computed, watch } from 'vue'
import { api, seg } from '@/api/client'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { TgDialog, TgField, TgSeg } from '@/components/ui'
const props = defineProps({ modelValue: Boolean, categories: { type: Object, default: () => ({}) } })
const emit = defineEmits(['update:modelValue', 'done'])
const scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore()

const kind = ref('doc')
const form = reactive({ level: 'project', project: '', module: '', category: 'requirement_docs', text: '', source: '' })
const files = ref([]), saving = ref(false), modules = ref([])
const projects = computed(() => scope.projects.filter(p => p.status !== 'archived' && auth.can(p.project, 'knowledge.manage')))
const KINDS = [['doc', '知识文档'], ['cases', '历史用例'], ['bugs', '历史缺陷']]
const ACCEPT = { doc: '.pdf,.docx,.doc,.md,.txt,.xlsx,.xls,.csv,.html,.zip', cases: '.xlsx,.xls,.csv,.xmind', bugs: '.xlsx,.xls,.csv' }

watch(() => props.modelValue, v => {
  if (!v) return
  Object.assign(form, { level: 'project', project: projects.value.some(p => p.project === scope.project) ? scope.project : (projects.value[0]?.project || ''), module: '', category: Object.keys(props.categories)[0] || 'requirement_docs', text: '', source: '' })
  files.value = []; kind.value = 'doc'
})
watch(() => form.project, async name => {
  modules.value = []
  if (!name) return
  try {
    const r = await api.get(`/api/v1/projects/${seg(name)}/modules`)
    const out = []
    const walk = (nodes, prefix) => (nodes || []).forEach(n => { const p = prefix ? `${prefix}/${n.name}` : n.name; out.push(p); walk(n.children, p) })
    walk(r.tree, '')
    modules.value = out
  } catch { /* 无模块树 */ }
}, { immediate: true })

function onFiles(e) { files.value = Array.from(e.target.files || []) }
async function submit() {
  if (form.level !== 'public' && !form.project) return ui.toast('请选择项目', 'warn')
  if (form.level === 'module' && !form.module) return ui.toast('模块层知识必须指定模块', 'warn')
  if (!files.value.length && !(kind.value === 'doc' && form.text.trim())) return ui.toast(kind.value === 'doc' ? '请上传文件或粘贴文本' : '请选择要导入的表格文件', 'warn')
  const fd = new FormData()
  fd.append('level', form.level); fd.append('project', form.level === 'public' ? '' : form.project); fd.append('module', form.level === 'module' ? form.module : '')
  if (kind.value === 'doc') { fd.append('category', form.category); fd.append('text', form.text); fd.append('source', form.source || 'text') }
  files.value.forEach(f => fd.append('files', f))
  saving.value = true
  try {
    const r = await api.upload({ doc: '/api/v1/knowledge/docs', cases: '/api/v1/knowledge/cases', bugs: '/api/v1/knowledge/bugs' }[kind.value], fd)
    ui.toast(`已入库 ${(r.ingested || []).length} 个文档`)
    emit('update:modelValue', false); emit('done', r)
  } catch (e) { ui.error(e) } finally { saving.value = false }
}
</script>
<template>
  <TgDialog :model-value="modelValue" title="导入知识" :ok="saving ? '切片向量化中…' : '入库'" :busy="saving" @update:model-value="emit('update:modelValue', $event)" @ok="submit">
    <TgField label="类型"><TgSeg v-model="kind" :items="KINDS" /><small class="muted">{{ kind === 'doc' ? '文件或粘贴文本 → 解析 → 600 字切片 → 向量化入库' : kind === 'cases' ? 'xlsx / csv / xmind / 禅道 xls 用例表 → 每条用例一个切片进测试用例库' : '禅道 / Jira / TAPD 缺陷导出表 → 每条缺陷一个切片进历史缺陷库，拆解与查漏阶段注入' }}</small></TgField>
    <div class="form-grid">
      <TgField label="层级" required>
        <select v-model="form.level" class="select"><option value="project">项目知识库</option><option value="module">模块知识库</option><option v-if="auth.isAdmin" value="public">公共知识库（跨项目，仅管理员）</option></select>
      </TgField>
      <TgField v-if="form.level !== 'public'" label="项目" required><select v-model="form.project" class="select"><option v-for="p in projects" :key="p.project" :value="p.project">{{ p.project }}</option></select></TgField>
      <TgField v-if="form.level === 'module'" label="模块" required hint="模块路径，如 登录/密码找回">
        <input v-model="form.module" class="field" list="kb-modules" placeholder="选择或输入模块路径" /><datalist id="kb-modules"><option v-for="m in modules" :key="m" :value="m" /></datalist>
      </TgField>
      <TgField v-if="kind === 'doc'" label="分类" required><select v-model="form.category" class="select"><option v-for="(v, k) in categories" :key="k" :value="k">{{ v }}</option></select></TgField>
    </div>
    <TgField :label="kind === 'doc' ? '文件（可多选）' : '表格文件（可多选）'"><input type="file" multiple :accept="ACCEPT[kind]" @change="onFiles" /><small v-if="files.length" class="muted">{{ files.map(f => f.name).join('、') }}</small></TgField>
    <template v-if="kind === 'doc'">
      <TgField label="或粘贴文本"><textarea v-model="form.text" class="field" rows="6" placeholder="直接粘贴需求 / 规则文本" /></TgField>
      <TgField label="来源名" hint="粘贴文本时的来源标识，默认 text"><input v-model="form.source" class="field" placeholder="如：V1.2 玩法规则" /></TgField>
    </template>
    <div class="information"><span>公共层知识跨项目可用、仅管理员维护；项目 / 模块层按项目可见范围过滤，需项目 knowledge.manage 权限。</span></div>
  </TgDialog>
</template>

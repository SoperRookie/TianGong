<script setup>
/* 新建测试设计任务（5.1）：上传需求文件 / 粘贴文本 → POST /api/v1/tasks（multipart）。记住上次的模型 / 模板 / 路径选择。 */
import { ref, computed, watch, onMounted } from 'vue'
import { api } from '@/api/client'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtSize } from '@/utils/format'
import { TgDialog, TgField, TgTabs, TgIcon } from '@/components/ui'
const props = defineProps({ modelValue: Boolean })
const emit = defineEmits(['update:modelValue', 'created'])
const scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore()
const LS = 'tg_task_new_prefs'
const prefs = JSON.parse(localStorage.getItem(LS) || '{}')

const src = ref('file')
const files = ref([])
const text = ref('')
const project = ref(scope.project || '')
const newProject = ref('')
const templateId = ref(prefs.template_id || '')
const model = ref(prefs.model || '')
const reviewer = ref(prefs.reviewer_model || '')
const confirmPoints = ref(prefs.confirm_points !== false)
const asyncMode = ref(prefs.async_mode !== false)
const knowledgeSpace = ref('')
const advanced = ref(false)
const busy = ref(false)
const dragging = ref(false)
const templates = ref([]), models = ref([])
const fileInput = ref(null)

const projects = computed(() => scope.projects.filter(p => p.status !== 'archived' && auth.can(p.project, 'point.ai')))
const canSubmit = computed(() => (files.value.length || text.value.trim()) && (project.value || auth.isAdmin) && !busy.value)
const totalSize = computed(() => files.value.reduce((s, f) => s + f.size, 0))

onMounted(async () => {
  try {
    const [t, m] = await Promise.all([api.get('/api/v1/templates'), api.get('/api/v1/models')])
    templates.value = t.templates || []
    if (!templateId.value || !templates.value.some(x => x.template_id === templateId.value)) templateId.value = t.default_id || templates.value[0]?.template_id || ''
    models.value = m.models || []
    if (model.value && !models.value.some(x => x.name === model.value)) model.value = ''
    if (reviewer.value && !models.value.some(x => x.name === reviewer.value)) reviewer.value = ''
  } catch (e) { ui.error(e) }
})
watch(() => props.modelValue, v => { if (v) { project.value = scope.project || project.value; busy.value = false } })
watch(() => scope.project, v => { if (v) project.value = v })

function addFiles(list) {
  const arr = Array.from(list || [])
  const names = new Set(files.value.map(f => f.name + f.size))
  files.value = files.value.concat(arr.filter(f => !names.has(f.name + f.size)))
}
function onDrop(e) { dragging.value = false; addFiles(e.dataTransfer.files) }
function removeFile(i) { files.value = files.value.filter((_, j) => j !== i) }

async function submit() {
  if (!canSubmit.value) return
  const proj = project.value === '__new__' ? newProject.value.trim() : project.value
  if (project.value === '__new__' && !proj) return ui.toast('请输入新项目名', 'warn')
  const fd = new FormData()
  for (const f of files.value) fd.append('files', f)
  fd.append('text', text.value)
  if (model.value) fd.append('model', model.value)
  if (reviewer.value) fd.append('reviewer_model', reviewer.value)
  if (proj) fd.append('project', proj)
  if (knowledgeSpace.value.trim()) fd.append('knowledge_space', knowledgeSpace.value.trim())
  fd.append('template_id', templateId.value)
  fd.append('confirm_points', confirmPoints.value ? 'true' : 'false')
  fd.append('async_mode', asyncMode.value ? 'true' : 'false')
  busy.value = true
  try {
    const r = await api.upload('/api/v1/tasks', fd)
    localStorage.setItem(LS, JSON.stringify({ template_id: templateId.value, model: model.value, reviewer_model: reviewer.value, confirm_points: confirmPoints.value, async_mode: asyncMode.value }))
    files.value = []; text.value = ''
    if (project.value === '__new__') scope.load(true)
    emit('update:modelValue', false)
    emit('created', r)
  } catch (e) { ui.error(e) } finally { busy.value = false }
}
</script>
<template>
  <TgDialog :model-value="modelValue" title="新建测试设计任务" :ok="asyncMode ? '创建任务' : '创建并等待完成'" :busy="busy" :ok-disabled="!canSubmit" wide @update:modelValue="$emit('update:modelValue', $event)" @ok="submit">
    <TgTabs v-model="src" :items="[{ key: 'file', label: '上传文件', count: files.length || undefined }, { key: 'text', label: '粘贴文本' }]" />
    <div v-show="src === 'file'">
      <div class="drop" :class="{ on: dragging }" style="padding:22px;cursor:pointer" @click="fileInput.click()" @dragover.prevent="dragging = true" @dragleave="dragging = false" @drop.prevent="onDrop">
        <TgIcon name="upload" :size="16" /> 拖放或点击选择需求文件：Office / WPS 全系、PDF、图片、文本、网页、压缩包（Axure 原型）、邮件 · 可多选、分批追加
        <input ref="fileInput" type="file" multiple class="hidden" @change="addFiles($event.target.files); $event.target.value = ''" />
      </div>
      <div v-if="files.length" class="list" style="margin-top:8px">
        <div v-for="(f, i) in files" :key="f.name + f.size" class="row"><TgIcon name="file" :size="14" /><span style="flex:1;min-width:0" class="ellipsis">{{ f.name }}</span><span class="tiny muted">{{ fmtSize(f.size) }}</span><button type="button" class="btn small ghost" @click="removeFile(i)">移除</button></div>
        <div class="tiny muted">共 {{ files.length }} 个 · {{ fmtSize(totalSize) }} · 可再切到「粘贴文本」补充说明（混合提交）</div>
      </div>
    </div>
    <div v-show="src === 'text'">
      <textarea v-model="text" class="field" rows="8" style="width:100%" placeholder="粘贴需求内容；可与文件混合提交"></textarea>
    </div>
    <div class="form-grid" style="margin-top:12px">
      <TgField label="所属项目" hint="同项目共享项目记忆、规则与知识空间" required>
        <select v-model="project" class="select">
          <option v-if="auth.isAdmin" value="">（未指定）</option>
          <option v-for="p in projects" :key="p.project" :value="p.project">{{ p.project }}</option>
          <option v-if="auth.isAdmin" value="__new__">＋ 新项目…</option>
        </select>
        <input v-if="project === '__new__'" v-model="newProject" class="field" placeholder="新项目名" style="margin-top:6px" />
      </TgField>
      <TgField label="用例模板" hint="决定生成字段规范、规则校验与导出列">
        <select v-model="templateId" class="select"><option v-for="t in templates" :key="t.template_id" :value="t.template_id">{{ t.name }}{{ t.is_default ? '（默认）' : '' }}</option></select>
      </TgField>
      <TgField label="生成模型"><select v-model="model" class="select"><option value="">默认模型</option><option v-for="m in models" :key="m.name" :value="m.name">{{ m.name }}{{ m.is_default ? '（默认）' : '' }}</option></select></TgField>
      <TgField label="评审模型"><select v-model="reviewer" class="select"><option value="">同生成模型</option><option v-for="m in models" :key="m.name" :value="m.name">{{ m.name }}</option></select></TgField>
    </div>
    <div class="row" style="margin-top:4px">
      <label class="row small"><input v-model="confirmPoints" type="checkbox" /> 先审核测试点再生成用例（推荐）</label>
      <label class="row small"><input v-model="asyncMode" type="checkbox" /> 后台执行</label>
      <a class="link small right" @click="advanced = !advanced">高级设置 {{ advanced ? '▴' : '▾' }}</a>
    </div>
    <div v-if="advanced" style="margin-top:10px">
      <TgField label="知识空间" hint="缺省为所属项目（检索范围 = 本项目 + 公共层）；只能填自己有权限的项目或 public"><input v-model="knowledgeSpace" class="field" placeholder="缺省同项目" /></TgField>
    </div>
    <div class="information" style="margin-top:10px"><TgIcon name="info" :size="13" /> <span>勾选「先审核测试点」：拆解、经验延伸与查漏 / 查重在后台执行，列表可随时查看进度、可取消；拆解完成后进入人工审核，确认后才按模块并行生成用例。由需求实体发起请到「需求 › 需求详情 › 发起测试设计」。</span></div>
  </TgDialog>
</template>
<style scoped>
.drop.on { background: #eef3ff; border-color: #6b93ff; }
</style>

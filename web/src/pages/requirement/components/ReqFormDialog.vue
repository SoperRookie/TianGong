<script setup>
/*
 录入需求 / 编辑需求对话框。
 新建：POST /api/v1/requirements（multipart：project/title/text/description/version_id/module_id/urls/files），
       提交前有文件时先 GET /requirements/same-files 查重，命中则问「作为新版本替换」还是「仍新建」。
 编辑：PUT /api/v1/requirements/{id}（title / description / version_id / module_id）。
*/
import { ref, computed, watch } from 'vue'
import { api, seg } from '@/api/client'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtTime } from '@/utils/format'
import { TgDialog, TgField, TgBadge } from '@/components/ui'
const props = defineProps({ modelValue: Boolean, req: { type: Object, default: null } })
const emit = defineEmits(['update:modelValue', 'created', 'saved', 'replaced'])
const scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore()
const editing = computed(() => !!props.req)
const form = ref({ project: '', title: '', text: '', description: '', version_id: '', module_id: '', urls: '' })
const files = ref([]), fileInput = ref(null)
const versions = ref([]), modules = ref([])
const busy = ref(false)
const dup = ref(null)   // 同名附件查重结果 {matches}
const projects = computed(() => scope.projects.filter(p => p.status !== 'archived' && auth.can(p.project, 'requirement.edit')))

async function loadRefs(project) {
  versions.value = []; modules.value = []
  if (!project) return
  try {
    const [v, m] = await Promise.all([api.get(`/api/v1/projects/${seg(project)}/versions`), api.get(`/api/v1/projects/${seg(project)}/modules`)])
    versions.value = v.versions || []
    const out = []
    const walk = (nodes, depth, parentPath) => (nodes || []).forEach(n => { const path = parentPath ? `${parentPath}/${n.name}` : n.name; out.push({ ...n, depth, path }); walk(n.children, depth + 1, path) })
    walk(m.tree, 0, '')
    modules.value = out
  } catch { /* 无权限时留空 */ }
}
watch(() => props.modelValue, v => {
  if (!v) return
  dup.value = null; busy.value = false; files.value = []
  if (props.req) {
    form.value = { project: props.req.project, title: props.req.title || '', text: '', description: props.req.description || '', version_id: props.req.version_id || '', module_id: props.req.module_id || '', urls: '' }
  } else {
    const p = projects.value.some(x => x.project === scope.project) ? scope.project : (projects.value[0]?.project || '')
    const mod = scope.module ? (scope.flatModules.find(m => m.name === scope.module || m.path === scope.module)?.module_id || '') : ''
    form.value = { project: p, title: '', text: '', description: '', version_id: '', module_id: mod, urls: '' }
  }
  loadRefs(form.value.project)
}, { immediate: true })
// 对话框打开时项目清单可能还没加载完（刷新页面带 ?new=1）：清单到了再补默认项目
watch(projects, list => { if (props.modelValue && !editing.value && !form.value.project && list.length) form.value.project = list.some(x => x.project === scope.project) ? scope.project : list[0].project })
watch(() => form.value.project, p => { if (props.modelValue) { form.value.version_id = ''; if (!editing.value) form.value.module_id = ''; loadRefs(p) } })
function pickFiles(e) { files.value = [...files.value, ...Array.from(e.target.files || [])]; e.target.value = '' }
function dropFiles(e) { files.value = [...files.value, ...Array.from(e.dataTransfer?.files || [])] }
function removeFile(i) { files.value.splice(i, 1) }

async function submit(force = false) {
  const f = form.value
  if (!f.title.trim()) return ui.toast('请填写需求标题', { kind: 'warn' })
  if (editing.value) {
    busy.value = true
    try {
      const r = await api.put(`/api/v1/requirements/${props.req.req_id}`, { title: f.title.trim(), description: f.description, version_id: f.version_id || '', module_id: f.module_id || '' })
      ui.toast('已保存'); emit('update:modelValue', false); emit('saved', r)
    } catch (e) { ui.error(e) } finally { busy.value = false }
    return
  }
  if (!f.project) return ui.toast('请选择项目', { kind: 'warn' })
  if (!f.text.trim() && !files.value.length && !f.urls.trim()) return ui.toast('请粘贴需求原文、上传文件或填写链接', { kind: 'warn' })
  busy.value = true
  try {
    if (!force && files.value.length) {   // 同项目已有同名附件的需求：先问是新版本还是重复导入
      const q = new URLSearchParams({ project: f.project }); files.value.forEach(x => q.append('filename', x.name))
      const m = (await api.get('/api/v1/requirements/same-files?' + q.toString())).matches || []
      if (m.length) { dup.value = { matches: m }; busy.value = false; return }
    }
    const fd = new FormData()
    fd.append('project', f.project); fd.append('title', f.title.trim()); fd.append('text', f.text); fd.append('description', f.description)
    fd.append('version_id', f.version_id || ''); fd.append('module_id', f.module_id || ''); fd.append('urls', f.urls)
    files.value.forEach(x => fd.append('files', x))
    const r = await api.upload('/api/v1/requirements', fd)
    ui.toast(r.parse_failed ? `需求已创建，${r.parse_failed} 个文件不合法，请在详情中查看原因` : (r.parsing ? `需求已创建，${r.parsing} 个附件 / 链接正在后台抓取解析` : '需求已创建'), { kind: r.parse_failed ? 'warn' : 'ok' })
    emit('update:modelValue', false); emit('created', r)
  } catch (e) { ui.error(e) } finally { busy.value = false }
}
/** 作为新版本替换到已有需求：删同名旧附件 → 追加新文件 → 补充说明追加到 description */
async function replaceInto(target) {
  busy.value = true
  const names = new Set(target.filenames)
  try {
    const r = await api.get(`/api/v1/requirements/${target.req_id}`)
    for (const a of r.attachments || []) if (names.has(a.filename)) await api.del(`/api/v1/requirements/${target.req_id}/attachments/${a.att_id}`)
    const fd = new FormData(); files.value.forEach(x => fd.append('files', x))
    if (form.value.urls.trim()) fd.append('urls', form.value.urls)
    await api.upload(`/api/v1/requirements/${target.req_id}/attachments`, fd)
    const desc = form.value.description.trim()
    if (desc) await api.put(`/api/v1/requirements/${target.req_id}`, { description: ((r.description || '').trim() + '\n\n' + desc).trim() })
    ui.toast(`已替换「${r.title}」的 ${names.size} 个附件，正在后台解析；解析完会自动重提整理稿`)
    dup.value = null; emit('update:modelValue', false); emit('replaced', r)
  } catch (e) { ui.error(e) } finally { busy.value = false }
}
</script>
<template>
  <TgDialog :model-value="modelValue" @update:model-value="v => emit('update:modelValue', v)" :title="editing ? '编辑需求' : '录入需求'" :ok="editing ? '保存' : '创建需求'" :busy="busy" wide @ok="submit(false)">
    <div v-if="!editing && !projects.length" class="attention" style="margin-bottom:10px">没有可录入需求的项目（需要测试人员及以上项目角色）</div>
    <div class="form-grid">
      <TgField label="项目" required>
        <select v-if="!editing" v-model="form.project" class="select"><option v-for="p in projects" :key="p.project" :value="p.project">{{ p.project }}</option></select>
        <input v-else class="field" :value="form.project" disabled />
      </TgField>
      <TgField label="需求标题" required><input v-model="form.title" class="field" placeholder="如：连赢活动前后端需求 10/25" /></TgField>
      <TgField label="版本" hint="可选"><select v-model="form.version_id" class="select"><option value="">—</option><option v-for="v in versions" :key="v.version_id" :value="v.version_id">{{ v.name }}</option></select></TgField>
      <TgField label="模块" hint="可选"><select v-model="form.module_id" class="select"><option value="">—</option><option v-for="m in modules" :key="m.module_id" :value="m.module_id">{{ '　'.repeat(m.depth) }}{{ m.path }}</option></select></TgField>
    </div>
    <template v-if="!editing">
      <TgField label="需求原文" hint="粘贴文本；原文里的 http(s) 链接会一并抓取。原文永久保留，AI 结果不覆盖"><textarea v-model="form.text" class="field" rows="7" placeholder="粘贴需求文档正文…" /></TgField>
      <TgField label="附件" hint="文本 / Word / PDF / PPT / 表格 / 网页 / 压缩包（Axure 原型包）/ 邮件 / 图片，多选；单文件上限 100MB，上传后后台解析">
        <div class="drop" @dragover.prevent @drop.prevent="dropFiles" @click="fileInput?.click()">拖拽文件到这里，或点击选择 <input ref="fileInput" type="file" multiple style="display:none" @change="pickFiles" /></div>
        <div v-if="files.length" class="inline-list" style="margin-top:6px"><span v-for="(f, i) in files" :key="i" class="tag">{{ f.name }} <a class="link" style="margin-left:4px" @click.stop="removeFile(i)">×</a></span></div>
      </TgField>
      <TgField label="链接" hint="每行一个，最多 10 个；网页 / 图片 / PDF / Word 直链"><textarea v-model="form.urls" class="field" rows="2" placeholder="https://…" /></TgField>
    </template>
    <TgField label="人工补充" hint="独立于原文保存，参与 AI 分析与测试设计"><textarea v-model="form.description" class="field" rows="3" placeholder="背景、口径、补充说明…" /></TgField>
  </TgDialog>
  <!-- 同名附件查重 -->
  <TgDialog :model-value="!!dup" @update:model-value="v => { if (!v) dup = null }" title="同项目已有同名附件的需求" no-foot wide>
    <div class="information" style="margin-bottom:10px">同一份文档再上传，多半是<b>新版本</b>或<b>重复导入</b>。替换到已有需求：旧文件删掉、新文件重新解析并提取，底稿核对里人工收录 / 排除过的句子按原句自动沿用，已确认的待确认事项重新分析后也保留；新建一条则是白板（任务、追溯链不在一起）。</div>
    <table class="table">
      <thead><tr><th>需求</th><th>状态</th><th>同名文件</th><th>创建</th><th></th></tr></thead>
      <tbody>
        <tr v-for="m in dup?.matches || []" :key="m.req_id">
          <td><b>{{ m.title }}</b><div class="sub">{{ m.task_count ? `${m.task_count} 个任务 · ` : '' }}{{ m.question_count ? `${m.question_count} 条待确认事项 · ` : '' }}{{ m.has_extraction ? '有整理稿' : '无整理稿' }}</div></td>
          <td><TgBadge :status="m.status" :text="m.status_label" /></td>
          <td class="tiny muted">{{ m.filenames.join('、') }}</td>
          <td class="tiny muted">{{ m.created_by }} {{ fmtTime(m.created_at) }}</td>
          <td><button type="button" class="btn small primary" :disabled="busy" @click="replaceInto(m)">作为新版本替换</button></td>
        </tr>
      </tbody>
    </table>
    <div class="row mt" style="justify-content:flex-end">
      <button type="button" class="btn" @click="dup = null">取消</button>
      <button type="button" class="btn" :disabled="busy" @click="dup = null; submit(true)">仍新建一条需求</button>
    </div>
  </TgDialog>
</template>

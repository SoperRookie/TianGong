<script setup>
/* 用例模板（5.3）：内置默认 + 上传 xlsx / docx 自动识别列映射 → 确认调整 → 设默认 / 删除；用例导入模板下载。 */
import { ref, computed, reactive, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { CASE_FIELD_LABELS } from '@/utils/consts'
import { PageHeading, TgPanel, TgBadge, TgDialog, TgField, TgEmpty, TgIcon, TgDropdown } from '@/components/ui'

const route = useRoute(), router = useRouter(), auth = useAuthStore(), ui = useUiStore()
const { data, loading, error, reload } = useLoad(() => api.get('/api/v1/templates'))
const list = computed(() => data.value?.templates || [])
const selId = ref(route.query.id || '')
watch(selId, v => router.replace({ query: { id: v || undefined } }))
watch(data, d => { if (d && !list.value.some(t => t.template_id === selId.value)) selId.value = d.default_id || list.value[0]?.template_id || '' })
const tpl = ref(null), tplLoading = ref(false)
watch(selId, async id => { tpl.value = null; if (!id) return; tplLoading.value = true; try { tpl.value = await api.get(`/api/v1/templates/${id}`) } catch (e) { ui.error(e) } finally { tplLoading.value = false } }, { immediate: true })

const FIELDS = [['case_id', '用例编号'], ['module', '所属模块'], ['title', '标题'], ['priority', '优先级'], ['precondition', '前置条件'], ['steps', '测试步骤'], ['expected', '预期结果'], ['keywords', '关键词'], ['remark', '备注'], ['custom', '自定义字段']]
const fieldLabel = k => (FIELDS.find(f => f[0] === k) || [k, k])[1]

/* 上传 → 识别草稿 → 映射确认 */
const upDlg = ref(false), upFile = ref(null), upName = ref(''), uploading = ref(false)
async function upload() {
  if (!upFile.value) return ui.toast('请选择模板文件', 'warn')
  const fd = new FormData(); fd.append('file', upFile.value); if (upName.value.trim()) fd.append('name', upName.value.trim())
  uploading.value = true
  try { const draft = await api.upload('/api/v1/templates', fd); upDlg.value = false; ui.toast('已识别字段结构，请确认映射'); await reload(true); selId.value = draft.template_id; openMap(draft) } catch (e) { ui.error(e) } finally { uploading.value = false }
}
const mapDlg = ref(false), mapForm = reactive({ template_id: '', name: '', columns: [] }), mapSaving = ref(false)
function openMap(t) { Object.assign(mapForm, { template_id: t.template_id, name: t.name, columns: (t.columns || []).map(c => ({ ...c, enum_text: (c.enum_values || []).join(', ') })) }); mapDlg.value = true }
function addCol() { mapForm.columns.push({ name: '', maps_to: 'custom', required: false, enum_text: '', description: '' }) }
async function saveMap() {
  if (!mapForm.name.trim()) return ui.toast('模板名称必填', 'warn')
  const columns = mapForm.columns.filter(c => c.name.trim()).map(c => ({ name: c.name.trim(), maps_to: c.maps_to, required: !!c.required, enum_values: c.enum_text.trim() ? c.enum_text.split(/[,，、\s]+/).filter(Boolean) : null, description: c.description || '' }))
  if (!columns.length) return ui.toast('至少保留一列', 'warn')
  mapSaving.value = true
  try { tpl.value = await api.put(`/api/v1/templates/${mapForm.template_id}`, { template_id: mapForm.template_id, name: mapForm.name.trim(), columns }); ui.toast('字段映射已保存'); mapDlg.value = false; reload(true) } catch (e) { ui.error(e) } finally { mapSaving.value = false }
}
async function setDefault(t) { try { await api.post(`/api/v1/templates/${t.template_id}/default`, {}); ui.toast(`已设为默认：${t.name}`); reload(true) } catch (e) { ui.error(e) } }
async function remove(t) {
  if (!await ui.confirm(`删除模板「${t.name}」？`, '已用该模板的任务保留其字段规范；内置默认模板不可删。', { ok: '删除', danger: true })) return
  try { await api.del(`/api/v1/templates/${t.template_id}`); ui.toast('已删除'); if (selId.value === t.template_id) selId.value = ''; reload(true) } catch (e) { ui.error(e) }
}
async function downloadImport() { try { await api.download('/api/v1/cases/import-template', '用例导入模板.xlsx'); ui.toast('已下载') } catch (e) { ui.error(e) } }
</script>
<template>
  <PageHeading title="用例模板" sub="内置默认模板 + 上传 xlsx / docx 自动识别列映射；模板决定生成 Prompt 字段规范、规则校验与导出列。内部字段固定，模板列通过 maps_to 映射；映射不到的列作为自定义字段进 extras。">
    <template #actions>
      <button v-if="auth.isAdmin" type="button" class="btn primary" @click="upFile = null; upName = ''; upDlg = true"><TgIcon name="upload" :size="14" /> 上传模板</button>
      <TgDropdown :items="[{ label: '下载用例导入模板（xlsx）', action: downloadImport, hint: '按默认模板列头' }]" />
    </template>
  </PageHeading>
  <div v-if="error" class="err-box">{{ error.message }}</div>
  <div class="split">
    <TgPanel :pad="false">
      <div v-if="tplLoading" class="page-loading"><span class="spinner" /> 加载中…</div>
      <template v-else-if="tpl">
        <div class="panel-pad" style="padding-bottom:0">
          <div class="panel-head"><div><div class="panel-title">{{ tpl.name }} <TgBadge v-if="tpl.template_id === data?.default_id" color="blue" text="默认" /></div><div class="panel-hint">template_id <span class="mono">{{ tpl.template_id }}</span> · {{ tpl.columns.length }} 列</div></div>
            <div v-if="auth.isAdmin" class="row"><button type="button" class="btn small" :disabled="tpl.template_id === data?.default_id" @click="setDefault(tpl)">设为默认</button><button type="button" class="btn small" @click="openMap(tpl)">编辑映射</button><button type="button" class="btn small danger" :disabled="tpl.template_id === 'builtin-default'" @click="remove(tpl)">删除</button></div>
          </div>
        </div>
        <div class="table-wrap"><table class="table">
          <thead><tr><th>列名</th><th>映射字段</th><th style="width:60px">必填</th><th>枚举</th><th>说明</th></tr></thead>
          <tbody><tr v-for="(c, i) in tpl.columns" :key="i"><td><b>{{ c.name }}</b></td><td><span class="mono">{{ c.maps_to }}</span> <span class="tiny muted">{{ fieldLabel(c.maps_to) }}</span></td><td><span v-if="c.required" class="ok">✓</span><span v-else class="no">—</span></td><td><span v-for="e in c.enum_values || []" :key="e" class="tag" style="margin-right:4px">{{ e }}</span><span v-if="!(c.enum_values || []).length" class="muted">—</span></td><td class="small muted">{{ c.description || '' }}</td></tr></tbody>
        </table></div>
      </template>
      <TgEmpty v-else :title="loading ? '加载中…' : '选择一个模板'" />
    </TgPanel>
    <div>
      <TgPanel title="模板列表" :hint="`${list.length} 个`">
        <div class="list">
          <div v-for="t in list" :key="t.template_id" class="row clickable" :class="{ cur: t.template_id === selId }" @click="selId = t.template_id"><b>{{ t.name }}</b><TgBadge v-if="t.template_id === data?.default_id" color="blue" text="默认" /><span class="tiny muted right">{{ t.columns }} 列</span></div>
        </div>
      </TgPanel>
      <TgPanel class="mt" title="模板如何起作用">
        <div class="list">
          <div v-for="[a, b] in [['生成', '字段规范写进 generator Prompt；优先级枚举 P0–P3（模板枚举为其子集时按模板）'], ['评审', '规则校验：字段完整、步骤预期一一对应、结构一致'], ['导出', 'xlsx / csv 按模板列输出；XMind 结构固定'], ['导入', 'Excel / CSV / XMind / 禅道导出按列映射解析，逐行校验后确认导入为草稿']]" :key="a"><b>{{ a }}</b><div class="tiny muted">{{ b }}</div></div>
        </div>
      </TgPanel>
    </div>
  </div>

  <TgDialog v-model="upDlg" title="上传模板" :ok="uploading ? '识别中…' : '上传并识别'" :busy="uploading" @ok="upload">
    <TgField label="模板文件" required hint="xlsx 取表头行；docx 取表格首行；识别后进入字段映射确认"><input type="file" accept=".xlsx,.xls,.docx,.csv" @change="upFile = $event.target.files?.[0] || null" /></TgField>
    <TgField label="模板名称" hint="留空取文件名"><input v-model="upName" class="field" /></TgField>
  </TgDialog>
  <TgDialog v-model="mapDlg" :title="`字段映射 · ${mapForm.name || mapForm.template_id}`" ok="保存映射" :busy="mapSaving" wide @ok="saveMap">
    <TgField label="模板名称" required><input v-model="mapForm.name" class="field" /></TgField>
    <div class="table-wrap"><table class="table dense">
      <thead><tr><th>列名</th><th style="width:150px">映射字段</th><th style="width:50px">必填</th><th>枚举（逗号分隔）</th><th>说明（注入 Prompt）</th><th style="width:40px"></th></tr></thead>
      <tbody>
        <tr v-for="(c, i) in mapForm.columns" :key="i">
          <td><input v-model="c.name" class="field small" style="width:100%" /></td>
          <td><select v-model="c.maps_to" class="select small" style="width:100%"><option v-for="[k, l] in FIELDS" :key="k" :value="k">{{ l }}（{{ k }}）</option></select></td>
          <td style="text-align:center"><input v-model="c.required" type="checkbox" /></td>
          <td><input v-model="c.enum_text" class="field small" style="width:100%" placeholder="如 P0, P1, P2, P3" /></td>
          <td><input v-model="c.description" class="field small" style="width:100%" /></td>
          <td><button type="button" class="btn small ghost" @click="mapForm.columns.splice(i, 1)">×</button></td>
        </tr>
      </tbody>
    </table></div>
    <div class="row" style="margin-top:8px"><button type="button" class="btn small" @click="addCol"><TgIcon name="plus" :size="12" /> 加一列</button><span class="tiny muted">规范字段：{{ CASE_FIELD_LABELS.map(([k, l]) => l).join(' / ') }}；同一规范字段映射多列时以第一列为准</span></div>
  </TgDialog>
</template>
<style scoped>
.list .row.cur { background: #eef3ff; margin: 0 -8px; padding-left: 8px; padding-right: 8px; border-radius: 3px; }
</style>

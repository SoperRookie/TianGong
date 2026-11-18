<script setup>
/* 表格导入五步：选文件（Excel / CSV / XMind / 禅道导出，可多选）→ 上传解析 → 预览逐行校验 → 确认导入（有错误行须显式只导有效行）。 */
import { ref, watch, computed } from 'vue'
import { api } from '@/api/client'
import { useUiStore } from '@/stores/ui'
import { TgDialog, TgEmpty } from '@/components/ui'
const props = defineProps({ modelValue: Boolean, tid: { type: String, required: true } })
const emit = defineEmits(['update:modelValue', 'imported'])
const ui = useUiStore()
const files = ref([]), preview = ref(null), busy = ref(false), onlyValid = ref(false), err = ref('')
watch(() => props.modelValue, v => { if (v) { files.value = []; preview.value = null; onlyValid.value = false; err.value = '' } })
const hasErrors = computed(() => (preview.value?.errors || 0) > 0)
async function doPreview() {
  if (!files.value.length) return
  busy.value = true; err.value = ''
  try { const fd = new FormData(); files.value.forEach(f => fd.append('files', f)); preview.value = await api.upload(`/api/v1/tasks/${props.tid}/cases/import/preview`, fd); onlyValid.value = false } catch (e) { err.value = e.message } finally { busy.value = false }
}
async function confirm() {
  if (!preview.value) return doPreview()
  if (hasErrors.value && !onlyValid.value) { err.value = `${preview.value.errors} 行存在错误，请修正后重新上传，或勾选「只导入无错误行」`; return }
  busy.value = true; err.value = ''
  try { const r = await api.post(`/api/v1/tasks/${props.tid}/cases/import/confirm`, { token: preview.value.token, only_valid: onlyValid.value }); emit('imported', r); emit('update:modelValue', false) } catch (e) { err.value = e.message } finally { busy.value = false }
}
const template = () => api.download('/api/v1/cases/import-template', '用例导入模板.xlsx').catch(ui.error)
</script>
<template>
  <TgDialog :model-value="modelValue" title="表格导入用例" :ok="preview ? (hasErrors ? `只导入无错误的 ${preview.total - preview.errors} 行` : `导入 ${preview?.total || 0} 行`) : '上传并预览'" wide :busy="busy" :ok-disabled="!files.length || (preview && hasErrors && !onlyValid)" @update:modelValue="v => emit('update:modelValue', v)" @ok="confirm">
    <div class="row">
      <input type="file" multiple accept=".xlsx,.csv,.xmind,.xls,.html,.htm" @change="e => { files = [...e.target.files]; preview = null }" />
      <span class="link tiny" @click="template">下载导入模板</span>
      <span class="tiny muted">Excel / CSV / XMind / 禅道导出；导入的用例为草稿，提交评审后进入待审核</span>
    </div>
    <div v-if="err" class="err-box mt">{{ err }}</div>
    <template v-if="preview">
      <div class="row mt small"><b>{{ preview.filename }}</b> · 共 {{ preview.total }} 行 · 错误 {{ preview.errors }} · 警告 {{ preview.warnings }}<label v-if="hasErrors" class="row tiny"><input type="checkbox" v-model="onlyValid" /> 只导入无错误行</label></div>
      <div class="table-wrap im-wrap">
        <table class="table dense">
          <thead><tr><th style="width:50px">行</th><th>标题</th><th style="width:140px">模块</th><th style="width:60px">优先级</th><th style="width:50px">步骤</th><th>校验</th></tr></thead>
          <tbody>
            <tr v-for="r in preview.rows" :key="r.file + r.row" :class="{ 'im-bad': r.errors.length }"><td>{{ r.row }}</td><td>{{ r.case.title }}</td><td>{{ r.case.module }}</td><td>{{ r.case.priority }}</td><td>{{ (r.case.steps || []).length }}</td><td><span v-for="e in r.errors" :key="e" class="badge red im-b">{{ e }}</span><span v-for="w in r.warnings" :key="w" class="badge orange im-b">{{ w }}</span><span v-if="!r.errors.length && !r.warnings.length" class="badge green">通过</span></td></tr>
          </tbody>
        </table>
        <TgEmpty v-if="!preview.rows.length" title="文件里没有解析到用例" />
      </div>
    </template>
  </TgDialog>
</template>
<style scoped>
.im-wrap { max-height: 50vh; overflow: auto; margin-top: 8px; }
.im-bad td { background: #fdecee; }
.im-b { margin: 0 4px 2px 0; }
</style>

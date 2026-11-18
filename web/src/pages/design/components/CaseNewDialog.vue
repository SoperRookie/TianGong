<script setup>
/* 人工新增用例（草稿）：模块 / 标题 / 优先级 / 前置 / 步骤 / 关键词 / 备注 / 来源测试点。 */
import { ref, watch } from 'vue'
import { PRIORITIES } from '@/utils/status'
import { TgDialog, TgField } from '@/components/ui'
const props = defineProps({ modelValue: Boolean, modules: { type: Array, default: () => [] }, busy: Boolean })
const emit = defineEmits(['update:modelValue', 'save'])
const blank = () => ({ module: '', title: '', priority: 'P2', precondition: '', steps: [{ action: '', expected: '' }], keywords: '', remark: '', point_ids: '' })
const f = ref(blank())
watch(() => props.modelValue, v => { if (v) f.value = blank() })
function ok() {
  const steps = f.value.steps.map(s => ({ action: s.action.trim(), expected: s.expected.trim() })).filter(s => s.action)
  if (!f.value.title.trim() || !f.value.module.trim() || !steps.length) return
  emit('save', { module: f.value.module.trim(), title: f.value.title.trim(), priority: f.value.priority, precondition: f.value.precondition.trim(), steps, keywords: f.value.keywords.trim(), remark: f.value.remark.trim(), extras: {}, point_ids: f.value.point_ids.split(/[\s,，、]+/).filter(Boolean) })
}
</script>
<template>
  <TgDialog :model-value="modelValue" title="新增用例（草稿）" ok="新增" wide :busy="busy" :ok-disabled="!f.title.trim() || !f.module.trim() || !f.steps.some(s => s.action.trim())" @update:modelValue="v => emit('update:modelValue', v)" @ok="ok">
    <div class="form-grid">
      <TgField label="模块" required><input v-model="f.module" class="field" list="cn-modules" /><datalist id="cn-modules"><option v-for="m in modules" :key="m" :value="m" /></datalist></TgField>
      <TgField label="优先级"><select v-model="f.priority" class="select"><option v-for="p in PRIORITIES" :key="p" :value="p">{{ p }}</option></select></TgField>
    </div>
    <TgField label="标题" required><input v-model="f.title" class="field" /></TgField>
    <TgField label="前置条件"><textarea v-model="f.precondition" class="field" rows="2" /></TgField>
    <TgField label="步骤" required hint="至少一步，操作必填">
      <table class="case-table"><thead><tr><th style="width:34px">#</th><th>步骤</th><th>预期结果</th><th style="width:40px"></th></tr></thead>
        <tbody><tr v-for="(s, i) in f.steps" :key="i"><td class="n">{{ i + 1 }}</td><td><input v-model="s.action" class="field" /></td><td><input v-model="s.expected" class="field" /></td><td><span class="link tiny" @click="f.steps.length > 1 && f.steps.splice(i, 1)">删</span></td></tr></tbody></table>
      <span class="link tiny" @click="f.steps.push({ action: '', expected: '' })">+ 加一步</span>
    </TgField>
    <div class="form-grid">
      <TgField label="关键词"><input v-model="f.keywords" class="field" placeholder="顿号分隔" /></TgField>
      <TgField label="来源测试点"><input v-model="f.point_ids" class="field" placeholder="TP001 TP002" /></TgField>
    </div>
    <TgField label="备注"><input v-model="f.remark" class="field" /></TgField>
    <div class="tiny muted">新增用例以草稿进入任务，提交评审后才进入待审核。</div>
  </TgDialog>
</template>

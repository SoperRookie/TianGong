<script setup>
/* 发起测试设计对话框：单需求 POST /requirements/{id}/design；多需求 POST /requirements/design（DesignBody 字段） */
import { ref, computed, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useUiStore } from '@/stores/ui'
import { TgDialog, TgField } from '@/components/ui'
import { useModels, useTemplates, withContentWarning } from './reqUtil'
const props = defineProps({ modelValue: Boolean, reqs: { type: Array, default: () => [] } })
const emit = defineEmits(['update:modelValue', 'done'])
const router = useRouter(), ui = useUiStore()
const models = useModels(), templates = useTemplates()
const form = ref({ template_id: '', model: '', reviewer_model: '', confirm_points: true, async_mode: true, knowledge_space: '' })
const advanced = ref(false), busy = ref(false)
const multi = computed(() => props.reqs.length > 1)
const blocked = computed(() => props.reqs.filter(r => r.blocking_questions))
const parsing = computed(() => props.reqs.filter(r => r.parsing))
const projects = computed(() => [...new Set(props.reqs.map(r => r.project))])
watch(() => props.modelValue, v => { if (v) { form.value.template_id = form.value.template_id || templates.value?.default_id || ''; busy.value = false } })
watch(templates, t => { if (t && !form.value.template_id) form.value.template_id = t.default_id || '' })
async function submit() {
  if (!props.reqs.length) return
  if (projects.value.length > 1) return ui.toast(`所选需求须属于同一项目（当前涉及：${projects.value.join('、')}）`, { kind: 'warn' })
  busy.value = true
  const body = { template_id: form.value.template_id || null, model: form.value.model || null, reviewer_model: form.value.reviewer_model || null, confirm_points: form.value.confirm_points, async_mode: form.value.async_mode, knowledge_space: form.value.knowledge_space.trim() || null }
  try {
    const r = await withContentWarning(ignore => multi.value
      ? api.post('/api/v1/requirements/design', { ...body, req_ids: props.reqs.map(x => x.req_id), ignore_warnings: ignore })
      : api.post(`/api/v1/requirements/${props.reqs[0].req_id}/design`, { ...body, ignore_warnings: ignore }))
    ui.toast(multi.value ? `已合并 ${props.reqs.length} 条需求创建测试设计任务` : '测试设计任务已创建')
    emit('update:modelValue', false); emit('done', r)
    if (r.task_id) router.push({ name: 'design-detail', params: { id: r.task_id } })
  } catch (e) { if (e.message !== '已取消') ui.error(e) } finally { busy.value = false }
}
</script>
<template>
  <TgDialog :model-value="modelValue" @update:model-value="v => emit('update:modelValue', v)" :title="multi ? `合并发起测试设计（${reqs.length} 条需求）` : '发起测试设计'" ok="开始设计" :busy="busy" :ok-disabled="!reqs.length || !!blocked.length || !!parsing.length" @ok="submit">
    <div class="information" style="margin-bottom:12px">
      <div>
        <b>{{ multi ? reqs.map(r => r.title).join('、') : reqs[0]?.title }}</b>
        <div class="tiny" style="margin-top:3px">{{ multi ? '多条需求合并为一份需求文本（逐条标注标题）走同一拆解 / 生成链路；任务回挂全部所选需求（追溯链）' : '拆解 / 生成使用：整理稿（或原文 + 附件解析）+ 人工补充 + 待确认事项的确认结论；任务回挂本需求' }}</div>
      </div>
    </div>
    <div v-if="blocked.length" class="attention" style="margin-bottom:10px">「{{ blocked[0].title }}」还有 {{ blocked[0].blocking_questions }} 项必须确认的事项，请先在需求详情第 ④ 步确认</div>
    <div v-if="parsing.length" class="attention" style="margin-bottom:10px">「{{ parsing[0].title }}」的附件仍在解析中，解析完成后再发起</div>
    <div class="form-grid">
      <TgField label="用例模板" required>
        <select v-model="form.template_id" class="select"><option v-for="t in templates?.templates || []" :key="t.template_id" :value="t.template_id">{{ t.name }}{{ t.is_default ? '（默认）' : '' }}</option></select>
      </TgField>
      <TgField label="生成模型" hint="空 = 系统默认模型">
        <select v-model="form.model" class="select"><option value="">默认（{{ models?.default_model || '未配置' }}）</option><option v-for="m in models?.models || []" :key="m.name" :value="m.name">{{ m.name }}</option></select>
      </TgField>
      <TgField label="评审模型" hint="独立评审测试点 / 用例，空 = 同生成模型">
        <select v-model="form.reviewer_model" class="select"><option value="">同生成模型</option><option v-for="m in models?.models || []" :key="m.name" :value="m.name">{{ m.name }}</option></select>
      </TgField>
    </div>
    <button type="button" class="btn ghost small" @click="advanced = !advanced">{{ advanced ? '收起高级选项' : '高级选项 ▾' }}</button>
    <div v-if="advanced" class="mt">
      <label class="row" style="margin-bottom:8px"><input type="checkbox" v-model="form.confirm_points" /> 拆解确认：先生成测试点，人工审核通过后再生成用例（推荐）</label>
      <label class="row" style="margin-bottom:8px"><input type="checkbox" v-model="form.async_mode" /> 后台异步执行（关闭后请求会等到任务完成）</label>
      <TgField label="知识空间" hint="空 = 本项目"><input v-model="form.knowledge_space" class="field" placeholder="项目名" /></TgField>
    </div>
  </TgDialog>
</template>

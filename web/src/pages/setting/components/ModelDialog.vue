<script setup>
/* 模型条目编辑（11.3）：ModelConfig 全部字段；密钥只填环境变量名，绝不输入 / 回显密钥值。 */
import { ref, reactive, watch } from 'vue'
import { useUiStore } from '@/stores/ui'
import { TgDialog, TgField } from '@/components/ui'
const props = defineProps({ modelValue: Boolean, model: { type: Object, default: null }, names: { type: Array, default: () => [] } })
const emit = defineEmits(['update:modelValue', 'save'])
const ui = useUiStore()
const form = reactive({ name: '', provider: 'openai', base_url: 'https://api.openai.com/v1', api_key_env: 'OPENAI_API_KEY', model: '', supports_vision: false, temperature: 1, max_tokens: 32768, timeout: 300, fallbacks: '', reasoning_effort: '', input_price: 0, output_price: 0 })
watch(() => props.modelValue, v => {
  if (!v) return
  const m = props.model
  Object.assign(form, { name: m?.name || '', provider: m?.provider || 'openai', base_url: m?.base_url || 'https://api.openai.com/v1', api_key_env: m?.api_key_env ?? 'OPENAI_API_KEY', model: m?.model || '', supports_vision: !!m?.supports_vision, temperature: m?.temperature ?? 1, max_tokens: m?.max_tokens ?? 32768, timeout: m?.timeout ?? 300, fallbacks: (m?.fallbacks || []).join(', '), reasoning_effort: m?.reasoning_effort || '', input_price: m?.input_price ?? 0, output_price: m?.output_price ?? 0 })
})
function submit() {
  if (!form.name.trim() || !form.model.trim() || !form.base_url.trim()) return ui.toast('名称、厂商侧模型名与 base_url 必填', 'warn')
  if (!props.model && props.names.includes(form.name.trim())) return ui.toast('名称已存在', 'warn')
  if (form.api_key_env && !/^[A-Z][A-Z0-9_]*_(API_KEY|TOKEN|KEY)$/.test(form.api_key_env)) return ui.toast('密钥环境变量名须形如 XXX_API_KEY / XXX_TOKEN / XXX_KEY', 'warn')
  if (form.api_key_env.startsWith('TIANGONG_')) return ui.toast('密钥变量名不能是 TIANGONG_ 配置项', 'warn')
  emit('save', {
    name: form.name.trim(), provider: form.provider.trim(), base_url: form.base_url.trim(), api_key_env: form.api_key_env.trim() || null, model: form.model.trim(),
    supports_vision: !!form.supports_vision, temperature: +form.temperature, max_tokens: +form.max_tokens, timeout: +form.timeout,
    fallbacks: form.fallbacks.split(/[,，\s]+/).map(s => s.trim()).filter(Boolean), reasoning_effort: form.reasoning_effort || null,
    input_price: +form.input_price || 0, output_price: +form.output_price || 0,
  })
}
</script>
<template>
  <TgDialog :model-value="modelValue" :title="model ? `编辑模型 · ${model.name}` : '添加模型'" ok="确定" wide @update:model-value="emit('update:modelValue', $event)" @ok="submit">
    <div class="form-grid">
      <TgField label="名称（系统内唯一标识）" required><input v-model="form.name" class="field" :disabled="!!model" placeholder="如 gpt-6-sol" /></TgField>
      <TgField label="厂商" required hint="openai / deepseek / dashscope / ollama / vllm 等；OpenAI 官方须为 openai"><input v-model="form.provider" class="field" /></TgField>
      <TgField label="厂商侧模型名" required><input v-model="form.model" class="field" /></TgField>
      <TgField label="base_url" required><input v-model="form.base_url" class="field" placeholder="https://api.openai.com/v1" /></TgField>
      <TgField label="密钥环境变量名" hint="只存变量名，密钥值写在服务器 .env；内网无鉴权服务可留空"><input v-model="form.api_key_env" class="field" placeholder="OPENAI_API_KEY" /></TgField>
      <TgField label="降级链路 fallbacks" hint="逗号分隔的模型名称，按顺序尝试"><input v-model="form.fallbacks" class="field" :list="'fb-names'" /><datalist id="fb-names"><option v-for="n in names" :key="n" :value="n" /></datalist></TgField>
      <TgField label="温度" hint="GPT-6 / 5.6 / 5.5 只能为 1；5.4 系可 0.3"><input v-model="form.temperature" class="field" type="number" step="0.1" min="0" max="2" /></TgField>
      <TgField label="推理强度" hint="推理型模型默认强度，拆解 / 评审自动提升到 high；留空表示不支持"><select v-model="form.reasoning_effort" class="select"><option value="">不传</option><option v-for="x in ['none', 'low', 'medium', 'high']" :key="x" :value="x">{{ x }}</option></select></TgField>
      <TgField label="max_tokens"><input v-model="form.max_tokens" class="field" type="number" min="256" /></TgField>
      <TgField label="超时（秒）"><input v-model="form.timeout" class="field" type="number" min="10" /></TgField>
      <TgField label="输入单价（美元 / 百万 token）"><input v-model="form.input_price" class="field" type="number" step="0.01" min="0" /></TgField>
      <TgField label="输出单价（美元 / 百万 token）"><input v-model="form.output_price" class="field" type="number" step="0.01" min="0" /></TgField>
    </div>
    <label class="row small"><input v-model="form.supports_vision" type="checkbox" /> 支持 Vision（图片理解）；不支持的模型自动改用第一个支持 Vision 的模型</label>
    <div class="information" style="margin-top:10px"><span>保存整份配置时整体校验、原子写回 config/models.yaml 并热重载；密钥值不进配置文件、不进页面。*-pro 型号不支持 chat 接口。</span></div>
  </TgDialog>
</template>

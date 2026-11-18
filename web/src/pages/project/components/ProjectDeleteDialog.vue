<script setup>
/* 删除项目（仅系统管理员）：先 GET /projects/{name}/contents 预检；空项目直删，非空须输入项目名确认后带 cascade=true 连同内容删除。 */
import { ref, watch, computed } from 'vue'
import { api, seg } from '@/api/client'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { TgDialog, TgKv } from '@/components/ui'
const props = defineProps({ modelValue: Boolean, project: { type: String, default: '' } })
const emit = defineEmits(['update:modelValue', 'deleted'])
const scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore()
const contents = ref(null), loading = ref(false), typed = ref(''), busy = ref(false), err = ref('')
watch(() => props.modelValue, async v => {
  if (!v) return
  contents.value = null; typed.value = ''; err.value = ''; loading.value = true
  try { contents.value = await api.get(`/api/v1/projects/${seg(props.project)}/contents`) } catch (e) { err.value = e.message } finally { loading.value = false }
}, { immediate: true })
const cascade = computed(() => contents.value && !contents.value.empty)
const blocked = computed(() => !!contents.value?.running)
const okDisabled = computed(() => loading.value || !contents.value || blocked.value || (cascade.value && typed.value.trim() !== props.project))
const lines = computed(() => {
  const c = contents.value; if (!c) return []
  const out = []
  if (c.requirements) out.push(`${c.requirements} 条需求（含附件与整理稿，彻底清除）`)
  if (c.plans) out.push(`${c.plans} 个测试计划${c.plans_with_runs ? `（其中 ${c.plans_with_runs} 个有执行记录）` : ''}`)
  if (c.tasks) out.push(`${c.tasks} 个测试设计任务、${c.cases} 条用例（进回收站，恢复后不再归属项目）`)
  return out
})
async function doDelete() {
  busy.value = true
  try {
    await api.del(`/api/v1/projects/${seg(props.project)}`, undefined, { query: cascade.value ? { cascade: true } : undefined })
    ui.toast(cascade.value ? `项目「${props.project}」及其内容已删除` : `项目「${props.project}」已删除`)
    if (scope.project === props.project) scope.setProject('')
    await Promise.all([scope.load(true), auth.fetchMe()])
    emit('update:modelValue', false)
    emit('deleted', props.project)
  } catch (e) { ui.error(e) } finally { busy.value = false }
}
</script>
<template>
  <TgDialog :model-value="modelValue" :title="cascade ? '删除项目及全部内容' : '删除项目'" ok="删除" danger :busy="busy" :ok-disabled="okDisabled" @update:model-value="$emit('update:modelValue', $event)" @ok="doDelete">
    <div v-if="loading" class="muted">正在检查项目内容…</div>
    <div v-else-if="err" class="err-box">{{ err }}</div>
    <template v-else-if="contents">
      <TgKv :pairs="[['任务', `${contents.tasks} 个（排队 / 进行中 ${contents.running}，回收站 ${contents.deleted_tasks}）`], ['用例', `${contents.cases} 条`], ['测试计划', `${contents.plans} 个（有执行记录 ${contents.plans_with_runs}）`], ['需求', `${contents.requirements} 条（回收站 ${contents.recycled_requirements}）`]]" />
      <div v-if="blocked" class="attention mt">项目下有 {{ contents.running }} 个排队 / 进行中的任务，等它们结束或取消后再删。</div>
      <template v-else-if="cascade">
        <div class="attention mt">项目「{{ project }}」不是空项目，删除会连同以下内容一起删，不可撤销：</div>
        <ul class="steps-mini" style="margin:8px 0 10px 18px;padding:0"><li v-for="l in lines" :key="l">{{ l }}</li></ul>
        <label class="frow"><span class="flabel">输入项目名确认</span><span class="fctl"><input v-model="typed" class="field" :placeholder="project" /></span></label>
      </template>
      <div v-else class="information mt">这是空项目，删除后不可恢复。<template v-if="contents.recycled_requirements">回收站里的 {{ contents.recycled_requirements }} 条需求会一并清除。</template></div>
    </template>
  </TgDialog>
</template>

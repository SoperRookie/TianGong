<script setup>
/* 知识回溯（9.15）：GET/POST /projects/{name}/knowledge/backfill；后台执行，进行中每 2 秒轮询；完成后自动提炼一版画像草稿。 */
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { api, seg } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useUiStore } from '@/stores/ui'
import { fmtTime, fmtAgo } from '@/utils/format'
import { TgPanel, TgBadge, TgKv } from '@/components/ui'
const props = defineProps({ project: { type: String, required: true }, canRun: Boolean, knowledgeAuto: { type: Boolean, default: true } })
const emit = defineEmits(['tab'])
const ui = useUiStore()
const { data: bf, loading, error, reload } = useLoad(() => api.get(`/api/v1/projects/${seg(props.project)}/knowledge/backfill`), { deps: [() => props.project] })
const last = computed(() => bf.value?.last || null), t = computed(() => bf.value?.targets || {})
const running = computed(() => last.value?.status === 'running')
const busy = ref(false)
let timer = null
watch(running, v => { clearInterval(timer); timer = null; if (v) timer = setInterval(() => reload(true), 2000) }, { immediate: true })
onBeforeUnmount(() => clearInterval(timer))
const disabledReason = computed(() => !bf.value?.knowledge_auto ? '本项目已关闭自动沉淀，请先在设置中开启' : (!(t.value.tasks || t.value.requirements) ? '项目还没有可回溯的历史任务或需求' : ''))
async function run() {
  if (!await ui.confirm('回溯沉淀历史任务？', `把本项目 ${t.value.tasks || 0} 个任务与 ${t.value.requirements || 0} 条需求补进知识库。后台执行，会调用 Embedding 模型，耗时与历史数量成正比；按来源替换，可重复执行。`, { ok: '开始回溯' })) return
  busy.value = true
  try { bf.value = await api.post(`/api/v1/projects/${seg(props.project)}/knowledge/backfill`, {}); ui.toast('已开始回溯，完成后知识库页可见「任务沉淀 / 需求沉淀」文档') }
  catch (e) { ui.error(e) } finally { busy.value = false }
}
const SOURCES = [['需求原文', '需求附件解析完成后入需求文档库'], ['已确认的需求分析', 'rules / boundaries / state_changes / permissions → 业务规则库；其余 → 需求文档库'], ['审核通过的测试点', '进业务规则库'], ['评审通过的用例', '延迟 30 秒合并，增量向量化入测试用例库'], ['离线终稿', '以终稿为准替换同来源文档'], ['存量回溯', '上线前的历史任务 / 需求按同样规则补进来，可重复']]
</script>
<template>
  <div v-if="error" class="err-box mb">{{ error.message }}</div>
  <div class="grid-2">
    <TgPanel title="存量回溯" hint="自动沉淀只覆盖上线后的确认 / 评审动作；历史任务要靠回溯补进知识库">
      <template #actions>
        <button v-if="canRun" type="button" class="btn small primary" :disabled="busy || running || !!disabledReason" :title="disabledReason" @click="run">{{ running ? '回溯中…' : '回溯沉淀历史任务' }}</button>
      </template>
      <TgKv :pairs="[['可回溯对象', `任务 ${t.tasks || 0} 个 · 需求 ${t.requirements || 0} 条`], ['项目自动沉淀', bf?.knowledge_auto === false ? '关闭' : '开启']]" />
      <div v-if="disabledReason && !running" class="attention mt">{{ disabledReason }}<button v-if="!bf?.knowledge_auto" type="button" class="btn small" @click="emit('tab', 'settings')">去设置</button></div>
      <div class="mt">
        <div v-if="loading && !bf" class="muted">加载中…</div>
        <template v-else-if="last">
          <div class="row mb"><TgBadge :status="last.status === 'done' ? 'completed' : last.status" /><span class="muted small">{{ last.by || '—' }} · 开始 {{ fmtTime(last.started_at) }}<template v-if="last.finished_at"> · 结束 {{ fmtTime(last.finished_at) }}</template></span></div>
          <template v-if="running">
            <div class="bar"><span :style="{ width: (last.total ? Math.round(last.done / last.total * 100) : 0) + '%' }" /></div>
            <div class="muted tiny mt">回溯中 {{ last.done }} / {{ last.total }}<template v-if="last.current"> · 当前：{{ last.current }}</template>（每 2 秒刷新）</div>
          </template>
          <TgKv v-else :pairs="[['来源', `任务 ${last.tasks} · 需求 ${last.requirements}`], ['入库', `${last.docs} 篇`], ['失败', last.errors ? `${last.errors}（详见服务日志）` : '0'], ['结果', last.status === 'failed' ? (last.error || '失败') : `完成于 ${fmtAgo(last.finished_at)}`]]" />
        </template>
        <div v-else class="muted small">该项目尚未回溯过。</div>
      </div>
    </TgPanel>
    <TgPanel title="自动沉淀来源" :hint="`项目开关：${knowledgeAuto ? '开启' : '关闭'} · 同来源单作业合并 · 关停时等待写完`">
      <div class="list"><div v-for="[a, b] in SOURCES" :key="a"><b>{{ a }}</b><div class="tiny muted">{{ b }}</div></div></div>
    </TgPanel>
  </div>
</template>

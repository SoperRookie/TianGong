<script setup>
/* 需求变更差异分析（5.23）：粘贴新版需求 → 对比找受影响测试点 / 用例 → 应用时只改受影响用例并生成新增，删除类只标记。 */
import { ref, computed } from 'vue'
import { api } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtTime } from '@/utils/format'
import { TgPanel, TgBadge, TgEmpty, TgIcon } from '@/components/ui'
const props = defineProps({ task: { type: Object, required: true } })
const emit = defineEmits(['reload'])
const auth = useAuthStore(), ui = useUiStore()
const text = ref(''), busy = ref(''), applied = ref(null), fileName = ref('')
const project = computed(() => (props.task.context || {}).project)
const canAi = computed(() => auth.can(project.value, 'point.ai')), canApply = computed(() => auth.can(project.value, 'point.review'))
const rd = computed(() => props.task.requirement_diff || null)
const TYPE_COLOR = { 新增: 'green', 修改: 'orange', 删除: 'red', 规则调整: 'orange', 页面调整: 'blue', 数据调整: 'blue', 仅文案调整: 'gray', 无变化: 'gray' }
function readFile(e) {
  const f = e.target.files?.[0]; if (!f) return
  if (!/\.(txt|md|markdown|csv)$/i.test(f.name)) return ui.toast('这里只读纯文本 / Markdown；Word / PDF 请先在需求中心解析后把整理稿粘贴进来', 'warn')
  const r = new FileReader(); r.onload = () => { text.value = String(r.result || ''); fileName.value = f.name }; r.readAsText(f)
}
async function analyze() {
  const t = text.value.trim(); if (!t) return ui.toast('请粘贴新版需求内容', 'warn')
  if (!await ui.confirm('开始需求变更分析？', '将调用模型对比新旧需求，识别变化类型并标记受影响的测试点 / 用例（只分析不改动）。', { ok: '分析' })) return
  busy.value = 'diff'
  try { await api.post(`/api/v1/tasks/${props.task.task_id}/requirement-diff`, { new_requirement: t }); applied.value = null; ui.toast('差异分析完成'); emit('reload') } catch (e) { ui.error(e) } finally { busy.value = '' }
}
async function apply() {
  if (!await ui.confirm('应用最小范围更新？', '只有受影响用例会创建新版本并回到待审核，新增需求生成新用例；删除类仅标记，其余用例与锁定资产不动。', { ok: '应用' })) return
  busy.value = 'apply'
  try {
    const r = await api.post(`/api/v1/tasks/${props.task.task_id}/requirement-diff/apply`, {})
    applied.value = r
    if ((r.removed_marks || []).length) ui.toast('存在删除类变更：相关用例仅标记受影响，请人工决定保留 / 作废', 'warn'); else ui.toast(`最小范围更新已应用（改动 ${(r.diff || []).length} 条）`)
    emit('reload')
  } catch (e) { ui.error(e) } finally { busy.value = '' }
}
</script>
<template>
  <div class="grid-2 mt">
    <TgPanel title="新版需求" hint="Prompt requirement_diff · POST /tasks/{id}/requirement-diff → /apply">
      <textarea v-model="text" class="field" rows="12" style="width:100%" placeholder="粘贴新版需求全文（或整理稿）…" :disabled="!canAi"></textarea>
      <div class="row" style="margin-top:8px">
        <label class="btn small" style="cursor:pointer">读入文本文件<input type="file" class="hidden" accept=".txt,.md,.markdown" @change="readFile" /></label><span v-if="fileName" class="tiny muted">{{ fileName }}</span>
        <span class="grow"></span>
        <button v-if="canAi" type="button" class="btn primary" :disabled="!!busy || !text.trim()" @click="analyze">{{ busy === 'diff' ? '分析中…' : '分析差异' }}</button>
      </div>
      <div class="tiny muted" style="margin-top:6px">旧版 = 任务 context.requirement（{{ ((task.context || {}).requirement || '').length }} 字）。识别 新增 / 修改 / 删除 / 仅文案 / 规则 / 页面 / 数据 调整，受影响 id 过滤到真实存在的测试点与用例。</div>
    </TgPanel>
    <TgPanel title="分析结果" :hint="rd ? `${fmtTime(rd.at)} · ${rd.model_name || ''}` : ''">
      <template #actions>
        <TgBadge v-if="rd" :status="rd.applied ? 'approved' : 'pending'" :text="rd.applied ? `已应用 ${fmtTime(rd.applied_at)}` : '待应用'" />
        <button v-if="rd && !rd.applied && canApply" type="button" class="btn primary small" :disabled="!!busy" @click="apply">{{ busy === 'apply' ? '更新中…' : '应用最小范围更新' }}</button>
      </template>
      <TgEmpty v-if="!rd" title="尚未做变更分析" hint="粘贴新版需求 → 分析 → 应用时只改受影响用例并生成新增，删除类只标记。" />
      <template v-else>
        <div class="source-block"><b>{{ rd.summary || '变更摘要' }}</b></div>
        <div class="list mt">
          <div v-for="(c, i) in rd.changes || []" :key="i">
            <TgBadge :status="c.type" :text="c.type" :color="TYPE_COLOR[c.type] || 'gray'" /> {{ c.description }}
            <div v-if="(c.affected_cases || []).length" class="tiny muted">受影响用例：<span v-for="id in c.affected_cases" :key="id" class="mono" style="margin-right:6px">{{ id }}</span></div>
            <div v-if="(c.affected_points || []).length" class="tiny muted">受影响测试点：<span v-for="id in c.affected_points" :key="id" class="mono" style="margin-right:6px">{{ id }}</span></div>
            <div v-if="c.action_hint" class="tiny muted">建议：{{ c.action_hint }}</div>
          </div>
          <div v-if="(rd.new_requirements || []).length"><b>新增需求</b><div v-for="(n, i) in rd.new_requirements" :key="i" class="tiny muted">· {{ typeof n === 'string' ? n : (n.description || n.text || JSON.stringify(n)) }}</div></div>
        </div>
        <div v-if="applied" class="information mt"><TgIcon name="info" :size="13" /> <span>本次应用改动 {{ (applied.diff || []).length }} 条用例<template v-if="(applied.removed_marks || []).length">；删除类标记 {{ applied.removed_marks.length }} 组：<span v-for="(m, i) in applied.removed_marks" :key="i">{{ m.description }}（{{ (m.affected_cases || []).join('、') }}）</span></template></span></div>
      </template>
    </TgPanel>
  </div>
</template>

<script setup>
/* 对话修订（5.13）：修订要求进入定点修正 → 评审回环，锁定用例不动；历史指令随 Prompt 注入。附 AI 定点修改留痕（fix_log）。 */
import { ref, computed, onMounted } from 'vue'
import { api } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtTime } from '@/utils/format'
import { TgPanel, TgEmpty, TgIcon } from '@/components/ui'
import { caseStats } from './taskUtils'
const props = defineProps({ task: { type: Object, required: true } })
const emit = defineEmits(['reload'])
const auth = useAuthStore(), ui = useUiStore()
const instruction = ref(''), model = ref(''), reviewer = ref(''), busy = ref(false), models = ref([])
const project = computed(() => (props.task.context || {}).project)
const canAi = computed(() => auth.can(project.value, 'case.ai'))
const cs = computed(() => caseStats(props.task))
const ready = computed(() => props.task.status === 'completed' && cs.value.total > 0)
const FIX_KIND = { points: '测试点定点修改', cases: '用例定点修改', requirement_change: '需求变更更新' }
onMounted(async () => { try { models.value = (await api.get('/api/v1/models')).models || [] } catch { /* 非关键 */ } })
async function submit() {
  const text = instruction.value.trim()
  if (!text) return ui.toast('请输入修订要求', 'warn')
  if (!await ui.confirm('提交修订？', `将调用模型对 ${cs.value.total - cs.value.locked} 条未锁定用例做定点修正并评审（已锁定 ${cs.value.locked} 条不动），耗时数分钟。`, { ok: '提交修订' })) return
  busy.value = true
  try {
    const r = await api.post(`/api/v1/tasks/${props.task.task_id}/revise`, { instruction: text, model: model.value || null, reviewer_model: reviewer.value || null })
    instruction.value = ''
    ui.toast(`第 ${r.revision_no} 轮修订已应用：${(r.cases || []).length || r.case_count || ''} 条用例${r.review_rounds != null ? `，评审 ${r.review_rounds} 轮` : ''}`)
    emit('reload')
  } catch (e) { ui.error(e) } finally { busy.value = false }
}
</script>
<template>
  <div class="grid-2 mt">
    <TgPanel title="对话修订" hint="修订要求作为定点修正问题进入生成 → 评审回环，只改受影响用例；重复出现的指令会固化为偏好记忆">
      <div v-if="(task.revisions || []).length" class="timeline">
        <div v-for="(r, i) in task.revisions" :key="i" class="entry"><b>第 {{ i + 1 }} 轮</b> · {{ r.instruction }}<small>{{ r.by }} · {{ fmtTime(r.at) }} · 已应用 {{ r.case_count }} 条用例，评审 {{ r.review_rounds }} 轮{{ r.passed === false ? ' · 含未通过项' : '' }}</small></div>
      </div>
      <TgEmpty v-else title="尚无修订指令" hint="例：所有涉及金额的用例补充币种；步骤里不要出现「点击」以外的动词" />
      <div v-if="!ready" class="attention mt"><TgIcon name="alert" :size="13" /> 任务状态为 {{ task.status }}{{ cs.total ? '' : '，且没有用例' }}，只有已完成且有用例的任务才能修订。</div>
      <template v-else-if="canAi">
        <textarea v-model="instruction" class="field mt" rows="4" style="width:100%" placeholder="输入修订要求…（⌘⏎ 提交）" @keydown.meta.enter="submit" @keydown.ctrl.enter="submit"></textarea>
        <div class="row" style="margin-top:8px">
          <select v-model="model" class="select small"><option value="">生成模型：沿用任务（{{ (task.context || {}).model || '默认' }}）</option><option v-for="m in models" :key="m.name" :value="m.name">{{ m.name }}</option></select>
          <select v-model="reviewer" class="select small"><option value="">评审模型：沿用任务</option><option v-for="m in models" :key="m.name" :value="m.name">{{ m.name }}</option></select>
          <span class="grow"></span>
          <button type="button" class="btn primary" :disabled="busy || !instruction.trim()" @click="submit">{{ busy ? '修订中…' : '提交修订' }}</button>
        </div>
        <div class="tiny muted" style="margin-top:6px">已锁定（通过）用例 {{ cs.locked }} 条不参与修订；修订后内容变化的用例 version +1 并回到待审核。</div>
      </template>
      <div v-else class="tiny muted mt">没有 case.ai 权限，只能查看修订历史。</div>
    </TgPanel>
    <TgPanel title="AI 定点修改留痕（fix_log）" hint="只输入被驳回项，产出提案，逐项确认后才落地">
      <div v-if="(task.fix_log || []).length" class="list">
        <div v-for="(f, i) in task.fix_log.slice().reverse()" :key="i">
          <b>{{ FIX_KIND[f.kind] || f.kind }}</b> <span class="tiny muted">{{ f.by || '' }} · {{ fmtTime(f.at) }} · 落地 {{ (f.diff || []).length }} 处{{ f.rejected_proposals ? ` · 拒绝 ${f.rejected_proposals}` : '' }}{{ (f.added || []).length ? ` · 新增 ${f.added.length}` : '' }}</span>
          <div v-for="(x, j) in (f.diff || []).slice(0, 3)" :key="j" class="tiny muted" style="margin-top:3px"><span class="mono">{{ x.case_id || x.tp_id }}</span> · {{ x.action || '修改' }} · {{ (x.changes || []).map(c => c.field).join(' / ') }}</div>
        </div>
      </div>
      <TgEmpty v-else title="暂无定点修改记录" />
      <div v-if="task.pending_fix" class="attention mt"><TgIcon name="alert" :size="13" /> 有 AI 修改提案待确认（{{ task.pending_fix.kind === 'cases' ? '用例' : '测试点' }}）<router-link class="btn small" :to="{ name: task.pending_fix.kind === 'cases' ? 'cases' : 'points', params: { tid: task.task_id } }">去确认</router-link></div>
    </TgPanel>
  </div>
</template>

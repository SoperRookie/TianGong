<script setup>
/* 需求盲区处置：左列表（待处置 / 全部 + 勾选批量）右详情（转待确认事项 / 转测试点 / 需求已说明 / 忽略 / 重开）。 */
import { ref, computed, watch } from 'vue'
import { fmtTime } from '@/utils/format'
import { TgSeg, TgEmpty, TgBadge, TgField } from '@/components/ui'
import { DIMENSIONS } from './WbSession'
const props = defineProps({
  items: { type: Array, default: () => [] }, tid: { type: String, required: true }, sel: { type: String, default: '' },
  modules: { type: Array, default: () => [] }, reqIds: { type: Array, default: () => [] },
  canEdit: Boolean, canReview: Boolean, busy: Boolean,
})
const emit = defineEmits(['act', 'batch', 'select'])
const STATUS = { open: ['待处置', 'orange'], question: ['已转待确认事项', 'blue'], point: ['已转测试点', 'green'], explained: ['需求已说明', 'gray'], ignored: ['已忽略', 'gray'] }
const filter = ref('open'), checked = ref(new Set())
const action = ref('to_point'), note = ref(''), level = ref('must'), module = ref(''), point = ref(''), dimension = ref(''), err = ref('')
const shown = computed(() => props.items.filter(b => filter.value === 'all' || b.status === filter.value))
const cur = computed(() => props.items.find(b => b.bs_id === props.sel) || shown.value[0] || null)
watch(shown, list => { if (list.length && !list.some(b => b.bs_id === props.sel)) emit('select', list[0].bs_id) }, { immediate: true })
watch(cur, b => { note.value = ''; err.value = ''; module.value = b?.module || ''; point.value = b?.text || ''; dimension.value = ''; action.value = b?.status === 'open' || b?.ai_resolved ? 'to_point' : 'reopen' }, { immediate: true })
const openCount = computed(() => props.items.filter(b => b.status === 'open').length)
function toggle(id) { const s = new Set(checked.value); s.has(id) ? s.delete(id) : s.add(id); checked.value = s }
function submit() {
  err.value = ''
  const body = { action: action.value, note: note.value.trim() }
  if (action.value === 'explain' && !body.note) { err.value = '请写明需求哪里已经说明（章节 / 原文片段）'; return }
  if (action.value === 'to_question') { body.level = level.value; if (!props.reqIds.length) { err.value = '任务未关联需求，无法转为待确认事项；可转为测试点'; return } }
  if (action.value === 'to_point') { body.module = module.value.trim(); body.point = point.value.trim(); body.dimension = dimension.value; if (!body.point) { err.value = '测试点描述不能为空'; return } }
  emit('act', { id: cur.value.bs_id, body })
}
function batch(act) {
  const body = { action: act, note: note.value.trim(), level: level.value }
  if (act === 'explain' && !body.note) { err.value = '批量标记「需求已说明」须填写说明（原文出处）'; return }
  emit('batch', { ids: [...checked.value], body }); checked.value = new Set()
}
const needEdit = a => a === 'to_point' || a === 'to_question'
</script>
<template>
  <div class="workbench">
    <div class="panel list-pane">
      <div class="list-tools">
        <TgSeg v-model="filter" :items="[['open', '待处置', openCount], ['point', '转测试点'], ['question', '转待确认'], ['explained', '已说明'], ['ignored', '已忽略'], ['all', '全部', items.length]]" />
        <div v-if="checked.size" class="row tiny">
          已勾选 {{ checked.size }} 条：
          <button type="button" class="btn small" :disabled="busy || !canEdit" @click="batch('to_point')">转测试点</button>
          <button type="button" class="btn small" :disabled="busy || !canEdit || !reqIds.length" @click="batch('to_question')">转待确认</button>
          <button type="button" class="btn small" :disabled="busy || !canReview" @click="batch('explain')">已说明</button>
          <button type="button" class="btn small" :disabled="busy || !canReview" @click="batch('ignore')">忽略</button>
          <button type="button" class="btn small" :disabled="busy || !canReview" @click="batch('reopen')">重开</button>
        </div>
      </div>
      <div class="list-scroll">
        <div v-for="b in shown" :key="b.bs_id" class="item bs-item" :class="{ sel: cur && cur.bs_id === b.bs_id, ok: b.status !== 'open' }" @click="emit('select', b.bs_id)">
          <input type="checkbox" :checked="checked.has(b.bs_id)" @click.stop @change="toggle(b.bs_id)" />
          <span class="st"></span>
          <div class="bs-txt"><div class="ttl">{{ b.text }}</div><div class="tiny muted" v-if="b.module || b.ref">{{ b.ref || b.module }}</div></div>
          <span v-if="b.stale" class="tag">已不在结果中</span>
        </div>
        <TgEmpty v-if="!shown.length" title="没有需求盲区" :hint="items.length ? '切到「全部」查看已处置的' : '拆解时模型没有标出「需求未说明」的点'" />
      </div>
    </div>
    <div class="panel panel-pad detail-pane">
      <template v-if="cur">
        <div class="detail-top">
          <span class="badge" :class="(STATUS[cur.status] || STATUS.open)[1]">{{ (STATUS[cur.status] || STATUS.open)[0] }}</span>
          <span v-if="cur.ai_resolved" class="tag">AI 复核</span>
          <span v-if="cur.module" class="tag">{{ cur.module }}</span>
          <span class="meta" v-if="cur.by">{{ cur.by }} · {{ fmtTime(cur.at) }}</span>
        </div>
        <div class="detail-title">{{ cur.text }}</div>
        <dl class="kv">
          <dt>出处</dt><dd>{{ cur.ref || '—' }}</dd>
          <dt>去向</dt><dd><template v-if="cur.target?.tp_id">测试点 <b class="mono">{{ cur.target.tp_id }}</b>（{{ cur.target.module }}）</template><template v-else-if="cur.target?.q_id">需求「{{ cur.target.req_title }}」待确认事项 {{ cur.target.q_id }}（{{ cur.target.level === 'must' ? '必须确认' : '建议确认' }}）</template><template v-else>—</template></dd>
          <dt>说明</dt><dd>{{ cur.note || '—' }}</dd>
          <dt v-if="cur.history?.length">历史</dt><dd v-if="cur.history?.length"><div v-for="(h, i) in cur.history" :key="i" class="tiny muted">{{ (STATUS[h.status] || [h.status])[0] }} · {{ h.by }} {{ fmtTime(h.at) }}<span v-if="h.note"> · {{ h.note }}</span></div></dd>
        </dl>
        <div class="bs-form">
          <div class="chips">
            <template v-if="cur.status === 'open' || cur.ai_resolved">
              <span class="chip" :class="{ on: action === 'to_point' }" @click="action = 'to_point'">转测试点</span>
              <span class="chip" :class="{ on: action === 'to_question' }" @click="action = 'to_question'">转待确认事项</span>
              <span class="chip" :class="{ on: action === 'explain' }" @click="action = 'explain'">需求已说明</span>
              <span class="chip" :class="{ on: action === 'ignore' }" @click="action = 'ignore'">忽略</span>
            </template>
            <span v-if="cur.status !== 'open'" class="chip" :class="{ on: action === 'reopen' }" @click="action = 'reopen'">重开</span>
          </div>
          <template v-if="action === 'to_point'">
            <TgField label="模块"><input v-model="module" class="field" list="bs-modules" placeholder="默认「需求盲区」" /><datalist id="bs-modules"><option v-for="m in modules" :key="m" :value="m" /></datalist></TgField>
            <TgField label="测试点描述" required><textarea v-model="point" class="field" rows="2" /></TgField>
            <TgField label="维度"><select v-model="dimension" class="select"><option value="">不指定</option><option v-for="d in DIMENSIONS" :key="d" :value="d">{{ d }}</option></select></TgField>
          </template>
          <TgField v-if="action === 'to_question'" label="事项等级"><select v-model="level" class="select"><option value="must">必须确认（阻塞发起设计）</option><option value="suggest">建议确认（可带假设继续）</option></select></TgField>
          <TgField v-if="action !== 'reopen'" :label="action === 'explain' ? '原文出处' : action === 'to_question' ? '假设 / 说明' : '处置说明'" :required="action === 'explain'"><input v-model="note" class="field" :placeholder="action === 'explain' ? '写明需求哪一节已经说明' : '可选'" /></TgField>
          <div v-if="err" class="tiny bs-err">{{ err }}</div>
          <div class="row">
            <button type="button" class="btn primary small" :disabled="busy || (needEdit(action) ? !canEdit : !canReview)" @click="submit">{{ action === 'reopen' ? '重开' : '处置' }}</button>
            <span class="ai tiny muted">转测试点 / 转待确认需 point.edit，其余需 point.review</span>
          </div>
        </div>
      </template>
      <TgEmpty v-else title="选择左侧一条盲区" hint="盲区是 AI 的提示；是否真是盲区、该去哪里由人工决定并留痕。" />
    </div>
  </div>
</template>
<style scoped>
.bs-item { align-items: flex-start; }
.bs-item .st { margin-top: 6px; }
.bs-txt { flex: 1; min-width: 0; }
.bs-txt .ttl { white-space: normal; }
.bs-form { margin-top: 14px; padding-top: 12px; border-top: 1px solid #edf0f3; }
.bs-form .chips { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; }
.bs-err { color: var(--red); margin-bottom: 6px; }
</style>

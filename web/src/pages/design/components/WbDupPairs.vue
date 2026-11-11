<script setup>
/* 疑似重复对（测试点 kind=points / 用例 kind=cases）：左列表右详情，忽略 / 重开 / 删除其中一条（删除走审核接口，由父页执行），测试点支持批量忽略 / 重开。 */
import { ref, computed, watch } from 'vue'
import { fmtTime } from '@/utils/format'
import { TgSeg, TgEmpty, TgBadge } from '@/components/ui'
const props = defineProps({
  pairs: { type: Array, default: () => [] }, kind: { type: String, default: 'points' }, tid: { type: String, required: true },
  cases: { type: Array, default: () => [] },   // kind=cases 时用于展示两条用例全文
  canReview: Boolean, canDelete: Boolean, canAi: Boolean, busy: Boolean, sel: { type: String, default: '' },
})
const emit = defineEmits(['act', 'batch', 'delete', 'recheck', 'select'])
const filter = ref('open'), checked = ref(new Set()), note = ref('')
const STATUS = { open: ['待处置', 'orange'], ignored: ['已忽略', 'gray'], resolved: ['已处置', 'green'] }
const shown = computed(() => props.pairs.filter(p => filter.value === 'all' || p.status === filter.value))
const cur = computed(() => props.pairs.find(p => p.key === props.sel) || shown.value[0] || null)
watch(shown, list => { if (list.length && !list.some(p => p.key === props.sel)) emit('select', list[0].key) }, { immediate: true })
const openCount = computed(() => props.pairs.filter(p => p.status === 'open').length)
const caseOf = id => props.cases.find(c => c.case_id === id)
function toggle(k) { const s = new Set(checked.value); s.has(k) ? s.delete(k) : s.add(k); checked.value = s }
function batch(action) { emit('batch', { keys: [...checked.value], action, note: note.value.trim() }); checked.value = new Set() }
const vColor = v => v === '同文不同对象' ? 'gray' : v === '重复' ? 'red' : v === '可合并' ? 'orange' : 'purple'
</script>
<template>
  <div class="workbench">
    <div class="panel list-pane">
      <div class="list-tools">
        <div class="row">
          <TgSeg v-model="filter" :items="[['open', '待处置', openCount], ['ignored', '已忽略'], ['resolved', '已处置'], ['all', '全部', pairs.length]]" />
          <button v-if="canAi" type="button" class="btn small" :disabled="busy" @click="emit('recheck')">重新检查</button>
        </div>
        <div v-if="kind === 'points' && checked.size" class="row tiny">
          已勾选 {{ checked.size }} 组 <input v-model="note" class="field small dp-note" placeholder="处置说明（可选）" />
          <button type="button" class="btn small" :disabled="busy || !canReview" @click="batch('ignore')">批量忽略</button>
          <button type="button" class="btn small" :disabled="busy || !canReview" @click="batch('reopen')">批量重开</button>
        </div>
      </div>
      <div class="list-scroll">
        <div v-for="p in shown" :key="p.key" class="item dp-item" :class="{ sel: cur && cur.key === p.key }" @click="emit('select', p.key)">
          <input v-if="kind === 'points'" type="checkbox" :checked="checked.has(p.key)" @click.stop @change="toggle(p.key)" />
          <span class="st" :class="{ ok: p.status !== 'open' }"></span>
          <div class="dp-txt">
            <div class="tiny muted">{{ p.a }} ≈ {{ p.b }} · {{ ((p.similarity || 0) * 100).toFixed(0) }}%</div>
            <div class="ttl">{{ kind === 'cases' ? p.a_title : p.a_point }}</div>
          </div>
          <TgBadge :text="p.verdict || '待复核'" :color="vColor(p.verdict)" />
        </div>
        <TgEmpty v-if="!shown.length" title="没有疑似重复对" :hint="pairs.length ? '切到「全部」查看已处置的' : '点「重新检查」做文字初筛 + 模型语义复判'" />
      </div>
    </div>
    <div class="panel panel-pad detail-pane">
      <template v-if="cur">
        <div class="detail-top">
          <TgBadge :text="cur.verdict || '待复核'" :color="vColor(cur.verdict)" />
          <span class="badge" :class="(STATUS[cur.status] || STATUS.open)[1]">{{ (STATUS[cur.status] || STATUS.open)[0] }}</span>
          <span class="tag">相似度 {{ ((cur.similarity || 0) * 100).toFixed(0) }}%</span>
          <span v-if="cur.deleted?.length" class="tag">已删除 {{ cur.deleted.join('、') }}</span>
          <span class="meta" v-if="cur.by">{{ cur.by }} · {{ fmtTime(cur.at) }}</span>
        </div>
        <div class="dp-pair">
          <div v-for="side in ['a', 'b']" :key="side" class="dp-side">
            <div class="row"><span class="mono dp-id">{{ cur[side] }}</span><span class="tag" v-if="cur[side + '_module']">{{ cur[side + '_module'] }}</span><span class="tag" v-else-if="caseOf(cur[side])">{{ caseOf(cur[side]).module }}</span></div>
            <div class="dp-point">{{ kind === 'cases' ? cur[side + '_title'] : cur[side + '_point'] }}</div>
            <template v-if="kind === 'cases' && caseOf(cur[side])">
              <div class="tiny muted">前置：{{ caseOf(cur[side]).precondition || '—' }}</div>
              <ol class="dp-steps"><li v-for="(s, i) in caseOf(cur[side]).steps" :key="i">{{ s.action }} <span class="muted">⇒ {{ s.expected }}</span></li></ol>
            </template>
            <button v-if="canDelete && cur.status === 'open' && !cur.deleted?.includes(cur[side])" type="button" class="btn small danger" :disabled="busy" @click="emit('delete', cur[side])">删除 {{ cur[side] }}</button>
          </div>
        </div>
        <dl class="kv dp-kv">
          <dt>裁判理由</dt><dd>{{ cur.reason || '—' }}</dd>
          <dt>建议</dt><dd>{{ cur.suggestion || (cur.keep ? `保留 ${cur.keep}` : '—') }}<span v-if="cur.merged_title"> · 合并标题：{{ cur.merged_title }}</span></dd>
          <dt>处置说明</dt><dd>{{ cur.note || '—' }}</dd>
        </dl>
        <div class="detail-actions">
          <template v-if="cur.status === 'open'">
            <input v-model="note" class="field small dp-note" placeholder="处置说明（可选）" />
            <button type="button" class="btn" :disabled="busy || !canReview" @click="emit('act', { key: cur.key, action: 'ignore', note: note.trim() })">忽略（判定不重复 / 都保留）</button>
          </template>
          <button v-else-if="cur.status === 'ignored'" type="button" class="btn" :disabled="busy || !canReview" @click="emit('act', { key: cur.key, action: 'reopen', note: '' })">重开</button>
          <span v-else class="tiny muted">其中一条已删除，该组自动变为已处置</span>
          <span class="ai">{{ kind === 'points' ? '字符二元组 Jaccard 初筛 + 模型语义复判；「同文不同对象」指同一规则在两个页面 / 对象下各验一次' : '标题 + 前置 + 步骤文字初筛 + 模型裁判（重复 / 可合并 / 不重复）' }}</span>
        </div>
      </template>
      <TgEmpty v-else title="选择左侧一组疑似重复对" />
    </div>
  </div>
</template>
<style scoped>
.dp-item { align-items: flex-start; }
.dp-item .st { margin-top: 6px; }
.dp-txt { flex: 1; min-width: 0; }
.dp-txt .ttl { white-space: normal; }
.dp-note { width: 180px; }
.dp-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 12px 0; }
.dp-side { border: 1px solid var(--line); border-radius: 3px; padding: 10px 12px; display: flex; flex-direction: column; gap: 6px; }
.dp-id { font-weight: 700; color: #2b3b5b; }
.dp-point { font-size: 13.5px; color: #1d2a44; font-weight: 600; }
.dp-steps { padding-left: 18px; margin: 0; font-size: 12px; }
.dp-kv { margin-top: 6px; }
</style>

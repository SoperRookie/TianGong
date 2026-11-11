<script setup>
/* 用例卡：编号 / 优先级 / 模块 / 来源测试点 / 端 / 状态 / 版本 / 来源；标题、前置、步骤表、关键词、备注、扩展列；驳回信息、评审记录、占用提示；
   文字按钮带快捷键（A 通过 · R 驳回 · E 修改 · D 删除 · 解锁 · 复制）；就地编辑完整用例后保存（modify，带 base_version 乐观锁）。 */
import { ref, computed, watch } from 'vue'
import { fmtTime } from '@/utils/format'
import { terminalLabel } from '@/utils/terminals'
import { PRIORITIES } from '@/utils/status'
import { TgBadge, TgPrio } from '@/components/ui'
import WbRejectForm from './WbRejectForm.vue'
import { sourceText } from './WbSession'
const props = defineProps({
  c: { type: Object, required: true },           // 用例（含 rv 审核状态、terminal）
  log: { type: Array, default: () => [] }, holder: { type: Object, default: null }, pointText: { type: Function, default: () => '' },
  mode: { type: String, default: '' },           // '' | 'reject' | 'edit'
  canReview: Boolean, canEdit: Boolean, canDelete: Boolean, canAi: Boolean, busy: Boolean,
})
const emit = defineEmits(['approve', 'submit', 'reject', 'edit', 'save', 'cancel', 'delete', 'unlock', 'copy', 'fix'])
const rv = computed(() => props.c.rv || {})
const st = computed(() => rv.value.status || 'pending')
const statusText = computed(() => st.value === 'approved' ? `已通过 · ${rv.value.locked ? '已锁定' : '未锁定'}` : st.value === 'rejected' ? '已驳回' : st.value === 'draft' ? '草稿' : '待审核')
const form = ref(null), feedback = ref(''), titleEl = ref(null)
watch(() => [props.mode, props.c.uid], () => {
  if (props.mode === 'edit') {
    const c = props.c
    form.value = { module: c.module, title: c.title, priority: c.priority || 'P2', precondition: c.precondition || '', keywords: c.keywords || '', remark: c.remark || '',
      steps: (c.steps || []).map(s => ({ action: s.action || '', expected: s.expected || '' })), extras: Object.entries(c.extras || {}).map(([k, v]) => ({ k, v })), point_ids: (c.point_ids || []).join(' ') }
    feedback.value = ''
    setTimeout(() => titleEl.value?.focus(), 0)
  }
}, { immediate: true })
const extrasList = computed(() => Object.entries(props.c.extras || {}).filter(([, v]) => v !== '' && v != null))
function save() {
  const f = form.value
  if (!f.title.trim() || !f.module.trim()) return
  const steps = f.steps.map(s => ({ action: s.action.trim(), expected: s.expected.trim() })).filter(s => s.action)
  if (!steps.length) return
  const extras = {}; f.extras.forEach(({ k, v }) => { if (k.trim()) extras[k.trim()] = v })
  emit('save', { case: { ...props.c, module: f.module.trim(), title: f.title.trim(), priority: f.priority, precondition: f.precondition.trim(), keywords: f.keywords.trim(), remark: f.remark.trim(), steps, extras, point_ids: f.point_ids.split(/[\s,，、]+/).filter(Boolean) }, feedback: feedback.value.trim(), base_version: props.c.version || 1 })
}
function onEditKey(e) { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); save() } if (e.key === 'Escape') emit('cancel') }
const ACTS = { approve: '通过', reject: '驳回', modify: '修改', delete: '删除', unlock: '解锁', submit: '提交评审', restore: '恢复版本', restore_bin: '回收站恢复', final: '终稿覆盖' }
</script>
<template>
  <div class="detail-top">
    <span class="id">{{ c.case_id }}</span>
    <TgPrio :p="c.priority" />
    <span class="tag" :title="c.module">{{ c.module }}</span>
    <span v-if="(c.point_ids || []).length" class="tag" :title="c.point_ids.map(id => `${id} ${pointText(id)}`).join('\n')">来源 {{ c.point_ids.slice(0, 4).join(' ') }}{{ c.point_ids.length > 4 ? '…' : '' }}</span>
    <span class="tag">{{ terminalLabel(c.terminal) }}</span>
    <TgBadge :status="st" :text="statusText" />
    <span class="meta">v{{ c.version || 1 }} · {{ sourceText(c.source || 'ai') }}<template v-if="rv.reject_count"> · 驳回 {{ rv.reject_count }} 次</template></span>
  </div>
  <div v-if="holder" class="attention cd-holder">{{ holder.by }} 正在编辑此用例（{{ fmtTime(holder.at) }}），注意保存冲突（版本号乐观锁会拦截覆盖）</div>

  <template v-if="mode === 'edit' && form">
    <div class="cd-edit" @keydown="onEditKey">
      <input ref="titleEl" v-model="form.title" class="field cd-title" placeholder="标题（必填）" />
      <div class="cd-grid">
        <input v-model="form.module" class="field" placeholder="模块（必填）" />
        <select v-model="form.priority" class="select"><option v-for="p in PRIORITIES" :key="p" :value="p">{{ p }}</option></select>
        <input v-model="form.point_ids" class="field" placeholder="来源测试点编号，空格分隔" />
      </div>
      <textarea v-model="form.precondition" class="field" rows="2" placeholder="前置条件" />
      <table class="case-table">
        <thead><tr><th style="width:34px">#</th><th>步骤</th><th>预期结果</th><th style="width:60px"></th></tr></thead>
        <tbody>
          <tr v-for="(s, i) in form.steps" :key="i"><td class="n">{{ i + 1 }}</td><td><textarea v-model="s.action" class="field cd-step" rows="2" placeholder="操作（必填）" /></td><td><textarea v-model="s.expected" class="field cd-step" rows="2" placeholder="预期" /></td><td><span class="link tiny" @click="form.steps.splice(i, 1)">删</span> <span class="link tiny" @click="form.steps.splice(i + 1, 0, { action: '', expected: '' })">插</span></td></tr>
        </tbody>
      </table>
      <div class="row cd-row"><span class="link tiny" @click="form.steps.push({ action: '', expected: '' })">+ 加一步</span></div>
      <div class="cd-grid2">
        <input v-model="form.keywords" class="field" placeholder="关键词，顿号分隔" />
        <input v-model="form.remark" class="field" placeholder="备注" />
      </div>
      <div v-for="(x, i) in form.extras" :key="i" class="cd-grid2"><input v-model="x.k" class="field" placeholder="扩展列名" /><div class="row"><input v-model="x.v" class="field cd-grow" placeholder="值" /><span class="link tiny" @click="form.extras.splice(i, 1)">删</span></div></div>
      <div class="row cd-row"><span class="link tiny" @click="form.extras.push({ k: '', v: '' })">+ 扩展列</span></div>
      <input v-model="feedback" class="field" placeholder="修改说明 / 一键反馈（可选，作为学习语料与版本原因）" />
      <div class="row cd-row">
        <button type="button" class="btn small" @click="emit('cancel')">取消 <kbd>Esc</kbd></button>
        <button type="button" class="btn primary small" :disabled="busy || !form.title.trim() || !form.module.trim() || !form.steps.some(s => s.action.trim())" @click="save">保存（人工定稿：版本 +1 并通过锁定）<kbd>⌘⏎</kbd></button>
        <span class="tiny muted">基于 v{{ c.version || 1 }}；他人已改过则保存被拒（409）</span>
      </div>
    </div>
  </template>
  <template v-else>
    <div class="detail-title">{{ c.title }}</div>
    <dl class="kv"><dt>前置</dt><dd>{{ c.precondition || '—' }}</dd></dl>
    <table class="case-table">
      <thead><tr><th style="width:34px">#</th><th>步骤</th><th>预期结果</th></tr></thead>
      <tbody><tr v-for="(s, i) in (c.steps || [])" :key="i" :class="{ 'cd-hl': (rv.steps || []).includes(i + 1) }"><td class="n">{{ i + 1 }}</td><td>{{ s.action }}</td><td>{{ s.expected }}</td></tr></tbody>
    </table>
    <dl class="kv cd-kv">
      <dt>关键词</dt><dd>{{ c.keywords || '—' }}</dd>
      <dt v-if="c.remark">备注</dt><dd v-if="c.remark">{{ c.remark }}</dd>
      <template v-for="([k, v]) in extrasList" :key="k"><dt>{{ k }}</dt><dd>{{ v }}</dd></template>
      <dt v-if="st === 'rejected'">驳回意见</dt><dd v-if="st === 'rejected'"><span v-for="t in (rv.reject_types || [])" :key="t" class="chip on cd-chip">{{ t }}</span> {{ rv.comment }}<div v-if="rv.fix_request" class="tiny">修改要求：{{ rv.fix_request }}</div><div v-if="rv.fix_note" class="tiny muted">备注：{{ rv.fix_note }}</div><div class="tiny muted">范围：{{ rv.fix_scope || '当前项' }}<template v-if="(rv.fields || []).length"> · 字段：{{ rv.fields.join('、') }}</template><template v-if="(rv.steps || []).length"> · 步骤：{{ rv.steps.join(',') }}</template></div><div v-if="rv.reject_count >= 2" class="attention cd-hint">已连续 {{ rv.reject_count }} 次未通过审核，建议检查需求是否有歧义、是否需要人工直接修改或补充需求信息</div><button v-if="canAi" type="button" class="btn small cd-fix" :disabled="busy" @click="emit('fix')">AI 定点修改驳回用例</button></dd>
      <dt>记录</dt><dd><template v-if="log.length"><span v-for="(l, i) in log" :key="i" class="tag cd-log" :title="l.comment || l.feedback || ''">{{ ACTS[l.action] || l.action }} · {{ l.by }} {{ fmtTime(l.at) }}</span></template><span v-else class="muted">无评审记录</span></dd>
    </dl>
    <div class="detail-actions">
      <template v-if="st === 'approved'">
        <button v-if="canReview" type="button" class="btn" :disabled="busy" @click="emit('unlock')">解锁（回待审核）</button>
      </template>
      <template v-else-if="st === 'draft'">
        <button v-if="canEdit" type="button" class="btn success solid" :disabled="busy" @click="emit('submit')"><kbd>A</kbd> 提交评审</button>
      </template>
      <template v-else>
        <button v-if="canReview" type="button" class="btn success solid" :disabled="busy" @click="emit('approve')"><kbd>A</kbd> 通过并锁定</button>
        <button v-if="canReview" type="button" class="btn danger" :disabled="busy" @click="mode === 'reject' ? emit('cancel') : emit('reject')"><kbd>R</kbd> 驳回</button>
      </template>
      <button v-if="canEdit" type="button" class="btn" :disabled="busy" @click="emit('edit')"><kbd>E</kbd> 修改</button>
      <button v-if="canEdit" type="button" class="btn" :disabled="busy" @click="emit('copy')">复制</button>
      <button v-if="canDelete" type="button" class="btn" :disabled="busy" @click="emit('delete')"><kbd>D</kbd> 删除</button>
      <span class="ai">AI 评审（reviewer）：{{ rv.reject_count ? `驳回 ${rv.reject_count} 次` : '通过' }}</span>
    </div>
    <WbRejectForm v-if="mode === 'reject'" kind="case" :label="`用例 ${c.case_id}`" :step-max="(c.steps || []).length" :busy="busy" @submit="v => emit('reject', v)" @cancel="emit('cancel')" />
  </template>
</template>
<style scoped>
.cd-holder { margin: 10px 0; }
.cd-edit { margin: 10px 0; display: flex; flex-direction: column; gap: 8px; }
.cd-edit .field, .cd-edit .select { width: 100%; }
.cd-title { font-size: 15px; font-weight: 700; height: 36px; }
.cd-grid { display: grid; grid-template-columns: 1fr 90px 1fr; gap: 8px; }
.cd-grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.cd-step { min-height: 44px; font-size: 12.5px; }
.cd-row { margin: 0; }
.cd-grow { flex: 1; }
.cd-kv { margin-top: 10px; }
.cd-chip { cursor: default; margin-right: 4px; }
.cd-hint { margin-top: 6px; }
.cd-fix { margin-top: 6px; }
.cd-log { margin: 0 4px 4px 0; }
.cd-hl td { background: #fff6e5; }
</style>

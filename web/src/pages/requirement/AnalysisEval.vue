<script setup>
/*
 需求分析评测（管理员）：评测集样本列表（查看 / 修订标准答案 PUT / 删除）、发起回归评测（POST /analysis-eval/runs）、评测记录列表（行点进 eval-run）。
*/
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtTime, fmtAgo, pct, num } from '@/utils/format'
import { PageHeading, TgPanel, TgTabs, TgTable, TgBadge, TgDialog, TgField, TgEmpty, TgDrawer } from '@/components/ui'
import { useModels } from './components/reqUtil'
const route = useRoute(), router = useRouter(), scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore(), models = useModels()
const tab = ref(route.query.tab || 'samples')
watch(tab, t => router.replace({ query: { ...route.query, tab: t === 'samples' ? undefined : t } }))
const { data, loading, error, reload } = useLoad(() => api.get('/api/v1/analysis-eval/samples', { project: scope.project || undefined }), { deps: [() => scope.project] })
const runs = useLoad(() => api.get('/api/v1/analysis-eval/runs', { limit: 50 }), { immediate: true })
let timer = null
watch(() => data.value?.running, r => { clearTimeout(timer); if (r) timer = setTimeout(() => { reload(true); runs.reload(true) }, 3000) })
onBeforeUnmount(() => clearTimeout(timer))
const labels = computed(() => data.value?.labels || {})
const selected = ref(new Set())
const runOpen = ref(false), runForm = ref({ model: '', reviewer_model: '', review: true }), starting = ref(false)
const sample = ref(null), sampleOpen = ref(false), golden = ref({}), notes = ref(''), saving = ref(false)
const viewing = ref(null)
const cols = [{ key: 'title', title: '需求' }, { key: 'project', title: '项目', w: '130px' }, { key: 'golden_count', title: '标准答案', w: '90px' }, { key: 'chars', title: '原文字数', w: '90px' }, { key: 'origin', title: '来源', w: '120px' }, { key: 'updated', title: '更新', w: '130px' }, { key: 'ops', title: '操作', w: '200px' }]
const runCols = [{ key: 'at', title: '时间', w: '140px' }, { key: 'status', title: '状态', w: '110px' }, { key: 'model', title: '模型 / 复核' }, { key: 'n', title: '样本', w: '80px' }, { key: 'accuracy', title: '准确率', w: '80px' }, { key: 'fabricated_rate', title: '捏造率', w: '80px' }, { key: 'wrong_rate', title: '理解错率', w: '80px' }, { key: 'missing_rate', title: '遗漏率', w: '80px' }, { key: 'pv', title: 'Prompt 版本' }, { key: 'by', title: '发起人', w: '80px' }]
const rate = v => v == null ? '—' : (v * 100).toFixed(1) + '%'
async function startRun() {
  starting.value = true
  try {
    const ids = [...selected.value]
    const r = await api.post('/api/v1/analysis-eval/runs', { project: scope.project || null, sample_ids: ids.length ? ids : null, model: runForm.value.model || null, reviewer_model: runForm.value.reviewer_model || null, review: runForm.value.review })
    ui.toast('评测已启动，后台逐样本执行'); runOpen.value = false; selected.value = new Set()
    router.push({ name: 'eval-run', params: { id: r.run_id } })
  } catch (e) { ui.error(e) } finally { starting.value = false }
}
async function openSample(s, edit) {
  try {
    const d = await api.get(`/api/v1/analysis-eval/samples/${s.sample_id}`)
    if (edit) { sample.value = d; golden.value = Object.fromEntries(Object.keys(d.labels || labels.value).filter(k => k !== 'open_questions').map(k => [k, (d.golden?.[k] || []).join('\n')])); notes.value = d.notes || ''; sampleOpen.value = true }
    else viewing.value = d
  } catch (e) { ui.error(e) }
}
async function saveSample() {
  saving.value = true
  try {
    const g = Object.fromEntries(Object.entries(golden.value).map(([k, v]) => [k, v.split('\n').map(x => x.trim()).filter(Boolean)]))
    await api.put(`/api/v1/analysis-eval/samples/${sample.value.sample_id}`, { golden: g, notes: notes.value })
    ui.toast('已保存标准答案'); sampleOpen.value = false; reload(true)
  } catch (e) { ui.error(e) } finally { saving.value = false }
}
async function delSample(s) {
  if (!await ui.confirm('删除评测样本', `只删除评测集中「${s.title}」的样本，不影响需求本身。确定？`, { ok: '删除', danger: true })) return
  try { await api.del(`/api/v1/analysis-eval/samples/${s.sample_id}`); ui.toast('已删除'); reload(true) } catch (e) { ui.error(e) }
}
</script>
<template>
  <PageHeading title="需求分析评测" sub="把「AI 理解需求的准确率」变成可反复测量的数字：评测集 = 需求原文 + 人工确认的标准答案；每次改 Prompt / 换模型跑一遍回归评测，比较准确率、捏造率、理解错率、遗漏率。">
    <template #actions>
      <button v-if="auth.isAdmin" type="button" class="btn primary" :disabled="!!data?.running || !(data?.samples || []).length" @click="runOpen = true">{{ data?.running ? `评测进行中 ${data.running.done}/${data.running.total}` : `运行回归评测${selected.size ? `（已选 ${selected.size}）` : scope.project ? '（本项目）' : ''}` }}</button>
    </template>
  </PageHeading>
  <div v-if="!auth.isAdmin" class="attention">需求分析评测仅系统管理员可用</div>
  <template v-else>
    <div v-if="error" class="err-box">{{ error.message }}</div>
    <div v-if="data?.running" class="information" style="margin-bottom:12px"><span class="spinner" /> 评测进行中 {{ data.running.done }}/{{ data.running.total }} <router-link class="link" :to="{ name: 'eval-run', params: { id: data.running.run_id } }">查看进度 →</router-link></div>
    <TgPanel :pad="false">
      <div style="padding:12px 14px 0"><TgTabs v-model="tab" :items="[{ key: 'samples', label: '评测集', count: (data?.samples || []).length }, { key: 'runs', label: '评测记录', count: (runs.data.value?.runs || []).length }, { key: 'metrics', label: '指标口径' }]" /></div>
      <template v-if="tab === 'samples'">
        <div class="tiny muted" style="padding:0 14px 10px">样本来源：需求详情 ③ 「加入评测集」；人工修改过分析结果再确认时{{ data?.auto_sample === false ? '不会自动加入（已关闭 TIANGONG_ANALYSIS_EVAL_AUTO_SAMPLE）' : '会自动加入' }}。勾选样本后可只对所选样本评测；建议 20～30 份覆盖主要业务的需求。</div>
        <TgTable :cols="cols" :rows="data?.samples || []" row-key="sample_id" :loading="loading" selectable v-model:selected="selected" empty="评测集为空" empty-hint="到需求详情的「AI 分析」步骤，把人工确认过的分析结果「加入评测集」">
          <template #cell-title="{ row }"><router-link class="link" :to="{ name: 'req-detail', params: { id: row.req_id, step: 3 } }"><b>{{ row.title }}</b></router-link><div v-if="row.notes" class="sub">{{ row.notes }}</div></template>
          <template #cell-golden_count="{ row }">{{ row.golden_count }} 条</template>
          <template #cell-chars="{ row }">{{ num(row.chars) }}</template>
          <template #cell-origin="{ row }">{{ row.origin === 'confirm' ? '确认时自动' : '手动加入' }}<div v-if="row.edits" class="sub">人工改 {{ row.edits }} 处</div></template>
          <template #cell-updated="{ row }"><span class="tiny muted">{{ row.updated_by || '' }} · {{ fmtAgo(row.updated_at) }}</span></template>
          <template #cell-ops="{ row }"><span class="row nowrap"><a class="link" @click="openSample(row, false)">查看</a><a class="link" @click="openSample(row, true)">修订标准答案</a><a class="link" style="color:var(--red)" @click="delSample(row)">删除</a></span></template>
        </TgTable>
      </template>
      <template v-else-if="tab === 'runs'">
        <TgTable :cols="runCols" :rows="runs.data.value?.runs || []" row-key="run_id" :loading="runs.loading.value" clickable empty="还没有评测记录" @row-click="r => router.push({ name: 'eval-run', params: { id: r.run_id } })">
          <template #cell-at="{ row }"><span class="tiny">{{ fmtTime(row.at) }}</span></template>
          <template #cell-status="{ row }"><TgBadge :status="row.status === 'running' ? 'running' : row.status === 'done' ? 'completed' : 'failed'" :text="row.status === 'running' ? `进行中 ${row.done}/${row.total}` : row.status === 'done' ? '完成' : '失败'" /></template>
          <template #cell-model="{ row }"><span class="tiny">{{ row.model || '默认' }}{{ row.review ? ` / 复核 ${row.reviewer_model || '同分析模型'}` : ' / 无复核' }}</span><div v-if="row.project" class="sub">项目 {{ row.project }}</div></template>
          <template #cell-n="{ row }">{{ row.done }}/{{ row.total }}<div v-if="row.failed" class="sub" style="color:var(--red)">失败 {{ row.failed }}</div></template>
          <template #cell-accuracy="{ row }"><b>{{ rate(row.totals?.accuracy) }}</b></template>
          <template #cell-fabricated_rate="{ row }">{{ rate(row.totals?.fabricated_rate) }}</template>
          <template #cell-wrong_rate="{ row }">{{ rate(row.totals?.wrong_rate) }}</template>
          <template #cell-missing_rate="{ row }">{{ rate(row.totals?.missing_rate) }}</template>
          <template #cell-pv="{ row }"><span class="tiny muted">{{ Object.entries(row.prompt_versions || {}).map(([k, v]) => `${k} v${v}`).join(' · ') || '—' }}</span></template>
          <template #cell-by="{ row }"><span class="tiny">{{ row.by || '' }}</span></template>
        </TgTable>
      </template>
      <div v-else class="panel-pad">
        <table class="table">
          <thead><tr><th style="width:110px">指标</th><th>定义</th></tr></thead>
          <tbody>
            <tr><td><b>准确率</b></td><td class="muted">matched / (标准答案条数 + 捏造条数)：AI 输出与标准答案语义一致（表述不同但事实相同）</td></tr>
            <tr><td><b>捏造率</b></td><td class="muted">extra / AI 输出条数：标准答案里没有对应事实的条目</td></tr>
            <tr><td><b>理解错率</b></td><td class="muted">wrong / (matched + wrong)：同一件事但数值 / 方向 / 条件 / 角色写错</td></tr>
            <tr><td><b>遗漏率</b></td><td class="muted">missing / 标准答案条数：标准答案有、AI 输出没有</td></tr>
          </tbody>
        </table>
        <div class="information mt">运行时重跑分析流水线（可指定分析模型 / 复核模型 / 是否独立复核），复核结论按「全部采纳」折算后由裁判 Prompt analysis_judge 逐条语义对齐；同一时间只允许一次评测，服务重启时中断的评测标记为失败。</div>
      </div>
    </TgPanel>
  </template>

  <TgDialog v-model="runOpen" title="运行回归评测" ok="开始评测" :busy="starting" @ok="startRun">
    <div class="information" style="margin-bottom:10px">范围：{{ selected.size ? `已选 ${selected.size} 个样本` : `${scope.project ? `项目「${scope.project}」` : '全部'} ${(data?.samples || []).length} 个样本` }}。后台串行执行，每个样本重跑分析 → 裁判对齐标准答案计分。</div>
    <div class="form-grid">
      <TgField label="分析模型"><select v-model="runForm.model" class="select"><option value="">默认（{{ models?.default_model || '' }}）</option><option v-for="m in models?.models || []" :key="m.name" :value="m.name">{{ m.name }}</option></select></TgField>
      <TgField label="独立复核模型"><select v-model="runForm.reviewer_model" class="select"><option value="">同分析模型</option><option v-for="m in models?.models || []" :key="m.name" :value="m.name">{{ m.name }}</option></select></TgField>
    </div>
    <label class="row"><input type="checkbox" v-model="runForm.review" /> 独立复核（与线上分析流程一致）</label>
  </TgDialog>
  <TgDialog v-model="sampleOpen" :title="`修订标准答案 · ${sample?.title || ''}`" ok="保存" :busy="saving" wide @ok="saveSample">
    <div class="tiny muted" style="margin-bottom:8px">每个分析项一行一条；全部清空不允许保存。</div>
    <TgField v-for="(label, k) in Object.fromEntries(Object.entries(sample?.labels || labels).filter(([x]) => x !== 'open_questions'))" :key="k" :label="`${label}（${(golden[k] || '').split('\n').filter(x => x.trim()).length} 条）`"><textarea v-model="golden[k]" class="field" :rows="Math.min(8, Math.max(2, (golden[k] || '').split('\n').length + 1))" /></TgField>
    <TgField label="备注"><input v-model="notes" class="field" placeholder="如：标准答案由产品确认 / 覆盖支付模块" /></TgField>
  </TgDialog>
  <TgDrawer :model-value="!!viewing" @update:model-value="v => { if (!v) viewing = null }" :title="viewing?.title || '样本'" :sub="`${viewing?.project || ''} · 标准答案 ${Object.values(viewing?.golden || {}).reduce((n, v) => n + v.length, 0)} 条 · ${viewing?.analysis_model || ''}`" wide>
    <template v-if="viewing">
      <h4>标准答案</h4>
      <template v-for="(label, k) in viewing.labels || labels" :key="k">
        <div v-if="(viewing.golden?.[k] || []).length" style="margin-bottom:8px"><b>{{ label }}</b><span class="tiny muted">（{{ viewing.golden[k].length }}）</span><ol style="margin:4px 0;padding-left:20px"><li v-for="(x, i) in viewing.golden[k]" :key="i">{{ x }}</li></ol></div>
      </template>
      <h4>需求原文</h4>
      <pre class="pre" style="max-height:none">{{ viewing.text }}</pre>
    </template>
  </TgDrawer>
</template>

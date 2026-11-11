<script setup>
/* 学习规则与记忆（10.1～10.15、9.20）：规则候选确认 / 忽略 / 编辑 / 删除、分析修改习惯；记忆增删改与清空；使用习惯默认值。 */
import { ref, computed, watch, reactive } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtTime, num } from '@/utils/format'
import { PageHeading, TgPanel, TgTable, TgBadge, TgSeg, TgTabs, TgDialog, TgField, TgEmpty, TgIcon, TgDropdown } from '@/components/ui'

const route = useRoute(), router = useRouter(), scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore()
const tab = ref(route.query.tab || 'rules')
const rStatus = ref(route.query.status || ''), mScope = ref(route.query.scope || '')
watch([tab, rStatus, mScope], () => router.replace({ query: { tab: tab.value !== 'rules' ? tab.value : undefined, status: rStatus.value || undefined, scope: mScope.value || undefined } }))

/* 规则（仅管理员可读） */
const { data: rd, loading: rLoading, error: rError, reload: rReload } = useLoad(() => auth.isAdmin ? api.get('/api/v1/learning/rules', { project: scope.project || undefined }) : Promise.resolve({ rules: [] }), { deps: [() => scope.project] })
const rules = computed(() => (rd.value?.rules || []).filter(r => !rStatus.value || r.status === rStatus.value))
const rCount = k => (rd.value?.rules || []).filter(r => r.status === k).length
const SCOPE = { system: '系统', team: '团队', project: '项目', module: '模块' }
const confirmScope = reactive({})
const rCols = [{ key: 'content', title: '规则' }, { key: 'scope', title: '范围', w: '120px' }, { key: 'occ', title: '出现 / 置信', w: '100px' }, { key: 'status', title: '状态', w: '80px' }, { key: 'updated_at', title: '更新', w: '110px' }, { key: 'ops', title: '操作', w: '240px' }]
async function confirmRule(r) {
  const sc = confirmScope[r.rule_id] || r.scope || 'project'
  if ((sc === 'project' || sc === 'module') && !(r.project || scope.project)) return ui.toast('项目 / 模块级规则需要项目，请先在顶栏选择项目', 'warn')
  try { await api.post(`/api/v1/learning/rules/${r.rule_id}/confirm`, { scope: sc, project: sc === 'system' || sc === 'team' ? null : (r.project || scope.project), module: sc === 'module' ? (r.module || scope.module || null) : null }); ui.toast('规则已生效'); rReload(true) } catch (e) { ui.error(e) }
}
async function ignoreRule(r) { try { await api.post(`/api/v1/learning/rules/${r.rule_id}/ignore`, {}); ui.toast('已忽略'); rReload(true) } catch (e) { ui.error(e) } }
async function deleteRule(r) {
  if (!await ui.confirm('删除规则？', r.content.slice(0, 80), { ok: '删除', danger: true })) return
  try { await api.del(`/api/v1/learning/rules/${r.rule_id}`); ui.toast('已删除'); rReload(true) } catch (e) { ui.error(e) }
}
const ruleDlg = ref(false), ruleForm = reactive({ id: '', content: '' })
function editRule(r) { ruleForm.id = r.rule_id; ruleForm.content = r.content; ruleDlg.value = true }
async function saveRule() { try { await api.put(`/api/v1/learning/rules/${ruleForm.id}`, { content: ruleForm.content }); ui.toast('已保存'); ruleDlg.value = false; rReload(true) } catch (e) { ui.error(e) } }
const analyzing = ref(false), analyzeDlg = ref(false), analyzeModel = ref('')
const { data: models } = useLoad(() => api.get('/api/v1/models'))
async function analyze() {
  analyzing.value = true; analyzeDlg.value = false
  try {
    const r = await api.post('/api/v1/learning/analyze', { project: scope.project || null, model: analyzeModel.value || null })
    const by = (r.by_project || []).map(x => `${x.project || '无项目'} ${x.samples} 样本 → ${x.added} 候选${x.error ? '（失败）' : ''}`).join('；')
    ui.toast(r.message || `样本 ${r.samples} 条 → 新候选 ${(r.candidates || []).length} 条${by ? '：' + by : ''}`, { duration: 8000 })
    rReload(true); tab.value = 'rules'; rStatus.value = 'candidate'
  } catch (e) { ui.error(e) } finally { analyzing.value = false }
}

/* 记忆 */
const { data: md, loading: mLoading, error: mError, reload: mReload } = useLoad(() => api.get('/api/v1/memories', { scope: mScope.value || undefined, project: scope.project || undefined }), { deps: [() => scope.project, mScope] })
const mems = computed(() => md.value?.memories || [])
const defaults = computed(() => md.value?.defaults || {})
const mCols = [{ key: 'content', title: '记忆' }, { key: 'scope', title: '维度', w: '140px' }, { key: 'kind', title: '类型', w: '110px' }, { key: 'source', title: '来源', w: '110px' }, { key: 'inject', title: '注入', w: '120px' }, { key: 'hits', title: '命中', w: '60px', align: 'right' }, { key: 'updated_at', title: '更新', w: '110px' }, { key: 'ops', title: '操作', w: '110px' }]
const memDlg = ref(false), memForm = reactive({ id: '', content: '', scope: 'user', project: '' })
function newMem() { Object.assign(memForm, { id: '', content: '', scope: scope.project ? 'project' : 'user', project: scope.project || '' }); memDlg.value = true }
function editMem(m) { Object.assign(memForm, { id: m.memory_id, content: m.content, scope: m.scope, project: m.project || '' }); memDlg.value = true }
async function saveMem() {
  if (!memForm.content.trim()) return ui.toast('内容不能为空', 'warn')
  if (memForm.scope === 'project' && !memForm.project) return ui.toast('项目记忆需要指定项目', 'warn')
  try {
    if (memForm.id) await api.put(`/api/v1/memories/${memForm.id}`, { content: memForm.content })
    else await api.post('/api/v1/memories', { content: memForm.content, scope: memForm.scope, project: memForm.scope === 'project' ? memForm.project : null })
    ui.toast(memForm.id ? '记忆已改写' : '已新增记忆'); memDlg.value = false; mReload(true)
  } catch (e) { ui.error(e) }
}
async function deleteMem(m) {
  if (!await ui.confirm('删除记忆？', m.content.slice(0, 80), { ok: '删除', danger: true })) return
  try { await api.del(`/api/v1/memories/${m.memory_id}`); ui.toast('已删除'); mReload(true) } catch (e) { ui.error(e) }
}
async function clearMems() {
  const what = `${mScope.value === 'user' ? '用户偏好' : mScope.value === 'project' ? '项目记忆' : '全部记忆'}${scope.project ? `（项目 ${scope.project}）` : ''}`
  if (!await ui.confirm(`一键清空${what}？`, '清用户偏好时同步清零使用计数；不可恢复。', { ok: '清空', danger: true })) return
  try { const r = await api.del('/api/v1/memories', undefined, { query: { scope: mScope.value || undefined, project: scope.project || undefined } }); ui.toast(`已清空 ${r.cleared} 条`); mReload(true) } catch (e) { ui.error(e) }
}
</script>
<template>
  <PageHeading title="学习规则与记忆" sub="人工修改留痕 → 按项目分组提炼规则候选 → 确认适用范围后生效，按 800 字预算注入生成节点；记忆记录用户偏好 / 项目记忆与使用习惯，按 600 字预算注入。">
    <template #actions>
      <template v-if="auth.isAdmin">
        <button v-if="tab === 'memories'" type="button" class="btn primary" @click="newMem"><TgIcon name="plus" :size="14" /> 新增记忆</button>
        <button v-else type="button" class="btn primary" :disabled="analyzing" @click="analyzeDlg = true"><TgIcon name="spark" :size="14" /> {{ analyzing ? '分析中…' : '分析修改习惯' }}</button>
        <TgDropdown :items="[{ label: '新增记忆', action: newMem }, { label: '分析修改习惯', action: () => analyzeDlg = true }, { divider: true }, { label: '一键清空记忆', action: clearMems, danger: true }]" />
      </template>
    </template>
  </PageHeading>
  <div v-if="rError" class="err-box">{{ rError.message }}</div>
  <div v-if="mError" class="err-box">{{ mError.message }}</div>
  <TgPanel :pad="false">
    <div class="panel-pad" style="padding-bottom:0"><TgTabs v-model="tab" :items="[{ key: 'rules', label: '学习规则', count: auth.isAdmin ? (rd?.rules || []).length : undefined }, { key: 'memories', label: '记忆', count: mems.length }, { key: 'usage', label: '使用习惯' }]" /></div>
    <template v-if="tab === 'rules'">
      <div v-if="!auth.isAdmin" class="panel-pad"><TgEmpty title="学习规则仅管理员可见" hint="规则注入生成走服务端内部逻辑，不受影响。" /></div>
      <template v-else>
        <div class="panel-pad filters" style="padding-bottom:10px">
          <TgSeg v-model="rStatus" :items="[{ key: '', label: '全部' }, { key: 'candidate', label: '候选', count: rCount('candidate') }, { key: 'active', label: '生效', count: rCount('active') }, { key: 'ignored', label: '已忽略', count: rCount('ignored') }]" />
          <span class="tiny muted">{{ scope.project ? `项目：${scope.project}（含全局规则）` : '全部项目' }}</span>
        </div>
        <TgTable :cols="rCols" :rows="rules" row-key="rule_id" :loading="rLoading" empty="还没有规则" empty-hint="在测试点 / 用例审核中留下修改后，点「分析修改习惯」提炼候选。">
          <template #cell-content="{ row }"><div class="small">{{ row.content }}</div><div v-if="row.evidence" class="sub">依据：{{ row.evidence.slice(0, 160) }}</div></template>
          <template #cell-scope="{ row }"><span class="tag">{{ SCOPE[row.scope] || row.scope }}</span><div v-if="row.project" class="sub">{{ row.project }}{{ row.module ? ' / ' + row.module : '' }}</div></template>
          <template #cell-occ="{ row }">{{ row.occurrences }} 次 · {{ row.confidence }}</template>
          <template #cell-status="{ row }"><TgBadge :status="row.status" /></template>
          <template #cell-updated_at="{ row }">{{ fmtTime(row.updated_at) }}</template>
          <template #cell-ops="{ row }">
            <span v-if="row.status === 'candidate'" class="row">
              <select class="select small" :value="confirmScope[row.rule_id] || row.scope || 'project'" @change="confirmScope[row.rule_id] = $event.target.value"><option v-for="(l, k) in SCOPE" :key="k" :value="k">{{ l }}</option></select>
              <a class="link" @click="confirmRule(row)">确认生效</a><a class="link muted" @click="ignoreRule(row)">忽略</a>
            </span>
            <span v-else class="row"><a class="link" @click="editRule(row)">修改</a><a v-if="row.status === 'ignored'" class="link" @click="confirmRule(row)">恢复生效</a><a class="link" style="color:#db5361" @click="deleteRule(row)">删除</a></span>
          </template>
        </TgTable>
        <div class="table-footer"><span>候选 / 生效 / 忽略；项目改名时 project 字段同步，项目连同内容删除时清掉项目级 / 模块级规则；报表汇总带候选 / 生效计数</span></div>
      </template>
    </template>
    <template v-else-if="tab === 'memories'">
      <div class="panel-pad filters" style="padding-bottom:10px">
        <TgSeg v-model="mScope" :items="[['', '全部'], ['user', '用户偏好'], ['project', '项目记忆']]" />
        <span class="tiny muted">{{ scope.project ? `项目：${scope.project}` : '全部项目' }}</span>
        <span v-if="defaults.template_id || defaults.model" class="tiny muted">· 使用习惯默认值：{{ defaults.template_id ? `模板 ${defaults.template_id}` : '' }} {{ defaults.model ? `模型 ${defaults.model}` : '' }}</span>
      </div>
      <TgTable :cols="mCols" :rows="mems" row-key="memory_id" :loading="mLoading" empty="还没有记忆" empty-hint="管理员可手工新增；成员的使用习惯由系统自动沉淀。">
        <template #cell-content="{ row }"><div class="small">{{ row.content }}</div></template>
        <template #cell-scope="{ row }"><span class="tag">{{ row.scope === 'user' ? '用户偏好' : '项目记忆' }}</span><span v-if="row.project" class="tiny" style="margin-left:4px">{{ row.project }}</span></template>
        <template #cell-kind="{ row }"><span class="mono tiny">{{ row.kind || '—' }}</span></template>
        <template #cell-source="{ row }">{{ row.source === 'usage' ? '使用习惯自动沉淀' : '手工' }}</template>
        <template #cell-inject="{ row }"><span v-if="row.inject" class="ok">注入生成 Prompt</span><span v-else class="muted">仅作界面默认值</span></template>
        <template #cell-hits="{ row }">{{ num(row.hits) }}</template>
        <template #cell-updated_at="{ row }">{{ fmtTime(row.updated_at) }}</template>
        <template #cell-ops="{ row }"><span v-if="auth.isAdmin" class="row"><a class="link" @click="editMem(row)">修改</a><a class="link" style="color:#db5361" @click="deleteMem(row)">删除</a></span><span v-else class="muted">—</span></template>
      </TgTable>
      <div class="table-footer"><span>按需求相关度在 600 字预算内挑记忆拼进生成 Prompt，快照写入任务记录</span><button v-if="auth.isAdmin" type="button" class="btn danger small" @click="clearMems">一键清空</button></div>
    </template>
    <div v-else class="panel-pad">
      <div class="grid-2">
        <TgPanel title="默认偏好（界面默认值）" hint="常用模板 / 常用模型使用满阈值次固化，任务创建时预选">
          <div class="list"><div><b>默认模板</b><div class="tiny muted">{{ defaults.template_id || '尚未固化（TIANGONG_MEMORY_PREF_THRESHOLD 次后生效）' }}</div></div><div><b>默认模型</b><div class="tiny muted">{{ defaults.model || '尚未固化' }}</div></div></div>
        </TgPanel>
        <TgPanel title="自动沉淀的记忆" :hint="`${mems.filter(m => m.source === 'usage').length} 条`">
          <div class="list"><div v-for="m in mems.filter(m => m.source === 'usage').slice(0, 8)" :key="m.memory_id" class="small"><span class="mono tiny">{{ m.kind }}</span> {{ m.content }}</div><div v-if="!mems.some(m => m.source === 'usage')" class="muted small">暂无</div></div>
        </TgPanel>
      </div>
      <div class="information" style="margin-top:10px"><TgIcon name="info" :size="14" /> 任务创建 / 需求测试设计 / 修订时服务端自动计数；跨任务重复的修订指令固化为注入型偏好；清用户偏好时同步清零使用计数。</div>
    </div>
  </TgPanel>

  <TgDialog v-model="ruleDlg" title="修改规则内容" ok="保存" @ok="saveRule"><TgField label="规则" required><textarea v-model="ruleForm.content" class="field" rows="5" /></TgField></TgDialog>
  <TgDialog v-model="memDlg" :title="memForm.id ? '修改记忆（纠错）' : '新增记忆'" ok="保存" @ok="saveMem">
    <TgField label="内容" required><textarea v-model="memForm.content" class="field" rows="5" placeholder="如：本项目用例标题统一以「场景-动作-结果」命名" /></TgField>
    <template v-if="!memForm.id">
      <TgField label="维度"><select v-model="memForm.scope" class="select"><option value="user">用户偏好</option><option value="project">项目记忆</option></select></TgField>
      <TgField v-if="memForm.scope === 'project'" label="项目" required><select v-model="memForm.project" class="select"><option v-for="p in scope.projects" :key="p.project" :value="p.project">{{ p.project }}</option></select></TgField>
    </template>
  </TgDialog>
  <TgDialog v-model="analyzeDlg" title="分析修改习惯" ok="开始分析" @ok="analyze">
    <div class="information" style="margin-bottom:10px"><span>收集{{ scope.project ? `项目「${scope.project}」` : '全部项目' }}的人工修改留痕样本，按项目分组各调一次 learning Prompt 提炼规则候选；候选须人工确认后才生效。会调用一次模型。</span></div>
    <TgField label="模型" hint="留空用默认模型"><select v-model="analyzeModel" class="select"><option value="">默认（{{ models?.default_model }}）</option><option v-for="m in models?.models || []" :key="m.name" :value="m.name">{{ m.name }}</option></select></TgField>
  </TgDialog>
</template>

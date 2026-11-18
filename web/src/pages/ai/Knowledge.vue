<script setup>
/* 知识库（9.1～9.9、9.13、9.15）：检索测试、知识台账（三层范围）、文档详情抽屉、手工导入、回溯沉淀、注入规则说明。 */
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, seg } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtTime, num, shortId } from '@/utils/format'
import { PageHeading, TgPanel, TgTable, TgBadge, TgSeg, TgTabs, TgDrawer, TgKv, TgEmpty, TgIcon, TgDropdown } from '@/components/ui'
import KnowledgeImportDialog from './components/KnowledgeImportDialog.vue'

const route = useRoute(), router = useRouter(), scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore()
const tab = ref(route.query.tab || 'docs')
const level = ref(route.query.level || ''), category = ref(route.query.category || '')
const docId = ref(route.query.doc || '')
watch([tab, level, category, docId], () => router.replace({ query: { ...route.query, tab: tab.value !== 'docs' ? tab.value : undefined, level: level.value || undefined, category: category.value || undefined, doc: docId.value || undefined } }))

const { data: meta } = useLoad(() => api.get('/api/v1/knowledge/categories'))
const CAT = computed(() => meta.value?.categories || {})
const LV = { public: '公共', project: '项目', module: '模块' }

/* 台账 */
const { data, loading, error, reload } = useLoad(
  () => api.get('/api/v1/knowledge/docs', { space: scope.project || undefined, level: level.value || undefined, category: category.value || undefined }),
  { deps: [() => scope.project, level, category] },
)
const docs = computed(() => data.value?.documents || [])
const cols = [{ key: 'source', title: '来源' }, { key: 'level', title: '层级', w: '70px' }, { key: 'space', title: '项目 / 模块' }, { key: 'category', title: '分类' }, { key: 'chunk_count', title: '切片', w: '60px', align: 'right' }, { key: 'embedding_model', title: 'Embedding' }, { key: 'created_at', title: '时间', w: '120px' }, { key: 'ops', title: '操作', w: '110px' }]
const canDelete = d => d.level === 'public' || d.space === 'public' || d.space === 'default' ? auth.isAdmin : (auth.isAdmin || (auth.accessMode === 'member' && auth.can(d.space, 'knowledge.manage')))
async function remove(d) {
  if (!await ui.confirm('删除知识文档？', `「${d.source}」的台账与全部 ${d.chunk_count} 个切片向量将被删除，不可恢复。`, { ok: '删除', danger: true })) return
  try { await api.del(`/api/v1/knowledge/docs/${d.doc_id}`); ui.toast('已删除'); if (docId.value === d.doc_id) docId.value = ''; reload(true) } catch (e) { ui.error(e) }
}

/* 文档详情抽屉 */
const doc = ref(null), docLoading = ref(false)
watch(docId, async id => {
  doc.value = null
  if (!id) return
  docLoading.value = true
  try { doc.value = await api.get(`/api/v1/knowledge/docs/${id}`) } catch (e) { ui.error(e); docId.value = '' } finally { docLoading.value = false }
}, { immediate: true })

/* 检索测试 */
const q = ref(''), qCat = ref(''), qSpace = ref(scope.project || ''), topK = ref(5), hits = ref(null), searching = ref(false)
watch(() => scope.project, v => { qSpace.value = v || '' })
async function search() {
  if (!q.value.trim()) return ui.toast('请输入检索内容', 'warn')
  searching.value = true
  try { hits.value = (await api.post('/api/v1/knowledge/search', { query: q.value, top_k: +topK.value || 5, category: qCat.value || null, space: qSpace.value || null })).hits || [] } catch (e) { ui.error(e) } finally { searching.value = false }
}

/* 导入 */
const importOpen = ref(false)
const canImport = computed(() => auth.isAdmin || scope.projects.some(p => auth.can(p.project, 'knowledge.manage')))

/* 回溯沉淀（管理员） */
const bf = ref(null), bfLoading = ref(false)
let bfTimer = null
async function loadBackfill() {
  if (!auth.isAdmin) return
  bfLoading.value = true
  try { bf.value = await api.get('/api/v1/knowledge/backfill') } catch (e) { ui.error(e) } finally { bfLoading.value = false }
  clearTimeout(bfTimer)
  if (bf.value?.running && tab.value === 'backfill') bfTimer = setTimeout(loadBackfill, 5000)
}
watch(tab, v => { if (v === 'backfill') loadBackfill(); else clearTimeout(bfTimer) }, { immediate: true })
onBeforeUnmount(() => clearTimeout(bfTimer))
async function backfillAll() {
  if (!await ui.confirm('回溯全部项目？', '把存量任务 / 需求按同样规则补进知识库，后台串行执行、可重复；跳过已关闭自动沉淀、没有历史产物或正在回溯中的项目。', { ok: '开始回溯' })) return
  try { const r = await api.post('/api/v1/knowledge/backfill', {}); ui.toast(`已发起 ${r.projects.length} 个项目的回溯${r.skipped.length ? `，跳过 ${r.skipped.length} 个` : ''}`); tab.value = 'backfill'; loadBackfill() } catch (e) { ui.error(e) }
}
async function backfillOne(name) {
  try { await api.post(`/api/v1/projects/${seg(name)}/knowledge/backfill`, {}); ui.toast(`已发起「${name}」回溯`); loadBackfill() } catch (e) { ui.error(e) }
}
const INJECT = [['历史用例', 'knowledge_cases_block', 1, 1, 0, 1, 1, 1, 0], ['需求文档 / 业务规则', 'knowledge_refs_block', 1, 0, 1, 1, 0, 0, 1], ['历史缺陷', 'knowledge_refs_block', 1, 0, 1, 0, 0, 0, 0], ['项目画像', 'profile_block', 1, 1, 1, 1, 1, 1, 1], ['记忆', 'memory_block', 0, 0, 0, 1, 1, 0, 0], ['学习规则', 'rules_block', 0, 0, 0, 1, 1, 0, 0]]
</script>
<template>
  <PageHeading title="知识库" sub="四类知识（历史用例 / 需求文档 / 业务规则 / 历史缺陷）× 三层范围（公共 / 项目 / 模块）；600 字切片 → text-embedding-3-large 3072 维 → 向量 + BM25 RRF 混合检索。">
    <template #actions>
      <button v-if="canImport" type="button" class="btn primary" @click="importOpen = true"><TgIcon name="upload" :size="14" /> 导入知识</button>
      <TgDropdown v-if="auth.isAdmin" :items="[{ label: '回溯全部项目', action: backfillAll }, { label: '刷新台账', action: () => reload() }]" />
      <button v-else type="button" class="btn" @click="reload()"><TgIcon name="refresh" :size="14" /> 刷新</button>
    </template>
  </PageHeading>
  <div v-if="error" class="err-box">{{ error.message }}</div>

  <TgPanel class="mb" title="知识检索" hint="向量 + 字符二元组 BM25 的 RRF 混合检索；范围 = 指定项目 + 同产品线项目 + 公共层（非管理员未指定项目时只检索公共层）">
    <div class="row">
      <input v-model="q" class="field" style="flex:1;min-width:240px" placeholder="输入需求片段 / 规则 / 场景关键词" @keyup.enter="search" />
      <select v-model="qCat" class="select"><option value="">全部分类</option><option v-for="(v, k) in CAT" :key="k" :value="k">{{ v }}</option></select>
      <select v-model="qSpace" class="select"><option value="">{{ auth.isAdmin ? '全部空间' : '仅公共层' }}</option><option value="public">公共层</option><option v-for="p in scope.projects" :key="p.project" :value="p.project">{{ p.project }}</option></select>
      <select v-model="topK" class="select"><option v-for="n in [3, 5, 10, 20]" :key="n" :value="n">Top {{ n }}</option></select>
      <button type="button" class="btn primary" :disabled="searching" @click="search">{{ searching ? '检索中…' : '检索' }}</button>
    </div>
    <div v-if="hits" style="margin-top:10px">
      <TgTable :cols="[{ key: 'score', title: '相似度', w: '80px' }, { key: 'source', title: '来源' }, { key: 'level', title: '层级', w: '70px' }, { key: 'category', title: '分类', w: '110px' }, { key: 'text', title: '切片' }]" :rows="hits" clickable empty="没有命中" empty-hint="换个说法，或先导入知识" @row-click="h => docId = h.doc_id">
        <template #cell-score="{ row }"><b>{{ (row.score * 100).toFixed(0) }}%</b></template>
        <template #cell-source="{ row }">{{ row.source }} <span class="tag">{{ row.space }}{{ row.module ? ' / ' + row.module : '' }}</span><div class="sub">第 {{ row.chunk_index + 1 }} 片</div></template>
        <template #cell-level="{ row }"><span class="tag">{{ LV[row.level] || row.level }}</span></template>
        <template #cell-category="{ row }"><span class="badge blue">{{ CAT[row.category] || row.category }}</span></template>
        <template #cell-text="{ row }"><span class="small">{{ row.text.slice(0, 200) }}{{ row.text.length > 200 ? '…' : '' }}</span></template>
      </TgTable>
    </div>
  </TgPanel>

  <TgPanel :pad="false">
    <div class="panel-pad" style="padding-bottom:0"><TgTabs v-model="tab" :items="[{ key: 'docs', label: '知识台账', count: docs.length }, ...(auth.isAdmin ? [{ key: 'backfill', label: '回溯沉淀', count: bf?.items?.length }] : []), { key: 'inject', label: '注入规则' }]" /></div>
    <template v-if="tab === 'docs'">
      <div class="panel-pad filters" style="padding-bottom:10px">
        <TgSeg v-model="level" :items="[['', '全部层级'], ['public', '公共'], ['project', '项目'], ['module', '模块']]" />
        <select v-model="category" class="select"><option value="">全部分类</option><option v-for="(v, k) in CAT" :key="k" :value="k">{{ v }}</option></select>
        <span class="tiny muted">{{ scope.project ? `空间：${scope.project} + 公共层` : '全部可见空间' }}</span>
      </div>
      <TgTable :cols="cols" :rows="docs" row-key="doc_id" :loading="loading" clickable :active-key="docId || null" empty="还没有知识" empty-hint="手工导入，或在项目设置开启自动沉淀后由任务 / 需求自动入库。" @row-click="d => docId = d.doc_id">
        <template #cell-source="{ row }"><div style="font-weight:650">{{ row.source }}</div><div class="sub">{{ row.origin_label }}<template v-if="row.origin_ref"> · <router-link v-if="row.origin === 'task'" :to="{ name: 'design-detail', params: { id: row.origin_ref } }" @click.stop>{{ shortId(row.origin_ref, 12) }}</router-link><router-link v-else-if="row.origin === 'requirement'" :to="{ name: 'req-detail', params: { id: row.origin_ref } }" @click.stop>{{ row.origin_ref }}</router-link><span v-else class="mono">{{ row.origin_ref }}</span></template> · {{ row.created_by || '—' }}</div></template>
        <template #cell-level="{ row }"><span class="tag">{{ LV[row.level] || row.level }}</span></template>
        <template #cell-space="{ row }">{{ row.space === 'default' ? '公共（历史）' : row.space }}{{ row.module ? ' / ' + row.module : '' }}</template>
        <template #cell-category="{ row }"><span class="badge blue">{{ CAT[row.category] || row.category }}</span></template>
        <template #cell-chunk_count="{ row }">{{ num(row.chunk_count) }}</template>
        <template #cell-embedding_model="{ row }"><span class="small">{{ row.embedding_model }}</span></template>
        <template #cell-created_at="{ row }">{{ fmtTime(row.created_at) }}</template>
        <template #cell-ops="{ row }"><span @click.stop><a class="link" @click="docId = row.doc_id">查看</a><a v-if="canDelete(row)" class="link" style="margin-left:10px;color:#db5361" @click="remove(row)">删除</a></span></template>
      </TgTable>
      <div class="table-footer"><span>{{ docs.length }} 条 · 公共层仅管理员可入库 / 删除；项目层按可见项目过滤，删除按可见范围模式收口</span></div>
    </template>
    <template v-else-if="tab === 'backfill'">
      <div class="table-wrap" :class="{ loading: bfLoading }"><table class="table">
        <thead><tr><th>项目</th><th>自动沉淀</th><th>可回溯</th><th>状态</th><th>进度</th><th>来源</th><th>错误</th><th>发起</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="it in (bf?.items || [])" :key="it.project">
            <td><b>{{ it.project }}</b></td>
            <td>{{ it.knowledge_auto ? '开' : '关' }}</td>
            <td>{{ it.targets.tasks }} 任务 · {{ it.targets.requirements }} 需求</td>
            <td><TgBadge v-if="it.last" :status="it.last.status === 'done' ? 'completed' : it.last.status === 'running' ? 'running' : it.last.status" /><span v-else class="muted">未回溯</span></td>
            <td><template v-if="it.last">{{ it.last.done }} / {{ it.last.total }} <div class="bar" style="width:90px"><span :style="{ width: (it.last.total ? it.last.done / it.last.total * 100 : 0) + '%' }" /></div><div v-if="it.last.current" class="sub">{{ it.last.current }}</div></template><span v-else class="muted">—</span></td>
            <td class="small">{{ it.last ? `${it.last.tasks} 任务 · ${it.last.requirements} 需求 · ${it.last.docs} 文档` : '—' }}</td>
            <td><span v-if="it.last?.errors" style="color:#db5361">{{ it.last.errors }}</span><span v-else>0</span><div v-if="it.last?.error" class="sub" style="color:#db5361">{{ it.last.error }}</div></td>
            <td class="small">{{ it.last ? `${it.last.by || '—'} · ${fmtTime(it.last.started_at)}${it.last.finished_at ? ' → ' + fmtTime(it.last.finished_at) : ''}` : '—' }}</td>
            <td><button type="button" class="btn small" :disabled="it.last?.status === 'running' || !it.knowledge_auto || !(it.targets.tasks || it.targets.requirements)" @click="backfillOne(it.project)">{{ it.last ? '重新回溯' : '回溯' }}</button></td>
          </tr>
          <tr v-if="!(bf?.items || []).length"><td colspan="9"><TgEmpty :title="bfLoading ? '加载中…' : '没有项目'" /></td></tr>
        </tbody>
      </table></div>
      <div class="table-footer"><span>把存量任务 / 需求按同样规则补进知识库，后台串行执行、可重复；errors 多为 applied 终稿任务跳过。完成后按条件自动提炼一版项目画像草稿。</span></div>
    </template>
    <div v-else class="panel-pad">
      <div class="table-wrap"><table class="table matrix dense">
        <thead><tr><th>注入内容</th><th>Prompt 块</th><th>拆解</th><th>经验延伸</th><th>查漏</th><th>全量生成</th><th>定点修正</th><th>评审</th><th>需求分析</th></tr></thead>
        <tbody><tr v-for="r in INJECT" :key="r[0]"><td><b>{{ r[0] }}</b></td><td><span class="mono">{{ r[1] }}</span></td><td v-for="(x, i) in r.slice(2)" :key="i"><span v-if="x" class="ok">✓</span><span v-else class="no">—</span></td></tr></tbody>
      </table></div>
      <div class="information" style="margin-top:10px"><TgIcon name="info" :size="14" /> 配额：知识 6000 字（用例 : 文档 : 规则 = 5 : 3 : 2，可让渡）· 画像 2500 字 · 记忆 600 字 · 规则 800 字。查询串 = 需求前 750 字 + 全部标题行，≤ 1500 字。历史用例复用提示：相似度 ≥ 0.78 最多 3 条。知识快照写入任务记录（任务概览 › 上下文与注入）。</div>
    </div>
  </TgPanel>

  <KnowledgeImportDialog v-model="importOpen" :categories="CAT" @done="reload(true)" />

  <TgDrawer :model-value="!!docId" :title="doc ? doc.source : '知识文档'" :sub="doc ? `${doc.level_label} · ${doc.space === 'default' ? '公共（历史）' : doc.space}${doc.module ? ' / ' + doc.module : ''} · ${doc.category_label}` : ''" wide @update:model-value="docId = ''">
    <template #head-actions><button v-if="doc && canDelete(doc)" type="button" class="btn small danger" @click="remove(doc)">删除</button></template>
    <div v-if="docLoading" class="page-loading"><span class="spinner" /> 加载中…</div>
    <template v-else-if="doc">
      <TgKv :pairs="[['doc_id', doc.doc_id], ['来源方式', `${doc.origin_label}${doc.origin_ref ? ' · ' + doc.origin_ref : ''}`], ['入库人', doc.created_by || '—'], ['时间', fmtTime(doc.created_at, true)], ['Embedding', doc.embedding_model], ['切片', `${doc.chunk_count} 片 · ${num(doc.total_chars)} 字`]]" />
      <h4>切片原文（向量库里实际保存的内容）</h4>
      <div v-for="c in doc.chunks" :key="c.chunk_index" style="margin-bottom:10px">
        <div class="tiny muted">第 {{ c.chunk_index + 1 }} 片 · {{ c.chars }} 字</div>
        <div class="pre" style="max-height:none">{{ c.text }}</div>
      </div>
    </template>
  </TgDrawer>
</template>

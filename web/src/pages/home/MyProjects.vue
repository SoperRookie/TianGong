<script setup>
/* 我的项目（3.1 / 3.2 / 3.6）：GET /projects 行（含 my_role / favorite / last_visited / 统计）；收藏置顶，最近访问排前；点击进项目详情。 */
import { ref, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtAgo, num } from '@/utils/format'
import { useProjectActions, roleLabel } from '@/pages/project/components/useProjectActions'
import ProjectDialog from '@/pages/project/components/ProjectDialog.vue'
import { PageHeading, TgPanel, TgStat, TgTable, TgBadge, TgSeg, TgIcon } from '@/components/ui'
const route = useRoute(), router = useRouter(), scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore()
const { canCreateProject, toggleFavorite } = useProjectActions()
const loading = ref(false), dlg = ref(false), view = ref(route.query.view === 'cards' ? 'cards' : 'list'), onlyActive = ref(true)
async function load() { loading.value = true; try { await scope.load(true) } catch (e) { ui.error(e) } finally { loading.value = false } }
load()
const mine = computed(() => scope.projects.filter(p => !p.builtin && (!onlyActive.value || p.status !== 'archived')))
const rows = computed(() => mine.value.slice().sort((a, b) => (b.favorite - a.favorite) || ((b.last_visited || '') > (a.last_visited || '') ? 1 : (b.last_visited || '') < (a.last_visited || '') ? -1 : 0) || ((b.last_activity || '') > (a.last_activity || '') ? 1 : -1)))
const favs = computed(() => mine.value.filter(p => p.favorite).length)
const recent = computed(() => mine.value.filter(p => p.last_visited).length)
const adminOf = computed(() => mine.value.filter(p => p.my_role === 'project_admin').length)
const pendingTotal = computed(() => mine.value.reduce((s, p) => s + (p.pending || 0), 0))
const cols = [{ key: 'fav', title: '', w: '30px' }, { key: 'project', title: '项目' }, { key: 'my_role', title: '我的角色', w: '110px' }, { key: 'tasks', title: '任务', w: '60px' }, { key: 'cases', title: '用例 / 正式', w: '100px' }, { key: 'pending', title: '待审', w: '60px' }, { key: 'exec', title: '执行', w: '90px' }, { key: 'last_visited', title: '最近访问', w: '100px' }, { key: 'status', title: '状态', w: '80px' }]
const modeLabel = computed(() => ({ open: '开放（全员可见）', line: '按产品线成员', member: '按项目成员' }[auth.accessMode] || auth.accessMode))
function open(p) { router.push({ name: 'project-detail', params: { name: p.project } }) }
async function fav(p) { try { await toggleFavorite(p.project) } catch (e) { ui.error(e) } }
function setView(v) { view.value = v; router.replace({ query: { ...route.query, view: v === 'list' ? undefined : v } }) }
</script>
<template>
  <PageHeading title="我的项目" :sub="`${mine.length} 个可见项目 · 收藏置顶，最近访问排前（最多记 10 条）；点击进入项目详情。可见范围模式：${modeLabel}。`">
    <template #actions>
      <button type="button" class="btn" @click="load"><TgIcon name="refresh" :size="14" /> 刷新</button>
      <button v-if="canCreateProject" type="button" class="btn primary" @click="dlg = true"><TgIcon name="plus" :size="14" /> 新建项目</button>
    </template>
  </PageHeading>
  <div class="stat-grid four">
    <TgStat label="我参与的项目" :value="mine.length" :foot="`项目管理员 ${adminOf} 个`" icon="folder" color="blue" />
    <TgStat label="收藏" :value="favs" foot="星标置顶，顶栏工作范围也优先显示" icon="star" color="orange" />
    <TgStat label="最近访问" :value="recent" foot="打开项目详情即记入" icon="clock" color="purple" />
    <TgStat label="待审核用例" :value="pendingTotal" foot="各项目待人工审核的用例合计" icon="checklist" color="green" :to="pendingTotal ? { name: 'cases' } : null" />
  </div>
  <TgPanel :pad="false">
    <div class="filters panel-pad" style="padding-bottom:10px">
      <TgSeg :model-value="view" :items="[['list', '列表'], ['cards', '卡片']]" @update:model-value="setView" />
      <label class="tiny muted row" style="gap:4px;cursor:pointer"><input v-model="onlyActive" type="checkbox" /> 隐藏已归档</label>
      <span class="grow" />
      <span class="muted tiny">当前工作范围：{{ scope.label }}</span>
    </div>
    <TgTable v-if="view === 'list'" :cols="cols" :rows="rows" row-key="project" :loading="loading" clickable :active-key="scope.project" empty="你还没有可见的项目" :empty-hint="canCreateProject ? '点击右上角「新建项目」' : '请联系管理员把你添加为项目成员'" @row-click="open">
      <template #cell-fav="{ row }"><button type="button" class="star" :class="{ on: row.favorite }" :title="row.favorite ? '取消收藏' : '收藏置顶'" @click.stop="fav(row)">{{ row.favorite ? '★' : '☆' }}</button></template>
      <template #cell-project="{ row }"><b>{{ row.project }}</b><span v-if="row.code" class="tag" style="margin-left:6px">{{ row.code }}</span><div class="sub">{{ row.business_line || '未归属产品线' }} · 负责人 {{ row.owner || '—' }} · {{ row.member_count || 0 }} 成员</div></template>
      <template #cell-my_role="{ row }"><span v-if="row.my_role" class="tag">{{ roleLabel(row.my_role) }}</span><span v-else class="muted">—</span></template>
      <template #cell-tasks="{ row }">{{ num(row.tasks) }}</template>
      <template #cell-cases="{ row }">{{ num(row.cases) }} / <b>{{ num(row.approved) }}</b></template>
      <template #cell-pending="{ row }"><b v-if="row.pending" style="color:var(--amber)">{{ row.pending }}</b><span v-else class="muted">0</span></template>
      <template #cell-exec="{ row }"><template v-if="row.executed">{{ row.executed }} <span class="muted tiny">通过 {{ Math.round(row.exec_pass / row.executed * 100) }}%</span></template><span v-else class="muted">—</span></template>
      <template #cell-last_visited="{ row }"><span class="muted">{{ row.last_visited ? fmtAgo(row.last_visited) : '—' }}</span></template>
      <template #cell-status="{ row }"><TgBadge :status="row.status" :text="row.status_label" /></template>
    </TgTable>
    <div v-else class="cards panel-pad">
      <div v-for="p in rows" :key="p.project" class="card clickable" :class="{ cur: p.project === scope.project }" @click="open(p)">
        <div class="row"><button type="button" class="star" :class="{ on: p.favorite }" @click.stop="fav(p)">{{ p.favorite ? '★' : '☆' }}</button><b class="ellipsis" style="flex:1">{{ p.project }}</b><TgBadge :status="p.status" :text="p.status_label" /></div>
        <div class="muted tiny">{{ p.business_line || '未归属产品线' }} · {{ roleLabel(p.my_role) || '—' }} · 负责人 {{ p.owner || '—' }}</div>
        <div class="nums"><span><b>{{ num(p.tasks) }}</b><small>任务</small></span><span><b>{{ num(p.cases) }}</b><small>用例</small></span><span><b>{{ num(p.approved) }}</b><small>正式</small></span><span><b :style="p.pending ? 'color:var(--amber)' : ''">{{ num(p.pending) }}</b><small>待审</small></span></div>
        <div class="muted tiny">最近访问 {{ p.last_visited ? fmtAgo(p.last_visited) : '—' }} · 最近活动 {{ fmtAgo(p.last_activity) || '—' }}</div>
      </div>
      <div v-if="!rows.length" class="empty" style="grid-column:1/-1"><strong>你还没有可见的项目</strong></div>
    </div>
  </TgPanel>
  <ProjectDialog v-model="dlg" />
</template>
<style scoped>
.star { border: 0; background: none; font-size: 15px; color: #c3ccd8; cursor: pointer; padding: 0 2px; }
.star.on, .star:hover { color: #f0a03d; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
.card { border: 1px solid var(--line); border-radius: 6px; padding: 12px 14px; display: flex; flex-direction: column; gap: 6px; background: #fff; }
.card:hover { border-color: #bdd0ff; } .card.cur { border-color: var(--blue); background: #f8fbff; }
.nums { display: flex; gap: 16px; margin: 4px 0; }
.nums span { display: flex; flex-direction: column; } .nums b { font-size: 18px; } .nums small { font-size: 11px; color: var(--muted); }
</style>

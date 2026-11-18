<script setup>
/* 项目列表（3.2–3.7）：GET /projects（include_archived）；前端筛选 关键字 / 状态 / 产品线 / 负责人 / 我的角色，按产品线分组；收藏置顶；
   新建 / 编辑 / 归档 / 暂停 / 恢复 / 删除（含内容预检）；管理员可见「（未指定）」聚合行与存量迁移入口。 */
import { ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtAgo, num } from '@/utils/format'
import { PROJECT_STATUSES } from '@/utils/consts'
import { PROJECT_ROLES } from '@/utils/permissions'
import { useProjectActions, UNASSIGNED, roleLabel } from './components/useProjectActions'
import ProjectDialog from './components/ProjectDialog.vue'
import ProjectDeleteDialog from './components/ProjectDeleteDialog.vue'
import { PageHeading, TgPanel, TgTable, TgBadge, TgSeg, TgDropdown, TgPager, TgIcon } from '@/components/ui'
const route = useRoute(), router = useRouter(), scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore()
const { canCreateProject, toggleFavorite, setStatus, isProjectAdmin } = useProjectActions()
const kw = ref(route.query.q || ''), status = ref(route.query.status || ''), line = ref(route.query.line || ''), owner = ref(route.query.owner || ''), role = ref(route.query.role || ''), group = ref(route.query.group === '1')
const page = ref(1), size = ref(20), loading = ref(false)
watch([kw, status, line, owner, role, group], () => { page.value = 1; router.replace({ query: { q: kw.value || undefined, status: status.value || undefined, line: line.value || undefined, owner: owner.value || undefined, role: role.value || undefined, group: group.value ? '1' : undefined } }) })
async function load(force = true) { loading.value = true; try { await scope.load(force) } catch (e) { ui.error(e) } finally { loading.value = false } }
load(true)
const all = computed(() => scope.projects)
const real = computed(() => all.value.filter(p => !p.builtin))
const countBy = f => real.value.reduce((m, p) => { const k = f(p) || ''; m[k] = (m[k] || 0) + 1; return m }, {})
const statusCounts = computed(() => countBy(p => p.status)), lineCounts = computed(() => countBy(p => p.business_line)), roleCounts = computed(() => countBy(p => p.my_role))
const owners = computed(() => [...new Set(real.value.map(p => p.owner).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'zh')))
const lines = computed(() => [...new Set([...(scope.lines || []), ...Object.keys(lineCounts.value).filter(Boolean)])].sort((a, b) => a.localeCompare(b, 'zh')))
const shown = computed(() => {
  const k = kw.value.trim().toLowerCase()
  return all.value.filter(p => (!status.value || p.status === status.value) && (!line.value || (p.business_line || '') === line.value) && (!owner.value || p.owner === owner.value) && (!role.value || p.my_role === role.value)
    && (!k || [p.project, p.code, p.owner, p.description, p.business_line, p.created_by].some(x => (x || '').toLowerCase().includes(k))))
})
const filtering = computed(() => !!(kw.value || status.value || line.value || owner.value || role.value))
const pageItems = computed(() => shown.value.slice((page.value - 1) * size.value, page.value * size.value))
/** 按产品线分组：组头行插入表格（builtin 行归「未分产品线」） */
const groups = computed(() => {
  if (!group.value) return [{ key: null, items: pageItems.value }]
  const m = new Map()
  for (const p of pageItems.value) { const k = p.builtin ? '' : (p.business_line || ''); if (!m.has(k)) m.set(k, []); m.get(k).push(p) }
  return [...m.keys()].sort((a, b) => (a === '') - (b === '') || a.localeCompare(b, 'zh')).map(k => ({ key: k, items: m.get(k), tasks: m.get(k).reduce((s, p) => s + (p.tasks || 0), 0), cases: m.get(k).reduce((s, p) => s + (p.cases || 0), 0) }))
})
const cols = [{ key: 'fav', title: '', w: '30px' }, { key: 'project', title: '项目' }, { key: 'business_line', title: '产品线', w: '110px' }, { key: 'owner', title: '负责人', w: '90px' }, { key: 'status', title: '状态', w: '80px' }, { key: 'my_role', title: '我的角色', w: '120px' }, { key: 'tasks', title: '任务', w: '60px' }, { key: 'cases', title: '用例', w: '70px' }, { key: 'life', title: '用例生命周期', w: '170px' }, { key: 'exec', title: '执行', w: '100px' }, { key: 'last_activity', title: '最近活动', w: '90px' }, { key: 'ops', title: '', w: '60px' }]
const dlg = ref(false), editing = ref(null), delDlg = ref(false), delName = ref('')
function openNew() { editing.value = null; dlg.value = true }
function openEdit(p) { editing.value = p; dlg.value = true }
function open(p) { router.push({ name: 'project-detail', params: { name: p.project } }) }
async function fav(p) { try { await toggleFavorite(p.project) } catch (e) { ui.error(e) } }
async function st(p, s) { try { await setStatus(p.project, s) } catch (e) { ui.error(e) } }
function menu(p) {
  const items = [{ label: '进入项目', action: () => open(p) }]
  if (p.builtin) return items
  items.push({ label: p.favorite ? '取消收藏' : '收藏置顶', action: () => fav(p) })
  if (scope.project !== p.project) items.push({ label: '设为工作范围', action: () => { scope.setProject(p.project); ui.toast(`工作范围已切换到「${p.project}」`) } })
  if (auth.can(p.project, 'project.edit') && p.status !== 'archived') items.push({ label: '编辑', action: () => openEdit(p) })
  if (isProjectAdmin(p.project)) {
    items.push({ divider: true })
    if (p.status === 'archived') items.push({ label: '恢复为进行中', action: () => st(p, 'active') })
    else { if (p.status === 'active') items.push({ label: '暂停', action: () => st(p, 'paused') }); else items.push({ label: '恢复为进行中', action: () => st(p, 'active') }); items.push({ label: '归档', action: () => st(p, 'archived') }) }
  }
  if (auth.isAdmin) items.push({ divider: true }, { label: '删除…', danger: true, action: () => { delName.value = p.project; delDlg.value = true } })
  return items
}
const reviewed = p => (p.approved || 0) + (p.pending || 0) + (p.rejected || 0)
if (route.query.new) openNew()
</script>
<template>
  <PageHeading title="项目列表" :sub="`${real.length} 个项目 · ${lines.length} 条产品线 · 项目是需求、任务、用例、计划与知识空间的范围边界；名称为自然键，改名联动全部引用。`">
    <template #actions>
      <router-link class="btn" :to="{ name: 'lines' }">产品线管理</router-link>
      <button v-if="canCreateProject" type="button" class="btn primary" @click="openNew"><TgIcon name="plus" :size="14" /> 新建项目</button>
      <TgDropdown v-if="auth.isAdmin" :items="[{ label: '存量迁移报告（未指定项目）', action: () => router.push({ name: 'project-detail', params: { name: UNASSIGNED } }) }, { label: '刷新', action: () => load(true) }]" />
    </template>
  </PageHeading>
  <TgPanel :pad="false">
    <div class="filters panel-pad" style="padding-bottom:10px">
      <input v-model="kw" class="field" style="width:240px" placeholder="搜索名称 / 编码 / 描述 / 负责人 / 产品线" />
      <TgSeg v-model="status" :items="[['', '全部', real.length], ...PROJECT_STATUSES.map(([k, l]) => [k, l, statusCounts[k] || 0])]" />
      <select v-model="line" class="select"><option value="">全部产品线</option><option v-for="l in lines" :key="l" :value="l">{{ l }}（{{ lineCounts[l] || 0 }}）</option></select>
      <select v-if="owners.length > 1 || owner" v-model="owner" class="select"><option value="">负责人</option><option v-for="o in owners" :key="o" :value="o">{{ o }}</option></select>
      <select v-if="Object.keys(roleCounts).filter(Boolean).length > 1 || role" v-model="role" class="select"><option value="">我的角色</option><option v-for="(l, k) in PROJECT_ROLES" :key="k" :value="k">{{ l }}（{{ roleCounts[k] || 0 }}）</option></select>
      <label class="tiny muted row" style="gap:4px;cursor:pointer"><input v-model="group" type="checkbox" /> 按产品线分组</label>
      <span class="grow" />
      <span v-if="filtering" class="muted small">筛选出 {{ shown.filter(p => !p.builtin).length }} 个 <button type="button" class="btn ghost small" @click="kw = ''; status = ''; line = ''; owner = ''; role = ''">清除</button></span>
      <span class="muted tiny">收藏置顶 · 其余按最近活动倒序 · 可见范围：{{ { open: '开放', line: '按产品线成员', member: '按项目成员' }[auth.accessMode] || auth.accessMode }}</span>
    </div>
    <div class="table-wrap" :class="{ loading }">
      <table class="table">
        <thead><tr><th v-for="c in cols" :key="c.key" :style="c.w ? { width: c.w } : null">{{ c.title }}</th></tr></thead>
        <tbody>
          <template v-for="g in groups" :key="g.key ?? '__all'">
            <tr v-if="g.key !== null" class="grp-row"><td :colspan="cols.length"><b>{{ g.key || '未分产品线' }}</b> <span class="muted tiny">{{ g.items.length }} 个项目 · {{ g.tasks }} 个任务 · {{ g.cases }} 条用例</span><router-link v-if="g.key" class="btn ghost small" style="margin-left:8px" :to="{ name: 'lines', query: { edit: g.key } }">编辑产品线</router-link></td></tr>
            <tr v-for="p in g.items" :key="p.project" class="row-click" :class="{ sel: p.project === scope.project }" @click="open(p)">
              <td @click.stop><button v-if="!p.builtin" type="button" class="star" :class="{ on: p.favorite }" :title="p.favorite ? '取消收藏' : '收藏置顶'" @click="fav(p)">{{ p.favorite ? '★' : '☆' }}</button></td>
              <td><b>{{ p.project }}</b><span v-if="p.code" class="tag" style="margin-left:6px">{{ p.code }}</span><span v-if="p.builtin" class="badge gray" style="margin-left:6px">聚合行</span><div v-if="p.description" class="sub ellipsis" style="max-width:420px">{{ p.description }}</div><div v-else-if="p.builtin" class="sub">未指定项目的历史任务；管理员可在概览中归属到项目</div></td>
              <td><span v-if="p.business_line" class="tag" title="同产品线项目共享历史用例复用">{{ p.business_line }}</span><span v-else class="muted">—</span></td>
              <td>{{ p.owner || '—' }}</td>
              <td><TgBadge :status="p.status" :text="p.status_label" /></td>
              <td><span v-if="p.my_role" class="tag">{{ roleLabel(p.my_role) }}</span><span v-else class="muted">—</span><div class="sub">{{ p.member_count || 0 }} 名成员</div></td>
              <td>{{ num(p.tasks) }}</td>
              <td><b>{{ num(p.cases) }}</b></td>
              <td>
                <template v-if="reviewed(p)">
                  <div class="segbar"><i v-if="p.approved" :style="{ flex: p.approved }" class="ok" :title="`正式 ${p.approved}`" /><i v-if="p.pending" :style="{ flex: p.pending }" class="pend" :title="`待审核 ${p.pending}`" /><i v-if="p.rejected" :style="{ flex: p.rejected }" class="rej" :title="`已驳回 ${p.rejected}`" /></div>
                  <div class="sub">正式 {{ p.approved }} · 待审 {{ p.pending }} · 驳回 {{ p.rejected }}</div>
                </template><span v-else class="muted">—</span>
              </td>
              <td><template v-if="p.executed">已执行 {{ p.executed }}<div class="sub">通过率 {{ Math.round(p.exec_pass / p.executed * 100) }}%</div></template><span v-else class="muted">未执行</span></td>
              <td class="muted">{{ fmtAgo(p.last_activity) || '—' }}</td>
              <td @click.stop><TgDropdown icon small label="更多" :items="menu(p)" /></td>
            </tr>
          </template>
          <tr v-if="!shown.length"><td :colspan="cols.length"><div class="empty"><strong>{{ loading ? '加载中…' : filtering ? '没有符合筛选条件的项目' : (canCreateProject ? '暂无项目' : '你尚未加入任何项目') }}</strong><div class="tiny muted">{{ filtering ? '' : canCreateProject ? '点击右上角「新建项目」创建' : '请联系管理员把你添加为项目成员' }}</div></div></td></tr>
        </tbody>
      </table>
    </div>
    <TgPager v-if="shown.length > 20 || page > 1" v-model:page="page" v-model:size="size" :total="shown.length" />
  </TgPanel>
  <ProjectDialog v-model="dlg" :project="editing" />
  <ProjectDeleteDialog v-model="delDlg" :project="delName" />
</template>
<style scoped>
.star { border: 0; background: none; font-size: 15px; color: #c3ccd8; cursor: pointer; padding: 0 2px; }
.star.on, .star:hover { color: #f0a03d; }
.grp-row td { background: #f6f8fa; padding: 7px 11px; }
.segbar { display: flex; height: 7px; border-radius: 4px; overflow: hidden; background: #edf1f9; margin: 2px 0 3px; }
.segbar i { display: block; height: 100%; }
.segbar .ok { background: var(--green); } .segbar .pend { background: var(--amber); } .segbar .rej { background: var(--red); }
</style>

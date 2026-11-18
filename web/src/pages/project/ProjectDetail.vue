<script setup>
/* 项目详情（3.7）：GET /projects/{name}（记最近访问）；页签 overview / members / versions / modules / profile / dependencies / coverage / knowledge / settings 走路由参数 sub。
   「（未指定）」聚合项目只有概览（管理员为存量迁移报告）。 */
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, seg } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { useProjectActions, UNASSIGNED, roleLabel } from './components/useProjectActions'
import OverviewTab from './components/OverviewTab.vue'
import MigrationReport from './components/MigrationReport.vue'
import MembersTab from './components/MembersTab.vue'
import VersionsTab from './components/VersionsTab.vue'
import ModuleTree from './components/ModuleTree.vue'
import ProfileTab from './components/ProfileTab.vue'
import DependenciesTab from './components/DependenciesTab.vue'
import CoverageTab from './components/CoverageTab.vue'
import KnowledgeTab from './components/KnowledgeTab.vue'
import SettingsTab from './components/SettingsTab.vue'
import ProjectDialog from './components/ProjectDialog.vue'
import ProjectDeleteDialog from './components/ProjectDeleteDialog.vue'
import { PageHeading, TgTabs, TgBadge, TgPanel, TgDropdown, TgIcon, TgEmpty } from '@/components/ui'
const route = useRoute(), router = useRouter(), scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore()
const { canDelete, toggleFavorite, setStatus, isProjectAdmin } = useProjectActions()
const name = computed(() => String(route.params.name || ''))
const legacy = computed(() => name.value === UNASSIGNED)
const TABS = [['overview', '概览'], ['members', '成员'], ['versions', '版本'], ['modules', '模块'], ['profile', '项目画像'], ['dependencies', '依赖关系'], ['coverage', '覆盖追溯'], ['knowledge', '知识回溯'], ['settings', '设置']]
const sub = computed({ get: () => (legacy.value ? 'overview' : (TABS.some(t => t[0] === route.params.sub) ? route.params.sub : 'overview')), set: v => router.replace({ name: 'project-detail', params: { name: name.value, sub: v === 'overview' ? undefined : v } }) })
const { data: detail, loading, error, reload } = useLoad(() => legacy.value ? Promise.resolve(null) : api.get(`/api/v1/projects/${seg(name.value)}`), { deps: [name] })
/** 列表行统计（任务 / 用例 / 审核分布）来自 /projects 聚合，详情接口统计为 0，所以优先取 scope.projects 的行 */
const row = computed(() => scope.projects.find(p => p.project === name.value) || detail.value || { project: name.value, status: 'active', tasks: 0, cases: 0, pending: 0, rejected: 0, approved: 0, executed: 0, exec_pass: 0 })
const tabs = computed(() => legacy.value ? [{ key: 'overview', label: '概览' }] : TABS.map(([k, l]) => ({ key: k, label: l, count: k === 'members' ? (row.value.member_count ?? detail.value?.member_count) : k === 'versions' ? detail.value?.versions : k === 'modules' ? detail.value?.modules : undefined })))
const profileBadge = computed(() => row.value.profile?.draft ? '草稿待确认' : (row.value.profile?.active ? `v${row.value.profile.version}` : ''))
const isCurrent = computed(() => scope.project === name.value)
watch(name, v => ui.setCrumb(v), { immediate: true })
onBeforeUnmount(() => ui.setCrumb(''))
scope.load()
const editDlg = ref(false), delDlg = ref(false)
async function refresh() { await Promise.all([reload(true), scope.load(true)]) }
async function fav() { try { await toggleFavorite(name.value); reload(true) } catch (e) { ui.error(e) } }
async function status(s) { try { if (await setStatus(name.value, s)) reload(true) } catch (e) { ui.error(e) } }
function setCurrent() { scope.setProject(name.value); ui.toast(`工作范围已切换到「${name.value}」`) }
function go(routeName, query) { if (!legacy.value && !isCurrent.value) scope.setProject(name.value); router.push({ name: routeName, query }) }
async function manualTask() {
  if (!await ui.confirm('创建人工用例集？', '不经 AI 的用例容器任务，用例以草稿进入评审流（人工新增 / Excel 导入的落脚点）。', { ok: '创建' })) return
  try { const r = await api.post(`/api/v1/projects/${seg(name.value)}/manual-task`, { title: `${name.value} 人工用例集` }); ui.toast('人工用例集已创建'); scope.load(true); router.push({ name: 'design-detail', params: { id: r.task_id } }) } catch (e) { ui.error(e) }
}
function onRenamed(newName) { router.replace({ name: 'project-detail', params: { name: newName, sub: route.params.sub } }) }
function onModulesChanged() { if (isCurrent.value) scope.loadModules(true); reload(true) }
const more = computed(() => {
  const r = row.value, items = []
  if (legacy.value) return items
  if (auth.can(name.value, 'project.edit') && r.status !== 'archived') items.push({ label: '编辑项目', action: () => { editDlg.value = true } })
  if (isProjectAdmin(name.value)) {
    if (r.status === 'archived') items.push({ label: '恢复为进行中', action: () => status('active') })
    else items.push({ label: '归档项目', action: () => status('archived') })
  }
  if (auth.can(name.value, 'case.edit') && r.status !== 'archived') items.push({ label: '＋ 人工用例集', action: manualTask })
  items.push({ label: '项目报表 ↗', action: () => go('report') })
  if (auth.isAdmin) items.push({ divider: true }, { label: '删除项目…', danger: true, action: () => { delDlg.value = true } })
  return items
})
</script>
<template>
  <PageHeading :title="name">
    <template #back><router-link :to="{ name: 'projects' }"><TgIcon name="back" :size="14" /> 项目列表</router-link></template>
    <template #badge><TgBadge v-if="!legacy" :status="row.status" :text="row.status_label" /><span v-if="row.code" class="tag">{{ row.code }}</span><span v-if="row.my_role" class="tag">{{ roleLabel(row.my_role) }}</span><span v-if="row.favorite" class="badge orange">★ 已收藏</span></template>
    <template #sub><template v-if="legacy">未指定项目的历史任务聚合（不可编辑 / 删除）· {{ row.tasks || 0 }} 个任务 · {{ row.cases || 0 }} 条用例</template><template v-else>{{ row.business_line || '未归属产品线' }} · 负责人 {{ row.owner || '—' }} · {{ row.member_count ?? 0 }} 成员 · {{ row.tasks || 0 }} 个任务 · {{ row.cases || 0 }} 条用例<template v-if="detail"> · 版本 {{ detail.versions }} · 模块 {{ detail.modules }}</template><span v-if="row.description" class="muted"> · {{ row.description }}</span></template></template>
    <template #actions>
      <button v-if="!legacy" type="button" class="btn" :title="row.favorite ? '取消收藏' : '收藏置顶'" @click="fav"><TgIcon name="star" :size="14" /> {{ row.favorite ? '已收藏' : '收藏' }}</button>
      <button v-if="!legacy && !isCurrent" type="button" class="btn" @click="setCurrent">设为工作范围</button>
      <button v-if="!legacy && auth.can(name, 'requirement.edit') && row.status !== 'archived'" type="button" class="btn primary" @click="go('requirements', { new: 1 })">录入需求</button>
      <TgDropdown v-if="more.length" :items="more" />
    </template>
  </PageHeading>
  <div v-if="error" class="err-box mb">{{ error.message }}</div>
  <div v-if="row.status === 'archived'" class="attention mb">项目已归档，数据只读；恢复后才能修改。<button v-if="isProjectAdmin(name)" type="button" class="btn small" @click="status('active')">恢复</button></div>
  <TgTabs v-model="sub" :items="tabs" />
  <template v-if="sub === 'overview'">
    <MigrationReport v-if="legacy && auth.isAdmin" />
    <TgPanel v-else-if="legacy"><TgEmpty title="未指定项目的任务" hint="仅系统管理员可查看存量迁移报告并归属任务" /></TgPanel>
    <OverviewTab v-else :project="name" :row="row" :detail="detail" @tab="sub = $event" />
  </template>
  <MembersTab v-else-if="sub === 'members'" :project="name" @changed="refresh" />
  <VersionsTab v-else-if="sub === 'versions'" :project="name" @changed="reload(true)" />
  <TgPanel v-else-if="sub === 'modules'" title="模块树" hint="每项目独立，最多 5 级；第一层建议对应端（H5 / 游戏后台管理 / 商户后台）；逻辑删除可恢复，被需求 / 用例引用的不可物理删除；同级拖动或上下移动排序">
    <ModuleTree :project="name" :can-manage="auth.can(name, 'version.manage') && row.status !== 'archived'" :can-delete="canDelete" :is-project-admin="isProjectAdmin(name)" @changed="onModulesChanged" />
  </TgPanel>
  <ProfileTab v-else-if="sub === 'profile'" :project="name" @changed="refresh" @tab="sub = $event" />
  <DependenciesTab v-else-if="sub === 'dependencies'" :project="name" />
  <CoverageTab v-else-if="sub === 'coverage'" :project="name" />
  <KnowledgeTab v-else-if="sub === 'knowledge'" :project="name" :can-run="auth.isAdmin || isProjectAdmin(name)" :knowledge-auto="row.knowledge_auto !== false" @tab="sub = $event" />
  <SettingsTab v-else-if="sub === 'settings'" :project="name" :row="row" @changed="refresh" @renamed="onRenamed" />
  <ProjectDialog v-model="editDlg" :project="row" @saved="n => n !== name ? onRenamed(n) : refresh()" />
  <ProjectDeleteDialog v-model="delDlg" :project="name" @deleted="router.replace({ name: 'projects' })" />
</template>

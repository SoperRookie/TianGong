<script setup>
/* 产品线管理（3.12）：GET/POST /business-lines；PUT/DELETE /business-lines/{name}（改名联动项目；有项目时 detach=true 解除归属后删）；
   成员 PUT/DELETE /business-lines/{name}/members/{username}（角色 line_admin / member / viewer，按产品线模式生效）。 */
import { ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, seg } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtAgo } from '@/utils/format'
import { useProjectActions, LINE_ROLES, lineRoleLabel } from './components/useProjectActions'
import ProjectDialog from './components/ProjectDialog.vue'
import { PageHeading, TgPanel, TgTable, TgDialog, TgDrawer, TgField, TgDropdown, TgEmpty, TgIcon } from '@/components/ui'
const route = useRoute(), router = useRouter(), scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore()
const { canManageLine } = useProjectActions()
const { data, loading, error, reload } = useLoad(() => api.get('/api/v1/business-lines'))
const lines = computed(() => data.value?.business_lines || [])
const kw = ref('')
const shown = computed(() => { const k = kw.value.trim().toLowerCase(); return k ? lines.value.filter(l => [l.name, l.code, l.description, l.owner, ...(l.project_names || [])].some(x => (x || '').toLowerCase().includes(k))) : lines.value })
const cols = [{ key: 'name', title: '名称', w: '180px' }, { key: 'code', title: '编码', w: '80px' }, { key: 'description', title: '说明' }, { key: 'owner', title: '负责人', w: '90px' }, { key: 'projects', title: '所含项目' }, { key: 'members', title: '成员', w: '70px' }, { key: 'created', title: '创建', w: '120px' }, { key: 'ops', title: '', w: '60px' }]
const canCreate = computed(() => auth.isAdmin || auth.accessMode === 'open')
const modeLabel = computed(() => ({ open: '开放（全员可见）', line: '按产品线成员', member: '按项目成员' }[auth.accessMode] || auth.accessMode))
/* 新建 / 编辑 */
const dlg = ref(false), editing = ref(null), form = ref({}), saving = ref(false)
function openDlg(l) { editing.value = l || null; form.value = { name: l?.name || '', code: l?.code || '', owner: l?.owner || '', description: l?.description || '' }; dlg.value = true }
async function save() {
  const f = form.value, name = f.name.trim()
  if (!name) return ui.toast('产品线名称不能为空', 'warn')
  saving.value = true
  try {
    if (editing.value) {
      const r = await api.put(`/api/v1/business-lines/${seg(editing.value.name)}`, { new_name: name, code: f.code.trim(), owner: f.owner.trim(), description: f.description.trim() })
      ui.toast(r.moved_projects ? `已保存，${r.moved_projects} 个项目已更新归属` : '产品线已保存')
      if (scope.line === editing.value.name && name !== editing.value.name) scope.setLine(name)
    } else { await api.post('/api/v1/business-lines', { name, code: f.code.trim(), owner: f.owner.trim(), description: f.description.trim() }); ui.toast(`产品线「${name}」已创建`) }
    dlg.value = false; await Promise.all([reload(true), scope.load(true)])
  } catch (e) { ui.error(e) } finally { saving.value = false }
}
async function remove(l) {
  const msg = l.projects ? `该产品线下有 ${l.projects} 个项目（${(l.project_names || []).slice(0, 3).join('、')}）。删除后这些项目将不再归属任何产品线（项目本身保留）。` : '删除后可重新创建。'
  if (!await ui.confirm(`删除产品线「${l.name}」？`, msg, { ok: l.projects ? '解除归属并删除' : '删除', danger: true })) return
  try { await api.del(`/api/v1/business-lines/${seg(l.name)}`, undefined, l.projects ? { query: { detach: true } } : undefined); ui.toast('产品线已删除'); if (scope.line === l.name) scope.setLine(''); await Promise.all([reload(true), scope.load(true), auth.fetchMe()]) } catch (e) { ui.error(e) }
}
/* 成员抽屉 */
const drawer = ref(false), cur = ref(null), mUser = ref(''), mRole = ref('member'), users = ref([]), mBusy = ref(false)
const curLine = computed(() => lines.value.find(l => l.name === cur.value) || null)
const members = computed(() => Object.entries(curLine.value?.members || {}))
async function openMembers(l) { cur.value = l.name; drawer.value = true; mUser.value = ''; mRole.value = 'member'; if (!users.value.length) { try { users.value = (await api.get('/api/v1/auth/users/lookup')).users || [] } catch { /* 无联想 */ } } }
async function memberReq(method, username, body) {
  mBusy.value = true
  try {
    const l = method === 'PUT' ? await api.put(`/api/v1/business-lines/${seg(cur.value)}/members/${seg(username)}`, body) : await api.del(`/api/v1/business-lines/${seg(cur.value)}/members/${seg(username)}`)
    const i = lines.value.findIndex(x => x.name === l.name); if (i >= 0) data.value.business_lines[i] = l
    auth.fetchMe()
  } catch (e) { ui.error(e); throw e } finally { mBusy.value = false }
}
async function addMember() { const u = mUser.value.trim(); if (!u) return ui.toast('请输入用户名', 'warn'); try { await memberReq('PUT', u, { role: mRole.value }); mUser.value = ''; ui.toast(`已添加 ${u}`) } catch { /* 已提示 */ } }
async function setRole(u, role) { try { await memberReq('PUT', u, { role }); ui.toast('角色已更新') } catch { /* 已提示 */ } }
async function removeMember(u) { if (!await ui.confirm('移除成员？', `把 ${u} 从产品线「${cur.value}」移除？按产品线模式下该用户将看不到此产品线的项目。`, { ok: '移除', danger: true })) return; try { await memberReq('DELETE', u); ui.toast('已移除') } catch { /* 已提示 */ } }
/* 从项目列表「编辑产品线」带 edit=名 进来 */
watch([lines, () => route.query.edit], ([ls, e]) => { if (e && ls.length && !dlg.value) { const l = ls.find(x => x.name === e); if (l) { openDlg(l); router.replace({ query: {} }) } } }, { immediate: true })
const newProjDlg = ref(false), presetLine = ref('')
function menu(l) {
  const items = []
  if (canManageLine(l.name)) items.push({ label: '编辑', action: () => openDlg(l) }, { label: '成员与角色', action: () => openMembers(l) })
  else items.push({ label: '查看成员', action: () => openMembers(l) })
  if (canManageLine(l.name)) items.push({ label: '＋ 在此产品线新建项目', action: () => { presetLine.value = l.name; newProjDlg.value = true } })
  if (scope.line !== l.name) items.push({ label: '设为工作范围（该产品线全部项目）', action: () => { scope.setLine(l.name); ui.toast(`工作范围已切换到「${l.name} / 全部项目」`) } })
  if (auth.isAdmin) items.push({ divider: true }, { label: '删除…', danger: true, action: () => remove(l) })
  return items
}
</script>
<template>
  <PageHeading title="产品线" :sub="`${lines.length} 条 · 项目通过 business_line 归属；同产品线项目共享历史用例复用提示；「按产品线成员」模式下产品线成员可见线内全部项目。当前可见范围：${modeLabel}。`">
    <template #actions><button v-if="canCreate" type="button" class="btn primary" @click="openDlg(null)"><TgIcon name="plus" :size="14" /> 新建产品线</button></template>
  </PageHeading>
  <div v-if="error" class="err-box mb">{{ error.message }}</div>
  <div class="grid-2">
    <TgPanel :pad="false">
      <div class="filters panel-pad" style="padding-bottom:10px"><input v-model="kw" class="field" style="width:240px" placeholder="搜索名称 / 编码 / 项目" /><span class="grow" /><span class="muted tiny">项目字段上出现过但未登记的产品线会自动登记</span></div>
      <TgTable :cols="cols" :rows="shown" row-key="name" :loading="loading" empty="还没有产品线" :empty-hint="canCreate ? '点击右上角「新建产品线」' : ''">
        <template #cell-name="{ row }"><b>{{ row.name }}</b><span v-if="row.name === scope.line" class="badge blue" style="margin-left:6px">当前范围</span></template>
        <template #cell-code="{ row }"><span class="mono">{{ row.code || '—' }}</span></template>
        <template #cell-description="{ row }"><span class="muted">{{ row.description || '—' }}</span></template>
        <template #cell-owner="{ row }">{{ row.owner || '—' }}</template>
        <template #cell-projects="{ row }"><span class="inline-list"><router-link v-for="p in row.project_names || []" :key="p" class="tag" :to="{ name: 'project-detail', params: { name: p } }">{{ p }}</router-link><span v-if="!(row.project_names || []).length" class="muted">—</span></span></template>
        <template #cell-members="{ row }"><span class="link" :title="Object.keys(row.members || {}).join('、')" @click="openMembers(row)">{{ Object.keys(row.members || {}).length }} 人</span></template>
        <template #cell-created="{ row }"><span class="muted">{{ row.created_by || '—' }} · {{ fmtAgo(row.created_at) }}</span></template>
        <template #cell-ops="{ row }"><TgDropdown icon small :items="menu(row)" /></template>
      </TgTable>
    </TgPanel>
    <TgPanel title="产品线角色折算" hint="与「设置 › 访问控制」的数据可见范围三模式配合">
      <div class="list">
        <div><b>产品线管理员</b><div class="tiny muted">对线内全部项目视同项目管理员；按产品线模式下可维护本产品线并在线内新建项目</div></div>
        <div><b>产品线成员</b><div class="tiny muted">按产品线模式下可见线内项目，折算为测试负责人；与直接项目角色取等级高者</div></div>
        <div><b>只读</b><div class="tiny muted">折算为只读人员</div></div>
        <div><b>跨项目知识</b><div class="tiny muted">同产品线其他未归档项目的历史正式用例可作复用提示（相似度 ≥ 0.78）</div></div>
        <div><b>删除</b><div class="tiny muted">任何模式下删除产品线只有系统管理员；有项目归属时解除归属后删，项目保留</div></div>
      </div>
    </TgPanel>
  </div>
  <TgDialog v-model="dlg" :title="editing ? `编辑产品线 · ${editing.name}` : '新建产品线'" :ok="editing ? '保存' : '创建'" :busy="saving" @ok="save">
    <div v-if="editing?.projects" class="information mb"><TgIcon name="info" :size="14" /><span>改名会同步更新其下 {{ editing.projects }} 个项目的归属。</span></div>
    <div class="form-grid">
      <TgField label="名称" required hint="≤ 100 字，不含尖括号、引号与反斜杠"><input v-model="form.name" class="field" maxlength="100" autofocus /></TgField>
      <TgField label="编码" hint="≤ 50 字"><input v-model="form.code" class="field" maxlength="50" /></TgField>
    </div>
    <TgField label="负责人" hint="纯文本，不校验用户"><input v-model="form.owner" class="field" /></TgField>
    <TgField label="说明"><textarea v-model="form.description" class="field" rows="2" /></TgField>
  </TgDialog>
  <TgDrawer v-model="drawer" :title="`产品线成员 · ${cur || ''}`" :sub="auth.accessMode === 'line' ? '成员可见并可操作该产品线下全部项目（管理员 = 项目管理员，成员 = 测试负责人，只读 = 只读）' : `当前为「${modeLabel}」模式，此处设置在系统管理员把数据可见范围切为「按产品线成员」后生效`">
    <div v-if="curLine && canManageLine(curLine.name)" class="row mb">
      <input v-model="mUser" class="field" style="width:180px" list="tg-line-users" placeholder="用户名" @keydown.enter="addMember" />
      <datalist id="tg-line-users"><option v-for="u in users" :key="u.username" :value="u.username">{{ u.name || u.username }}</option></datalist>
      <select v-model="mRole" class="select"><option v-for="(l, k) in LINE_ROLES" :key="k" :value="k">{{ l }}</option></select>
      <button type="button" class="btn small primary" :disabled="mBusy" @click="addMember">添加</button>
    </div>
    <table v-if="members.length" class="table dense">
      <thead><tr><th>用户名</th><th style="width:170px">角色</th><th style="width:60px"></th></tr></thead>
      <tbody><tr v-for="[u, r] in members" :key="u"><td><b>{{ u }}</b><span v-if="u === auth.me" class="tag" style="margin-left:6px">我</span></td>
        <td><select v-if="canManageLine(curLine.name)" class="select small" :value="r" :disabled="mBusy" @change="setRole(u, $event.target.value)"><option v-for="(l, k) in LINE_ROLES" :key="k" :value="k">{{ l }}</option></select><span v-else class="tag">{{ lineRoleLabel(r) }}</span></td>
        <td><button v-if="canManageLine(curLine.name)" type="button" class="btn ghost small" style="color:var(--red)" :disabled="mBusy" @click="removeMember(u)">移除</button></td></tr></tbody>
    </table>
    <TgEmpty v-else title="还没有成员" hint="按产品线模式下，未加入的普通用户看不到该产品线的项目" />
    <div v-if="curLine" class="mt"><div class="panel-title mb">所含项目（{{ curLine.projects }}）</div><span class="inline-list"><router-link v-for="p in curLine.project_names || []" :key="p" class="tag" :to="{ name: 'project-detail', params: { name: p } }">{{ p }}</router-link><span v-if="!(curLine.project_names || []).length" class="muted">—</span></span></div>
  </TgDrawer>
  <ProjectDialog v-model="newProjDlg" :preset-line="presetLine" @saved="reload(true)" />
</template>

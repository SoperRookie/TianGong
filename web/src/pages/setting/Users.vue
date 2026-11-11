<script setup>
/* 用户与安全（2.10、2.12～2.14）：用户列表、新建 / 编辑 / 禁用 / 重置口令 / 解绑两步验证 / 删除；仅管理员。 */
import { ref, computed, watch, reactive } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtTime, fmtAgo } from '@/utils/format'
import { PROJECT_ROLES } from '@/utils/permissions'
import { PageHeading, TgPanel, TgTable, TgBadge, TgSeg, TgPager, TgDialog, TgField, TgEmpty, TgIcon, TgDropdown, TgStat } from '@/components/ui'
import UserDialog from './components/UserDialog.vue'

const route = useRoute(), router = useRouter(), auth = useAuthStore(), ui = useUiStore()
const q = route.query
const keyword = ref(q.keyword || ''), kw = ref(keyword.value), role = ref(q.role || ''), status = ref(q.status || ''), project = ref(q.project || ''), totp = ref(q.totp || '')
const page = ref(+q.page || 1), size = ref(+q.page_size || 20)
watch([keyword, role, status, project, totp, size], () => { page.value = 1 })
watch([keyword, role, status, project, totp, page, size], () => router.replace({ query: { keyword: keyword.value || undefined, role: role.value || undefined, status: status.value || undefined, project: project.value || undefined, totp: totp.value || undefined, page: page.value > 1 ? page.value : undefined, page_size: size.value !== 20 ? size.value : undefined } }))

const { data, loading, error, reload } = useLoad(
  () => auth.isAdmin ? api.get('/api/v1/auth/users', { keyword: keyword.value || undefined, role: role.value || undefined, status: status.value || undefined, project: project.value || undefined, totp: totp.value || undefined, page: page.value, page_size: size.value }) : Promise.resolve(null),
  { deps: [keyword, role, status, project, totp, page, size] },
)
const users = computed(() => data.value?.users || [])
const rc = computed(() => data.value?.role_counts || {}), sc = computed(() => data.value?.status_counts || {})
const cols = [{ key: 'username', title: '用户名' }, { key: 'name', title: '姓名 / 联系' }, { key: 'role', title: '系统角色', w: '100px' }, { key: 'projects', title: '所属项目与角色' }, { key: 'totp', title: '两步验证', w: '80px' }, { key: 'status', title: '状态', w: '70px' }, { key: 'last', title: '最近登录', w: '150px' }, { key: 'ops', title: '操作', w: '90px' }]
const isLastAdmin = u => u.role === 'admin' && users.value.filter(x => x.role === 'admin' && x.status === 'active').length <= 1 && (rc.value.admin || 1) <= 1

/* 新建 / 编辑 */
const dlg = ref(false), editing = ref(null)
function openNew() { editing.value = null; dlg.value = true }
function openEdit(u) { editing.value = u; dlg.value = true }

/* 启用 / 禁用 */
async function toggleStatus(u) {
  const to = u.status === 'active' ? 'disabled' : 'active'
  if (to === 'disabled' && !await ui.confirm(`禁用 ${u.username}？`, '禁用后该用户立即登出且无法登录；项目成员关系保留，可随时重新启用。', { ok: '禁用', danger: true })) return
  try { await api.put(`/api/v1/auth/users/${encodeURIComponent(u.username)}`, { status: to }); ui.toast(to === 'disabled' ? '已禁用' : '已启用'); reload(true) } catch (e) { ui.error(e) }
}
/* 重置口令 */
const pwdDlg = ref(false), pwdUser = ref(null), pwdForm = reactive({ p1: '', p2: '' })
function openPwd(u) { pwdUser.value = u; pwdForm.p1 = ''; pwdForm.p2 = ''; pwdDlg.value = true }
async function resetPwd() {
  if (pwdForm.p1.length < 6) return ui.toast('口令至少 6 位', 'warn')
  if (pwdForm.p1 !== pwdForm.p2) return ui.toast('两次输入不一致', 'warn')
  try { await api.put(`/api/v1/auth/users/${encodeURIComponent(pwdUser.value.username)}`, { new_password: pwdForm.p1 }); ui.toast('口令已重置，该用户全部会话失效'); pwdDlg.value = false } catch (e) { ui.error(e) }
}
/* 解绑两步验证 */
async function resetTotp(u) {
  if (!await ui.confirm(`解绑 ${u.username} 的两步验证？`, '手机丢失等场景使用：清除其验证器密钥，用户可重新绑定。', { ok: '解绑', danger: true })) return
  try { await api.put(`/api/v1/auth/users/${encodeURIComponent(u.username)}`, { reset_totp: true }); ui.toast('已解绑'); reload(true) } catch (e) { ui.error(e) }
}
/* 删除 */
async function remove(u) {
  if (!await ui.confirm(`删除用户 ${u.username}？`, '删除账号并清理其全部项目成员关系，不可恢复；唯一项目管理员需先指定其他管理员。', { ok: '删除', danger: true })) return
  try { await api.del(`/api/v1/auth/users/${encodeURIComponent(u.username)}`); ui.toast('已删除'); reload(true) } catch (e) { ui.error(e) }
}
const menu = u => [
  { label: '编辑', action: () => openEdit(u) },
  { label: u.status === 'active' ? '禁用' : '启用', action: () => toggleStatus(u), disabled: u.username === auth.me },
  { label: '重置口令', action: () => openPwd(u) },
  { label: '解绑两步验证', action: () => resetTotp(u), disabled: !u.totp_enabled },
  { divider: true },
  { label: '删除', action: () => remove(u), danger: true, disabled: u.username === auth.me || isLastAdmin(u), hint: u.username === auth.me ? '自己' : isLastAdmin(u) ? '最后一个管理员' : '' },
]
</script>
<template>
  <PageHeading title="用户与安全" :sub="`${data?.total ?? 0} 个账号；系统角色 admin / member；项目角色四级（项目 › 成员）；两步验证 TOTP；个人资料 / 改密 / 绑定验证器在右上角个人资料抽屉。`">
    <template #actions><button v-if="auth.isAdmin" type="button" class="btn primary" @click="openNew"><TgIcon name="plus" :size="14" /> 添加用户</button><router-link v-if="auth.isAdmin" class="btn" :to="{ name: 'access' }">访问控制</router-link></template>
  </PageHeading>
  <TgPanel v-if="!auth.isAdmin"><TgEmpty title="仅系统管理员可管理用户" hint="个人资料、修改口令与两步验证请使用右上角头像 › 个人资料。" /></TgPanel>
  <template v-else>
    <div v-if="error" class="err-box">{{ error.message }}</div>
    <div class="stat-grid">
      <TgStat label="账号" :value="(rc.admin || 0) + (rc.member || 0)" :foot="`管理员 ${rc.admin || 0} · 成员 ${rc.member || 0}`" icon="user" color="blue" />
      <TgStat label="已禁用" :value="sc.disabled || 0" foot="禁用即会话失效" icon="lock" color="orange" />
      <TgStat label="两步验证" :value="auth.settings?.totp_enabled ? '已开启' : '已关闭'" foot="总开关在访问控制页" icon="shield" color="green" />
      <TgStat label="数据可见范围" :value="{ open: '开放', line: '按产品线', member: '按项目成员' }[auth.settings?.access_mode] || '—'" foot="访问控制页切换" icon="eye" color="purple" />
    </div>
    <TgPanel :pad="false">
      <div class="panel-pad filters" style="padding-bottom:10px">
        <input v-model="kw" class="field" style="width:200px" placeholder="用户名 / 姓名 / 邮箱 / 手机 / 项目" @keyup.enter="keyword = kw" @blur="keyword = kw" />
        <TgSeg v-model="role" :items="[{ key: '', label: '全部角色' }, { key: 'admin', label: '管理员', count: rc.admin }, { key: 'member', label: '成员', count: rc.member }]" />
        <TgSeg v-model="status" :items="[{ key: '', label: '全部状态' }, { key: 'active', label: '启用', count: sc.active }, { key: 'disabled', label: '禁用', count: sc.disabled }]" />
        <select v-model="project" class="select"><option value="">全部项目</option><option v-for="p in data?.projects || []" :key="p" :value="p">{{ p }}</option></select>
        <select v-model="totp" class="select"><option value="">两步验证：全部</option><option value="on">已绑定</option><option value="off">未绑定</option></select>
      </div>
      <TgTable :cols="cols" :rows="users" row-key="username" :loading="loading" empty="没有匹配的用户">
        <template #cell-username="{ row }"><b>{{ row.username }}</b><span v-if="row.username === auth.me" class="tag" style="margin-left:6px">我</span><span v-if="row.must_change_password" class="tag" style="margin-left:6px" title="下次登录须改密">待改密</span></template>
        <template #cell-name="{ row }">{{ row.name || '—' }}<div class="sub">{{ [row.email, row.phone].filter(Boolean).join(' · ') }}</div></template>
        <template #cell-role="{ row }"><TgBadge :color="row.role === 'admin' ? 'purple' : 'gray'" :text="row.role === 'admin' ? '系统管理员' : '成员'" /></template>
        <template #cell-projects="{ row }"><span v-if="row.projects?.length" class="inline-list"><span v-for="p in row.projects" :key="p.project" class="tag" :title="PROJECT_ROLES[p.role] || p.role">{{ p.project }} · {{ PROJECT_ROLES[p.role] || p.role }}</span></span><span v-else class="muted">—</span></template>
        <template #cell-totp="{ row }"><span v-if="row.totp_enabled" class="ok">已绑定</span><span v-else class="muted">未绑定</span></template>
        <template #cell-status="{ row }"><TgBadge :status="row.status === 'active' ? 'enabled' : 'disabled'" /></template>
        <template #cell-last="{ row }"><template v-if="row.last_login_at"><span :title="fmtTime(row.last_login_at, true)">{{ fmtAgo(row.last_login_at) }}</span><div class="sub mono">{{ row.last_login_ip || '' }}</div></template><span v-else class="muted">从未登录</span></template>
        <template #cell-ops="{ row }"><TgDropdown :items="menu(row)" small /></template>
      </TgTable>
      <TgPager v-model:page="page" v-model:size="size" :total="data?.total || 0" />
    </TgPanel>
  </template>

  <UserDialog v-model="dlg" :user="editing" @saved="reload(true)" />
  <TgDialog v-model="pwdDlg" :title="`重置口令 · ${pwdUser?.username || ''}`" ok="重置" danger @ok="resetPwd">
    <TgField label="新口令" required hint="至少 6 位；重置后该用户全部会话失效，需重新登录"><input v-model="pwdForm.p1" class="field" type="password" autocomplete="new-password" /></TgField>
    <TgField label="再输一次" required><input v-model="pwdForm.p2" class="field" type="password" autocomplete="new-password" /></TgField>
  </TgDialog>
</template>
<style scoped>
/* 行尾「更多 ▾」菜单不被表格容器裁剪 */
.table-wrap { overflow: visible; }
</style>

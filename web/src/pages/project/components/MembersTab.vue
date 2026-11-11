<script setup>
/* 项目成员与角色（3.8）：GET/PUT /projects/{name}/members、DELETE .../members/{username}；至少保留一名项目管理员。 */
import { ref, computed } from 'vue'
import { api, seg } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useAuthStore } from '@/stores/auth'
import { useScopeStore } from '@/stores/scope'
import { useUiStore } from '@/stores/ui'
import { PROJECT_ROLES } from '@/utils/permissions'
import { TgPanel, TgTable, TgBadge, TgDialog, TgField, TgIcon } from '@/components/ui'
const props = defineProps({ project: { type: String, required: true } })
const emit = defineEmits(['changed'])
const auth = useAuthStore(), scope = useScopeStore(), ui = useUiStore()
const { data, loading, error, reload } = useLoad(() => api.get(`/api/v1/projects/${seg(props.project)}/members`), { deps: [() => props.project] })
const manage = computed(() => auth.can(props.project, 'project.members'))
const ROLE_DESC = { project_admin: '当前项目全部管理权限（成员、设置、删除类动作按可见范围模式）', test_lead: '测试设计、评审、计划、分配、统计；不含项目设置与成员', tester: '编写、AI 生成、修改授权内容、执行测试', viewer: '仅查看授权项目数据' }
const cols = [{ key: 'username', title: '用户名', w: '150px' }, { key: 'name', title: '姓名', w: '110px' }, { key: 'role', title: '项目角色', w: '120px' }, { key: 'desc', title: '权限说明' }, { key: 'system_role', title: '系统角色', w: '90px' }, { key: 'status', title: '状态', w: '80px' }, { key: 'ops', title: '', w: '110px' }]
const adminCount = computed(() => (data.value?.members || []).filter(m => m.role === 'project_admin').length)
const dlg = ref(false), editing = ref(''), form = ref({ username: '', role: 'tester' }), users = ref([]), saving = ref(false)
async function open(m) {
  editing.value = m ? m.username : ''
  form.value = { username: m ? m.username : '', role: m ? m.role : 'tester' }
  dlg.value = true
  if (!users.value.length) { try { users.value = (await api.get('/api/v1/auth/users/lookup')).users || [] } catch { /* 非项目管理员无联想 */ } }
}
async function save() {
  const username = form.value.username.trim()
  if (!username) return ui.toast('请输入用户名', 'warn')
  saving.value = true
  try {
    await api.put(`/api/v1/projects/${seg(props.project)}/members`, { username, role: form.value.role })
    dlg.value = false; ui.toast(`${username} → ${PROJECT_ROLES[form.value.role]}`)
    await Promise.all([reload(true), scope.load(true), auth.fetchMe()]); emit('changed')
  } catch (e) { ui.error(e) } finally { saving.value = false }
}
async function remove(m) {
  if (!await ui.confirm('移出成员？', `将「${m.username}」移出项目「${props.project}」后，其将无法再访问本项目数据。`, { ok: '移出', danger: true })) return
  try {
    await api.del(`/api/v1/projects/${seg(props.project)}/members/${seg(m.username)}`)
    ui.toast(`已移出 ${m.username}`)
    await Promise.all([reload(true), scope.load(true), auth.fetchMe()]); emit('changed')
  } catch (e) { ui.error(e) }
}
</script>
<template>
  <div v-if="error" class="err-box mb">{{ error.message }}</div>
  <TgPanel title="项目成员与角色" :hint="`${(data?.members || []).length} 人 · 系统级角色与项目级角色分离；按产品线模式下产品线成员按线内角色折算（管理员 → 项目管理员，成员 → 测试负责人，只读 → 只读）`" :pad="false">
    <template #actions><button v-if="manage" type="button" class="btn small primary" @click="open(null)"><TgIcon name="plus" :size="13" /> 添加成员</button></template>
    <TgTable :cols="cols" :rows="data?.members || []" row-key="username" :loading="loading" empty="暂无成员">
      <template #cell-username="{ row }"><b>{{ row.username }}</b><span v-if="row.username === auth.me" class="tag" style="margin-left:6px">我</span></template>
      <template #cell-name="{ row }">{{ row.name || '—' }}</template>
      <template #cell-role="{ row }"><span class="tag">{{ row.role_label || PROJECT_ROLES[row.role] || row.role }}</span></template>
      <template #cell-desc="{ row }"><span class="muted">{{ ROLE_DESC[row.role] || '' }}</span></template>
      <template #cell-system_role="{ row }"><TgBadge v-if="row.system_role" :status="row.system_role === 'admin' ? 'approved' : 'pending'" :text="row.system_role === 'admin' ? '管理员' : '成员'" /><span v-else class="muted">—</span></template>
      <template #cell-status="{ row }"><TgBadge v-if="row.status === 'disabled'" status="disabled" text="已禁用" /><span v-else-if="row.status === 'unknown'" class="muted">账号不存在</span><TgBadge v-else status="enabled" text="正常" /></template>
      <template #cell-ops="{ row }">
        <span v-if="manage" class="row nowrap" style="gap:2px;flex-wrap:nowrap">
          <button type="button" class="btn ghost small" @click="open(row)">改角色</button>
          <button type="button" class="btn ghost small" :disabled="row.role === 'project_admin' && adminCount <= 1" :title="row.role === 'project_admin' && adminCount <= 1 ? '唯一项目管理员不可移出' : ''" style="color:var(--red)" @click="remove(row)">移出</button>
        </span>
      </template>
    </TgTable>
  </TgPanel>
  <TgDialog v-model="dlg" :title="editing ? `修改角色 · ${editing}` : '添加成员'" ok="保存" :busy="saving" @ok="save">
    <TgField label="用户名" required hint="须是已存在且未禁用的用户">
      <input v-model="form.username" class="field" :disabled="!!editing" list="tg-member-users" autofocus />
      <datalist id="tg-member-users"><option v-for="u in users" :key="u.username" :value="u.username">{{ u.name || u.username }}</option></datalist>
    </TgField>
    <TgField label="项目角色" required>
      <select v-model="form.role" class="select"><option v-for="(l, k) in PROJECT_ROLES" :key="k" :value="k">{{ l }}</option></select>
      <small class="muted">{{ ROLE_DESC[form.role] }}</small>
    </TgField>
  </TgDialog>
</template>

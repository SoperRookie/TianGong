<script setup>
/* 用户新建 / 编辑对话框（2.12 / 2.13）：新建填用户名 + 初始口令；编辑改角色 / 资料 / 状态。 */
import { ref, reactive, watch } from 'vue'
import { api } from '@/api/client'
import { useUiStore } from '@/stores/ui'
import { useAuthStore } from '@/stores/auth'
import { TgDialog, TgField } from '@/components/ui'
const props = defineProps({ modelValue: Boolean, user: { type: Object, default: null } })
const emit = defineEmits(['update:modelValue', 'saved'])
const ui = useUiStore(), auth = useAuthStore()
const form = reactive({ username: '', password: '', role: 'member', name: '', email: '', phone: '', status: 'active' })
const saving = ref(false)
watch(() => props.modelValue, v => {
  if (!v) return
  const u = props.user
  Object.assign(form, { username: u?.username || '', password: '', role: u?.role || 'member', name: u?.name || '', email: u?.email || '', phone: u?.phone || '', status: u?.status || 'active' })
})
async function save() {
  saving.value = true
  try {
    if (props.user) {
      const body = { role: form.role !== props.user.role ? form.role : undefined, name: form.name, email: form.email, phone: form.phone, status: form.status !== props.user.status ? form.status : undefined }
      const r = await api.put(`/api/v1/auth/users/${encodeURIComponent(props.user.username)}`, body)
      ui.toast(`已保存${body.role || body.status ? '，该用户会话已失效' : ''}`); emit('saved', r)
    } else {
      if (!form.username.trim() || !form.password) return ui.toast('用户名与初始口令必填', 'warn')
      const r = await api.post('/api/v1/auth/users', { username: form.username.trim(), password: form.password, role: form.role, name: form.name, email: form.email, phone: form.phone })
      ui.toast('用户已创建'); emit('saved', r)
    }
    emit('update:modelValue', false)
  } catch (e) { ui.error(e) } finally { saving.value = false }
}
</script>
<template>
  <TgDialog :model-value="modelValue" :title="user ? `编辑用户 · ${user.username}` : '添加用户'" ok="保存" :busy="saving" @update:model-value="emit('update:modelValue', $event)" @ok="save">
    <div class="form-grid">
      <TgField v-if="!user" label="用户名" required><input v-model="form.username" class="field" autocomplete="off" /></TgField>
      <TgField v-if="!user" label="初始口令" required hint="至少 6 位；告知用户后建议其自行修改"><input v-model="form.password" class="field" type="password" autocomplete="new-password" /></TgField>
      <TgField label="系统角色"><select v-model="form.role" class="select" :disabled="user && user.username === auth.me"><option value="member">成员</option><option value="admin">系统管理员</option></select></TgField>
      <TgField v-if="user" label="状态"><select v-model="form.status" class="select" :disabled="user.username === auth.me"><option value="active">启用</option><option value="disabled">禁用（会话立即失效）</option></select></TgField>
      <TgField label="姓名"><input v-model="form.name" class="field" /></TgField>
      <TgField label="邮箱"><input v-model="form.email" class="field" type="email" /></TgField>
      <TgField label="手机"><input v-model="form.phone" class="field" /></TgField>
    </div>
    <div class="information"><span>角色变更 / 禁用 / 重置口令均使该用户全部会话失效；项目角色在项目 › 成员中设置。</span></div>
  </TgDialog>
</template>

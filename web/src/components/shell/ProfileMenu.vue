<script setup>
import { ref, reactive, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useUiStore } from '@/stores/ui'
import { useAuthStore } from '@/stores/auth'
import { api } from '@/api/client'
import { TgDrawer, TgField, TgBadge } from '@/components/ui'
const ui = useUiStore(), auth = useAuthStore(), router = useRouter()
const form = reactive({ name: '', email: '', phone: '', avatar: '' })
const saving = ref(false), pwd = reactive({ old: '', n1: '', n2: '' }), totp = reactive({ step: 0, uri: '', secret: '', code: '', password: '' })
watch(() => ui.profile, v => { if (v && auth.user) Object.assign(form, { name: auth.user.name || '', email: auth.user.email || '', phone: auth.user.phone || '', avatar: auth.user.avatar || '' }) })
async function save() {
  saving.value = true
  try { auth.user = await api.put('/api/v1/auth/me', form); ui.toast('资料已保存') } catch (e) { ui.error(e) } finally { saving.value = false }
}
function onAvatar(e) {
  const f = e.target.files?.[0]; if (!f) return
  if (f.size > 150 * 1024) return ui.toast('头像须 ≤ 150KB', 'warn')
  const r = new FileReader(); r.onload = () => { form.avatar = r.result }; r.readAsDataURL(f)
}
async function changePwd() {
  if (!pwd.old || !pwd.n1) return ui.toast('请填写旧口令与新口令', 'warn')
  if (pwd.n1 !== pwd.n2) return ui.toast('两次输入的新口令不一致', 'warn')
  try { await auth.changePassword(pwd.old, pwd.n1); ui.toast('口令已修改'); Object.assign(pwd, { old: '', n1: '', n2: '' }) } catch (e) { ui.error(e) }
}
async function totpSetup() { try { const r = await api.post('/api/v1/auth/totp/setup', {}); Object.assign(totp, { step: 1, uri: r.otpauth_uri || r.uri || '', secret: r.secret || '', code: '' }) } catch (e) { ui.error(e) } }
async function totpEnable() { try { await api.post('/api/v1/auth/totp/enable', { code: totp.code }); ui.toast('两步验证已开启'); totp.step = 0; await auth.fetchMe() } catch (e) { ui.error(e) } }
async function totpDisable() { try { await api.post('/api/v1/auth/totp/disable', { password: totp.password, code: totp.code }); ui.toast('两步验证已关闭'); totp.step = 0; totp.password = ''; totp.code = ''; await auth.fetchMe() } catch (e) { ui.error(e) } }
async function logout() { ui.profile = false; await auth.logout(); router.push({ name: 'login' }) }
</script>
<template>
  <TgDrawer :model-value="ui.profile" title="个人资料" :sub="`${auth.user?.username || ''} · ${auth.isAdmin ? '系统管理员' : '成员'}`" @update:model-value="ui.profile = $event">
    <template v-if="auth.user">
      <div class="row" style="margin-bottom:12px"><span class="avatar big"><img v-if="form.avatar" :src="form.avatar" alt="" /><template v-else>{{ (form.name || auth.user.username).slice(0, 1).toUpperCase() }}</template></span><label class="btn small">更换头像<input type="file" accept="image/*" hidden @change="onAvatar" /></label><button v-if="form.avatar" type="button" class="btn small ghost" @click="form.avatar = ''">移除</button></div>
      <TgField label="姓名"><input v-model="form.name" class="field" /></TgField>
      <TgField label="邮箱"><input v-model="form.email" class="field" type="email" /></TgField>
      <TgField label="手机"><input v-model="form.phone" class="field" /></TgField>
      <div class="row right" style="margin:8px 0 18px"><button type="button" class="btn primary" :disabled="saving" @click="save">保存资料</button></div>
      <h4>修改口令</h4>
      <TgField label="当前口令"><input v-model="pwd.old" class="field" type="password" autocomplete="current-password" /></TgField>
      <TgField label="新口令" hint="至少 8 位，含字母与数字"><input v-model="pwd.n1" class="field" type="password" autocomplete="new-password" /></TgField>
      <TgField label="再输一次"><input v-model="pwd.n2" class="field" type="password" autocomplete="new-password" /></TgField>
      <div class="row right" style="margin:8px 0 18px"><button type="button" class="btn" @click="changePwd">修改口令</button></div>
      <h4>两步验证 <TgBadge v-if="auth.user.totp_bound" status="enabled" text="已绑定" /><TgBadge v-else status="disabled" text="未绑定" /></h4>
      <div v-if="auth.settings && !auth.settings.totp_enabled" class="information">系统未开启两步验证功能（设置 › 访问控制）。</div>
      <template v-else-if="!auth.user.totp_bound">
        <button v-if="totp.step === 0" type="button" class="btn" @click="totpSetup">绑定验证器</button>
        <div v-else class="information" style="display:block">
          <div>用验证器 App 扫描或手动输入密钥：</div>
          <div class="mono pre" style="margin:8px 0">{{ totp.secret || totp.uri }}</div>
          <div class="row"><input v-model="totp.code" class="field" style="width:140px" placeholder="6 位动态码" /><button type="button" class="btn primary" @click="totpEnable">确认开启</button><button type="button" class="btn ghost" @click="totp.step = 0">取消</button></div>
        </div>
      </template>
      <div v-else class="row"><input v-model="totp.password" class="field" type="password" style="width:160px" placeholder="当前口令" /><input v-model="totp.code" class="field" style="width:120px" placeholder="动态码" /><button type="button" class="btn danger" @click="totpDisable">关闭两步验证</button></div>
      <div class="danger-zone" style="margin-top:22px"><button type="button" class="btn" @click="logout">退出登录</button></div>
    </template>
  </TgDrawer>
</template>

<script setup>
/* 强制绑定两步验证：管理员开启「强制全员绑定」后，未绑定的账号登录进来（或任一接口返回 403 must_bind_totp）即弹出，
   绑完才能继续；不能关闭，只能绑定或退出登录。初始口令强制修改优先（改完口令再弹这个）。 */
import { reactive, ref, computed, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { api } from '@/api/client'
import { TgDialog } from '@/components/ui'
const auth = useAuthStore(), ui = useUiStore(), router = useRouter()
const open = computed(() => auth.mustBindTotp && !auth.mustChangePassword && !!auth.token)
const t = reactive({ secret: '', qr: '', code: '', err: '' }), loading = ref(false), busy = ref(false)
async function setup() {
  loading.value = true; t.err = ''
  try { const r = await api.post('/api/v1/auth/totp/setup', {}); Object.assign(t, { secret: r.secret || '', qr: r.qr_svg || '', code: '' }) }
  catch (e) {
    // 已绑定（别的标签页刚绑完）或管理员刚关了强制：以服务端为准刷新一次
    await auth.fetchMe()
    if (auth.mustBindTotp) t.err = e.message || '获取绑定二维码失败'
  } finally { loading.value = false }
}
watch(open, v => { if (v) setup() }, { immediate: true })
async function ok() {
  const code = t.code.trim()
  if (!/^\d{6}$/.test(code)) { t.err = '请输入验证器上的 6 位数字动态码'; return }
  busy.value = true; t.err = ''
  try {
    await api.post('/api/v1/auth/totp/enable', { code })
    await auth.fetchMe()
    auth.mustBindTotp = !!auth.user?.must_bind_totp
    ui.toast('两步验证已绑定，下次登录需输入动态验证码')
    // 绑定前页面上的请求都被拦了，绑完整页重载一次把数据拉回来
    if (!auth.mustBindTotp) setTimeout(() => window.location.reload(), 600)
  } catch (e) { t.err = e.message || '绑定失败' } finally { busy.value = false }
}
async function logout() { await auth.logout(); router.push({ name: 'login' }) }
</script>
<template>
  <TgDialog :model-value="open" title="须先绑定两步验证" no-foot @update:model-value="() => {}">
    <div class="attention" style="margin-bottom:12px">管理员已要求全员开启两步验证。绑定验证器后才能继续使用系统；之后每次登录除口令外还需输入 6 位动态验证码。</div>
    <div v-if="loading" class="muted">正在生成绑定二维码…</div>
    <template v-else-if="t.secret">
      <ol style="margin:0 0 10px;padding-left:18px;line-height:1.9">
        <li>手机安装验证器 App：Google Authenticator、Microsoft Authenticator 等任一标准验证器。</li>
        <li>在 App 里扫描下面的二维码；无法扫码时选择「手动输入密钥」。</li>
        <li>把 App 上显示的 6 位数字填到下面，点「绑定并继续」。</li>
      </ol>
      <div class="row" style="align-items:flex-start;gap:16px;flex-wrap:wrap">
        <div v-if="t.qr" class="totp-qr" v-html="t.qr"></div>
        <div style="flex:1;min-width:220px">
          <div class="tiny muted">手动输入密钥</div>
          <div class="mono pre" style="margin:4px 0 12px;word-break:break-all;user-select:all;-webkit-user-select:all">{{ t.secret }}</div>
          <div class="tiny muted">6 位动态验证码</div>
          <input v-model="t.code" class="field mono" style="width:160px;margin-top:4px;font-size:16px;letter-spacing:3px" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="000000" autofocus @keydown.enter="ok" />
        </div>
      </div>
    </template>
    <div v-if="t.err" class="err-box" style="margin-top:10px">{{ t.err }}</div>
    <div class="row" style="justify-content:space-between;margin-top:16px">
      <button type="button" class="btn ghost" @click="logout">退出登录</button>
      <span class="row"><button v-if="!loading && !t.secret" type="button" class="btn" @click="setup">重新获取二维码</button><button type="button" class="btn primary" :disabled="busy || loading || !t.secret" @click="ok">{{ busy ? '绑定中…' : '绑定并继续' }}</button></span>
    </div>
  </TgDialog>
</template>

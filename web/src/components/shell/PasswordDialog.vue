<script setup>
/* 初始口令强制修改：403 must_change_password 时弹出，改完才能继续。 */
import { reactive, ref } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { TgDialog, TgField } from '@/components/ui'
const auth = useAuthStore(), ui = useUiStore()
const f = reactive({ old: '', n1: '', n2: '' }), busy = ref(false)
async function ok() {
  if (f.n1 !== f.n2) return ui.toast('两次输入的新口令不一致', 'warn')
  busy.value = true
  try { await auth.changePassword(f.old, f.n1); ui.toast('口令已修改，可以继续使用系统') } catch (e) { ui.error(e) } finally { busy.value = false }
}
</script>
<template>
  <TgDialog :model-value="auth.mustChangePassword" title="初始口令须先修改" ok="修改并继续" :busy="busy" @update:model-value="() => {}" @ok="ok">
    <div class="attention" style="margin-bottom:10px">首次登录或管理员重置口令后，须先设置自己的口令才能使用系统。</div>
    <TgField label="当前口令"><input v-model="f.old" class="field" type="password" /></TgField>
    <TgField label="新口令" hint="至少 8 位，含字母与数字"><input v-model="f.n1" class="field" type="password" /></TgField>
    <TgField label="再输一次"><input v-model="f.n2" class="field" type="password" /></TgField>
  </TgDialog>
</template>

<script setup>
/* 访问控制（2.9 / 2.15）：数据可见范围三模式、两步验证总开关（仅管理员可改）；角色动作矩阵与仅管理员动作说明。 */
import { ref, reactive, onMounted } from 'vue'
import { api } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { PageHeading, TgPanel, TgIcon, TgBadge } from '@/components/ui'
const auth = useAuthStore(), ui = useUiStore()
const form = reactive({ access_mode: 'member', totp_enabled: true }), cur = ref(null), saving = ref(false)
async function load() { try { cur.value = await api.get('/api/v1/auth/settings'); Object.assign(form, cur.value) } catch (e) { ui.error(e) } }
onMounted(load)
const MODES = [
  ['open', '开放', '所有登录用户可见全部项目，一律视同项目管理员；删除仅系统管理员'],
  ['line', '按产品线成员', '可见 = 成员项目 ∪ 所属产品线下全部项目；项目角色与产品线角色折算取高者；删除仅系统管理员'],
  ['member', '按项目成员', '只见自己是成员的项目，角色按项目成员设置；删除按项目角色矩阵'],
]
const dirty = () => cur.value && (form.access_mode !== cur.value.access_mode || form.totp_enabled !== cur.value.totp_enabled)
async function save() {
  if (!dirty()) return
  const notes = []
  if (form.access_mode !== cur.value.access_mode) notes.push(`数据可见范围 → ${MODES.find(m => m[0] === form.access_mode)[1]}`)
  if (form.totp_enabled !== cur.value.totp_enabled) notes.push(form.totp_enabled ? '开启两步验证功能' : '关闭两步验证功能（已绑定用户登录不再要求动态码，密钥保留）')
  if (!await ui.confirm('保存访问控制？', notes.join('；') + '。立即对全平台生效。', { ok: '保存' })) return
  saving.value = true
  try { cur.value = await api.put('/api/v1/auth/settings', { access_mode: form.access_mode, totp_enabled: form.totp_enabled }); Object.assign(form, cur.value); auth.settings = cur.value; await auth.fetchMe(); ui.toast('已保存') } catch (e) { ui.error(e) } finally { saving.value = false }
}
/* 登录 IP 白名单：开启后只有名单内 IP（单个 / CIDR）能登录；开启状态下保存时操作人当前 IP 必须在名单里（后端校验） */
const wl = reactive({ enabled: false, entries: [], client_ip: '' }), wlCur = ref(null), wlSaving = ref(false), wlNew = reactive({ ip: '', note: '' })
async function loadWl() { if (!auth.isAdmin) return; try { wlCur.value = await api.get('/api/v1/auth/ip-whitelist'); Object.assign(wl, { enabled: wlCur.value.enabled, entries: wlCur.value.entries.map(e => ({ ...e })), client_ip: wlCur.value.client_ip }) } catch (e) { ui.error(e) } }
onMounted(loadWl)
const wlLoop = () => !wl.client_ip || wl.client_ip === '127.0.0.1' || wl.client_ip === '::1'
const wlCovered = () => wl.entries.some(e => e.ip === wl.client_ip)
const wlDirty = () => wlCur.value && JSON.stringify({ e: wl.enabled, l: wl.entries.map(e => [e.ip, e.note || '']) }) !== JSON.stringify({ e: wlCur.value.enabled, l: wlCur.value.entries.map(e => [e.ip, e.note || '']) })
function wlAdd(ip, note) {
  ip = (ip ?? wlNew.ip).trim(); note = (note ?? wlNew.note).trim()
  if (!ip) return ui.toast('请填写 IP 或网段', { kind: 'warn' })
  if (wl.entries.some(e => e.ip === ip)) return ui.toast('该 IP 已在名单里', { kind: 'warn' })
  wl.entries.push({ ip, note }); wlNew.ip = ''; wlNew.note = ''
}
async function saveWl() {
  if (!wlDirty()) return
  if (wl.enabled && !await ui.confirm('开启登录 IP 白名单？', `保存后只有名单内的 ${wl.entries.length} 条 IP / 网段能登录，其他地址一律拒绝（已登录的会话不受影响）。`, { ok: '保存' })) return
  wlSaving.value = true
  try {
    wlCur.value = await api.put('/api/v1/auth/ip-whitelist', { enabled: wl.enabled, entries: wl.entries.map(e => ({ ip: e.ip, note: e.note || '' })) })
    Object.assign(wl, { enabled: wlCur.value.enabled, entries: wlCur.value.entries.map(e => ({ ...e })), client_ip: wlCur.value.client_ip })
    ui.toast(wl.enabled ? `登录 IP 白名单已开启：${wl.entries.length} 条` : '登录 IP 白名单已关闭')
  } catch (e) { ui.error(e) } finally { wlSaving.value = false }
}
const MATRIX = [['查看需求 / 任务 / 用例 / 计划', 1, 1, 1, 1], ['新建需求 / 发起设计 / 修改测试点与用例', 1, 1, 1, 0], ['审核测试点 / 用例', 1, 1, 0, 0], ['加入计划 / 分配 / 新建轮次', 1, 1, 0, 0], ['记录执行结果 / 归因 / 附件', 1, 1, 1, 0], ['导出用例', 1, 1, 1, 0], ['知识入库（项目 / 模块层）', 1, 1, 0, 0], ['项目成员 / 项目设置', 1, 0, 0, 0], ['删除（仅 member 模式按矩阵）', 1, 1, 0, 0]]
const ADMIN_ONLY = ['安全设置写', '用户管理', '模型配置', '模板管理', '记忆增删改', '公共层知识入库 / 删除', '回溯沉淀', '学习规则', 'Prompt 版本', '评测集与评测运行', '推荐答案', '测试计划删除', '产品线删除', '项目删除', '遗留任务归属', 'AI 调用统计全量视角', '他人 AI 调用详情', '「未指定项目」遗留数据']
</script>
<template>
  <PageHeading title="访问控制" sub="系统角色 admin / member；项目角色四级动作矩阵；产品线角色折算；三种可见范围模式下的可见过滤与写校验；删除权限单独收口。后端必须鉴权，前端禁用按钮只做显隐。">
    <template #actions><button v-if="auth.isAdmin" type="button" class="btn primary" :disabled="!dirty() || saving" @click="save">{{ saving ? '保存中…' : '保存' }}</button></template>
  </PageHeading>
  <div class="grid-2">
    <TgPanel title="数据可见范围" hint="GET / PUT /api/v1/auth/settings · 仅管理员可改">
      <div class="list">
        <label v-for="[k, n, d] in MODES" :key="k" class="row" style="align-items:flex-start;cursor:pointer"><input v-model="form.access_mode" type="radio" :value="k" :disabled="!auth.isAdmin" style="margin-top:4px" /><span><b>{{ n }}</b> <span class="mono tiny muted">{{ k }}</span><TgBadge v-if="cur?.access_mode === k" color="green" text="当前" style="margin-left:6px" /><div class="tiny muted">{{ d }}</div></span></label>
      </div>
      <div class="attention" style="margin-top:10px"><TgIcon name="alert" :size="14" /> 删除权限收口（_require_delete）：开放 / 按产品线模式下，需求、测试点、用例、计划、版本、模块、项目层知识、依赖边等的删除仅系统管理员；仅在按项目成员模式下沿用项目角色矩阵。「未指定项目」的遗留数据非管理员一律不可见。</div>
    </TgPanel>
    <div>
      <TgPanel title="两步验证" hint="TOTP · SHA1 / 30 秒 / 6 位；用户在个人资料抽屉绑定 / 解绑">
        <label class="row" style="cursor:pointer"><input v-model="form.totp_enabled" type="checkbox" :disabled="!auth.isAdmin" /> <b>{{ form.totp_enabled ? '已开启' : '已关闭' }}</b><span class="tiny muted">关闭后全平台登录不再校验动态码、不可新绑定；已绑定用户的密钥保留，重新开启后继续生效。</span></label>
        <div class="list" style="margin-top:10px">
          <div><b>登录保护</b><div class="tiny muted">口令 PBKDF2；同用户名 + IP 连续失败锁定；初始管理员随机口令只打印一次并强制改密</div></div>
          <div><b>会话</b><div class="tiny muted">Bearer Token；下载类路径支持 ?token=；会话过期 / 账号禁用 / 强制改密拦截；角色变更、禁用、重置口令使会话失效</div></div>
          <div><b>管理员解绑</b><div class="tiny muted">用户与安全页行内「解绑两步验证」，用于手机丢失后重新绑定</div></div>
        </div>
      </TgPanel>
      <TgPanel class="mt" title="仅系统管理员"><div class="pill-row"><span v-for="x in ADMIN_ONLY" :key="x" class="tag">{{ x }}</span></div></TgPanel>
    </div>
  </div>
  <TgPanel v-if="auth.isAdmin" class="mt" title="登录 IP 白名单" hint="GET / PUT /api/v1/auth/ip-whitelist · 开启后只有名单内 IP 能登录，其余在校验密码前即拒绝并记入安全日志（动作「登录被拒（IP 未加白）」，带 IP）">
    <template #actions><button type="button" class="btn primary" :disabled="!wlDirty() || wlSaving" @click="saveWl">{{ wlSaving ? '保存中…' : '保存' }}</button></template>
    <div class="row" style="flex-wrap:wrap;gap:12px;align-items:center">
      <label class="row" style="cursor:pointer"><input v-model="wl.enabled" type="checkbox" /> <b>{{ wl.enabled ? '已开启' : '已关闭' }}</b></label>
      <span class="tiny muted">服务端看到的你当前的 IP：<b class="mono">{{ wl.client_ip || '未知' }}</b>
        <TgBadge v-if="wlCovered()" color="green" text="已在名单" style="margin-left:6px" />
        <a v-else-if="wl.client_ip" href="javascript:;" style="margin-left:6px" @click="wlAdd(wl.client_ip, auth.user?.username || '')">加入名单</a></span>
    </div>
    <div v-if="wlLoop()" class="attention" style="margin-top:8px"><TgIcon name="alert" :size="14" /> 看到的是本机回环地址或为空：请求经过了反向代理且未配置 TIANGONG_TRUSTED_PROXIES，此时所有人的 IP 都会被当成代理地址，白名单形同虚设。请先按部署文档配置可信代理再开启。</div>
    <div class="tiny muted" style="margin-top:6px">开启状态下保存时，你当前的 IP 必须在名单里，否则会被拒绝保存，避免把自己锁在外面。</div>
    <div class="table-wrap" style="margin-top:10px"><table class="table dense">
      <thead><tr><th style="width:200px">IP / 网段</th><th>备注（谁 / 哪里）</th><th style="width:170px">加入</th><th style="width:50px"></th></tr></thead>
      <tbody>
        <tr v-for="(e, i) in wl.entries" :key="e.ip"><td class="mono">{{ e.ip }}<TgBadge v-if="e.ip === wl.client_ip" text="当前" style="margin-left:6px" /></td><td><input v-model="e.note" class="input" maxlength="64" placeholder="如 张三 / 办公室出口" style="width:100%" /></td><td class="tiny muted">{{ e.by || '' }}<div v-if="e.at">{{ e.at.replace('T', ' ').slice(0, 16) }}</div></td><td><button type="button" class="btn ghost sm" title="移除" @click="wl.entries.splice(i, 1)">✕</button></td></tr>
        <tr v-if="!wl.entries.length"><td colspan="4" class="muted" style="text-align:center">名单为空（为空时不能开启）</td></tr>
      </tbody>
    </table></div>
    <div class="row" style="gap:8px;margin-top:10px">
      <input v-model="wlNew.ip" class="input mono" placeholder="单个 IP 如 10.1.2.3，网段如 10.1.2.0/24" style="width:280px" @keydown.enter="wlAdd()" />
      <input v-model="wlNew.note" class="input" maxlength="64" placeholder="备注：谁 / 哪里" style="flex:1" @keydown.enter="wlAdd()" />
      <button type="button" class="btn" @click="wlAdd()">＋ 添加</button>
    </div>
  </TgPanel>
  <TgPanel class="mt" title="项目角色动作矩阵" hint="can(project, action) 控制按钮显隐；_require_* 在接口里校验（app/permissions.py）">
    <div class="table-wrap"><table class="table matrix dense">
      <thead><tr><th>动作</th><th>项目管理员</th><th>测试负责人</th><th>测试人员</th><th>只读人员</th></tr></thead>
      <tbody><tr v-for="r in MATRIX" :key="r[0]"><td>{{ r[0] }}</td><td v-for="(x, i) in r.slice(1)" :key="i"><span v-if="x" class="ok">✓</span><span v-else class="no">—</span></td></tr></tbody>
    </table></div>
    <div class="tiny muted" style="margin-top:8px">产品线角色折算：产品线负责人 → 项目管理员，产品线成员 → 测试人员；按产品线模式下与直接项目角色取高者（viewer &lt; tester &lt; test_lead &lt; project_admin）。</div>
  </TgPanel>
</template>

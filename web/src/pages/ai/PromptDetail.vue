<script setup>
/* Prompt 详情（11.16～11.20）：版本链、查看内容 / 展开共享片段、基于某版本新建、激活 / 回滚、归档草稿、版本间逐行对比。 */
import { ref, computed, watch, reactive } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtTime, num, copyText } from '@/utils/format'
import { PageHeading, TgPanel, TgBadge, TgDialog, TgField, TgSeg, TgEmpty, TgIcon } from '@/components/ui'
import { lineDiff } from './components/lineDiff'

const route = useRoute(), router = useRouter(), auth = useAuthStore(), ui = useUiStore()
const key = computed(() => route.params.key)
const { data: p, loading, error, reload } = useLoad(() => api.get(`/api/v1/ai/prompts/${key.value}`), { deps: [key] })
const versions = computed(() => [...(p.value?.versions || [])].sort((a, b) => b.version_no - a.version_no))
const viewNo = ref(route.query.v ? +route.query.v : 0)
watch(p, v => { if (v && !versions.value.some(x => x.version_no === viewNo.value)) viewNo.value = v.active_version })
watch(viewNo, v => router.replace({ query: { ...route.query, v: v && v !== p.value?.active_version ? v : undefined } }))
const cur = computed(() => versions.value.find(x => x.version_no === viewNo.value) || versions.value[0])
const ST = { active: ['生效', 'green'], draft: ['草稿', 'orange'], archived: ['已归档', 'gray'] }
const mode = ref('content')   // content / expanded / diff
const expanded = computed(() => cur.value && cur.value.version_no === p.value?.active_version ? (p.value?.expanded || '') : '')
const hasShared = computed(() => /【共享:[^】]+】/.test(cur.value?.content || ''))
function view(v) { viewNo.value = v.version_no; if (mode.value === 'expanded') mode.value = 'content' }

/* 对比 */
const diffNo = ref(0)
watch(cur, () => { if (!diffNo.value || diffNo.value === cur.value?.version_no) diffNo.value = (versions.value.find(x => x.version_no !== cur.value?.version_no) || {}).version_no || 0 })
const diffTarget = computed(() => versions.value.find(x => x.version_no === diffNo.value))
const diff = computed(() => diffTarget.value && cur.value ? lineDiff(diffTarget.value.content, cur.value.content) : [])
const diffStat = computed(() => ({ add: diff.value.filter(d => d.type === 'add').length, del: diff.value.filter(d => d.type === 'del').length }))

/* 新建版本 */
const dlg = ref(false), saving = ref(false)
const form = reactive({ content: '', note: '', activate: false, base: 0 })
function openNew() { form.content = cur.value?.content || ''; form.note = ''; form.activate = false; form.base = cur.value?.version_no || 0; dlg.value = true }
async function save() {
  if (!form.content.trim()) return ui.toast('内容不能为空', 'warn')
  saving.value = true
  try {
    const r = await api.post(`/api/v1/ai/prompts/${key.value}/versions`, { content: form.content, note: form.note || `基于 v${form.base} 修改`, activate: form.activate })
    ui.toast(form.activate ? '新版本已激活生效' : '已保存为草稿')
    dlg.value = false; p.value = r; viewNo.value = Math.max(...r.versions.map(v => v.version_no)); mode.value = 'content'
  } catch (e) { ui.error(e) } finally { saving.value = false }
}
async function activate(v) {
  if (!await ui.confirm(v.status === 'archived' ? `回滚到 v${v.version_no}？` : `激活 v${v.version_no}？`, '该版本立即生效，原生效版本归档；之后的模型调用登记新版本号。', { ok: '激活' })) return
  try { p.value = await api.post(`/api/v1/ai/prompts/${key.value}/versions/${v.version_no}/activate`, {}); ui.toast(`v${v.version_no} 已生效`) } catch (e) { ui.error(e) }
}
async function archive(v) {
  try { p.value = await api.post(`/api/v1/ai/prompts/${key.value}/versions/${v.version_no}/archive`, {}); ui.toast(`v${v.version_no} 已归档`) } catch (e) { ui.error(e) }
}
</script>
<template>
  <PageHeading :title="p?.name || key">
    <template #back><router-link :to="{ name: 'prompts' }"><TgIcon name="back" :size="14" /> Prompt 管理</router-link></template>
    <template #badge><TgBadge v-if="p" :color="p.active_version !== 1 ? 'purple' : 'gray'" :text="p.active_version !== 1 ? '已定制' : '内置默认'" /></template>
    <template #sub><span class="mono">{{ key }}</span> · {{ p?.kind }} · {{ p?.where }}</template>
    <template #actions><button v-if="auth.isAdmin && cur" type="button" class="btn primary" @click="openNew">基于 v{{ cur.version_no }} 新建版本</button></template>
  </PageHeading>
  <div v-if="error" class="err-box">{{ error.message }}</div>
  <div v-if="loading && !p" class="page-loading"><span class="spinner" /> 加载中…</div>
  <div v-else-if="p" class="split">
    <TgPanel>
      <template #title>v{{ cur?.version_no }} <TgBadge v-if="cur" :color="ST[cur.status]?.[1]" :text="ST[cur.status]?.[0] || cur.status" /></template>
      <template #hint>占位符：{{ (p.placeholders || []).map(x => '{' + x + '}').join(' ') || '无' }}<template v-if="hasShared"> · 正文含【共享:名称】标记，模型实际收到的是展开后的全文</template></template>
      <template #actions>
        <TgSeg v-model="mode" :items="[['content', '正文'], ...(expanded ? [['expanded', '展开后全文']] : []), ...(versions.length > 1 ? [['diff', '版本对比']] : [])]" />
        <button type="button" class="btn small" @click="copyText(mode === 'expanded' ? expanded : (cur?.content || '')).then(() => ui.toast('已复制'))"><TgIcon name="copy" :size="13" /></button>
      </template>
      <template v-if="mode === 'diff'">
        <div class="row mb"><span class="small">对比：</span><select v-model="diffNo" class="select small"><option v-for="v in versions.filter(x => x.version_no !== cur?.version_no)" :key="v.version_no" :value="v.version_no">v{{ v.version_no }}（{{ ST[v.status]?.[0] }}）</option></select><span class="small">→ v{{ cur?.version_no }}</span><span class="tiny muted">新增 <span class="ok">+{{ diffStat.add }}</span> · 删除 <span style="color:#db5361">−{{ diffStat.del }}</span> 行</span></div>
        <div class="pre" style="max-height:640px"><div v-for="(d, i) in diff" :key="i" :class="{ 'diff-add': d.type === 'add', 'diff-del': d.type === 'del' }">{{ d.type === 'add' ? '+ ' : d.type === 'del' ? '− ' : '  ' }}{{ d.text || ' ' }}</div></div>
      </template>
      <div v-else class="pre" style="max-height:640px">{{ mode === 'expanded' ? expanded : cur?.content }}</div>
      <div v-if="cur" class="tiny muted" style="margin-top:6px">{{ cur.note || '（无说明）' }} · {{ cur.created_by || 'system' }} · {{ fmtTime(cur.created_at) }} · {{ num((cur.content || '').length) }} 字<template v-if="cur.activated_at"> · 激活于 {{ fmtTime(cur.activated_at) }}（{{ cur.activated_by || '—' }}）</template></div>
      <div v-if="auth.isAdmin && cur && cur.status !== 'active'" class="row" style="margin-top:10px">
        <button type="button" class="btn success small" @click="activate(cur)">{{ cur.status === 'archived' ? '回滚到此版本' : '激活生效' }}</button>
        <button v-if="cur.status === 'draft'" type="button" class="btn small" @click="archive(cur)">归档草稿</button>
      </div>
    </TgPanel>
    <div>
      <TgPanel title="版本链" hint="占位符须与默认版本一致方可保存；激活使原生效版本归档">
        <div class="list">
          <div v-for="v in versions" :key="v.version_no" class="row clickable" :class="{ cur: v.version_no === cur?.version_no }" @click="view(v)">
            <b>v{{ v.version_no }}</b> <TgBadge :color="ST[v.status]?.[1]" :text="ST[v.status]?.[0] || v.status" />
            <span class="tiny muted ellipsis" style="max-width:150px" :title="v.note">{{ v.note || '' }} · {{ fmtTime(v.created_at) }}</span>
            <span v-if="auth.isAdmin && v.status !== 'active'" class="link tiny right" @click.stop="activate(v)">{{ v.status === 'archived' ? '回滚' : '激活' }}</span>
          </div>
        </div>
      </TgPanel>
      <TgPanel class="mt" title="取用说明">
        <div class="list small">
          <div><b>服务端取用</b><div class="tiny muted">prompt_text(key) 取生效版本并展开【共享:名称】，登记版本到调用上下文，调用日志与任务记录可回溯。</div></div>
          <div><b>内置默认</b><div class="tiny muted">v1 随代码更新；定制版本保存在数据库，升级不覆盖。</div></div>
          <div><router-link :to="{ name: 'calls', query: { keyword: key } }">本 Prompt 的调用日志 →</router-link></div>
        </div>
      </TgPanel>
    </div>
  </div>
  <TgEmpty v-else-if="!loading" title="Prompt 不存在" />

  <TgDialog v-model="dlg" :title="`新建版本 · 基于 v${form.base}`" :ok="form.activate ? '保存并激活' : '保存为草稿'" :busy="saving" wide @ok="save">
    <TgField label="内容" required :hint="(p?.placeholders || []).length ? `必须包含占位符 ${(p.placeholders).map(x => '{' + x + '}').join(' ')}；字面花括号写成 {{ }}` : '该 Prompt 不经 .format 注入，可自由编辑'"><textarea v-model="form.content" class="field mono" rows="18" /></TgField>
    <TgField label="版本说明"><input v-model="form.note" class="field" placeholder="改了什么、为什么" /></TgField>
    <label class="row small"><input v-model="form.activate" type="checkbox" /> 保存后立即激活生效（原生效版本归档）</label>
  </TgDialog>
</template>
<style scoped>
.list .row.cur { background: #eef3ff; margin: 0 -8px; padding-left: 8px; padding-right: 8px; border-radius: 3px; }
</style>

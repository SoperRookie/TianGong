<script setup>
/* ① 原文与附件：附件表（解析状态 / 解析全文 / 下载 / 重新解析 / 删除）、追加上传 / 链接、原文与人工补充 */
import { ref } from 'vue'
import { api } from '@/api/client'
import { useUiStore } from '@/stores/ui'
import { fmtTime, fmtSize, num } from '@/utils/format'
import { TgBadge, TgDrawer, TgDialog, TgEmpty, TgIcon } from '@/components/ui'
import { parserName } from './reqUtil'
const props = defineProps({ r: { type: Object, required: true }, canEdit: Boolean })
const emit = defineEmits(['reload'])
const ui = useUiStore()
const textDrawer = ref(null), textLoading = ref(false)
const linkOpen = ref(false), urls = ref(''), busy = ref(false)
const fileInput = ref(null)
const attState = a => a.parsing ? { status: 'running', text: a.source_url && !a.stored ? '抓取中' : '解析中' } : a.parsed ? (a.vision?.failed ? { status: 'partial', text: '部分解析' } : { status: 'ok', text: '已解析' }) : { status: 'error', text: a.source_url && !a.stored ? '抓取失败' : '解析失败' }
async function viewText(a) {
  textLoading.value = true; textDrawer.value = { filename: a.filename, text: '' }
  try { const d = await api.get(`/api/v1/requirements/${props.r.req_id}/attachments/${a.att_id}/text`); textDrawer.value = d } catch (e) { ui.error(e); textDrawer.value = null } finally { textLoading.value = false }
}
function download(a) { api.download(`/api/v1/requirements/${props.r.req_id}/attachments/${a.att_id}`, a.filename).catch(ui.error) }
async function reparse(a) {
  try { await api.post(`/api/v1/requirements/${props.r.req_id}/attachments/${a.att_id}/reparse`, {}); ui.toast(a.source_url && !a.stored ? '已开始重新抓取' : '已开始重新解析'); emit('reload') } catch (e) { ui.error(e) }
}
async function remove(a) {
  if (!await ui.confirm(`删除附件「${a.filename}」？`, '原文与其他附件不动；整理稿与分析会按输入变化标为过期，重新提取时底稿里人工收录 / 排除过的句子按原句沿用。', { ok: '删除', danger: true })) return
  try { await api.del(`/api/v1/requirements/${props.r.req_id}/attachments/${a.att_id}`); ui.toast('附件已删除'); emit('reload') } catch (e) { ui.error(e) }
}
async function upload(e) {
  const files = Array.from(e.target.files || []); e.target.value = ''
  if (!files.length) return
  const fd = new FormData(); files.forEach(f => fd.append('files', f))
  busy.value = true
  try { const x = await api.upload(`/api/v1/requirements/${props.r.req_id}/attachments`, fd); ui.toast(x.parse_failed ? `已上传，${x.parse_failed} 个文件不合法` : '已上传，正在后台解析', { kind: x.parse_failed ? 'warn' : 'ok' }); emit('reload') } catch (err) { ui.error(err) } finally { busy.value = false }
}
async function addLinks() {
  if (!urls.value.trim()) return ui.toast('请填写链接', { kind: 'warn' })
  const fd = new FormData(); fd.append('urls', urls.value)
  busy.value = true
  try { const x = await api.upload(`/api/v1/requirements/${props.r.req_id}/attachments`, fd); ui.toast(`已添加链接，${x.parsing} 个附件正在后台抓取解析`); linkOpen.value = false; urls.value = ''; emit('reload') } catch (e) { ui.error(e) } finally { busy.value = false }
}
</script>
<template>
  <div class="panel-head">
    <div><div class="panel-title">① 原文与附件</div><div class="panel-hint">文本 + 文件 + 链接三种来源；上传即落盘，后台逐个解析回写，完成后知识沉淀 + 自动提取整理稿。单文件上限 100MB。</div></div>
    <div v-if="canEdit" class="row">
      <button type="button" class="btn small" @click="linkOpen = true"><TgIcon name="link" :size="13" /> 添加链接</button>
      <button type="button" class="btn small primary" :disabled="busy" @click="fileInput?.click()"><TgIcon name="upload" :size="13" /> 上传附件</button>
      <input ref="fileInput" type="file" multiple style="display:none" @change="upload" />
    </div>
  </div>
  <div v-if="r.parsing" class="information" style="margin-bottom:10px"><span class="spinner" /> {{ r.parsing }} 个附件 / 链接正在后台解析，完成后自动刷新</div>
  <div v-if="r.content_warnings?.length" class="attention" style="margin-bottom:10px"><TgIcon name="alert" :size="14" /> <span>附件内容告警：{{ r.content_warnings.map(w => typeof w === 'string' ? w : (w.message || w.filename || JSON.stringify(w))).join('；') }}</span></div>
  <div class="table-wrap">
    <table class="table">
      <thead><tr><th>附件</th><th style="width:120px">解析器</th><th style="width:110px">结果</th><th style="width:150px">文字 / 图片</th><th style="width:230px">操作</th></tr></thead>
      <tbody>
        <tr v-for="a in r.attachments" :key="a.att_id">
          <td>
            <div style="font-weight:650">{{ a.filename || a.source_url || a.att_id }}</div>
            <div class="sub">{{ a.att_id }}<template v-if="a.size"> · {{ fmtSize(a.size) }}</template><template v-if="a.parsed_at"> · 解析 {{ fmtTime(a.parsed_at) }}</template></div>
            <div v-if="a.source_url" class="sub" style="word-break:break-all"><span class="tag">链接</span> <a :href="a.source_url" target="_blank" rel="noopener">{{ a.source_url }}</a></div>
            <div v-if="a.error" class="tiny" style="color:var(--red)">{{ a.error }}</div>
            <div v-if="a.warning" class="tiny" style="color:var(--amber)">⚠ {{ a.warning }}</div>
            <div v-if="a.check" class="tiny" :style="{ color: a.check.ok ? 'var(--green)' : 'var(--red)' }">{{ a.check.ok ? `原型自校验 ✓ 0 缺字（${(a.check.pages || []).length} 页）` : `原型自校验 ⚠ 缺 ${a.check.missing} 字，见底稿核对` }}</div>
          </td>
          <td><span class="tag">{{ parserName(a) }}</span></td>
          <td><TgBadge :status="attState(a).status" :text="attState(a).text" /><span v-if="a.parsing" class="spinner" style="margin-left:6px" /></td>
          <td class="tiny muted"><template v-if="a.parsed">{{ num(a.chars) }} 字<template v-if="a.vision?.total"> · {{ a.vision.total }} 图（Vision 成功 {{ a.vision.done }}<template v-if="a.vision.failed">，失败 {{ a.vision.failed }}</template>）</template></template><template v-else>—</template></td>
          <td class="nowrap">
            <a v-if="a.parsed" class="link" @click="viewText(a)">解析全文</a>
            <a v-if="a.stored" class="link" style="margin-left:8px" @click="download(a)">下载</a>
            <a v-if="canEdit && (a.stored || a.source_url) && !a.parsing" class="link" style="margin-left:8px" @click="reparse(a)">{{ a.source_url && !a.stored ? '重新抓取' : '重新解析' }}</a>
            <a v-if="canEdit && !a.parsing" class="link" style="margin-left:8px;color:var(--red)" @click="remove(a)">删除</a>
          </td>
        </tr>
        <tr v-if="!r.attachments.length"><td colspan="5"><TgEmpty title="没有附件" hint="上传文件或添加链接后自动解析；纯文本需求直接用原文分析" /></td></tr>
      </tbody>
    </table>
  </div>
  <div v-if="r.raw_text" class="source-block mt"><div class="source-label">需求原文（永久保留，AI 结果不覆盖）</div><div style="white-space:pre-wrap">{{ r.raw_text }}</div></div>
  <div v-if="r.description" class="source-block mt"><div class="source-label">人工补充（独立于原文，参与分析与设计）</div><div style="white-space:pre-wrap">{{ r.description }}</div></div>
  <div class="information mt"><TgIcon name="info" :size="14" /> <span>解析器：文本 / Word / PDF（含扫描 OCR）/ PPT / 表格 / 老 Office / 网页 / 压缩包 / 邮件 / 图片；Axure 原型包按站点地图逐页、按控件带编号提取文字并按坐标还原阅读顺序，整页字符自校验；独立图片与文档内嵌图经 Vision 按固定框架转文字，大图切块。</span></div>

  <TgDrawer :model-value="!!textDrawer" @update:model-value="v => { if (!v) textDrawer = null }" :title="textDrawer?.filename || '解析全文'" sub="页面文字按控件带编号、表格行、流程块、独立区域标记、图片理解块" wide>
    <div v-if="textLoading" class="page-loading"><span class="spinner" /> 加载中…</div>
    <pre v-else class="pre" style="max-height:none">{{ textDrawer?.text || '（无解析文本）' }}</pre>
  </TgDrawer>
  <TgDialog v-model="linkOpen" title="添加链接" ok="添加并抓取" :busy="busy" @ok="addLinks">
    <textarea v-model="urls" class="field" rows="4" placeholder="每行一个 http(s) 链接，最多 10 个；网页 / 图片 / PDF / Word 直链" />
  </TgDialog>
</template>

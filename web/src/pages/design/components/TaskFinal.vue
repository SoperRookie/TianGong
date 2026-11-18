<script setup>
/* 离线终稿回传（7.13）：上传离线改完的 xlsx / csv / xmind，与生成结果做字段级 diff；勾「以终稿为准」整份覆盖并定稿（approved + locked）。 */
import { ref, computed } from 'vue'
import { api } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtTime, fmtSize } from '@/utils/format'
import { TgPanel, TgKv, TgBadge, TgEmpty, TgIcon, TgSeg } from '@/components/ui'
const props = defineProps({ task: { type: Object, required: true } })
const emit = defineEmits(['reload'])
const auth = useAuthStore(), ui = useUiStore()
const files = ref([]), apply = ref(true), busy = ref(false), dragging = ref(false), view = ref('modified'), input = ref(null)
const canReview = computed(() => auth.can((props.task.context || {}).project, 'case.review'))
const o = computed(() => props.task.offline_review || null)
const hasCases = computed(() => ((props.task.result || {}).cases || []).length > 0)
function add(list) { const names = new Set(files.value.map(f => f.name + f.size)); files.value = files.value.concat(Array.from(list || []).filter(f => !names.has(f.name + f.size))) }
async function upload() {
  if (!files.value.length) return ui.toast('请选择终稿文件', 'warn')
  if (apply.value && !await ui.confirm('以终稿为准？', `用 ${files.value.length} 个文件里的用例替换线上用例并整份标为人工定稿（已通过、锁定）？文件里没有的用例进回收站（可恢复），多出来的新增。`, { ok: '上传并应用', danger: true })) return
  const fd = new FormData(); for (const f of files.value) fd.append('files', f); fd.append('apply', apply.value ? 'true' : 'false')
  busy.value = true
  try {
    const r = await api.upload(`/api/v1/tasks/${props.task.task_id}/final`, fd)
    const a = r.apply_stats
    ui.toast(a ? `已按终稿覆盖：${a.total} 条用例全部为人工定稿（改 ${a.modified} / 增 ${a.added} / 删 ${a.deleted}）` : `终稿对比完成：采纳率 ${((r.stats?.adoption_rate || 0) * 100).toFixed(1)}%`)
    files.value = []; emit('reload')
  } catch (e) { ui.error(e) } finally { busy.value = false }
}
const diffRows = computed(() => { const x = o.value; if (!x) return []; return view.value === 'added' ? x.added || [] : view.value === 'deleted' ? x.deleted || [] : x.modified || [] })
</script>
<template>
  <div class="grid-2 mt">
    <TgPanel title="回传终稿" hint="按「编号 + 标题 › 标题 › 编号」配对；可多文件（按模块拆分的多个 Excel）合并为一份终稿">
      <div v-if="!hasCases" class="attention"><TgIcon name="alert" :size="13" /> 任务没有用例，无法对比终稿。</div>
      <div v-else-if="!canReview" class="tiny muted">没有 case.review 权限，只能查看对照结果。</div>
      <template v-else>
        <div class="drop" :class="{ on: dragging }" style="padding:20px;cursor:pointer" @click="input.click()" @dragover.prevent="dragging = true" @dragleave="dragging = false" @drop.prevent="dragging = false; add($event.dataTransfer.files)">
          <TgIcon name="upload" :size="16" /> 上传离线改完的 xlsx / csv / xmind（可多选）
          <input ref="input" type="file" class="hidden" multiple accept=".xlsx,.xls,.csv,.xmind,.html" @change="add($event.target.files); $event.target.value = ''" />
        </div>
        <div v-if="files.length" class="list" style="margin-top:8px"><div v-for="(f, i) in files" :key="f.name" class="row"><TgIcon name="file" :size="14" /><span style="flex:1" class="ellipsis">{{ f.name }}</span><span class="tiny muted">{{ fmtSize(f.size) }}</span><button type="button" class="btn small ghost" @click="files.splice(i, 1)">移除</button></div></div>
        <label class="row small" style="margin-top:10px;align-items:flex-start"><input v-model="apply" type="checkbox" style="margin-top:3px" /> <span><b>以终稿为准</b>：上传的用例直接成为线上用例并整份标为人工定稿（已通过、锁定）；配上的沿用审核与版本链、内容变了记一版，文件里没有的进回收站（可恢复，需删除权限），多出来的新增，顺序按文件。不勾则只做差异留痕与采纳率统计，线上用例不动。</span></label>
        <div class="row" style="margin-top:10px"><span class="grow"></span><button type="button" class="btn primary" :disabled="busy || !files.length" @click="upload">{{ busy ? '处理中…' : apply ? '上传并应用' : '上传并对照' }}</button></div>
      </template>
    </TgPanel>
    <TgPanel title="对照结果" :hint="o ? `${o.by} · ${fmtTime(o.at)}` : ''">
      <template #actions><TgBadge v-if="o" :status="o.applied ? 'approved' : 'modified'" :text="o.applied ? '已按终稿覆盖' : '仅留痕'" /></template>
      <TgEmpty v-if="!o" title="未回传终稿" hint="下载导出文件离线评审后回传；差异自动计算，作为学习规则的语料。" />
      <template v-else>
        <TgKv :pairs="[['文件', o.filename], ['差异', `新增 ${o.stats.added} · 删除 ${o.stats.deleted} · 修改 ${o.stats.modified} · 未变 ${o.stats.unchanged}`], ['完全采纳率', `${(o.stats.adoption_rate * 100).toFixed(1)}%（生成 ${o.stats.generated} → 终稿 ${o.stats.final}）`], ['应用结果', o.apply_stats ? `整份标 approved + locked：${o.apply_stats.total} 条，改 ${o.apply_stats.modified} / 增 ${o.apply_stats.added} / 删 ${o.apply_stats.deleted}（删的在回收站）` : '未应用（线上用例不动）']]" />
        <div class="row mt"><TgSeg v-model="view" :items="[['modified', `修改 ${(o.modified || []).length}`], ['added', `新增 ${(o.added || []).length}`], ['deleted', `删除 ${(o.deleted || []).length}`]]" /></div>
        <div class="list mt" style="max-height:50vh;overflow:auto">
          <TgEmpty v-if="!diffRows.length" title="无此类差异" />
          <div v-for="(x, i) in diffRows" :key="i"><b>{{ x.title }}</b><span v-if="x.case_id" class="mono tiny muted"> {{ x.case_id }}</span>
            <div v-for="(c, j) in x.changes || []" :key="j" class="tiny"><b>{{ c.field }}</b>：<span class="diff-del">{{ c.before }}</span> → <span class="diff-add">{{ c.after }}</span></div>
          </div>
        </div>
      </template>
    </TgPanel>
  </div>
</template>
<style scoped>
.drop.on { background: #eef3ff; border-color: #6b93ff; }
</style>

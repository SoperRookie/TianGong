<script setup>
/* 测试报告：POST runs/{rid}/report 生成（轮次未结束为阶段性），GET runs/{rid}/report.md 取 Markdown 用 TgMd 展示，复制 / 下载 */
import { ref, computed, watch } from 'vue'
import { api } from '@/api/client'
import { useUiStore } from '@/stores/ui'
import { fmtTime, copyText } from '@/utils/format'
import { TgPanel, TgBadge, TgMd, TgEmpty, TgTable } from '@/components/ui'
import RunBar from './RunBar.vue'
const props = defineProps({ plan: { type: Object, required: true }, canRun: Boolean, runId: { type: String, default: '' } })
const emit = defineEmits(['reload', 'pick-run'])
const ui = useUiStore()
const base = computed(() => `/api/v1/plans/${props.plan.plan_id}`)
const runs = computed(() => props.plan.runs || [])
const rid = ref(props.runId || (runs.value.length ? runs.value[runs.value.length - 1].run_id : ''))
watch(() => props.runId, v => { if (v) rid.value = v })
watch(rid, v => emit('pick-run', v))
const run = computed(() => runs.value.find(r => r.run_id === rid.value) || null)
const report = computed(() => run.value?.report || null)
const md = ref(''), mdLoading = ref(false), generating = ref(false)
async function loadMd() {
  md.value = ''
  if (!run.value?.report) return
  mdLoading.value = true
  try { md.value = await api.text(`${base.value}/runs/${rid.value}/report.md`) } catch (e) { md.value = run.value.report.markdown || ''; if (!md.value) ui.error(e) } finally { mdLoading.value = false }
}
watch([rid, () => run.value?.report?.at], loadMd, { immediate: true })
async function generate() {
  if (!run.value) return
  if (report.value && !await ui.confirm('重新生成报告？', `本轮已有 ${fmtTime(report.value.at)} 生成的报告（${report.value.conclusion}），重新生成会覆盖。`)) return
  generating.value = true
  try { await api.post(`${base.value}/runs/${rid.value}/report`, {}); ui.toast('报告已生成'); emit('reload') } catch (e) { ui.error(e) } finally { generating.value = false }
}
async function copy() { await copyText(md.value || report.value?.markdown || ''); ui.toast('报告 Markdown 已复制') }
async function download() { try { await api.download(`${base.value}/runs/${rid.value}/report.md`) } catch (e) { ui.error(e) } }
const color = c => c === '可发布' ? 'green' : c === '不可发布' ? 'red' : 'orange'
const total = computed(() => props.plan.items.length)
const stats = computed(() => report.value?.stats || null)
const mcols = [{ key: 'module', title: '范围' }, { key: 'pass', title: '通过', w: '60px' }, { key: 'fail', title: '失败', w: '60px' }, { key: 'blocked', title: '阻塞', w: '60px' }, { key: 'skipped', title: '跳过', w: '60px' }, { key: 'not_executed', title: '未执行', w: '70px' }]
const byMod = computed(() => stats.value ? [{ module: '总体', ...stats.value }, ...(stats.value.by_module || []).map(m => ({ ...m, module: m.module || '（无模块）' }))] : [])
</script>
<template>
  <TgEmpty v-if="!runs.length" title="还没有执行轮次" hint="有执行记录后才能生成测试报告。" />
  <template v-else>
    <div class="row" style="margin-bottom:10px">
      <select v-model="rid" class="select"><option v-for="r in runs" :key="r.run_id" :value="r.run_id">{{ r.name }} · {{ r.finished_at ? '已结束' : '进行中' }} · 已执行 {{ r.summary?.executed || 0 }} / {{ total }}{{ r.report ? ' · 已有报告' : '' }}</option></select>
      <RunBar v-if="run" :summary="run.summary" :total="total" mini />
      <span class="right" />
      <template v-if="report"><button type="button" class="btn small" @click="copy">复制 Markdown</button><button type="button" class="btn small" @click="download">下载 .md</button></template>
      <button v-if="canRun" type="button" class="btn primary small" :disabled="generating || !run?.summary?.executed" @click="generate"><span v-if="generating" class="spinner" /> {{ generating ? '生成中…' : (report ? '重新生成' : '生成报告') }}</button>
    </div>
    <div class="grid-2">
      <div>
        <TgPanel :title="`测试报告 · ${run?.name || ''}`" :hint="report ? `${report.by} ${fmtTime(report.at)} · ${report.model || ''}${report.interim ? ' · 阶段性报告（轮次未结束）' : ''}` : '还没有生成'">
          <template v-if="report" #actions><TgBadge :color="color(report.conclusion)" :text="report.conclusion" /></template>
          <TgEmpty v-if="!report" title="本轮还没有报告" :hint="run?.summary?.executed ? '点「生成报告」：范围 + 程序统计 + 失败清单（带归因 / 缺陷单）+ 未关闭待确认事项 → 结论 / 风险 / 遗留 / 建议。' : '本轮还没有执行记录，无从总结。'" />
          <template v-else>
            <div class="small" style="margin-bottom:8px"><b>判据：</b>{{ report.verdict_basis || '—' }}<div v-if="report.adjusted?.length" class="tiny" style="color:#d68a28;margin-top:4px">程序校正：{{ report.adjusted.join('；') }}</div></div>
            <div v-if="report.summary" class="small" style="margin-bottom:10px">{{ report.summary }}</div>
            <h4 class="sec">执行数据（程序统计，以此为准）</h4>
            <TgTable :cols="mcols" :rows="byMod" row-key="module" dense><template #cell-pass="{ row }"><span class="ok">{{ row.pass }}</span></template><template #cell-fail="{ row }"><span :style="row.fail ? 'color:#db5361' : ''">{{ row.fail }}</span></template></TgTable>
            <h4 class="sec">风险</h4><ul v-if="report.risks?.length" class="small"><li v-for="(r, i) in report.risks" :key="i">{{ r.text }}<span v-if="r.impact" class="muted"> · 影响：{{ r.impact }}</span></li></ul><div v-else class="muted small">无</div>
            <h4 class="sec">遗留</h4><ul v-if="report.open_items?.length" class="small"><li v-for="(o, i) in report.open_items" :key="i">【{{ o.kind }}】<span v-if="o.ref" class="mono">{{ o.ref }} </span>{{ o.text }}</li></ul><div v-else class="muted small">无</div>
            <h4 class="sec">建议</h4><ul v-if="report.suggestions?.length" class="small"><li v-for="(s, i) in report.suggestions" :key="i"><span class="tag" style="margin-right:4px">{{ s.when === 'later' ? '后续' : '必须' }}</span>{{ s.text }}</li></ul><div v-else class="muted small">无</div>
          </template>
        </TgPanel>
        <TgPanel v-if="report" class="mt" title="报告全文（Markdown）" :hint="mdLoading ? '加载中…' : 'GET …/report.md'"><TgMd :text="md" /></TgPanel>
      </div>
      <TgPanel title="报告输入组装" hint="Prompt test_report；结论必须有数据支撑，不编造编号">
        <div class="list">
          <div v-for="[a, b] in [['范围', '计划 / 周期 / 负责人 / 需求标题 / 覆盖模块 / 来源文件 / 第几轮'], ['执行数据', '总体、按模块、按优先级的通过 / 失败 / 阻塞 / 跳过 / 未执行（程序统计）'], ['失败清单', '编号 / 优先级 / 失败分类 / 执行说明 / AI 归因与缺陷单摘要 / 复测次数'], ['待确认事项', '计划内用例所属需求上未关闭的事项（等级 / 模块 / 假设）'], ['画像', '项目画像注入']]" :key="a"><b>{{ a }}</b><div class="tiny muted">{{ b }}</div></div>
        </div>
        <div class="information" style="margin-top:10px">程序校正：有 P0 失败且分类不属用例问题 → 必须「不可发布」；还有失败 / 阻塞 / 未执行 → 不得「可发布」。</div>
        <div v-if="report" class="tiny muted" style="margin-top:10px">失败与阻塞 {{ report.failures }} 条 · 未关闭待确认事项 {{ report.open_questions }} 条</div>
      </TgPanel>
    </div>
  </template>
</template>
<style scoped>
.sec { margin: 14px 0 6px; font-size: 12.5px; color: #3768ce; }
ul.small { margin: 4px 0; padding-left: 18px; }
</style>

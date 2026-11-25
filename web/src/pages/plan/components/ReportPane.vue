<script setup>
/* 测试报告：POST runs/{rid}/report 生成（轮次未结束为阶段性）。页面以图表为主：结论横幅 → ReportCharts（指标 / 构成 / 模块 / 优先级 / 失败分类 / 各轮通过率）→ 风险 · 遗留 · 建议三栏；
   数据明细表、Markdown 全文（GET runs/{rid}/report.md，复制 / 下载）与生成说明默认收起 */
import { ref, computed, watch } from 'vue'
import { api } from '@/api/client'
import { useUiStore } from '@/stores/ui'
import { fmtTime, copyText } from '@/utils/format'
import { TgPanel, TgBadge, TgMd, TgEmpty, TgTable } from '@/components/ui'
import RunBar from './RunBar.vue'
import ReportCharts from './ReportCharts.vue'
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
const showTable = ref(false), showMd = ref(false), showHow = ref(false)   // 文字明细默认收起，页面以图表为主
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
const pdfBusy = ref(false)
async function downloadPdf() {
  pdfBusy.value = true
  try { await api.download(`${base.value}/runs/${rid.value}/report.pdf`); ui.toast('PDF 已导出') } catch (e) { ui.error(e) } finally { pdfBusy.value = false }
}
async function download() { try { await api.download(`${base.value}/runs/${rid.value}/report.md`) } catch (e) { ui.error(e) } }
const color = c => c === '可发布' ? 'green' : c === '不可发布' ? 'red' : 'orange'
const total = computed(() => props.plan.items.length)
const stats = computed(() => report.value?.stats || null)
const mcols = [{ key: 'module', title: '范围' }, { key: 'total', title: '用例数', w: '70px' }, { key: 'pass', title: '通过', w: '60px' }, { key: 'fail', title: '失败', w: '60px' }, { key: 'blocked', title: '阻塞', w: '60px' }, { key: 'skipped', title: '跳过', w: '60px' }, { key: 'not_executed', title: '未执行', w: '70px' }]
const byMod = computed(() => stats.value ? [{ module: '总体', ...stats.value }, ...(stats.value.by_module || []).map(m => ({ ...m, module: '模块 · ' + (m.module || '（无模块）') })), ...(stats.value.by_priority || []).map(m => ({ ...m, module: '优先级 · ' + (m.priority || '—') }))] : [])
</script>
<template>
  <TgEmpty v-if="!runs.length" title="还没有执行轮次" hint="有执行记录后才能生成测试报告。" />
  <template v-else>
    <div class="row" style="margin-bottom:10px">
      <select v-model="rid" class="select"><option v-for="r in runs" :key="r.run_id" :value="r.run_id">{{ r.name }} · {{ r.finished_at ? '已结束' : '进行中' }} · 已执行 {{ r.summary?.executed || 0 }} / {{ total }}{{ r.report ? ' · 已有报告' : '' }}</option></select>
      <RunBar v-if="run" :summary="run.summary" :total="total" mini />
      <span class="right" />
      <template v-if="report"><button type="button" class="btn small" @click="copy">复制 Markdown</button><button type="button" class="btn small" @click="download">下载 .md</button><button type="button" class="btn small" :disabled="pdfBusy" title="带图表的 PDF：结论、指标、各项分布图、风险 / 遗留 / 建议、失败与阻塞用例清单" @click="downloadPdf"><span v-if="pdfBusy" class="spinner" /> {{ pdfBusy ? '导出中…' : '导出 PDF' }}</button></template>
      <button v-if="canRun" type="button" class="btn primary small" :disabled="generating || !run?.summary?.executed" @click="generate"><span v-if="generating" class="spinner" /> {{ generating ? '生成中…' : (report ? '重新生成' : '生成报告') }}</button>
    </div>
    <TgEmpty v-if="!report" title="本轮还没有报告" :hint="run?.summary?.executed ? '点「生成报告」：范围 + 程序统计 + 失败清单（带归因 / 缺陷单）+ 未关闭待确认事项 → 结论 / 风险 / 遗留 / 建议。' : '本轮还没有执行记录，无从总结。'" />
    <template v-else>
      <!-- 结论横幅：一眼看到能不能发 -->
      <div class="rp-verdict" :class="color(report.conclusion)">
        <div class="rp-verdict-main"><span class="rp-verdict-tag">{{ report.conclusion }}</span><span v-if="report.interim" class="tag">阶段性报告（轮次未结束）</span><b>{{ run?.name }}</b></div>
        <div class="rp-verdict-basis"><b>判据：</b>{{ report.verdict_basis || '—' }}</div>
        <div v-if="report.adjusted?.length" class="tiny" style="color:#a86414;margin-top:4px">程序校正：{{ report.adjusted.join('；') }}</div>
        <div v-if="report.summary" class="rp-verdict-sum">{{ report.summary }}</div>
        <div class="tiny muted" style="margin-top:6px">{{ report.by }} · {{ fmtTime(report.at) }}{{ report.model ? ' · ' + report.model : '' }} · 失败与阻塞 {{ report.failures }} 条 · 未关闭待确认事项 {{ report.open_questions }} 条</div>
      </div>
      <ReportCharts v-if="stats" :report="report" :plan="plan" :run="run" />
      <!-- 风险 / 遗留 / 建议：三栏并排，标题带条数 -->
      <div class="rp-three">
        <TgPanel :title="`风险 · ${report.risks?.length || 0}`">
          <div v-if="!report.risks?.length" class="muted small">无</div>
          <div v-for="(r, i) in report.risks || []" :key="i" class="rp-item"><span class="rp-no red">{{ i + 1 }}</span><div>{{ r.text }}<div v-if="r.impact" class="tiny muted">影响：{{ r.impact }}</div></div></div>
        </TgPanel>
        <TgPanel :title="`遗留 · ${report.open_items?.length || 0}`">
          <div v-if="!report.open_items?.length" class="muted small">无</div>
          <div v-for="(o, i) in report.open_items || []" :key="i" class="rp-item"><span class="tag">{{ o.kind }}</span><div><span v-if="o.ref" class="mono tiny">{{ o.ref }} </span>{{ o.text }}</div></div>
        </TgPanel>
        <TgPanel :title="`建议 · ${report.suggestions?.length || 0}`">
          <div v-if="!report.suggestions?.length" class="muted small">无</div>
          <div v-for="(x, i) in report.suggestions || []" :key="i" class="rp-item"><span class="tag" :class="{ must: x.when !== 'later' }">{{ x.when === 'later' ? '可延后' : '发布前必须' }}</span><div>{{ x.text }}</div></div>
        </TgPanel>
      </div>
      <TgPanel class="mt" title="执行数据明细表" hint="程序统计，以此为准；与上面的图是同一份数据">
        <template #actions><button type="button" class="btn small ghost" @click="showTable = !showTable">{{ showTable ? '收起' : '展开' }}</button></template>
        <TgTable v-if="showTable" :cols="mcols" :rows="byMod" row-key="module" dense><template #cell-pass="{ row }"><span class="ok">{{ row.pass }}</span></template><template #cell-fail="{ row }"><span :style="row.fail ? 'color:#db5361' : ''">{{ row.fail }}</span></template></TgTable>
      </TgPanel>
      <TgPanel class="mt" title="报告全文（Markdown）" :hint="mdLoading ? '加载中…' : '复制 / 下载的就是这一份'">
        <template #actions><button type="button" class="btn small ghost" @click="showMd = !showMd">{{ showMd ? '收起' : '展开' }}</button></template>
        <TgMd v-if="showMd" :text="md" />
      </TgPanel>
      <TgPanel class="mt" title="这份报告是怎么生成的">
        <template #actions><button type="button" class="btn small ghost" @click="showHow = !showHow">{{ showHow ? '收起' : '展开' }}</button></template>
        <template v-if="showHow">
          <div class="list">
            <div v-for="[a, b] in [['范围', '计划 / 周期 / 负责人 / 需求标题 / 覆盖模块 / 来源文件 / 第几轮'], ['执行数据', '总体、按模块、按优先级的通过 / 失败 / 阻塞 / 跳过 / 未执行（程序统计）'], ['失败清单', '编号 / 优先级 / 失败分类 / 执行说明 / AI 归因与缺陷单摘要 / 复测次数'], ['待确认事项', '计划内用例所属需求上未关闭的事项（等级 / 模块 / 假设）'], ['画像', '项目画像注入']]" :key="a"><b>{{ a }}</b><div class="tiny muted">{{ b }}</div></div>
          </div>
          <div class="information" style="margin-top:10px">程序校正：有 P0 失败且分类不属用例问题 → 必须「不可发布」；还有失败 / 阻塞 / 未执行 → 不得「可发布」。</div>
        </template>
      </TgPanel>
    </template>
  </template>
</template>
<style scoped>
.rp-verdict { background: #fff; border: 1px solid var(--line); border-left: 4px solid var(--amber); border-radius: var(--radius); padding: 14px 16px; margin-bottom: 12px; }
.rp-verdict.green { border-left-color: #2aa784; } .rp-verdict.red { border-left-color: #c8323f; }
.rp-verdict-main { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.rp-verdict-tag { font-size: 18px; font-weight: 750; color: #a86414; }
.rp-verdict.green .rp-verdict-tag { color: #12795b; } .rp-verdict.red .rp-verdict-tag { color: #b02a36; }
.rp-verdict-basis { margin-top: 8px; font-size: 13px; }
.rp-verdict-sum { margin-top: 6px; font-size: 12.5px; color: var(--text-2); }
.rp-three { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; align-items: start; }
.rp-item { display: flex; gap: 8px; align-items: flex-start; font-size: 12.5px; padding: 7px 0; border-bottom: 1px solid var(--line-2); }
.rp-item:last-child { border-bottom: 0; }
.rp-item .tag { flex-shrink: 0; }
.rp-item .tag.must { color: #b02a36; border-color: #f3c2c8; background: #fdecee; }
.rp-no { flex-shrink: 0; width: 18px; height: 18px; border-radius: 50%; background: #fdecee; color: #b02a36; font-size: 11px; font-weight: 700; display: grid; place-items: center; margin-top: 1px; }
@media (max-width: 1100px) { .rp-three { grid-template-columns: 1fr; } }
</style>

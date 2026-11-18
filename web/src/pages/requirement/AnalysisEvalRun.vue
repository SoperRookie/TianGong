<script setup>
/* 评测运行详情：四个指标卡 + 计数、按分析项明细、逐样本得分与差异（理解错 / 捏造 / 遗漏）；进行中每 3 秒轮询 */
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useUiStore } from '@/stores/ui'
import { fmtTime } from '@/utils/format'
import { PageHeading, TgPanel, TgStat, TgBadge, TgEmpty, TgIcon } from '@/components/ui'
const route = useRoute(), ui = useUiStore()
const id = computed(() => String(route.params.id || ''))
const { data: r, loading, error, reload } = useLoad(() => api.get(`/api/v1/analysis-eval/runs/${id.value}`), { deps: [id] })
let timer = null
watch(r, x => { clearTimeout(timer); if (x?.status === 'running') timer = setTimeout(() => reload(true), 3000); if (x) ui.setCrumb(`评测 ${x.run_id}`) })
onBeforeUnmount(() => { clearTimeout(timer); ui.setCrumb('') })
const L = computed(() => r.value?.labels || {}), t = computed(() => r.value?.totals || null)
const rate = v => v == null ? '—' : (v * 100).toFixed(1) + '%'
const diffOf = s => Object.entries(s.per_key || {}).filter(([, v]) => (v.wrong_items || []).length || (v.extra_items || []).length || (v.missing_items || []).length)
</script>
<template>
  <PageHeading :title="r ? `评测 ${r.run_id}` : '评测详情'">
    <template #back><router-link class="back" :to="{ name: 'req-eval', query: { tab: 'runs' } }"><TgIcon name="back" :size="14" /> 评测记录</router-link></template>
    <template #badge><TgBadge v-if="r" :status="r.status === 'running' ? 'running' : r.status === 'done' ? 'completed' : 'failed'" :text="r.status === 'running' ? `进行中 ${r.done}/${r.total}` : r.status === 'done' ? '完成' : `失败${r.error ? '：' + r.error : ''}`" /></template>
    <template #sub v-if="r">{{ fmtTime(r.at) }} · {{ r.by || '' }} · 模型 {{ r.model || '默认' }}{{ r.review ? ` · 复核 ${r.reviewer_model || '同分析模型'}` : ' · 无复核' }}<template v-if="r.project"> · 项目 {{ r.project }}</template> · {{ Object.entries(r.prompt_versions || {}).map(([k, v]) => `${k} v${v}`).join(' · ') }}</template>
    <template #actions><button type="button" class="btn" @click="reload()"><TgIcon name="refresh" :size="14" /> 刷新</button></template>
  </PageHeading>
  <div v-if="error" class="err-box">{{ error.message }}</div>
  <div v-else-if="!r" class="page-loading"><span class="spinner" /> 加载中…</div>
  <template v-else>
    <div v-if="t" class="stat-grid four">
      <TgStat label="准确率" :value="rate(t.accuracy)" foot="匹配 / (标准答案 + 捏造)" color="green" icon="check" />
      <TgStat label="捏造率" :value="rate(t.fabricated_rate)" foot="捏造 / AI 输出" color="orange" icon="alert" />
      <TgStat label="理解错率" :value="rate(t.wrong_rate)" foot="理解错 / (匹配 + 理解错)" color="orange" icon="bug" />
      <TgStat label="遗漏率" :value="rate(t.missing_rate)" foot="遗漏 / 标准答案" color="purple" icon="search" />
    </div>
    <div v-if="t" class="information" style="margin-bottom:14px">计数：标准答案 {{ t.golden }} · AI 输出 {{ t.predicted }} · 匹配 {{ t.matched }} · 理解错 {{ t.wrong }} · 捏造 {{ t.extra }} · 遗漏 {{ t.missing }}</div>
    <TgPanel v-if="r.per_key" title="按分析项" class="mb">
      <table class="table dense">
        <thead><tr><th>分析项</th><th>标准答案</th><th>AI 输出</th><th>匹配</th><th>理解错</th><th>捏造</th><th>遗漏</th><th>准确率</th></tr></thead>
        <tbody><tr v-for="(m, k) in r.per_key" :key="k"><td>{{ L[k] || k }}</td><td>{{ m.golden }}</td><td>{{ m.predicted }}</td><td>{{ m.matched }}</td><td :style="m.wrong ? 'color:var(--red)' : ''">{{ m.wrong }}</td><td :style="m.extra ? 'color:var(--amber)' : ''">{{ m.extra }}</td><td :style="m.missing ? 'color:var(--amber)' : ''">{{ m.missing }}</td><td>{{ rate(m.accuracy) }}</td></tr></tbody>
      </table>
    </TgPanel>
    <TgPanel title="逐样本" :hint="`${r.done}/${r.total} 已完成${r.failed ? ` · 失败 ${r.failed}` : ''}`">
      <TgEmpty v-if="!(r.samples || []).length" title="没有样本" />
      <details v-for="s in r.samples" :key="s.sample_id" class="sample">
        <summary>
          <b v-if="s.status === 'done'">{{ rate(s.metrics?.accuracy) }}</b><TgBadge v-else-if="s.status === 'failed'" status="failed" /><span v-else class="muted">{{ s.status === 'running' ? '分析中' : '等待' }}</span>
          <router-link class="link" :to="{ name: 'req-detail', params: { id: s.req_id || (s.sample_id || '').replace(/^req-/, ''), step: 3 } }" @click.stop>{{ s.title }}</router-link>
          <span class="tiny muted" v-if="s.status === 'done'">匹配 {{ s.metrics?.matched }} · 理解错 {{ s.metrics?.wrong }} · 捏造 {{ s.metrics?.extra }} · 遗漏 {{ s.metrics?.missing }}<template v-if="s.verification"> · 核验降级 {{ s.verification.demoted }}</template><template v-if="s.review?.wrong != null"> · 复核质疑 {{ s.review.wrong + s.review.unsupported }} 补充 {{ s.review.added }}</template><template v-if="s.model"> · {{ s.model }}</template></span>
          <span v-else-if="s.error" class="tiny" style="color:var(--red)">{{ s.error }}</span>
        </summary>
        <div v-if="s.status === 'done'" class="sample-body">
          <div v-if="!diffOf(s).length" class="tiny muted">与标准答案完全一致</div>
          <div v-for="[k, v] in diffOf(s)" :key="k" style="margin-top:6px">
            <b>{{ L[k] || k }}</b>
            <div v-for="(w, i) in v.wrong_items || []" :key="'w' + i" style="color:var(--red)">理解错：AI「{{ w[1] }}」 ← 标准「{{ w[0] }}」<template v-if="w[2]">（{{ w[2] }}）</template></div>
            <div v-for="(x, i) in v.extra_items || []" :key="'e' + i" style="color:var(--amber)">捏造：{{ x }}</div>
            <div v-for="(x, i) in v.missing_items || []" :key="'m' + i" style="color:var(--amber)">遗漏：{{ x }}</div>
          </div>
          <div v-if="(s.open_questions || []).length" class="tiny muted" style="margin-top:6px">待确认事项 {{ s.open_questions.length }} 条：{{ s.open_questions.slice(0, 5).join('；') }}</div>
        </div>
      </details>
    </TgPanel>
  </template>
</template>
<style scoped>
.sample { border: 1px solid var(--line); border-radius: 4px; padding: 8px 12px; margin-bottom: 8px; }
.sample summary { cursor: pointer; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.sample-body { margin-top: 6px; font-size: 12.5px; }
</style>

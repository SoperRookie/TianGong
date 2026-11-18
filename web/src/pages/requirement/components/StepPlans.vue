<script setup>
/* ⑦ 计划与执行：引用了本需求关联任务用例的测试计划（GET /api/v1/plans?task_id=），链接到 plan-detail；没有任务时列本项目计划 */
import { ref, watch } from 'vue'
import { api } from '@/api/client'
import { fmtTime, num, pct } from '@/utils/format'
import { TgBadge, TgEmpty } from '@/components/ui'
const props = defineProps({ r: { type: Object, required: true } })
const plans = ref([]), loading = ref(false), scopeNote = ref('')
async function load() {
  loading.value = true
  try {
    const ids = props.r.tasks || []
    if (ids.length) {
      const seen = new Map()
      for (const tid of ids) { const d = await api.get('/api/v1/plans', { task_id: tid, page: 0 }); (d.plans || []).forEach(p => seen.set(p.plan_id, p)) }
      plans.value = [...seen.values()]; scopeNote.value = '引用了本需求用例的计划'
    } else {
      const d = await api.get('/api/v1/plans', { project: props.r.project, page: 0 })
      plans.value = d.plans || []; scopeNote.value = '本需求尚未发起设计，下面是本项目的计划'
    }
  } catch { plans.value = [] } finally { loading.value = false }
}
watch(() => [props.r.req_id, (props.r.tasks || []).join(',')], load, { immediate: true })
const latest = p => p.latest_run || p.summary?.latest_run || null
const cases = p => p.cases ?? p.summary?.cases ?? (p.items || []).length
</script>
<template>
  <div class="panel-head">
    <div><div class="panel-title">⑦ 计划与执行</div><div class="panel-hint">正式用例加入测试计划后在轮次中执行；{{ scopeNote }}。</div></div>
    <router-link class="btn small primary" :to="{ name: 'plans', query: { new: 1, project: r.project } }">＋ 新建计划</router-link>
  </div>
  <div v-if="loading" class="page-loading"><span class="spinner" /> 加载中…</div>
  <table v-else-if="plans.length" class="table">
    <thead><tr><th>计划</th><th style="width:100px">状态</th><th style="width:80px">用例</th><th style="width:70px">轮次</th><th>最近执行</th><th style="width:130px">更新</th></tr></thead>
    <tbody>
      <tr v-for="p in plans" :key="p.plan_id" class="row-click" @click="$router.push({ name: 'plan-detail', params: { id: p.plan_id } })">
        <td><b>{{ p.name }}</b><div class="sub">{{ p.project }}<template v-if="p.owner"> · 负责人 {{ p.owner }}</template></div></td>
        <td><TgBadge :status="p.status" /></td>
        <td>{{ num(cases(p)) }}</td>
        <td>{{ p.runs ?? p.summary?.runs ?? 0 }}</td>
        <td><template v-if="latest(p)">{{ latest(p).name || latest(p).run_id }} · 已执行 {{ num(latest(p).executed ?? 0) }} / {{ num(latest(p).total ?? cases(p)) }} <span class="minibar"><span :style="{ width: pct(latest(p).executed ?? 0, latest(p).total ?? cases(p)) }" /></span><span v-if="latest(p).pass != null" class="tiny muted"> · 通过 {{ latest(p).pass }}</span></template><span v-else class="muted">—</span></td>
        <td class="tiny muted">{{ fmtTime(p.updated_at || p.created_at) }}</td>
      </tr>
    </tbody>
  </table>
  <TgEmpty v-else :title="(r.tasks || []).length ? '本需求的用例还没有进入测试计划' : '本项目还没有测试计划'" hint="用例审核通过后，在测试计划中「加入用例」并分配执行" />
</template>

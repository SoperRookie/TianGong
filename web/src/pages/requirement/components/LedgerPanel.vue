<script setup>
/* 底稿核对：材料每一句的去向（收录 / 排除 / 待处理）；GET …/extraction/ledger；POST include / exclude / confirm */
import { ref, computed, onMounted } from 'vue'
import { api } from '@/api/client'
import { useUiStore } from '@/stores/ui'
import { TgBadge, TgEmpty } from '@/components/ui'
const props = defineProps({ r: { type: Object, required: true }, canEdit: Boolean })
const emit = defineEmits(['changed'])
const ui = useUiStore()
const L = ref(null), loading = ref(false), tab = ref(''), reasonFor = ref(null), reason = ref('')
const BY = { model: '模型', auto: '程序', user: '人工' }
const CTL = { table_cell: '单元格', table_row: '表格行', _段落: '段落', label: '标签', box_1: '流程框', box_2: '矩形', box_3: '矩形', flow_shape: '流程框', _连接: '连接线', _连接线: '连接线', droplist: '下拉框', text_field: '输入框', text_area: '文本域', checkbox1: '复选框', radio_button: '单选框', primary_button: '按钮', button: '按钮', heading_1: '标题', heading_2: '标题', placeholder: '占位区', image: '图片', _图片: '图片', table: '表格', ellipse: '椭圆', shape: '形状', icon: '图标', line: '线', arrow: '箭头' }
async function load() {
  loading.value = true
  try { L.value = await api.get(`/api/v1/requirements/${props.r.req_id}/extraction/ledger`); if (!tab.value) tab.value = L.value.stats?.todo ? 'todo' : L.value.stats?.review_open ? 'review' : 'ex' } catch (e) { ui.error(e) } finally { loading.value = false }
}
onMounted(load)
const st = computed(() => L.value?.stats || {}), img = computed(() => L.value?.image || {})
const all = computed(() => (L.value?.pages || []).flatMap(pg => pg.units))
const src = computed(() => all.value.filter(u => u.kind !== 'image')), imgs = computed(() => all.value.filter(u => u.kind === 'image'))
const tabs = computed(() => [['review', `需复核 ${st.value.review || 0}${st.value.review_open ? `（未看 ${st.value.review_open}）` : ''}`], ['todo', `待处理 ${st.value.todo || 0}`], ['ex', `排除 ${st.value.ex || 0}`], ['in', `收录 ${st.value.in || 0}`], ['image', `图中文字 ${img.value.total || 0}`], ['all', `全部 ${st.value.total || 0}`]])
const HINT = { review: '模型判「不是需求」但句子像规则（≥ 8 个汉字且带数字 / 条件词）：看一眼，是需求就「收录」，确实不是就「确认」', ex: '程序按规则排除的只有无汉字的纯样例一类；模型排除的按原因分组', image: '模型从截图里转录的文字，不是产品写的原文：低优先级，不送补漏、不拦审核；需要的可以手动收录', todo: '既没收录也没有排除原因的句子，AI 提取完成时应为 0；删了摘录会回到这里', in: '去向以整理稿正文为准', all: '材料每一句（原型行带控件编号）的去向' }
const groupBy = (units, keyFn) => { const m = new Map(); units.forEach(u => { const k = keyFn(u); if (!m.has(k)) m.set(k, []); m.get(k).push(u) }); return [...m].map(([key, units]) => ({ key, units })) }
const groups = computed(() => {
  const f = tab.value
  if (f === 'review') return [...groupBy(src.value.filter(u => u.review && !u.checked), u => u.path), ...(src.value.some(u => u.review && u.checked) ? [{ key: '已确认', units: src.value.filter(u => u.review && u.checked), fold: true }] : [])]
  if (f === 'todo') return groupBy(src.value.filter(u => u.status === 'todo'), u => u.path)
  if (f === 'in') return groupBy(src.value.filter(u => u.status === 'in'), u => u.path)
  if (f === 'image') return ['todo', 'ex', 'in'].map(k => ({ key: { todo: '未收录（可手动收录）', ex: '排除', in: '已在整理稿' }[k], units: imgs.value.filter(u => u.status === k), fold: k !== 'todo' })).filter(g => g.units.length)
  if (f === 'ex') return groupBy(src.value.filter(u => u.status === 'ex'), u => `${BY[u.by] || u.by}：${u.reason || ''}`).sort((a, b) => b.units.length - a.units.length).map(g => ({ ...g, fold: true }))
  return groupBy(src.value, u => u.path)
})
const ctlName = c => CTL[(c?.cls || '').split(' ')[0]] || c?.cls || '控件'
async function act(u, action, rs) {
  try {
    const x = await api.post(`/api/v1/requirements/${props.r.req_id}/extraction/ledger`, { uid: u.uid, action, reason: rs || null })
    const s = x.extraction?.ledger?.stats || {}
    ui.toast(action === 'include' ? '已按原句收进整理稿' : action === 'confirm' ? `已确认，需复核未看 ${s.review_open || 0}` : `已排除，待处理 ${s.todo || 0}`)
    reasonFor.value = null; reason.value = ''
    await load(); emit('changed')
  } catch (e) { ui.error(e) }
}
</script>
<template>
  <div v-if="loading && !L" class="page-loading"><span class="spinner" /> 正在核算底稿…</div>
  <template v-else-if="L">
    <div class="row" style="margin-bottom:6px">
      <div class="seg"><button v-for="[k, t] in tabs" :key="k" type="button" :class="{ on: tab === k }" @click="tab = k">{{ t }}</button></div>
      <span class="tiny muted">{{ HINT[tab] }}</span>
    </div>
    <div v-if="(r.extraction?.ledger?.inherited || []).length" class="tiny muted" style="margin-bottom:6px">已沿用人工底稿决定（同一句话上次人已判过，不再问第二遍）：{{ r.extraction.ledger.inherited.map(x => `${x.self ? '本需求上一版' : `需求「${x.title || ''}」（材料重合 ${Math.round((x.overlap || 0) * 100)}%）`}：收录 ${x.include || 0} · 排除 ${x.exclude || 0} · 已看过 ${x.checked || 0}`).join('；') }}</div>
    <div v-if="(L.checks || []).length" class="tiny muted" style="margin-bottom:6px">原型解析自校验：<template v-for="c in L.checks" :key="c.filename">{{ c.filename }} <TgBadge :status="c.ok ? 'ok' : 'error'" :text="c.ok ? `0 缺字 · ${c.pages} 页` : `缺 ${c.missing} 字`" />　</template></div>
    <div class="ledger-scroll">
      <template v-for="g in groups" :key="g.key">
        <details :open="!g.fold" class="ledger-group">
          <summary><b>{{ g.key }}</b> <span class="tiny muted">{{ g.units.length }} 句</span></summary>
          <div v-for="u in g.units" :key="u.uid" class="ledger-row">
            <span class="mono muted uid" :title="u.ctl ? `控件类型 ${u.ctl.cls || ''} · 坐标 (${u.ctl.x},${u.ctl.y})` : '单元编号'">{{ u.uid }}</span>
            <span class="txt">
              <TgBadge v-if="u.status === 'in'" status="in" :text="u.forced ? '兜底收录' : u.manual ? '人工收录' : '收录'" />
              <template v-else-if="u.status === 'ex'"><TgBadge status="ex" text="排除" /> <span class="tiny muted">{{ u.reason }}（{{ BY[u.by] || u.by }}<template v-if="u.checked">，{{ u.checked.by }} 已确认<template v-if="u.checked.inherited">〔沿用「{{ u.checked.inherited.title }}」〕</template></template>）</span></template>
              <template v-else><TgBadge status="todo" /><span v-if="u.partial" class="tiny muted"> 整理稿已有其中 {{ Math.round(u.partial * 100) }}%</span></template>
              <TgBadge v-if="u.review && !u.checked" status="review" text="需复核" />
              {{ u.text }}
              <span v-if="u.count > 1" class="tiny muted" :title="(u.pages || []).join('\n')"> ×{{ u.count }}</span>
              <span v-if="u.kind === 'image'" class="tiny muted"> 〔{{ (u.path || '').split(' › ').pop() }}<template v-if="u.image"> · 图片 {{ u.image }}</template>〕</span>
              <span v-if="u.ctl" class="tiny muted"> 〔{{ ctlName(u.ctl) }}<template v-if="u.ctl.hidden"> · 隐藏态</template><template v-if="u.ctl.region"> · <span style="color:var(--red)">独立区域 {{ u.ctl.region }}</span></template>〕</span>
            </span>
            <span v-if="canEdit && reasonFor !== u.uid" class="ops">
              <button v-if="u.status !== 'in'" type="button" class="btn ghost small" title="按原句收进整理稿（标人工收录）" @click="act(u, 'include')">收录</button>
              <button v-if="u.review && !u.checked" type="button" class="btn ghost small" title="看过了，确实不是需求，维持排除" @click="act(u, 'confirm')">确认</button>
              <button v-if="u.status === 'todo' || (u.status === 'in' && (u.forced || u.manual))" type="button" class="btn ghost small danger" @click="reasonFor = u.uid; reason = ''">排除</button>
            </span>
            <div v-if="reasonFor === u.uid" class="ask row">
              <input v-model="reason" class="field small" style="flex:1" placeholder="排除原因（可空，默认「人工排除」）" @keydown.enter="act(u, 'exclude', reason)" />
              <button type="button" class="btn small danger" @click="act(u, 'exclude', reason)">确定排除</button>
              <button type="button" class="btn small" @click="reasonFor = null">取消</button>
            </div>
          </div>
        </details>
      </template>
      <TgEmpty v-if="!groups.length" title="没有符合筛选的句子" />
    </div>
  </template>
</template>
<style scoped>
.ledger-scroll { max-height: 640px; overflow: auto; background: var(--line-2); padding: 4px 10px; border-radius: 6px; }
.ledger-group { margin: 6px 0; } .ledger-group summary { cursor: pointer; padding: 4px 0; }
.ledger-row { display: flex; gap: 6px; align-items: flex-start; flex-wrap: wrap; padding: 4px 6px; border-bottom: 1px solid var(--line); font-size: 12.5px; line-height: 1.6; background: #fff; }
.ledger-row .uid { flex-shrink: 0; min-width: 64px; }
.ledger-row .txt { flex: 1; min-width: 0; }
.ledger-row .ops { margin-left: auto; display: inline-flex; gap: 4px; flex-shrink: 0; }
.ledger-row .ask { flex-basis: 100%; margin-top: 4px; }
</style>

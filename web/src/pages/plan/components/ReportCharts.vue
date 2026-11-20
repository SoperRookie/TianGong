<script setup>
/* 测试报告图表：指标卡 + 执行结果构成（分段条）+ 按模块 / 按优先级分布（对齐的分段条列表）+ 失败与阻塞分类（排序条）+ 各轮次通过率（柱）。
   数据全部来自报告里程序统计的快照 report.stats / report.failure_breakdown（老报告没有 failure_breakdown 时用本轮当前结果兜底）与各轮次 summary。
   纯 HTML / CSS，不引图表库；每个图形悬停 / 键盘聚焦有数值提示，数值同时写在图旁，不靠颜色单独表达。 */
import { computed, reactive } from 'vue'
const props = defineProps({ report: { type: Object, required: true }, plan: { type: Object, required: true }, run: { type: Object, default: null } })

/* 状态色（图表专用，经色弱可分性校验：通过 / 失败相邻也能区分）；未执行是轨道底色 */
const STATES = [
  ['pass', '通过', '#2aa784', '#fff'],
  ['fail', '失败', '#c8323f', '#fff'],
  ['blocked', '阻塞', '#f0a03d', '#0b0b0b'],
  ['skipped', '跳过', '#9aa6b8', '#0b0b0b'],
  ['not_executed', '未执行', '#dfe5ee', '#0b0b0b'],
]
const s = computed(() => props.report.stats || {})
const n = (o, k) => Number(o?.[k] || 0)
const pct = (a, b) => (b ? Math.round((1000 * a) / b) / 10 : 0)
const pctText = (a, b) => (b ? `${pct(a, b)}%` : '—')
/* 通过率 = 通过 / 已执行（与后端 run_summary 同口径）；执行率 = 已执行 / 总数 */
const executed = o => n(o, 'total') - n(o, 'not_executed')
const passRate = o => pctText(n(o, 'pass'), executed(o))

const tiles = computed(() => {
  const o = s.value
  return [
    { label: '已执行', value: `${executed(o)}`, unit: `/ ${n(o, 'total')}`, foot: `执行率 ${pctText(executed(o), n(o, 'total'))}` },
    { label: '失败', value: n(o, 'fail'), foot: `占已执行 ${pctText(n(o, 'fail'), executed(o))}`, dot: '#c8323f' },
    { label: '阻塞', value: n(o, 'blocked'), foot: `占已执行 ${pctText(n(o, 'blocked'), executed(o))}`, dot: '#f0a03d' },
    { label: '未执行', value: n(o, 'not_executed'), foot: `占全部 ${pctText(n(o, 'not_executed'), n(o, 'total'))}`, dot: '#dfe5ee' },
  ]
})
/* 构成条的各段：占比 ≥ 9% 才在块内印百分比，放不下的数值在图例里 */
const segs = o => STATES.map(([k, name, color, ink]) => ({ k, name, color, ink, v: n(o, k), share: pct(n(o, k), n(o, 'total')) })).filter(x => x.v > 0)
const overall = computed(() => segs(s.value))

/* 分组行（模块 / 优先级）：有失败或阻塞的排前面，其余按用例数；模块最多 12 行，其余合并为「其他」 */
function rowsOf(list, nameKey, emptyName, cap) {
  let rows = (list || []).map(m => ({ ...m, name: m[nameKey] || emptyName }))
  if (cap) {
    rows.sort((a, b) => (n(b, 'fail') + n(b, 'blocked')) - (n(a, 'fail') + n(a, 'blocked')) || n(b, 'total') - n(a, 'total'))
    if (rows.length > cap) {
      const rest = rows.slice(cap - 1), other = { name: `其他 ${rest.length} 个模块`, total: 0 }
      STATES.forEach(([k]) => { other[k] = rest.reduce((t, r) => t + n(r, k), 0) })
      other.total = rest.reduce((t, r) => t + n(r, 'total'), 0)
      rows = [...rows.slice(0, cap - 1), other]
    }
  }
  return rows
}
const modRows = computed(() => rowsOf(s.value.by_module, 'module', '（未分模块）', 12))
const prioRows = computed(() => rowsOf(s.value.by_priority, 'priority', '（无优先级）', 0))

/* 失败与阻塞分类：报告快照优先；老报告没有快照时按本轮当前结果算（可能与报告生成时不完全一致，图上注明） */
const breakdown = computed(() => {
  const b = props.report.failure_breakdown
  if (b) return { rows: b.by_reason || [], severity: b.by_severity || [], live: false }
  const results = props.run?.results
  if (!results) return { rows: [], severity: [], live: false }
  const acc = {}
  for (const it of props.plan.items || []) {
    const r = results[it.item_id]
    if (!r || (r.status !== 'fail' && r.status !== 'blocked')) continue
    const name = r.reason || '未填分类'
    acc[name] = acc[name] || { name, fail: 0, blocked: 0 }
    acc[name][r.status === 'fail' ? 'fail' : 'blocked']++
  }
  return { rows: Object.values(acc).sort((a, b) => (b.fail + b.blocked) - (a.fail + a.blocked)), severity: [], live: true }
})
const reasonMax = computed(() => Math.max(1, ...breakdown.value.rows.map(r => r.fail + r.blocked)))
const sevMax = computed(() => Math.max(1, ...breakdown.value.severity.map(r => r.fail + r.blocked)))

/* 各轮次通过率：当前轮次强调色，其余灰；没执行过的轮次不画柱 */
const rounds = computed(() => (props.plan.runs || []).map((r, i) => {
  const sm = r.summary || {}, ex = n(sm, 'executed')
  return { id: r.run_id, name: r.name || `第 ${i + 1} 轮`, idx: i + 1, executed: ex, pass: n(sm, 'pass'), fail: n(sm, 'fail'), blocked: n(sm, 'blocked'), rate: ex ? pct(n(sm, 'pass'), ex) : null, cur: r.run_id === props.run?.run_id }
}))

/* 悬停 / 聚焦提示：一个浮层，跟随指针；键盘聚焦时贴在图形下方 */
const tip = reactive({ show: false, x: 0, y: 0, title: '', lines: [] })
function showTip(e, title, lines) {
  const r = e.currentTarget.getBoundingClientRect()
  const x = e.clientX || r.left + r.width / 2, y = e.clientY || r.bottom
  Object.assign(tip, { show: true, title, lines, x: Math.min(x + 12, window.innerWidth - 230), y: y + 14 })
}
const hideTip = () => { tip.show = false }
const stateLines = o => STATES.filter(([k]) => n(o, k) > 0).map(([k, name, color]) => ({ color, name, value: `${n(o, k)} 条`, sub: pctText(n(o, k), n(o, 'total')) }))
const rowTip = (e, o) => showTip(e, o.name, [...stateLines(o), { name: '通过率（通过 / 已执行）', value: passRate(o) }])
const segTip = (e, o, g, title) => showTip(e, title, [{ color: g.color, name: g.name, value: `${g.v} 条`, sub: `${g.share}%` }])
</script>
<template>
  <div class="rc">
    <!-- 指标：通过率是这一页唯一的大数字 -->
    <div class="rc-kpi">
      <div class="rc-hero">
        <div class="rc-label">通过率</div>
        <div class="rc-hero-num">{{ passRate(s) }}</div>
        <div class="rc-foot">通过 {{ n(s, 'pass') }} / 已执行 {{ executed(s) }}</div>
      </div>
      <div v-for="t in tiles" :key="t.label" class="rc-tile">
        <div class="rc-label"><i v-if="t.dot" class="rc-dot" :style="{ background: t.dot }" />{{ t.label }}</div>
        <div class="rc-num">{{ t.value }}<span v-if="t.unit" class="rc-unit"> {{ t.unit }}</span></div>
        <div class="rc-foot">{{ t.foot }}</div>
      </div>
    </div>

    <!-- 执行结果构成：一条分段条，块内印占比，图例给名称与条数 -->
    <div class="rc-card">
      <div class="rc-title">执行结果构成 <span class="rc-sub">共 {{ n(s, 'total') }} 条用例</span></div>
      <div class="rc-seg tall" role="img" :aria-label="`执行结果构成：${overall.map(g => `${g.name} ${g.v} 条 ${g.share}%`).join('，')}`">
        <span v-for="g in overall" :key="g.k" class="rc-block" :style="{ flexGrow: g.v, background: g.color, color: g.ink }" tabindex="0"
          @pointermove="segTip($event, s, g, '执行结果构成')" @pointerleave="hideTip" @focus="segTip($event, s, g, '执行结果构成')" @blur="hideTip">
          <b v-if="g.share >= 9">{{ g.share }}%</b>
        </span>
      </div>
      <div class="rc-legend"><span v-for="g in overall" :key="g.k"><i :style="{ background: g.color }" />{{ g.name }} <b>{{ g.v }}</b><em v-if="g.share < 9"> · {{ g.share }}%</em></span></div>
    </div>

    <div class="rc-two">
      <!-- 按模块：每行一条对齐的分段条 + 通过率，有失败 / 阻塞的排前面 -->
      <div class="rc-card">
        <div class="rc-title">按模块 <span class="rc-sub">有失败或阻塞的排在前面 · 右侧为通过率</span></div>
        <div v-if="!modRows.length" class="muted small">没有模块数据</div>
        <div v-for="m in modRows" :key="m.name" class="rc-row" tabindex="0" @pointermove="rowTip($event, m)" @pointerleave="hideTip" @focus="rowTip($event, m)" @blur="hideTip">
          <span class="rc-name" :title="m.name">{{ m.name }}</span>
          <span class="rc-seg thin"><span v-for="g in segs(m)" :key="g.k" class="rc-block" :style="{ flexGrow: g.v, background: g.color }" /></span>
          <span class="rc-val"><b>{{ passRate(m) }}</b><em>{{ n(m, 'total') }} 条<template v-if="n(m, 'fail')"> · 失败 {{ n(m, 'fail') }}</template><template v-if="n(m, 'blocked')"> · 阻塞 {{ n(m, 'blocked') }}</template></em></span>
        </div>
        <div class="rc-legend small"><span v-for="[k, name, color] in STATES" :key="k"><i :style="{ background: color }" />{{ name }}</span></div>
      </div>

      <div>
        <!-- 按优先级 -->
        <div class="rc-card">
          <div class="rc-title">按优先级 <span class="rc-sub">右侧为通过率</span></div>
          <div v-if="!prioRows.length" class="muted small">没有优先级数据</div>
          <div v-for="m in prioRows" :key="m.name" class="rc-row narrow" tabindex="0" @pointermove="rowTip($event, m)" @pointerleave="hideTip" @focus="rowTip($event, m)" @blur="hideTip">
            <span class="rc-name">{{ m.name }}</span>
            <span class="rc-seg thin"><span v-for="g in segs(m)" :key="g.k" class="rc-block" :style="{ flexGrow: g.v, background: g.color }" /></span>
            <span class="rc-val"><b>{{ passRate(m) }}</b><em>{{ n(m, 'total') }} 条<template v-if="n(m, 'fail')"> · 失败 {{ n(m, 'fail') }}</template><template v-if="n(m, 'blocked')"> · 阻塞 {{ n(m, 'blocked') }}</template></em></span>
          </div>
        </div>

        <!-- 失败与阻塞分类：排序条，失败 / 阻塞两段 -->
        <div class="rc-card">
          <div class="rc-title">失败与阻塞分类 <span class="rc-sub">{{ breakdown.live ? '按本轮当前结果统计（此报告生成时未存分类快照）' : '报告生成时的快照' }}</span></div>
          <div v-if="!breakdown.rows.length" class="muted small">本轮没有失败或阻塞的用例</div>
          <div v-for="r in breakdown.rows" :key="r.name" class="rc-row narrow" tabindex="0"
            @pointermove="showTip($event, r.name, [{ color: '#c8323f', name: '失败', value: `${r.fail} 条` }, { color: '#f0a03d', name: '阻塞', value: `${r.blocked} 条` }])" @pointerleave="hideTip"
            @focus="showTip($event, r.name, [{ color: '#c8323f', name: '失败', value: `${r.fail} 条` }, { color: '#f0a03d', name: '阻塞', value: `${r.blocked} 条` }])" @blur="hideTip">
            <span class="rc-name" :title="r.name">{{ r.name }}</span>
            <span class="rc-bar"><span class="rc-fill" :style="{ width: (100 * (r.fail + r.blocked) / reasonMax) + '%' }"><i v-if="r.fail" :style="{ flexGrow: r.fail, background: '#c8323f' }" /><i v-if="r.blocked" :style="{ flexGrow: r.blocked, background: '#f0a03d' }" /></span></span>
            <span class="rc-val"><b>{{ r.fail + r.blocked }}</b><em v-if="r.fail && r.blocked">失败 {{ r.fail }} · 阻塞 {{ r.blocked }}</em><em v-else>{{ r.fail ? '失败' : '阻塞' }}</em></span>
          </div>
          <template v-if="breakdown.severity.length">
            <div class="rc-title" style="margin-top:14px">缺陷严重程度 <span class="rc-sub">已出缺陷单的失败用例</span></div>
            <div v-for="r in breakdown.severity" :key="r.name" class="rc-row narrow">
              <span class="rc-name">{{ r.name }}</span>
              <span class="rc-bar"><span class="rc-fill" :style="{ width: (100 * (r.fail + r.blocked) / sevMax) + '%' }"><i :style="{ flexGrow: 1, background: '#5b7fd6' }" /></span></span>
              <span class="rc-val"><b>{{ r.fail + r.blocked }}</b><em>条</em></span>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- 各轮次通过率：多于一轮才画 -->
    <div v-if="rounds.length > 1" class="rc-card">
      <div class="rc-title">各轮次通过率 <span class="rc-sub">通过 / 已执行 · 深色为本报告所在轮次</span></div>
      <div class="rc-cols">
        <div v-for="r in rounds" :key="r.id" class="rc-col" tabindex="0"
          @pointermove="showTip($event, r.name, r.executed ? [{ name: '通过率', value: r.rate + '%' }, { name: '已执行', value: `${r.executed} 条` }, { color: '#2aa784', name: '通过', value: `${r.pass} 条` }, { color: '#c8323f', name: '失败', value: `${r.fail} 条` }, { color: '#f0a03d', name: '阻塞', value: `${r.blocked} 条` }] : [{ name: '还没有执行记录', value: '' }])" @pointerleave="hideTip"
          @focus="showTip($event, r.name, r.executed ? [{ name: '通过率', value: r.rate + '%' }, { name: '已执行', value: `${r.executed} 条` }] : [{ name: '还没有执行记录', value: '' }])" @blur="hideTip">
          <div class="rc-col-plot"><span class="rc-col-val">{{ r.rate === null ? '—' : r.rate + '%' }}</span><span v-if="r.rate !== null" class="rc-col-bar" :class="{ cur: r.cur }" :style="{ height: `calc(${Math.max(2, r.rate)} * (100% - 22px) / 100)` }" /></div>
          <div class="rc-col-name" :class="{ cur: r.cur }" :title="r.name">{{ r.name }}</div>
        </div>
      </div>
    </div>

    <Teleport to="body">
      <div v-if="tip.show" class="rc-tip" :style="{ left: tip.x + 'px', top: tip.y + 'px' }">
        <div class="rc-tip-title">{{ tip.title }}</div>
        <div v-for="(l, i) in tip.lines" :key="i" class="rc-tip-line"><i v-if="l.color" :style="{ background: l.color }" /><span>{{ l.name }}</span><b>{{ l.value }}</b><em v-if="l.sub">{{ l.sub }}</em></div>
      </div>
    </Teleport>
  </div>
</template>
<style scoped>
.rc { display: flex; flex-direction: column; gap: 12px; margin-bottom: 14px; }
.rc-kpi { display: grid; grid-template-columns: 1.4fr repeat(4, minmax(0, 1fr)); gap: 12px; }
.rc-hero, .rc-tile, .rc-card { background: #fff; border: 1px solid var(--line); border-radius: var(--radius); padding: 14px 16px; min-width: 0; }
.rc-label { font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 6px; }
.rc-dot { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }
.rc-hero-num { font-size: 48px; line-height: 1.1; font-weight: 700; letter-spacing: -1px; color: var(--text); margin: 2px 0; }
.rc-num { font-size: 26px; line-height: 1.2; font-weight: 700; color: var(--text); margin-top: 4px; }
.rc-unit { font-size: 13px; font-weight: 500; color: var(--muted); }
.rc-foot { font-size: 11.5px; color: var(--muted-2); margin-top: 4px; }
.rc-title { font-size: 13px; font-weight: 700; color: var(--text); margin-bottom: 10px; }
.rc-sub { font-size: 11.5px; font-weight: 400; color: var(--muted-2); margin-left: 6px; }
/* 分段条：块之间 2px 留白分隔，不描边 */
.rc-seg { display: flex; gap: 2px; min-width: 0; }
.rc-seg.tall { height: 28px; }
.rc-seg.thin { height: 10px; flex: 1 1 auto; }
.rc-block { flex: 1 1 0; min-width: 3px; border-radius: 4px; display: flex; align-items: center; padding-left: 8px; font-size: 12px; outline: none; }
.rc-seg.thin .rc-block { border-radius: 3px; padding: 0; }
.rc-block b { font-weight: 650; white-space: nowrap; }
.rc-block:hover, .rc-block:focus-visible { filter: brightness(1.08); box-shadow: 0 0 0 2px #fff, 0 0 0 3px #8eabff; }
.rc-legend { display: flex; flex-wrap: wrap; gap: 6px 16px; margin-top: 10px; font-size: 12px; color: var(--text-2); }
.rc-legend.small { font-size: 11.5px; margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--line-2); }
.rc-legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 5px; vertical-align: -1px; }
.rc-legend b { color: var(--text); margin-left: 2px; }
.rc-legend em { font-style: normal; color: var(--muted-2); }
.rc-two { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr); gap: 12px; align-items: start; }
.rc-two > div:last-child { display: flex; flex-direction: column; gap: 12px; }
/* 对齐的行：名称 | 条 | 数值 */
.rc-row { display: grid; grid-template-columns: minmax(80px, 170px) minmax(60px, 1fr) 190px; align-items: center; gap: 12px; padding: 6px 6px; margin: 0 -6px; border-radius: 4px; outline: none; }
.rc-row.narrow { grid-template-columns: minmax(60px, 100px) minmax(60px, 1fr) 170px; }
.rc-row:hover, .rc-row:focus-visible { background: #f4f7fc; }
.rc-name { font-size: 12.5px; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rc-val { font-size: 12px; text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
.rc-val b { color: var(--text); font-weight: 650; }
.rc-val em { font-style: normal; color: var(--muted-2); margin-left: 6px; font-size: 11.5px; }
.rc-bar { display: block; height: 10px; }
.rc-fill { display: flex; gap: 2px; height: 100%; min-width: 4px; }
.rc-fill i { flex: 1 1 0; min-width: 3px; border-radius: 0 4px 4px 0; }
.rc-fill i:not(:last-child) { border-radius: 0; }
/* 各轮次柱：≤ 24px 宽，顶端圆角，数值在柱顶 */
.rc-cols { display: flex; gap: 6px; align-items: stretch; overflow-x: auto; padding-bottom: 2px; }
.rc-col { flex: 1 1 0; min-width: 56px; max-width: 120px; outline: none; border-radius: 4px; padding: 4px 2px; }
.rc-col:hover, .rc-col:focus-visible { background: #f4f7fc; }
.rc-col-plot { height: 130px; display: flex; flex-direction: column; justify-content: flex-end; align-items: center; border-bottom: 1px solid #d5dbe5; }
.rc-col-val { font-size: 11.5px; color: var(--text-2); margin-bottom: 4px; font-variant-numeric: tabular-nums; }
.rc-col-bar { width: 22px; border-radius: 4px 4px 0 0; background: #c3ccda; }
.rc-col-bar.cur { background: #1f6fe5; }
.rc-col-name { font-size: 11.5px; color: var(--muted); text-align: center; margin-top: 6px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rc-col-name.cur { color: var(--text); font-weight: 650; }
@media (max-width: 1100px) { .rc-kpi { grid-template-columns: repeat(2, minmax(0, 1fr)); } .rc-hero { grid-column: 1 / -1; } .rc-two { grid-template-columns: 1fr; } }
</style>
<style>
/* 提示浮层挂在 body 下，不能 scoped */
.rc-tip { position: fixed; z-index: 140; pointer-events: none; min-width: 150px; max-width: 220px; background: #1c2740; color: #fff; border-radius: 5px; padding: 8px 10px; font-size: 12px; box-shadow: 0 8px 24px #0d203c40; }
.rc-tip-title { color: #b9c5dd; margin-bottom: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rc-tip-line { display: flex; align-items: center; gap: 6px; line-height: 1.7; }
.rc-tip-line i { width: 10px; height: 3px; border-radius: 2px; flex-shrink: 0; }
.rc-tip-line span { color: #d6deee; flex: 1; }
.rc-tip-line b { font-weight: 650; }
.rc-tip-line em { font-style: normal; color: #b9c5dd; }
</style>

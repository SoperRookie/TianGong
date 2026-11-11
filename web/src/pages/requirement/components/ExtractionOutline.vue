<script setup>
/*
 整理稿逐条结构渲染：页面（## N.）› 区块（### N.M）› 条目；表格行还原成表格、流程行成等宽块、示例图说明 + 文案并成一行。
 每条可 核对通过（⚠ 且未处理）/ 修改 / 删除 → emit('review', { item_id, action, text })。
*/
import { ref, computed } from 'vue'
const props = defineProps({ ex: { type: Object, required: true }, filter: { type: String, default: 'all' }, canEdit: Boolean })
const emit = defineEmits(['review'])
const editId = ref(null), editText = ref('')
const items = computed(() => (props.ex.modules || []).flatMap(m => (m.items || []).map(it => ({ ...it, module: m.name }))))
const byId = computed(() => Object.fromEntries(items.value.map(it => [it.id, it])))
const outline = computed(() => props.ex.outline || (props.ex.modules || []).map((mod, i) => ({ no: String(i + 1), title: mod.name, source: '', blocks: [{ no: `${i + 1}.1`, name: mod.name, module: mod.name, source: '', rows: (mod.items || []).map(it => ({ t: 'item', id: it.id, lv: 0 })) }] })))
const keep = it => {
  const f = props.filter
  return f === 'all' || (f === 'unverified' && it.verified === false && !it.review) || (f === 'gap' && it.gap && !it.forced) || (f === 'forced' && it.forced) || (f === 'doubt' && it.doubt) || (f === 'draft' && it.draft) || (f === 'reviewed' && it.review)
}
const tail = (it, main) => {
  const others = (it.sources || []).filter(x => x !== main)
  return (it.doubt ? `〔疑点：${it.doubt}〕` : '') + (others.length ? ` 〔${others.join('；')}〕` : '')
}
const cells = it => (it.text || '').trim().split(/\s*｜\s*/)
function startEdit(it) { editId.value = it.id; editText.value = it.text }
function saveEdit() { emit('review', { item_id: editId.value, action: 'edit', text: editText.value }); editId.value = null }
function rowItems(r) { return (r.ids || []).map(id => byId.value[id]).filter(Boolean) }
/** 区块里有没有可显示的条目 */
function blockVisible(b) {
  return (b.rows || []).some(r => r.id ? (byId.value[r.id] && keep(byId.value[r.id])) : rowItems(r).some(keep))
}
let curGroup = null
const sections = computed(() => {
  curGroup = null
  return outline.value.map(sec => {
    const blocks = (sec.blocks || []).filter(blockVisible)
    const showGroup = sec.group && sec.group !== curGroup
    curGroup = sec.group || null
    return { ...sec, blocks, showGroup }
  }).filter(s => s.blocks.length)
})
const count = b => (b.rows || []).reduce((n, r) => n + (r.ids ? r.ids.length : 0) + (r.id ? 1 : 0), 0)
</script>
<template>
  <div class="ex-scroll">
    <template v-for="sec in sections" :key="sec.no + sec.title">
      <div v-if="sec.showGroup" class="ex-group"><b>{{ sec.gno }}. {{ sec.group }}</b></div>
      <div class="ex-sec" :class="{ sub: sec.group }"><b>{{ sec.no }}{{ sec.group ? '' : '.' }} {{ sec.title }}</b><span v-if="sec.source" class="tiny muted"> 〔来源：{{ sec.source }}〕</span></div>
      <template v-for="b in sec.blocks" :key="b.no">
        <div class="ex-block"><b>{{ b.no }} {{ b.name }}</b> <span class="tiny muted">{{ count(b) }} 条</span><span v-if="b.draft" class="badge red" style="margin-left:6px">疑似遗留草稿</span></div>
        <template v-for="(row, ri) in b.rows" :key="ri">
          <!-- 表格行 -->
          <table v-if="row.t === 'table' && rowItems(row).some(keep)" class="ex-table">
            <tr v-for="(it, ti) in (filter === 'all' ? rowItems(row) : [rowItems(row)[0], ...rowItems(row).slice(1).filter(keep)])" :key="it.id" :class="{ head: ti === 0 }">
              <template v-if="editId === it.id"><td :colspan="(row.cols || 1) + 1"><textarea v-model="editText" class="field" rows="2" /><div class="row" style="margin-top:4px"><button type="button" class="btn small primary" @click="saveEdit">保存</button><button type="button" class="btn small" @click="editId = null">取消</button></div></td></template>
              <template v-else>
                <td v-for="(c, ci) in cells(it)" :key="ci"><template v-if="ci === 0"><slot name="badge" :it="it" /></template>{{ c }}<span v-if="ci === cells(it).length - 1" class="tiny muted"> {{ tail(it, b.source) }}</span></td>
                <td class="ops"><slot name="ops" :it="it" :edit="() => startEdit(it)" /></td>
              </template>
            </tr>
          </table>
          <!-- 流程行 -->
          <div v-else-if="row.t === 'flow' && rowItems(row).some(keep)" class="ex-flow">
            <div v-for="it in rowItems(row).filter(keep)" :key="it.id" class="ex-item mono">
              <template v-if="editId === it.id"><div style="flex:1"><textarea v-model="editText" class="field" rows="2" /><div class="row" style="margin-top:4px"><button type="button" class="btn small primary" @click="saveEdit">保存</button><button type="button" class="btn small" @click="editId = null">取消</button></div></div></template>
              <template v-else><span style="flex:1"><slot name="badge" :it="it" />{{ it.text }}<span class="tiny muted"> {{ tail(it, b.source) }}</span></span><span class="ops"><slot name="ops" :it="it" :edit="() => startEdit(it)" /></span></template>
            </div>
          </div>
          <!-- 示例图：说明 + 文案 -->
          <template v-else-if="row.t === 'example'">
            <template v-for="it in [byId[row.id], ...rowItems(row)].filter(x => x && keep(x))" :key="it.id">
              <div class="ex-item" :class="{ chip: it.id !== row.id }">
                <template v-if="editId === it.id"><div style="flex:1"><textarea v-model="editText" class="field" rows="2" /><div class="row" style="margin-top:4px"><button type="button" class="btn small primary" @click="saveEdit">保存</button><button type="button" class="btn small" @click="editId = null">取消</button></div></div></template>
                <template v-else><span style="flex:1" :style="it.id === row.id ? 'font-weight:600' : ''"><slot name="badge" :it="it" />{{ it.text }}<span class="tiny muted"> {{ tail(it, b.source) }}</span></span><span class="ops"><slot name="ops" :it="it" :edit="() => startEdit(it)" /></span></template>
              </div>
            </template>
          </template>
          <!-- 普通条目 / 子标题 -->
          <div v-else-if="(row.t === 'item' || row.t === 'label') && byId[row.id] && keep(byId[row.id])" class="ex-item" :style="{ paddingLeft: (row.lv ? 30 : 6) + 'px' }">
            <template v-if="editId === row.id"><div style="flex:1"><textarea v-model="editText" class="field" rows="2" /><div class="row" style="margin-top:4px"><button type="button" class="btn small primary" @click="saveEdit">保存</button><button type="button" class="btn small" @click="editId = null">取消</button></div></div></template>
            <template v-else><span style="flex:1" :style="row.t === 'label' ? 'font-weight:600' : ''"><slot name="badge" :it="byId[row.id]" />{{ byId[row.id].text }}<span class="tiny muted"> {{ tail(byId[row.id], b.source) }}</span></span><span class="ops"><slot name="ops" :it="byId[row.id]" :edit="() => startEdit(byId[row.id])" /></span></template>
          </div>
        </template>
      </template>
    </template>
    <div v-if="!sections.length" class="empty"><strong>没有符合筛选的条目</strong></div>
  </div>
</template>
<style scoped>
.ex-scroll { max-height: 640px; overflow: auto; background: var(--line-2); padding: 4px 10px; border-radius: 6px; }
.ex-group { margin: 16px 0 4px; font-size: 15px; border-bottom: 2px solid var(--line); padding-bottom: 2px; }
.ex-sec { margin: 14px 0 4px; font-size: 14px; border-bottom: 2px solid var(--line); padding-bottom: 2px; }
.ex-sec.sub { margin: 10px 0 4px 4px; font-size: 13.5px; border-bottom: 0; border-left: 3px solid var(--blue); padding-left: 8px; }
.ex-block { margin: 8px 0 2px 8px; }
.ex-item { display: flex; gap: 6px; align-items: flex-start; padding: 4px 6px; border-bottom: 1px solid var(--line); line-height: 1.6; font-size: 12.5px; }
.ex-item.chip { padding-left: 24px; }
.ex-item .ops, .ex-table .ops { margin-left: auto; display: inline-flex; gap: 2px; flex-shrink: 0; white-space: nowrap; }
.ex-flow { margin: 4px 6px 6px; padding: 4px 0; border-left: 3px solid var(--line); }
.ex-table { border-collapse: collapse; width: 100%; margin: 4px 0; font-size: 12.5px; background: #fff; }
.ex-table td { border: 1px solid var(--line); padding: 4px 8px; vertical-align: top; }
.ex-table tr.head td { background: #f6f8fa; font-weight: 600; }
</style>

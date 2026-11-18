<script setup>
/*
 表格：cols [{ key, title, w?, cls?, align? }]，rows，rowKey（默认 index）。
 单元格插槽 #cell-<key>="{ row, i }"；行点击 emit row-click；loading / empty / emptyHint。
 selectable + v-model:selected（Set 或数组）显示勾选列。
*/
import { computed } from 'vue'
import TgEmpty from './TgEmpty.vue'
const props = defineProps({
  cols: { type: Array, required: true }, rows: { type: Array, default: () => [] }, rowKey: { type: [String, Function], default: '' },
  loading: Boolean, empty: { type: String, default: '暂无数据' }, emptyHint: { type: String, default: '' }, clickable: Boolean,
  selectable: Boolean, selected: { type: [Set, Array], default: () => new Set() }, dense: Boolean, activeKey: { type: [String, Number], default: null },
})
const emit = defineEmits(['row-click', 'update:selected'])
const keyOf = (r, i) => typeof props.rowKey === 'function' ? props.rowKey(r) : (props.rowKey ? r[props.rowKey] : i)
const selSet = computed(() => props.selected instanceof Set ? props.selected : new Set(props.selected))
const allOn = computed(() => props.rows.length > 0 && props.rows.every((r, i) => selSet.value.has(keyOf(r, i))))
function toggle(k) { const s = new Set(selSet.value); s.has(k) ? s.delete(k) : s.add(k); emit('update:selected', s) }
function toggleAll() { emit('update:selected', allOn.value ? new Set() : new Set(props.rows.map((r, i) => keyOf(r, i)))) }
</script>
<template>
  <div class="table-wrap" :class="{ loading }">
    <table class="table" :class="{ dense }">
      <thead><tr>
        <th v-if="selectable" style="width:34px"><input type="checkbox" :checked="allOn" @change="toggleAll" /></th>
        <th v-for="c in cols" :key="c.key" :style="c.w ? { width: c.w } : null" :class="[c.cls, c.align ? 'al-' + c.align : '']">{{ c.title }}</th>
      </tr></thead>
      <tbody>
        <tr v-for="(r, i) in rows" :key="keyOf(r, i)" :class="{ 'row-click': clickable, sel: activeKey !== null && keyOf(r, i) === activeKey }" @click="clickable && emit('row-click', r, i)">
          <td v-if="selectable" @click.stop><input type="checkbox" :checked="selSet.has(keyOf(r, i))" @change="toggle(keyOf(r, i))" /></td>
          <td v-for="c in cols" :key="c.key" :class="[c.cls, c.align ? 'al-' + c.align : '']"><slot :name="'cell-' + c.key" :row="r" :i="i">{{ r[c.key] ?? '—' }}</slot></td>
        </tr>
        <tr v-if="!rows.length"><td :colspan="cols.length + (selectable ? 1 : 0)"><TgEmpty :title="loading ? '加载中…' : empty" :hint="loading ? '' : emptyHint" /></td></tr>
      </tbody>
    </table>
  </div>
</template>

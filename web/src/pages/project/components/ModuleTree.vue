<script setup>
/* 项目模块树管理（3.10）：最多 5 级；同级拖动或上移 / 下移排序（POST modules/reorder）；编辑可改父级（move）；
   逻辑删除进回收站可恢复，被引用的不可物理删除（include_deleted=true 时回收站每项带 referenced）。
   props.project 为项目名；改动后 emit('changed')，由父页面决定是否刷新工作范围的模块树。 */
import { ref, computed, watch, onMounted } from 'vue'
import { api, seg } from '@/api/client'
import { useUiStore } from '@/stores/ui'
import { fmtTime } from '@/utils/format'
import { TgDialog, TgField, TgEmpty, TgIcon, TgBadge } from '@/components/ui'
const props = defineProps({ project: { type: String, required: true }, canManage: Boolean, canDelete: Boolean, isProjectAdmin: Boolean, hint: { type: String, default: '' } })
const emit = defineEmits(['changed'])
const ui = useUiStore()
const data = ref({ tree: [], deleted: [], max_depth: 5 }), loading = ref(false), error = ref(null)
const showDeleted = ref(false), collapsed = ref(new Set())
const dlg = ref(false), editing = ref(null), form = ref({ name: '', description: '', parent_id: '' }), saving = ref(false)
const dragId = ref(null), overId = ref(null)

async function reload(silent = false) {
  if (!silent) loading.value = true
  error.value = null
  try { data.value = await api.get(`/api/v1/projects/${seg(props.project)}/modules`, showDeleted.value ? { include_deleted: true } : undefined) }
  catch (e) { error.value = e } finally { loading.value = false }
}
onMounted(reload)
watch(() => props.project, () => reload())
watch(showDeleted, () => reload(true))
defineExpose({ reload })

const flatAll = computed(() => { const out = []; const walk = ns => (ns || []).forEach(n => { out.push(n); walk(n.children) }); walk(data.value.tree); return out })
/** 可见行（折叠的子树不展示） */
const rows = computed(() => {
  const out = []
  const walk = (ns, hidden) => (ns || []).forEach((n, i) => {
    if (!hidden) out.push({ n, idx: i, count: ns.length, has: !!(n.children && n.children.length) })
    walk(n.children, hidden || collapsed.value.has(n.module_id))
  })
  walk(data.value.tree, false)
  return out
})
const total = computed(() => flatAll.value.length)
function toggle(id) { const s = new Set(collapsed.value); s.has(id) ? s.delete(id) : s.add(id); collapsed.value = s }
function siblingsOf(n) { const p = n.parent_id ? flatAll.value.find(x => x.module_id === n.parent_id) : null; return p ? p.children : data.value.tree }
async function act(fn, okMsg) {
  try { await fn(); if (okMsg) ui.toast(okMsg); await reload(true); emit('changed') } catch (e) { ui.error(e) }
}
/** 上移 / 下移：交换相邻两项后整层提交 */
function move(n, delta) {
  const ids = siblingsOf(n).map(x => x.module_id)
  const i = ids.indexOf(n.module_id), j = i + delta
  if (j < 0 || j >= ids.length) return
  ;[ids[i], ids[j]] = [ids[j], ids[i]]
  act(() => api.post(`/api/v1/projects/${seg(props.project)}/modules/reorder`, { parent_id: n.parent_id || null, ordered_ids: ids }))
}
/* 同级拖动排序：拖到目标前面；跨层级不允许（改父级请用编辑） */
function onDragStart(n, e) { if (!props.canManage) return; dragId.value = n.module_id; e.dataTransfer.effectAllowed = 'move' }
function onDragOver(n, e) {
  const src = flatAll.value.find(x => x.module_id === dragId.value)
  if (!src || src.module_id === n.module_id || (src.parent_id || null) !== (n.parent_id || null)) { overId.value = null; return }
  e.preventDefault(); overId.value = n.module_id
}
function onDrop(n) {
  const src = flatAll.value.find(x => x.module_id === dragId.value)
  overId.value = null
  if (!src || src.module_id === n.module_id || (src.parent_id || null) !== (n.parent_id || null)) { dragId.value = null; return }
  const ids = siblingsOf(n).map(x => x.module_id).filter(id => id !== src.module_id)
  ids.splice(ids.indexOf(n.module_id), 0, src.module_id)
  dragId.value = null
  act(() => api.post(`/api/v1/projects/${seg(props.project)}/modules/reorder`, { parent_id: n.parent_id || null, ordered_ids: ids }))
}
function openDlg(m, parentId) {
  editing.value = m || null
  form.value = { name: m ? m.name : '', description: m ? (m.description || '') : '', parent_id: m ? (m.parent_id || '') : (parentId || '') }
  dlg.value = true
}
/** 父级候选：排除自身与子孙、深度 ≥ 5 的节点 */
const parentOptions = computed(() => {
  const ex = new Set()
  if (editing.value) { const walk = n => { ex.add(n.module_id); (n.children || []).forEach(walk) }; walk(editing.value) }
  return flatAll.value.filter(x => !ex.has(x.module_id) && x.depth < data.value.max_depth)
})
async function save() {
  const name = form.value.name.trim(), description = form.value.description.trim(), parent_id = form.value.parent_id || null
  if (!name) return ui.toast('模块名称不能为空', 'warn')
  if (name.includes('/')) return ui.toast('模块名不能含「/」（路径分隔符）', 'warn')
  saving.value = true
  const base = `/api/v1/projects/${seg(props.project)}/modules`
  try {
    if (editing.value) {
      const body = { name, description }
      if ((editing.value.parent_id || null) !== parent_id) { body.move = true; body.parent_id = parent_id }
      await api.put(`${base}/${editing.value.module_id}`, body)
    } else await api.post(base, { name, description, parent_id })
    dlg.value = false; ui.toast(editing.value ? '模块已保存' : `模块「${name}」已创建`)
    await reload(true); emit('changed')
  } catch (e) { ui.error(e) } finally { saving.value = false }
}
async function remove(n) {
  if (!await ui.confirm('删除模块？', `「${n.path}」及其子模块将进入回收站，可恢复；被需求 / 用例引用的模块不能永久删除。`, { ok: '删除', danger: true })) return
  act(() => api.del(`/api/v1/projects/${seg(props.project)}/modules/${n.module_id}`), '已移入回收站')
}
function restore(m) { act(() => api.post(`/api/v1/projects/${seg(props.project)}/modules/${m.module_id}/restore`, {}), `已恢复「${m.path}」`) }
async function purge(m) {
  if (!await ui.confirm('永久删除？', `永久删除模块「${m.path}」及其子模块，不可恢复。`, { ok: '永久删除', danger: true })) return
  act(() => api.del(`/api/v1/projects/${seg(props.project)}/modules/${m.module_id}`, undefined, { query: { permanent: true } }), '已永久删除')
}
</script>
<template>
  <div class="mtree">
    <div class="row mb">
      <span class="muted small">{{ total }} 个模块 · 最多 {{ data.max_depth }} 级<template v-if="hint"> · {{ hint }}</template></span>
      <span class="right row">
        <label class="tiny muted row" style="gap:4px;cursor:pointer"><input v-model="showDeleted" type="checkbox" /> 显示回收站</label>
        <button v-if="canManage" type="button" class="btn small primary" @click="openDlg(null, '')"><TgIcon name="plus" :size="13" /> 新建一级模块</button>
      </span>
    </div>
    <div v-if="error" class="err-box mb">{{ error.message }}</div>
    <div v-if="loading && !rows.length" class="muted">加载中…</div>
    <TgEmpty v-else-if="!rows.length" title="还没有模块" hint="新建一级模块（建议第一层对应端：H5 / 游戏后台管理 / 商户后台），或在需求整理稿「按端汇总」中按章节回写。" />
    <div v-else class="tree">
      <div v-for="r in rows" :key="r.n.module_id" class="node" :class="{ over: overId === r.n.module_id, dragging: dragId === r.n.module_id }" :style="{ paddingLeft: ((r.n.depth - 1) * 22 + 6) + 'px' }"
        :draggable="canManage" @dragstart="onDragStart(r.n, $event)" @dragover="onDragOver(r.n, $event)" @dragleave="overId === r.n.module_id && (overId = null)" @drop.prevent="onDrop(r.n)" @dragend="dragId = null; overId = null">
        <span class="tw" @click="r.has && toggle(r.n.module_id)">{{ r.has ? (collapsed.has(r.n.module_id) ? '▸' : '▾') : '·' }}</span>
        <TgIcon :name="r.n.depth === 1 ? 'folder' : 'file'" :size="13" />
        <span class="nm" :class="{ l1: r.n.depth === 1 }" :title="r.n.path">{{ r.n.name }}</span>
        <span v-if="r.n.description" class="muted tiny ellipsis desc">{{ r.n.description }}</span>
        <span v-if="r.has" class="tag">{{ r.n.children.length }} 子模块</span>
        <span class="muted tiny meta">L{{ r.n.depth }} · {{ r.n.created_by || '—' }}</span>
        <span v-if="canManage" class="ops row">
          <button v-if="r.n.depth < data.max_depth" type="button" class="btn ghost small" title="新建子模块" @click="openDlg(null, r.n.module_id)">＋ 子模块</button>
          <button type="button" class="btn ghost small" title="上移" :disabled="r.idx === 0" @click="move(r.n, -1)">↑</button>
          <button type="button" class="btn ghost small" title="下移" :disabled="r.idx === r.count - 1" @click="move(r.n, 1)">↓</button>
          <button type="button" class="btn ghost small" title="编辑 / 移动" @click="openDlg(r.n)">编辑</button>
          <button v-if="canDelete" type="button" class="btn ghost small danger-text" title="删除（逻辑删除，可恢复）" @click="remove(r.n)">删除</button>
        </span>
      </div>
    </div>
    <template v-if="showDeleted">
      <div class="panel-title mt mb">回收站 <span class="muted tiny">{{ data.deleted.length }} 项 · 恢复时父级仍在回收站则恢复到根</span></div>
      <TgEmpty v-if="!data.deleted.length" title="回收站为空" />
      <table v-else class="table dense">
        <thead><tr><th>模块路径</th><th style="width:90px">删除人</th><th style="width:130px">删除时间</th><th style="width:90px">引用</th><th style="width:170px"></th></tr></thead>
        <tbody>
          <tr v-for="m in data.deleted" :key="m.module_id">
            <td>{{ m.path }}</td><td class="muted">{{ m.deleted_by || '—' }}</td><td class="muted">{{ fmtTime(m.deleted_at) }}</td>
            <td><TgBadge v-if="m.referenced" status="candidate" text="有引用" /><span v-else class="muted">无</span></td>
            <td><span class="row">
              <button v-if="canManage" type="button" class="btn small" @click="restore(m)">恢复</button>
              <button v-if="isProjectAdmin && canDelete && !m.referenced" type="button" class="btn small danger" @click="purge(m)">永久删除</button>
            </span></td>
          </tr>
        </tbody>
      </table>
    </template>
    <TgDialog v-model="dlg" :title="editing ? `编辑模块 · ${editing.path}` : '新建模块'" :ok="editing ? '保存' : '创建'" :busy="saving" @ok="save">
      <TgField label="父级模块" hint="空为一级模块；改父级即移动（移动后子树最深不超过 5 级，不能移到自身或子孙下）">
        <select v-model="form.parent_id" class="select">
          <option value="">（一级模块）</option>
          <option v-for="x in parentOptions" :key="x.module_id" :value="x.module_id">{{ '　'.repeat(x.depth - 1) }}{{ x.path }}</option>
        </select>
      </TgField>
      <TgField label="模块名称" required hint="≤ 60 字，不含「/」；同级不可重名"><input v-model="form.name" class="field" maxlength="60" autofocus /></TgField>
      <TgField label="说明"><input v-model="form.description" class="field" placeholder="可空" /></TgField>
    </TgDialog>
  </div>
</template>
<style scoped>
.tree { border: 1px solid var(--line-2); border-radius: 4px; }
.node { display: flex; align-items: center; gap: 8px; min-height: 36px; padding-right: 8px; border-bottom: 1px solid var(--line-2); font-size: 12.5px; }
.node:last-child { border-bottom: 0; }
.node:hover { background: #f7f9fd; }
.node.over { box-shadow: inset 0 2px 0 var(--blue); }
.node.dragging { opacity: .45; }
.node[draggable="true"] { cursor: grab; }
.tw { width: 16px; text-align: center; color: #8aa0bb; font-size: 11px; cursor: pointer; flex-shrink: 0; }
.nm { color: var(--text-2); font-weight: 600; }
.nm.l1 { color: #2b3b5b; font-weight: 700; }
.desc { max-width: 260px; }
.meta { margin-left: auto; white-space: nowrap; }
.ops { gap: 2px; }
.ops .btn { height: 24px; padding: 0 6px; font-size: 11.5px; }
.danger-text { color: var(--red); }
</style>

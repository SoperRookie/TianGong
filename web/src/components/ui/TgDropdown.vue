<script setup>
/* 「更多 ▾」菜单：items [{ label, action: fn, danger?, disabled?, hint? }]，支持 divider: true。
   菜单 Teleport 到 body 并按按钮位置固定定位，不被 .table-wrap 等 overflow 容器裁剪；靠近底部时向上弹。 */
import { ref, onMounted, onBeforeUnmount, nextTick } from 'vue'
import TgIcon from './TgIcon.vue'
const props = defineProps({ label: { type: String, default: '更多' }, items: { type: Array, default: () => [] }, small: Boolean, icon: Boolean, align: { type: String, default: 'right' } })
const open = ref(false), root = ref(null), menu = ref(null), pos = ref({})
const onDoc = e => { if (root.value && !root.value.contains(e.target) && !(menu.value && menu.value.contains(e.target))) open.value = false }
const onScroll = () => { if (open.value) open.value = false }
onMounted(() => { document.addEventListener('click', onDoc); window.addEventListener('scroll', onScroll, true); window.addEventListener('resize', onScroll) })
onBeforeUnmount(() => { document.removeEventListener('click', onDoc); window.removeEventListener('scroll', onScroll, true); window.removeEventListener('resize', onScroll) })
async function toggle() {
  open.value = !open.value
  if (!open.value) return
  await nextTick()
  const r = root.value.getBoundingClientRect(), m = menu.value?.getBoundingClientRect() || { height: 0, width: 180 }
  const up = r.bottom + m.height + 8 > window.innerHeight && r.top > m.height + 8
  const style = { position: 'fixed', zIndex: 140 }
  if (up) style.bottom = (window.innerHeight - r.top + 4) + 'px'; else style.top = (r.bottom + 4) + 'px'
  if (props.align === 'right') style.right = Math.max(8, window.innerWidth - r.right) + 'px'; else style.left = r.left + 'px'
  pos.value = style
}
function run(it) { if (it.disabled) return; open.value = false; it.action && it.action() }
</script>
<template>
  <div ref="root" class="dd">
    <button type="button" class="btn" :class="{ small, 'icon-only': icon }" @click.stop="toggle" :title="icon ? label : ''">
      <template v-if="icon"><TgIcon name="more" :size="16" /></template>
      <template v-else>{{ label }} <TgIcon name="chevron" :size="12" /></template>
    </button>
    <Teleport to="body">
      <div v-if="open" ref="menu" class="dd-menu" :style="pos" @click.stop>
        <template v-for="(it, i) in items" :key="i">
          <div v-if="it.divider" class="dd-div" />
          <button v-else type="button" class="dd-item" :class="{ danger: it.danger, disabled: it.disabled }" :disabled="it.disabled" @click="run(it)">
            <span>{{ it.label }}</span><small v-if="it.hint" class="muted">{{ it.hint }}</small>
          </button>
        </template>
      </div>
    </Teleport>
  </div>
</template>

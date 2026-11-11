<script setup>
import { computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useScopeStore } from '@/stores/scope'
import { useUiStore } from '@/stores/ui'
import { useAuthStore } from '@/stores/auth'
import { MODULE_PAGES, sectionOf, groupOf, PAGE_INFO } from '@/nav'
import { useKeys } from '@/utils/useKeys'
import TopBar from './TopBar.vue'
import SubNav from './SubNav.vue'
import ModuleTree from './ModuleTree.vue'
import SearchOverlay from './SearchOverlay.vue'
import ToastHost from './ToastHost.vue'
import DialogHost from './DialogHost.vue'
import ProfileMenu from './ProfileMenu.vue'
import PasswordDialog from './PasswordDialog.vue'
import { TgIcon } from '@/components/ui'

const route = useRoute(), router = useRouter()
const scope = useScopeStore(), ui = useUiStore(), auth = useAuthStore()
const section = computed(() => sectionOf(route.name))
const group = computed(() => groupOf(route.name))
const showTree = computed(() => MODULE_PAGES.has(section.value))
const crumb = computed(() => {
  const info = PAGE_INFO[section.value]
  const parts = [group.value.name, info?.title]
  if (ui.crumb) parts.push(ui.crumb)
  return parts.filter(Boolean)
})
onMounted(() => scope.load())
watch(() => route.fullPath, () => { ui.crumb = ''; scope.pickerOpen = false })
useKeys({
  'mod+k': () => { ui.search = !ui.search },
  escape: () => { if (ui.search) ui.search = false; else if (scope.pickerOpen) scope.pickerOpen = false; else if (ui.profile) ui.profile = false },
})
</script>
<template>
  <TopBar />
  <SubNav :group="group" :section="section" />
  <div class="work-layout" :class="{ wide: !showTree }">
    <ModuleTree v-if="showTree" />
    <div class="workspace">
      <div class="page-utility">
        <div><TgIcon name="layers" :size="13" /> 当前位置 / <strong>{{ crumb.join(' / ') }}</strong><span v-if="scope.project" class="muted"> · {{ scope.project }}<template v-if="scope.module"> / {{ scope.module }}</template></span></div>
        <div class="util-r"><TgIcon name="info" :size="13" /> <span v-if="auth.version">v{{ auth.version }} · </span>{{ auth.displayName }}<span v-if="auth.isAdmin" class="badge gray" style="margin-left:6px">管理员</span></div>
      </div>
      <div class="content">
        <router-view v-slot="{ Component }">
          <component :is="Component" :key="route.name === 'req-detail' || route.name === 'design-detail' || route.name === 'plan-detail' || route.name === 'project-detail' ? String(route.params.id || route.params.name) : route.fullPath.split('?')[0]" />
        </router-view>
      </div>
    </div>
  </div>
  <SearchOverlay />
  <ProfileMenu />
  <PasswordDialog />
  <DialogHost />
  <ToastHost />
</template>

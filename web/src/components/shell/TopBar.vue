<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NAV, groupOf } from '@/nav'
import { useScopeStore } from '@/stores/scope'
import { useUiStore } from '@/stores/ui'
import { useAuthStore } from '@/stores/auth'
import { useBadgeStore } from '@/stores/badge'
import { TgIcon } from '@/components/ui'
import ScopePicker from './ScopePicker.vue'

const route = useRoute(), router = useRouter()
const scope = useScopeStore(), ui = useUiStore(), auth = useAuthStore(), badge = useBadgeStore()
const cur = computed(() => groupOf(route.name).id)
const initial = computed(() => (auth.displayName || '?').slice(0, 1).toUpperCase())
const notifyOpen = ref(false)
function go(g) { router.push({ name: g.pages[0][0] }) }
let timer = null
onMounted(() => { badge.refresh(); timer = setInterval(() => badge.refresh(true), 60000) })
onBeforeUnmount(() => clearInterval(timer))
</script>
<template>
  <header class="global-top">
    <router-link to="/" class="brand"><span class="brand-mark"><img src="/brand/owl-mono.svg" alt="" width="16" height="16" /></span>天工 <b>AI</b></router-link>
    <div class="scope" :class="{ open: scope.pickerOpen }" title="切换工作范围（产品线 / 项目）" @click="scope.pickerOpen = !scope.pickerOpen">
      <TgIcon name="folder" :size="16" /><div><small>工作范围</small><strong>{{ scope.label }} ▾</strong></div>
    </div>
    <ScopePicker v-if="scope.pickerOpen" @close="scope.pickerOpen = false" />
    <nav class="main-menu" aria-label="主导航">
      <button v-for="g in NAV" :key="g.id" type="button" class="main-tab" :class="{ active: g.id === cur }" @click="go(g)">
        {{ g.name }}<span v-if="g.id === 'design' && badge.design" class="n">{{ badge.design }}</span><span v-else-if="g.id === 'plan' && badge.exec" class="n">{{ badge.exec }}</span><span v-else-if="g.id === 'requirement' && badge.requirement" class="n">{{ badge.requirement }}</span>
      </button>
    </nav>
    <div class="global-actions">
      <div class="search-top" @click="ui.search = true"><TgIcon name="search" :size="14" /> 搜索需求 / 任务 / 用例 / 计划 / 知识… <kbd>⌘K</kbd></div>
      <div class="notify-wrap">
        <button type="button" class="icon-btn" title="通知：待审核 / 待修改 / 分配给我 / 待确认需求 / AI 任务" @click="notifyOpen = !notifyOpen"><TgIcon name="bell" :size="17" /><span v-if="badge.total" class="dot">{{ badge.total > 99 ? '99+' : badge.total }}</span></button>
        <div v-if="notifyOpen" class="notify-menu" @click="notifyOpen = false">
          <div class="notify-title">待我处理</div>
          <router-link v-for="n in badge.items" :key="n.key" :to="n.to" class="notify-item"><span>{{ n.label }}</span><b>{{ n.count }}</b></router-link>
          <div v-if="!badge.items.length" class="muted tiny" style="padding:10px 12px">没有待办</div>
        </div>
      </div>
      <button type="button" class="avatar" title="个人资料 / 修改密码 / 两步验证 / 退出" @click="ui.profile = !ui.profile">
        <img v-if="auth.user?.avatar" :src="auth.user.avatar" alt="" /><template v-else>{{ initial }}</template>
      </button>
    </div>
  </header>
</template>

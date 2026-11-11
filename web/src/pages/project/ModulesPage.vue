<script setup>
/* 模块与端（3.10 + v2.2.0 端）：当前工作范围项目的模块树管理（复用 ModuleTree）+ 端的定义与三层判定；改动后刷新 scope 的模块树。 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { TERMINALS, BUSINESS_TERMINALS } from '@/utils/terminals'
import { useProjectActions } from './components/useProjectActions'
import ModuleTree from './components/ModuleTree.vue'
import { PageHeading, TgPanel, TgEmpty, TgIcon, TgBadge } from '@/components/ui'
const router = useRouter(), scope = useScopeStore(), auth = useAuthStore()
const { canDelete, isProjectAdmin } = useProjectActions()
scope.load()
const project = computed(() => scope.project)
const row = computed(() => scope.current || {})
const canManage = computed(() => !!project.value && auth.can(project.value, 'version.manage') && row.value.status !== 'archived')
/* 端清单与关键词示例（与 app/terminals.py 内置一致；config/terminals.yaml 可覆盖业务端） */
const TERM_DESC = {
  h5: { who: 'H5（玩家端）', kw: 'h5 / 前台 / 用户端 / 玩家端 / 会员端 / 移动端 / app / 小程序 / 游戏大厅 / 活动页 / C 端', eg: '活动页、报名、榜单、我的记录' },
  game_admin: { who: '游戏后台管理', kw: '游戏后台 / 活动运营后台 / 运营后台 / 总后台 / 平台后台 / 管理后台 / 活动配置 / 派奖 / 风控后台', eg: '活动列表 / 派奖查询 / 连赢活动统计' },
  merchant_admin: { who: '商户后台', kw: '商户后台 / 商户端 / 商户管理 / 商家后台 / 代理后台 / 租户后台 / 商户报表', eg: '会员 / 商户管理、活动设置、榜单、报名记录、增量奖池流水、操作记录' },
  common: { who: '通用', kw: '需求说明 / 需求背景 / 版本记录 / 修订记录 / 功能清单 / 术语 / 通用规则', eg: '对不上任何端的章节' },
  e2e: { who: '端到端', kw: '端到端', eg: '跨端主线一致性场景，来源标「端到端场景」' },
  other: { who: '其他', kw: '—', eg: '三层判定都对不上时记「其他」' },
}
</script>
<template>
  <PageHeading title="模块与端" :sub="project ? `${project} 的模块树（第一层建议对应端）与端的定义；整理稿「按端汇总」可按章节归端并回写模块。` : '模块树按项目独立维护，请先在顶栏选择一个项目作为工作范围。'">
    <template #actions>
      <router-link v-if="project" class="btn" :to="{ name: 'project-detail', params: { name: project, sub: 'modules' } }">项目详情 ›</router-link>
      <button type="button" class="btn primary" @click="scope.pickerOpen = true"><TgIcon name="layers" :size="14" /> {{ project ? '切换项目' : '选择项目' }}</button>
    </template>
  </PageHeading>
  <div class="grid-2">
    <TgPanel>
      <template #title>模块树 <TgBadge v-if="row.status" :status="row.status" :text="row.status_label" /></template>
      <template #hint>最多 5 级；逻辑删除可恢复（POST modules/{id}/restore）；被需求 / 用例引用的不可物理删除；同级拖动或上下移动排序（POST modules/reorder）。<template v-if="project && !canManage">当前角色只能查看。</template></template>
      <ModuleTree v-if="project" :key="project" :project="project" :can-manage="canManage" :can-delete="canDelete" :is-project-admin="isProjectAdmin(project)" @changed="scope.loadModules(true)" />
      <TgEmpty v-else title="未选择项目" hint="在顶栏「工作范围」选择一个项目后，这里显示它的模块树。">
        <template #action><button type="button" class="btn primary" @click="scope.pickerOpen = true">选择项目</button></template>
      </TgEmpty>
    </TgPanel>
    <div class="stack">
      <TgPanel title="已知的端与三层判定" hint="app/terminals.py · 端由提取时从文档里识别，这里是识别时的参考与关键词兜底" :pad="false">
        <table class="table dense">
          <thead><tr><th style="width:120px">端</th><th>关键词</th><th>章节示例</th></tr></thead>
          <tbody><tr v-for="t in TERMINALS" :key="t.key"><td><b>{{ t.label }}</b><span v-if="BUSINESS_TERMINALS.some(b => b.key === t.key)" class="badge blue" style="margin-left:6px">业务端</span></td><td class="muted">{{ TERM_DESC[t.key]?.kw || '—' }}</td><td class="muted">{{ TERM_DESC[t.key]?.eg || '—' }}</td></tr></tbody>
        </table>
        <div class="information" style="margin:12px"><TgIcon name="info" :size="14" /><span>一份需求有哪些端不用配置：需求提取完成后模型从文档里识别，文档里的端就是上表某一个时用它，表里没有的（如荷官端、现场大屏、开放接口）按文档叫法新建。判定三层：① 提取完成后模型识别端并按整理稿顶层章节归端一次（Prompt terminal_classify），人可在提取页改、新建端或重新识别；② 发起测试设计时模块名按章节前缀对回端；③ 都对不上按名字关键词兜底（商户后台、游戏后台优先于 H5），再兜不住记「其他」。</span></div>
      </TgPanel>
      <TgPanel title="端在各环节的作用">
        <div class="list">
          <div><b>需求提取</b><div class="tiny muted">按端汇总：按端看总需求，章节归端可人工改</div></div>
          <div><b>测试点审核</b><div class="tiny muted">按端分列表审核；端到端场景单独一组；可按端单独确认生成用例</div></div>
          <div><b>用例</b><div class="tiny muted">按端页签查看、按端导出；左侧模块树第一层按端筛选</div></div>
        </div>
      </TgPanel>
    </div>
  </div>
</template>

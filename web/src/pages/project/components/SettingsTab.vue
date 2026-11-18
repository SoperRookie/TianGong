<script setup>
/* 项目设置（3.4 / 3.5 / 9.14）：编辑项目信息、knowledge_auto 开关、归档 / 恢复、危险区（删除项目，仅系统管理员）。 */
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { api, seg } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtTime } from '@/utils/format'
import { useProjectActions, roleLabel } from './useProjectActions'
import ProjectDialog from './ProjectDialog.vue'
import ProjectDeleteDialog from './ProjectDeleteDialog.vue'
import { TgPanel, TgKv, TgBadge } from '@/components/ui'
const props = defineProps({ project: { type: String, required: true }, row: { type: Object, default: () => ({}) } })
const emit = defineEmits(['changed', 'renamed'])
const router = useRouter(), auth = useAuthStore(), ui = useUiStore()
const { setStatus, isProjectAdmin } = useProjectActions()
const r = computed(() => props.row || {})
const canEdit = computed(() => auth.can(props.project, 'project.edit') && r.value.status !== 'archived')
const admin = computed(() => isProjectAdmin(props.project))
const editDlg = ref(false), delDlg = ref(false), toggling = ref(false)
async function toggleAuto(e) {
  const v = e.target.checked
  toggling.value = true
  try { await api.put(`/api/v1/projects/${seg(props.project)}`, { knowledge_auto: v }); ui.toast(v ? '已开启自动沉淀与画像自动提炼' : '已关闭自动沉淀：本项目不再沉淀知识、不再自动提炼画像'); emit('changed') }
  catch (err) { ui.error(err); e.target.checked = !v } finally { toggling.value = false }
}
async function status(s) { try { if (await setStatus(props.project, s)) emit('changed') } catch (e) { ui.error(e) } }
function onSaved(name) { if (name !== props.project) emit('renamed', name); else emit('changed') }
</script>
<template>
  <div class="grid-2">
    <div class="stack">
      <TgPanel title="项目信息" hint="名称为自然键，改名联动全部引用">
        <template #actions><button v-if="auth.can(project, 'project.edit')" type="button" class="btn small primary" :disabled="r.status === 'archived'" :title="r.status === 'archived' ? '已归档，先恢复再编辑' : ''" @click="editDlg = true">编辑项目</button></template>
        <TgKv :pairs="[['名称', r.project], ['编码', r.code || '—'], ['产品线', r.business_line || '未归属'], ['负责人', r.owner || '—'], ['我的角色', roleLabel(r.my_role) || '—'], ['创建', `${fmtTime(r.created_at)} · ${r.created_by || '—'}`], ['更新', `${fmtTime(r.updated_at)} · ${r.updated_by || '—'}`]]">
          <dt>状态</dt><dd><TgBadge :status="r.status" :text="r.status_label" /></dd>
        </TgKv>
        <div v-if="r.description" class="source-block mt" style="white-space:pre-wrap">{{ r.description }}</div>
      </TgPanel>
      <TgPanel title="知识沉淀" hint="需求原文 / 通过的测试点 / 通过的用例 / 终稿 / 已确认分析自动入项目知识库，并每新增几个任务自动提炼一版画像草稿">
        <label class="row" style="cursor:pointer"><input type="checkbox" :checked="r.knowledge_auto !== false" :disabled="!canEdit || toggling" @change="toggleAuto" /> 自动沉淀知识与项目画像草稿 <span class="muted tiny">（关闭后回溯按钮禁用，画像只能手动提炼）</span></label>
      </TgPanel>
    </div>
    <div class="stack">
      <TgPanel title="状态流转" hint="归档后项目内数据只读，仅项目管理员可恢复；暂停不限制修改">
        <div class="row">
          <button v-if="r.status !== 'active'" type="button" class="btn small success" :disabled="!admin" @click="status('active')">恢复为进行中</button>
          <button v-if="r.status === 'active'" type="button" class="btn small" :disabled="!admin" @click="status('paused')">暂停</button>
          <button v-if="r.status !== 'archived'" type="button" class="btn small" :disabled="!admin" @click="status('archived')">归档</button>
          <span v-if="!admin" class="muted tiny">仅项目管理员可变更状态</span>
        </div>
      </TgPanel>
      <TgPanel v-if="auth.isAdmin" title="危险区">
        <div class="danger-zone">
          <div class="row"><div><b>删除项目</b><div class="tiny muted">先预检项目内容：空项目直接删除；非空须输入项目名确认并连同需求 / 计划 / 任务一起删除（任务进回收站）。仅系统管理员。</div></div><button type="button" class="btn small danger right" @click="delDlg = true">删除项目…</button></div>
        </div>
      </TgPanel>
    </div>
  </div>
  <ProjectDialog v-model="editDlg" :project="r" @saved="onSaved" />
  <ProjectDeleteDialog v-model="delDlg" :project="project" @deleted="router.replace({ name: 'projects' })" />
</template>

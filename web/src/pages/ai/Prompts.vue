<script setup>
/* Prompt 清单（11.15 / 11.20）：类型 / 使用场景 / 占位符 / 生效版本 / 定制状态；行点击进详情。 */
import { ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { fmtTime } from '@/utils/format'
import { PageHeading, TgPanel, TgTable, TgBadge, TgSeg, TgIcon } from '@/components/ui'

const route = useRoute(), router = useRouter()
const kind = ref(route.query.kind || ''), state = ref(route.query.state || ''), keyword = ref(route.query.keyword || ''), kw = ref(keyword.value)
watch([kind, state, keyword], () => router.replace({ query: { kind: kind.value || undefined, state: state.value || undefined, keyword: keyword.value || undefined } }))
const { data, loading, error, reload } = useLoad(() => api.get('/api/v1/ai/prompts', { kind: kind.value || undefined, state: state.value || undefined, keyword: keyword.value || undefined, page_size: 0 }), { deps: [kind, state, keyword] })
const rows = computed(() => data.value?.prompts || [])
const kc = computed(() => data.value?.kind_counts || {}), sc = computed(() => data.value?.state_counts || {})
const KIND = { system: 'system', user: 'user', block: '注入块', shared: '共享片段' }
const cols = [{ key: 'key', title: 'Key', w: '200px' }, { key: 'name', title: '名称' }, { key: 'kind', title: '类型', w: '80px' }, { key: 'where', title: '使用场景' }, { key: 'placeholders', title: '占位符' }, { key: 'active_version', title: '生效版本', w: '100px' }, { key: 'state', title: '状态', w: '100px' }, { key: 'updated_at', title: '更新', w: '110px' }]
</script>
<template>
  <PageHeading title="Prompt 管理" :sub="`${data?.total ?? 0} 个纳管 Prompt：类型 / 使用场景 / 占位符 / 生效版本 / 定制状态；内置默认随代码更新，定制版本可新建、激活、回滚、归档（管理员）。`">
    <template #actions><button type="button" class="btn" @click="reload()"><TgIcon name="refresh" :size="14" /> 刷新</button></template>
  </PageHeading>
  <div v-if="error" class="err-box">{{ error.message }}</div>
  <TgPanel :pad="false">
    <div class="panel-pad filters" style="padding-bottom:10px">
      <TgSeg v-model="kind" :items="[{ key: '', label: '全部类型' }, { key: 'system', label: 'system', count: kc.system }, { key: 'user', label: 'user', count: kc.user }, { key: 'block', label: '注入块', count: kc.block }, { key: 'shared', label: '共享片段', count: kc.shared }]" />
      <TgSeg v-model="state" :items="[{ key: '', label: '全部' }, { key: 'builtin', label: '内置', count: sc.builtin }, { key: 'customized', label: '已定制', count: sc.customized }, { key: 'draft', label: '有草稿', count: sc.draft }]" />
      <input v-model="kw" class="field" style="width:240px" placeholder="搜索 key / 名称 / 场景 / 正文" @keyup.enter="keyword = kw" @blur="keyword = kw" />
    </div>
    <TgTable :cols="cols" :rows="rows" row-key="key" :loading="loading" clickable @row-click="p => router.push({ name: 'prompt-detail', params: { key: p.key } })">
      <template #cell-key="{ row }"><span class="mono">{{ row.key }}</span></template>
      <template #cell-name="{ row }"><b>{{ row.name }}</b><div class="sub ellipsis" style="max-width:360px" :title="row.preview">{{ row.preview }}</div></template>
      <template #cell-kind="{ row }"><span class="tag">{{ KIND[row.kind] || row.kind }}</span></template>
      <template #cell-where="{ row }"><span class="small muted">{{ row.where }}</span></template>
      <template #cell-placeholders="{ row }"><template v-if="row.placeholders?.length"><span v-for="x in row.placeholders.slice(0, 4)" :key="x" class="tag mono" style="margin-right:4px">{{ '{' + x + '}' }}</span><span v-if="row.placeholders.length > 4" class="tiny muted">+{{ row.placeholders.length - 4 }}</span></template><span v-else class="muted">—</span></template>
      <template #cell-active_version="{ row }">v{{ row.active_version }} <span class="tiny muted">/ {{ row.versions }} 版</span></template>
      <template #cell-state="{ row }"><TgBadge v-if="row.customized" color="purple" text="已定制" /><TgBadge v-else color="gray" text="内置默认" /><TgBadge v-if="row.drafts" color="orange" :text="`草稿 ${row.drafts}`" style="margin-left:4px" /></template>
      <template #cell-updated_at="{ row }">{{ fmtTime(row.updated_at) }}</template>
    </TgTable>
    <div class="table-footer"><span>行点击进详情：版本链、各版本内容、共享片段展开后的全文（【共享:名称】）、版本对比</span></div>
  </TgPanel>
</template>

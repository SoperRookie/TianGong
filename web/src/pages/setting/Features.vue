<script setup>
/* 功能清单（docs/xt/00，232 项）：按分册分组、关键字搜索、入口 / 说明列；编号经 FEATURE_MAP 映射到页面给「去页面」链接。 */
import { ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import features from '@/data/features.json'
import { FEATURE_MAP } from '@/specs'
import { PAGE_INFO, sectionOf } from '@/nav'
import { PageHeading, TgPanel, TgTabs, TgEmpty } from '@/components/ui'

const route = useRoute(), router = useRouter()
const domain = ref(route.query.domain || 'all'), kw = ref(route.query.q || ''), q = ref(kw.value)
watch([domain, q], () => router.replace({ query: { domain: domain.value !== 'all' ? domain.value : undefined, q: q.value || undefined } }))
const domains = computed(() => { const m = new Map(); features.forEach(f => m.set(f.domain, f.domain_name)); return [...m.entries()] })
/* 编号 → 路由名：优先非详情页（列表页能直达），其次详情页的所属二级页 */
const pageOf = no => {
  const hits = Object.entries(FEATURE_MAP).filter(([, arr]) => arr.includes(no)).map(([k]) => k)
  if (!hits.length) return null
  return hits.find(k => sectionOf(k) === k) || sectionOf(hits[0])
}
const rows = computed(() => {
  const k = q.value.trim().toLowerCase()
  return features.filter(f => (domain.value === 'all' || f.domain === domain.value) && (!k || `${f.no} ${f.name} ${f.entry} ${f.desc}`.toLowerCase().includes(k)))
})
const covered = computed(() => features.filter(f => pageOf(f.no)).length)
const tabs = computed(() => [{ key: 'all', label: '全部', count: features.length }, ...domains.value.map(([k, n]) => ({ key: k, label: `${k} ${n}`, count: features.filter(f => f.domain === k).length }))])
</script>
<template>
  <PageHeading :title="`功能清单（${features.length} 项）`" :sub="`来自 docs/xt/00-功能清单与文档索引.md；每项标注对应的页面，已映射 ${covered} / ${features.length}，其余为服务端机制。`">
    <template #actions><input v-model="kw" class="field" style="width:240px" placeholder="搜索编号 / 功能 / 入口 / 说明" @keyup.enter="q = kw" @input="q = kw" /></template>
  </PageHeading>
  <TgPanel :pad="false">
    <div class="panel-pad" style="padding-bottom:0"><TgTabs v-model="domain" :items="tabs" /></div>
    <div class="panel-pad" style="padding-top:0">
      <div class="feature-row" style="color:#6f7c8c;font-weight:700;font-size:11px;border-bottom:1px solid #e0e6ed"><span>编号</span><span>功能</span><span>入口</span><span>说明</span><span>页面</span></div>
      <div v-for="f in rows" :key="f.no" class="feature-row">
        <span class="no">{{ f.no }}</span>
        <span><b>{{ f.name }}</b></span>
        <span class="muted">{{ f.entry }}</span>
        <span class="muted">{{ f.desc }}</span>
        <span><router-link v-if="pageOf(f.no)" :to="{ name: pageOf(f.no) }" style="font-weight:700">{{ PAGE_INFO[pageOf(f.no)]?.title || pageOf(f.no) }} →</router-link><span v-else class="muted tiny">服务端机制</span></span>
      </div>
      <TgEmpty v-if="!rows.length" title="没有匹配的功能" />
    </div>
  </TgPanel>
</template>

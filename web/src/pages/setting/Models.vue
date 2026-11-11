<script setup>
/* 模型配置（1.10、11.1～11.9）：模型清单与默认、完整接入配置（密钥只显示环境变量名）、保存热重载、连通性测试。 */
import { ref, computed, reactive, onMounted } from 'vue'
import { api } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { num } from '@/utils/format'
import { PageHeading, TgPanel, TgBadge, TgStat, TgKv, TgEmpty, TgIcon, TgDropdown, TgField } from '@/components/ui'
import ModelDialog from './components/ModelDialog.vue'

const auth = useAuthStore(), ui = useUiStore()
const { data: pub, reload: reloadPub } = useLoad(() => api.get('/api/v1/models'))
const cfg = ref(null), loading = ref(false), error = ref(null), saving = ref(false), dirty = ref(false)
const head = reactive({ default_model: '', max_retries: 1, monthly_budget_usd: 0 })
async function load() {
  if (!auth.isAdmin) return
  loading.value = true; error.value = null
  try { cfg.value = await api.get('/api/v1/models/config'); Object.assign(head, { default_model: cfg.value.default_model || '', max_retries: cfg.value.max_retries, monthly_budget_usd: cfg.value.monthly_budget_usd }); dirty.value = false } catch (e) { error.value = e } finally { loading.value = false }
}
onMounted(load)
const models = computed(() => cfg.value?.models || [])
const names = computed(() => models.value.map(m => m.name))

/** 保存整份配置：PUT /models/config（去掉只读的 api_key_set） */
async function save(nextModels, toastMsg) {
  const body = { default_model: head.default_model || null, max_retries: +head.max_retries || 0, monthly_budget_usd: +head.monthly_budget_usd || 0, models: (nextModels || models.value).map(({ api_key_set, ...m }) => m) }
  if (!body.models.length && body.default_model) body.default_model = null
  saving.value = true
  try { await api.put('/api/v1/models/config', body); ui.toast(toastMsg || '已保存并热重载'); await load(); reloadPub() } catch (e) { ui.error(e) } finally { saving.value = false }
}
const dlg = ref(false), editing = ref(null)
function openNew() { editing.value = null; dlg.value = true }
function openEdit(m) { editing.value = m; dlg.value = true }
function onDialogSave(m) {
  const next = editing.value ? models.value.map(x => x.name === editing.value.name ? { ...m } : x) : [...models.value, m]
  if (!head.default_model) head.default_model = m.name
  dlg.value = false
  save(next, editing.value ? `已更新 ${m.name}` : `已添加 ${m.name}`)
}
async function remove(m) {
  if (!await ui.confirm(`删除模型 ${m.name}？`, `从 models.yaml 移除该条目；其它模型降级链路里对它的引用需同时清理${m.name === head.default_model ? '；它是当前默认模型，删除后默认改为清单第一个' : ''}。`, { ok: '删除', danger: true })) return
  const next = models.value.filter(x => x.name !== m.name).map(x => ({ ...x, fallbacks: (x.fallbacks || []).filter(f => f !== m.name) }))
  if (head.default_model === m.name) head.default_model = next[0]?.name || ''
  save(next, `已删除 ${m.name}`)
}
function setDefault(m) { head.default_model = m.name; save(null, `默认模型已设为 ${m.name}`) }
const testing = reactive({}), testResult = reactive({})
async function test(m) {
  testing[m.name] = true; delete testResult[m.name]
  try { testResult[m.name] = await api.post('/api/v1/models/test', { name: m.name }) } catch (e) { testResult[m.name] = { ok: false, error: e.message } } finally { testing[m.name] = false }
}
async function testAll() { for (const m of models.value) await test(m) }
const hasReasoning = m => !!m.reasoning_effort
</script>
<template>
  <PageHeading title="模型配置" :sub="auth.isAdmin ? 'config/models.yaml 由本页管理：默认模型、失败重试、月预算、各模型接入与降级链路；密钥只存环境变量名（服务器 .env），页面不回显密钥。保存整体校验后原子写回并热重载。' : '可用模型清单与默认模型；完整接入配置仅管理员可见。'">
    <template #actions>
      <template v-if="auth.isAdmin">
        <button type="button" class="btn primary" @click="openNew"><TgIcon name="plus" :size="14" /> 添加模型</button>
        <TgDropdown :items="[{ label: '全部连通性测试', action: testAll, hint: '各发一次 ping' }, { label: '重新读取配置', action: load }]" />
      </template>
    </template>
  </PageHeading>
  <div v-if="error" class="err-box">{{ error.message }}</div>
  <div class="stat-grid">
    <TgStat label="默认模型" :value="pub?.default_model || '—'" foot="任务创建 / 需求页可覆盖；评审默认同模型" icon="spark" color="blue" />
    <TgStat label="需求提取模型" :value="pub?.requirement_extract_model || pub?.default_model || '—'" foot="TIANGONG_REQUIREMENT_EXTRACT_MODEL，以服务器 .env 为准" icon="file" color="purple" />
    <TgStat label="失败重试" :value="cfg ? `${cfg.max_retries} 次` : '—'" foot="之后沿降级链路 fallbacks 逐个尝试" icon="refresh" color="orange" />
    <TgStat label="月预算" :value="cfg ? (cfg.monthly_budget_usd ? '$' + num(cfg.monthly_budget_usd) : '未设') : '—'" foot="用量统计页按单价折算余额" icon="chart" color="green" :to="{ name: 'usage' }" />
  </div>

  <template v-if="auth.isAdmin">
    <TgPanel class="mb" title="全局参数" hint="改动后点保存；各用途模型：默认模型为生成 / 评审 / 分析的兜底，需求提取模型与 Embedding 由配置项决定">
      <div class="row gap">
        <TgField label="默认模型" inline><select v-model="head.default_model" class="select" @change="dirty = true"><option v-for="n in names" :key="n" :value="n">{{ n }}</option></select></TgField>
        <TgField label="失败重试次数" inline><input v-model="head.max_retries" class="field" type="number" min="0" max="5" style="width:80px" @input="dirty = true" /></TgField>
        <TgField label="月预算（美元）" inline><input v-model="head.monthly_budget_usd" class="field" type="number" min="0" step="10" style="width:120px" @input="dirty = true" /></TgField>
        <button type="button" class="btn primary" :disabled="!dirty || saving" @click="save()">{{ saving ? '保存中…' : '保存' }}</button>
      </div>
    </TgPanel>
    <TgPanel :pad="false">
      <div class="table-wrap" :class="{ loading }"><table class="table">
        <thead><tr><th>名称</th><th>接入</th><th>单价 $/1M 入 / 出</th><th>参数</th><th>Vision</th><th>降级链路</th><th>连通性</th><th style="width:90px">操作</th></tr></thead>
        <tbody>
          <tr v-for="m in models" :key="m.name">
            <td><b>{{ m.name }}</b><TgBadge v-if="m.name === cfg?.default_model" color="blue" text="默认" style="margin-left:6px" /><div class="sub">{{ m.provider }} · {{ m.model }}</div></td>
            <td><span class="mono tiny">{{ m.base_url }}</span><div class="sub">密钥 <span class="mono">{{ m.api_key_env || '（无鉴权）' }}</span> <span v-if="!m.api_key_env" class="muted">—</span><span v-else-if="m.api_key_set" class="ok">已设</span><span v-else style="color:#db5361;font-weight:700">未设置</span></div></td>
            <td>{{ m.input_price }} / {{ m.output_price }}</td>
            <td>温度 {{ m.temperature }}<template v-if="hasReasoning(m)"> · 推理 {{ m.reasoning_effort }}</template><div class="sub">max_tokens {{ num(m.max_tokens) }} · 超时 {{ m.timeout }}s</div></td>
            <td><span v-if="m.supports_vision" class="ok">✓</span><span v-else class="no">—</span></td>
            <td><template v-if="m.fallbacks?.length"><span v-for="(f, i) in m.fallbacks" :key="f"><span class="tag">{{ f }}</span><span v-if="i < m.fallbacks.length - 1" class="muted"> → </span></span></template><span v-else class="muted">无</span></td>
            <td>
              <button type="button" class="btn small" :disabled="testing[m.name]" @click="test(m)">{{ testing[m.name] ? '测试中…' : '测试' }}</button>
              <div v-if="testResult[m.name]" class="sub" :style="{ color: testResult[m.name].ok ? '#179b75' : '#db5361' }">{{ testResult[m.name].ok ? `通过 · ${testResult[m.name].elapsed_ms} ms · ${testResult[m.name].reply || ''}` : testResult[m.name].error }}</div>
            </td>
            <td><TgDropdown small :items="[{ label: '编辑', action: () => openEdit(m) }, { label: '设为默认', action: () => setDefault(m), disabled: m.name === cfg?.default_model }, { divider: true }, { label: '删除', action: () => remove(m), danger: true }]" /></td>
          </tr>
          <tr v-if="!models.length"><td colspan="8"><TgEmpty :title="loading ? '加载中…' : '还没有模型'" hint="点「添加模型」接入第一个模型；密钥写在服务器 .env。" /></td></tr>
        </tbody>
      </table></div>
      <div class="table-footer"><span>GPT-6 / 5.6 / 5.5 温度只能为 1，仅 5.4 系可设 0.3；*-pro 型号不支持 chat 接口。连通性测试只对该模型发一次 ping，不走降级链路，会产生一次真实调用。</span></div>
    </TgPanel>
  </template>
  <TgPanel v-else :pad="false">
    <div class="table-wrap"><table class="table"><thead><tr><th>名称</th><th>厂商</th><th>厂商侧模型名</th><th>Vision</th></tr></thead><tbody>
      <tr v-for="m in pub?.models || []" :key="m.name"><td><b>{{ m.name }}</b><TgBadge v-if="m.is_default" color="blue" text="默认" style="margin-left:6px" /></td><td>{{ m.provider }}</td><td>{{ m.model }}</td><td><span v-if="m.supports_vision" class="ok">✓</span><span v-else class="no">—</span></td></tr>
    </tbody></table></div>
  </TgPanel>

  <div class="grid-2 mt">
    <TgPanel title="Embedding" hint="models.yaml embeddings 段，页面不管理">
      <TgKv :pairs="[['模型', 'text-embedding-3-large（2026-09-24 起）'], ['维度', '3072（与向量库校验，不一致报错）'], ['分批 / 重试', '按批向量化（16 条一批）；失败重试；复用未改切片的旧向量'], ['并发', 'TIANGONG_EMBEDDING_MAX_CONCURRENCY=4'], ['说明', '换模型需重建 data/knowledge；bge-m3 等私有化接入保留配置示例']]" />
    </TgPanel>
    <TgPanel title="调用封装（LLMClient）">
      <div class="list">
        <div v-for="[a, b] in [['降级链路与重试', 'fallbacks 逐个尝试，退避重试 max_retries 次（限流 base 3 秒、其余 1 秒）'], ['并发信号量', 'LLM / 任务 / Embedding 三级闸门，超限排队'], ['参数适配', 'OpenAI 兼容：temperature / reasoning_effort / max_tokens 按模型能力裁剪'], ['JSON 守卫', '代码块 / 花括号段提取、逐对象抢救、截断判断、节点级重试'], ['Vision 路由', '需求图片、文档内嵌图、执行截图；大图切块'], ['调用日志', '每次 chat 落 ai_calls，含缓存命中 / 推理 token']]" :key="a"><b>{{ a }}</b><div class="tiny muted">{{ b }}</div></div>
      </div>
    </TgPanel>
  </div>
  <ModelDialog v-model="dlg" :model="editing" :names="names" @save="onDialogSave" />
</template>
<style scoped>
/* 行尾「更多 ▾」菜单不被表格容器裁剪 */
.table-wrap { overflow: visible; }
</style>

<script setup>
/* 系统与部署（1.1～1.20 等，只读）：/health 版本与状态、运行信息、配置项一览（以服务器 .env 为准）、部署与运维脚本、日志留存。 */
import { ref, onMounted } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { fmtTime } from '@/utils/format'
import { PageHeading, TgPanel, TgStat, TgKv, TgIcon, TgBadge } from '@/components/ui'
const auth = useAuthStore()
const health = ref(null), healthErr = ref(''), checkedAt = ref(''), kw = ref('')
async function check() {
  healthErr.value = ''
  try { const r = await fetch('/health'); health.value = await r.json(); checkedAt.value = new Date().toISOString() } catch (e) { healthErr.value = String(e.message || e) }
}
onMounted(check)
/* 配置项一览：来自 docs/xt/01 §2.9 / §3，静态写死；实际值以服务器 .env 为准，页面不读取 */
const CONFIG = [
  ['数据库', 'TIANGONG_DB_URL', 'mysql+pymysql://root@127.0.0.1:3306/tiangong?charset=utf8mb4', 'MySQL / SQLite 连接串；kv_docs 文档表 + 四张明细表；密码特殊字符自动编码'],
  ['数据库', 'TIANGONG_SINGLE_INSTANCE_LOCK', 'true', 'outputs/.instance.lock 文件锁拒绝第二个实例'],
  ['模型', 'TIANGONG_MODELS_CONFIG_PATH', 'config/models.yaml', '模型接入配置；缺失时按 config/models.example.yaml 生成空清单'],
  ['模型', 'TIANGONG_REQUIREMENT_EXTRACT_MODEL', '（默认模型）', '需求提取用的模型'],
  ['模型', 'TIANGONG_LLM_MAX_CONCURRENCY', '4', '模型调用并发信号量'],
  ['模型', 'TIANGONG_TASK_MAX_CONCURRENCY', '2', '同时执行的测试设计任务数，超出排队 queued'],
  ['模型', 'TIANGONG_EMBEDDING_MAX_CONCURRENCY', '4', 'Embedding 并发；每批 16 条，客户端重试 2 次'],
  ['密钥', 'OPENAI_API_KEY 等', '—', '各模型 api_key_env 指向的环境变量；只在服务器 .env，页面不回显'],
  ['流水线', 'TIANGONG_POINT_E2E', 'true', '拆解前通读需求找端到端场景'],
  ['流水线', 'TIANGONG_POINT_COMBINE', 'true', '拆解 / 延伸 / 查漏后自动模块级组合延伸'],
  ['流水线', 'TIANGONG_REQUIREMENT_EXTRACT_TERMINALS', 'true', '需求提取后从文档里识别端并按顶层章节归端（已知的 H5 / 游戏后台管理 / 商户后台 / 通用，或按文档叫法新建）'],
  ['流水线', 'TIANGONG_GENERATION_MODULE_SCOPE', 'true', '多模块生成时只带本模块所在章节原文'],
  ['知识', 'TIANGONG_KNOWLEDGE_AUTO_INGEST', 'true', '知识自动沉淀全局开关；项目设置可单独关闭'],
  ['记忆', 'TIANGONG_MEMORY_PREF_THRESHOLD / REVISION_THRESHOLD', '3 / 2', '模板 / 模型使用满 N 次固化默认；修订指令跨任务重复 N 次固化注入'],
  ['上传', 'TIANGONG_MAX_UPLOAD_SIZE_MB', '100', '单文件上限，超限 413'],
  ['上传', 'TIANGONG_MAX_ATTACHMENT_SIZE_MB', '200', '执行附件上限'],
  ['上传', 'TIANGONG_MAX_PDF_PAGES', '300', 'PDF 页数上限'],
  ['上传', 'TIANGONG_MAX_ZIP_UNCOMPRESSED_MB', '300', 'zip 解压上限；压缩比 > 200:1、条目 > 20000、嵌套 > 2 层拒绝'],
  ['上传', 'TIANGONG_MAX_VISION_IMAGE_MB', '5', 'Vision 单图上限（缩放后）'],
  ['日志', 'TIANGONG_LOG_LEVEL / TIANGONG_CONSOLE_LOG', 'INFO / true', '控制台日志；server.sh 置 false'],
  ['日志', 'TIANGONG_LOG_FILE_LEVEL', 'INFO', 'logs/tiangong_YYYY-MM-DD.log 文件日志级别'],
  ['日志', 'TIANGONG_LOG_FILE_MAX_MB / RETENTION_DAYS / COMPRESS', '50 / 7 / true', '每日零点或按大小轮转、保留天数、gzip 压缩'],
  ['日志', 'TIANGONG_LOG_RETENTION_DAYS', '90', '数据库 ai_calls 与 audit_log 保留天数（0 不清），启动 + 每 24 小时清理'],
  ['网络', 'TIANGONG_HOST / TIANGONG_PORT', '0.0.0.0 / 8000', '监听地址与端口（仅 server.sh 读）'],
  ['网络', 'TIANGONG_TRUSTED_PROXIES', '（空）', '只有来自可信代理的 X-Forwarded-For 才采信；compose 固定 172.28.0.10'],
  ['网络', 'TIANGONG_EXPOSE_DOCS', 'false', '/docs、/openapi.json 开关'],
  ['安全', 'TIANGONG_ADMIN_PASSWORD', '（随机，只打印一次）', '初始管理员口令；未配置则随机生成并强制首次改密'],
]
const SCRIPTS = [
  ['scripts/dev.sh', '本地开发：拉起 mysqld（scripts/mysql_dev.sh，data/mysql）后以 --reload 启动 uvicorn'],
  ['scripts/mysql_dev.sh', '本地 MySQL：socket /tmp/tiangong-mysql.sock、3306，等待 30 秒'],
  ['scripts/server.sh start|stop|restart|status|logs|update', '服务器源码部署：等待 /health 通过（90 秒）；停止 20 秒后强杀；server.out 超 10MB 轮转；升级前磁盘 ≥ 1GB'],
  ['Dockerfile + docker-compose.yml', 'mysql + app + nginx 三容器；nginx 固定 IP 作为可信代理；健康检查 30 秒间隔；容器日志 json-file 50m × 3'],
  ['docker/nginx.conf', 'client_max_body_size 210m；读写超时 300 秒'],
  ['scripts/migrate_prod.sh', '生产迁移：备份 + 迁移完成度报告'],
  ['scripts/reset_data.py', '数据重置（开发 / 测试环境）'],
  ['scripts/disk_cleanup.sh', '磁盘清理：LOG_KEEP_DAYS=7、KEEP_BACKUPS=3、binlog 保留 3 天'],
  ['scripts/reset_case_reviews.py', '重置用例审核状态'],
  ['scripts/upload_final.py', '离线终稿回传'],
  ['scripts/embedding_poc.py / prompt_poc.py / vision_poc.py', 'Embedding / Prompt / Vision POC 脚本'],
  ['scripts/gen_prompt_doc.py', '生成 Prompt 清单文档'],
]
const RUNTIME = [['进程模型', '单进程、单事件循环；outputs/.instance.lock 拒绝第二个实例'], ['启动流程', '按固定顺序初始化 Store → 幂等迁移（任务 → 需求实体、执行轮次 → 计划、待确认事项模块回填）→ 后台清理；关停等知识沉淀写完（最多 60 秒）'], ['中间件', '%2F 保留、安全响应头、静态 no-cache、可信代理 IP 采信、Bearer 鉴权（免登 /api/v1/auth/login、/health）'], ['Store', '内存态 + 写穿：启动把命名空间全部读进内存，写同步 / 后台单线程（tg-persist）写穿到库；旧 JSON 一次性迁移'], ['并发闸门', '模型调用 / 测试设计任务 / Embedding 三级信号量'], ['测试体系', 'pytest：临时目录 + 每测试一个 SQLite；StubLLM 按顺序回包隔离模型']]
const filtered = () => kw.value ? CONFIG.filter(r => r.join(' ').toLowerCase().includes(kw.value.toLowerCase())) : CONFIG
</script>
<template>
  <PageHeading title="系统与部署" :sub="`v${health?.version || auth.version || '—'} · FastAPI 单进程 + Vue 前端 · MySQL kv_docs 文档表 + 四张明细表（ai_calls / audit_log / entity_versions / recycle_bin）· Qdrant 本地向量库。本页只读；配置实际值以服务器 .env 为准。`">
    <template #actions><button type="button" class="btn" @click="check"><TgIcon name="refresh" :size="14" /> 健康检查</button></template>
  </PageHeading>
  <div class="stat-grid">
    <TgStat label="服务状态" :value="healthErr ? '异常' : (health?.status === 'ok' ? '正常' : (health?.status || '—'))" :foot="healthErr || `GET /health · ${checkedAt ? fmtTime(checkedAt, true) : ''}`" icon="shield" :color="healthErr ? 'red' : 'green'" />
    <TgStat label="版本" :value="health?.version ? 'v' + health.version : '—'" foot="app/__init__.py 单一来源" icon="git" color="blue" />
    <TgStat label="当前登录" :value="auth.displayName" :foot="`${auth.user?.username} · ${auth.isAdmin ? '系统管理员' : '成员'} · 可见范围 ${auth.accessMode}`" icon="user" color="purple" />
    <TgStat label="两步验证 / 可见范围" :value="auth.settings ? `${auth.settings.totp_enabled ? '开' : '关'} / ${auth.settings.access_mode}` : '—'" foot="设置 › 访问控制" icon="lock" color="orange" :to="auth.isAdmin ? { name: 'access' } : null" />
  </div>
  <TgPanel :pad="false" class="mb">
    <div class="panel-pad" style="padding-bottom:10px"><div class="panel-head" style="margin-bottom:0"><div><div class="panel-title">配置项一览</div><div class="panel-hint">环境变量前缀 TIANGONG_，.env 经 load_dotenv 先载入；此表为静态说明（docs/xt/01），默认值与说明<b>以服务器 .env 为准</b>，页面不读取实际值。</div></div><input v-model="kw" class="field" style="width:220px" placeholder="搜索配置项" /></div></div>
    <div class="table-wrap"><table class="table dense">
      <thead><tr><th style="width:70px">分组</th><th>环境变量名</th><th>默认值</th><th>说明</th></tr></thead>
      <tbody><tr v-for="r in filtered()" :key="r[1]"><td><span class="tag">{{ r[0] }}</span></td><td class="mono">{{ r[1] }}</td><td class="mono tiny">{{ r[2] }}</td><td class="small">{{ r[3] }}</td></tr></tbody>
    </table></div>
  </TgPanel>
  <div class="grid-2">
    <TgPanel title="部署与运维脚本" hint="scripts/*.sh、*.py 与 docker-compose">
      <div class="list"><div v-for="[a, b] in SCRIPTS" :key="a"><b class="mono">{{ a }}</b><div class="tiny muted">{{ b }}</div></div></div>
    </TgPanel>
    <div>
      <TgPanel title="运行与防护">
        <div class="list"><div v-for="[a, b] in RUNTIME" :key="a"><b>{{ a }}</b><div class="tiny muted">{{ b }}</div></div></div>
      </TgPanel>
      <TgPanel class="mt" title="日志留存">
        <TgKv :pairs="[['应用日志', 'logs/tiangong_YYYY-MM-DD.log：每日零点或 50MB 轮转，gzip，保留 7 天；uvicorn 日志一并接管'], ['数据库日志', 'ai_calls 与 audit_log 保留 TIANGONG_LOG_RETENTION_DAYS（默认 90）天，启动及每 24 小时清理'], ['容器日志', 'json-file 50m × 3'], ['server.out', '超 10MB 轮转（server.sh）'], ['查看', '操作日志页 / 调用日志页；服务器 scripts/server.sh logs']]" />
      </TgPanel>
    </div>
  </div>
</template>

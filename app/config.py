"""全局配置：环境变量优先，配置与代码分离。"""

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

# 将 .env 中的密钥（如 DEEPSEEK_API_KEY）载入环境变量，供模型配置的 api_key_env 解析
load_dotenv(BASE_DIR / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="TIANGONG_", extra="ignore")

    app_name: str = "tiangong"
    debug: bool = False

    # LLM 适配层
    models_config_path: Path = BASE_DIR / "config" / "models.yaml"

    # 任务队列
    redis_url: str = "redis://localhost:6379/0"

    # 数据库（所有业务数据入库）：生产 MySQL；测试经 TIANGONG_DB_URL 指向 SQLite 隔离
    db_url: str = "mysql+pymysql://root@127.0.0.1:3306/tiangong?charset=utf8mb4"

    # 持久化目录：任务产物与应用数据（模板/记忆）；测试经环境变量指向临时目录隔离
    outputs_dir: Path = BASE_DIR / "outputs"
    data_dir: Path = BASE_DIR / "data"

    # 日志（Loguru）：控制台级别与文件目录（按日滚动，DEBUG 全量）
    log_level: str = "INFO"
    log_dir: Path = BASE_DIR / "logs"

    # 文件上传限制（F-2-8），单位 MB
    max_upload_size_mb: int = 50

    # 大文档分片阈值（F-2-6），单位字符：超过则按章节切分并行分段生成
    chunk_max_chars: int = 10000

    # 知识库（F-7-x）：本地向量库目录与检索切片大小（字符）
    knowledge_dir: Path = BASE_DIR / "data" / "knowledge"
    knowledge_chunk_chars: int = 600

    # 知识注入任务级总预算（字符，按 5:3:2 配额分配，F-7-6）
    knowledge_budget_chars: int = 6000

    # 记忆注入独立预算（F-8-7）：不占知识库配额，约为知识预算的 10%（PRD 建议 ≤5%-10%）
    memory_budget_chars: int = 600

    # 使用习惯自动沉淀阈值（F-8-2）：模板/模型按使用次数、修订指令按跨任务重复次数。
    # 调低会更快沉淀但更容易把一次性行为固化为偏好（修订阈值为 1 时每条修订都会注入后续生成）
    memory_pref_threshold: int = 3
    memory_revision_threshold: int = 2

    # 学习规则注入预算（需求三十九）：已确认生效的团队/项目规则，独立于知识与记忆配额
    rules_budget_chars: int = 800

    # 历史用例复用提示阈值（需求二十九）：向量相似度达到该值提示「高度相关，可复用」
    reuse_hint_score: float = 0.78

    # 登录认证：默认开启；初始管理员首启自动创建——未显式配置 TIANGONG_ADMIN_PASSWORD 时生成随机口令
    # 只打印一次并强制首次登录改密
    auth_enabled: bool = True
    admin_username: str = "admin"
    admin_password: str = ""
    session_ttl_hours: int = 72
    # 登录防爆破：同一用户名+IP 连续失败次数与锁定时长（分钟）
    login_max_failures: int = 5
    login_lockout_minutes: int = 15

    # 反向代理：只有来自这些代理 IP 的请求才信任 X-Forwarded-For（逗号分隔；为空则一律用直连 IP）
    trusted_proxies: str = ""
    # OpenAPI 文档（/docs、/openapi.json）：生产默认关闭
    expose_docs: bool = False
    # 单实例守卫：内存态存储不支持多 worker，启动时加文件锁防止误起多进程
    single_instance_lock: bool = True

    # 上传与解析防护
    max_attachment_size_mb: int = 200   # 执行附件（视频/压缩包）上限
    max_pdf_pages: int = 300            # PDF 解析页数上限
    max_zip_uncompressed_mb: int = 300  # zip 类文档（docx/xlsx/xmind）解压后总大小上限
    max_vision_image_mb: int = 5        # 送 Vision 模型的单图上限

    # LLM / Embedding 并发上限与限流退避
    llm_max_concurrency: int = 4
    embedding_max_concurrency: int = 4

    # 日志留存（天）：AI 调用日志与操作日志启动时清理更早的记录；0 表示不清理
    log_retention_days: int = 180


@lru_cache
def get_settings() -> Settings:
    return Settings()

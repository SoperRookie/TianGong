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


@lru_cache
def get_settings() -> Settings:
    return Settings()

import os
import tempfile
from pathlib import Path

import pytest

# 存储隔离：必须在任何 app 模块导入前设置（app.main 导入即初始化日志文件目录），
# 因此放在 conftest 导入期而非 fixture——pytest 保证 conftest 先于测试模块加载。
# 避免测试污染仓库 data/（记忆、使用计数、模板）、outputs/ 与 logs/。
_TEST_STORAGE = Path(tempfile.mkdtemp(prefix="tiangong-test-"))
for _env, _sub in [
    ("TIANGONG_OUTPUTS_DIR", "outputs"),
    ("TIANGONG_DATA_DIR", "data"),
    ("TIANGONG_KNOWLEDGE_DIR", "knowledge"),
    ("TIANGONG_LOG_DIR", "logs"),
]:
    os.environ[_env] = str(_TEST_STORAGE / _sub)

# 业务接口测试默认免登录；登录鉴权行为由 test_auth.py 显式开启后单独覆盖
os.environ["TIANGONG_AUTH_ENABLED"] = "false"

# 数据库隔离：测试用临时 SQLite（持久化层同一套 SQLAlchemy 代码，生产为 MySQL）
os.environ["TIANGONG_DB_URL"] = f"sqlite:///{_TEST_STORAGE / 'test.db'}"

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.db import reset_engine_cache  # noqa: E402

reset_engine_cache()


@pytest.fixture(autouse=True)
def _db_isolation(tmp_path, monkeypatch):
    """每个测试独立数据库（与旧文件时代 tmp_path 隔离等价），避免状态跨测试泄漏。"""
    monkeypatch.setenv("TIANGONG_DB_URL", f"sqlite:///{tmp_path / 'db.sqlite'}")
    get_settings.cache_clear()
    reset_engine_cache()
    yield
    get_settings.cache_clear()
    reset_engine_cache()

from app.llm.registry import ModelRegistry  # noqa: E402
from app.llm.schemas import ModelConfig  # noqa: E402


@pytest.fixture
def registry_with_vision() -> ModelRegistry:
    return ModelRegistry(
        default_model="deepseek-chat",
        models=[
            ModelConfig(
                name="deepseek-chat",
                provider="deepseek",
                base_url="https://api.deepseek.com/v1",
                api_key_env="DEEPSEEK_API_KEY",
                model="deepseek-chat",
            ),
            ModelConfig(
                name="qwen-vl",
                provider="dashscope",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                api_key_env="DASHSCOPE_API_KEY",
                model="qwen-vl-max",
                supports_vision=True,
            ),
        ],
    )

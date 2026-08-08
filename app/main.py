"""FastAPI 应用入口。启动：uvicorn app.main:app --reload；Web 界面访问 http://localhost:8000/"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from loguru import logger

from app.api.routes import router
from app.config import BASE_DIR, get_settings
from app.llm.client import LLMClient
from app.llm.registry import ModelRegistry
from app.logging_setup import setup_logging
from app.memory import MemoryStore
from app.tasks import TaskStore
from app.templates import TemplateStore

setup_logging(get_settings().log_level, get_settings().log_dir)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    registry = ModelRegistry.from_yaml(settings.models_config_path)
    app.state.registry = registry
    app.state.llm = LLMClient(registry)
    app.state.tasks = TaskStore(output_dir=settings.outputs_dir)
    app.state.templates = TemplateStore(storage_path=settings.data_dir / "templates.json")
    app.state.memory = MemoryStore(storage_path=settings.data_dir / "memory.json")
    logger.info(
        "服务启动：默认模型={} 可用模型={} 输出目录={} 日志目录={}",
        registry.default_model,
        [m["name"] for m in registry.list_public()],
        settings.outputs_dir,
        settings.log_dir,
    )
    yield
    logger.info("服务关闭")


app = FastAPI(title="TestCase Agent", version="0.1.0", lifespan=lifespan)
app.include_router(router)


@app.get("/", include_in_schema=False)
async def web_index() -> FileResponse:
    """Web 界面（M4-W2）：单页静态实现，后续可平移 Vue3 工程化前端。"""
    return FileResponse(BASE_DIR / "app" / "web" / "index.html")

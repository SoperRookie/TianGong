"""FastAPI 应用入口。启动：uvicorn app.main:app --reload"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.config import get_settings
from app.llm.client import LLMClient
from app.llm.registry import ModelRegistry


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    registry = ModelRegistry.from_yaml(settings.models_config_path)
    app.state.registry = registry
    app.state.llm = LLMClient(registry)
    yield


app = FastAPI(title="TestCase Agent", version="0.1.0", lifespan=lifespan)
app.include_router(router)

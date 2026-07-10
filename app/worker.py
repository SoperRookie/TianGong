"""Celery 应用骨架（F-6-2）：长任务异步执行。启动：celery -A app.worker worker -l info"""

from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "tiangong",
    broker=settings.redis_url,
    backend=settings.redis_url,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
)

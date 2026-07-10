"""任务记录（M1 内存实现）。

M4 任务管理（F-6-1/2）落地时替换为数据库 + Celery 异步队列；
当前仅支撑单进程内的创建-查询-下载闭环。
"""

import uuid
from pathlib import Path

from pydantic import BaseModel, Field


class TaskRecord(BaseModel):
    task_id: str
    status: str = "completed"  # M1 同步执行：completed / failed
    sources: list[str] = Field(default_factory=list)
    result: dict | None = None
    error: str | None = None
    files: dict[str, str] = Field(default_factory=dict)  # 格式 -> 文件路径


class TaskStore:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self._records: dict[str, TaskRecord] = {}

    def new_task_dir(self) -> tuple[str, Path]:
        task_id = uuid.uuid4().hex[:12]
        task_dir = self.output_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        return task_id, task_dir

    def save(self, record: TaskRecord) -> None:
        self._records[record.task_id] = record

    def get(self, task_id: str) -> TaskRecord | None:
        return self._records.get(task_id)

"""任务记录与异步执行（F-6-1/2）。

M4-W1 实现：记录 JSON 落盘（重启可恢复）+ 进程内 asyncio 后台执行与进度状态。
分布式部署时执行层替换为 Celery + Redis（PRD 选型），记录层接口不变。
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from pydantic import BaseModel, Field

# 任务状态机：queued → running → completed / failed；拆解确认流程有 awaiting_confirmation
TASK_STATUSES = ("queued", "running", "completed", "failed", "awaiting_confirmation")


class TaskRecord(BaseModel):
    task_id: str
    status: str = "completed"
    progress: str | None = Field(default=None, description="执行阶段：analyzing / generating_reviewing / exporting")
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    sources: list[str] = Field(default_factory=list)
    result: dict | None = None
    error: str | None = None
    files: dict[str, str] = Field(default_factory=dict)  # 格式 -> 文件路径
    analysis: dict | None = Field(default=None, description="拆解确认阶段的分析结果（F-3-3）")
    context: dict | None = Field(default=None, description="待确认任务的生成上下文（需求文本/模型/模板）")
    knowledge: list[dict] = Field(default_factory=list, description="知识快照（F-7-13）：本次任务注入的知识切片留痕")
    revisions: list[dict] = Field(default_factory=list, description="多轮修订历史（F-3-5）：指令与结果留痕")
    review_log: list[dict] = Field(default_factory=list, description="在线评审留痕（F-6-6）：逐条采纳/修改/删除与反馈")
    offline_review: dict | None = Field(default=None, description="离线评审终稿回传 diff（F-6-8）")
    memories: list[dict] = Field(default_factory=list, description="记忆快照（F-8-6 引用可见）：本次生成注入的记忆")


class TaskStore:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self._records_path = output_dir / "tasks.json"
        self._records: dict[str, TaskRecord] = self._load()
        self._jobs: dict[str, asyncio.Task] = {}

    def _load(self) -> dict[str, TaskRecord]:
        if not self._records_path.exists():
            return {}
        try:
            raw = json.loads(self._records_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        records = {}
        for item in raw:
            record = TaskRecord.model_validate(item)
            # 重启恢复：进行中的任务已随进程丢失，显式标记失败（Celery 接入后由队列重投）
            if record.status in ("queued", "running"):
                record.status = "failed"
                record.error = "服务重启导致任务中断，请重新提交"
            records[record.task_id] = record
        return records

    def _persist(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        data = [r.model_dump() for r in self._records.values()]
        self._records_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def new_task_dir(self) -> tuple[str, Path]:
        task_id = uuid.uuid4().hex[:12]
        task_dir = self.output_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        return task_id, task_dir

    def save(self, record: TaskRecord) -> None:
        self._records[record.task_id] = record
        self._persist()

    def get(self, task_id: str) -> TaskRecord | None:
        return self._records.get(task_id)

    def list(self, status: str | None = None, limit: int = 50) -> list[TaskRecord]:
        records = sorted(self._records.values(), key=lambda r: r.created_at, reverse=True)
        if status:
            records = [r for r in records if r.status == status]
        return records[:limit]

    def set_progress(self, task_id: str, status: str | None = None, progress: str | None = None) -> None:
        record = self._records.get(task_id)
        if record is None:
            return
        if status:
            record.status = status
        record.progress = progress
        self._persist()

    # ---- 异步执行（F-6-2）：进程内后台任务，Celery 落地时替换此层 ----

    def submit(self, task_id: str, job: Callable[[], Awaitable[None]]) -> None:
        """将任务放入后台执行；job 自身负责更新最终状态。"""
        self.set_progress(task_id, status="queued")

        async def _run() -> None:
            self.set_progress(task_id, status="running")
            try:
                await job()
            except Exception as e:  # 后台任务兜底：任何未捕获异常标记失败
                record = self.get(task_id)
                if record is not None:
                    record.status = "failed"
                    record.error = str(e)
                    record.progress = None
                    self.save(record)
            finally:
                self._jobs.pop(task_id, None)

        self._jobs[task_id] = asyncio.create_task(_run())

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

from loguru import logger
from pydantic import BaseModel, Field

# 任务状态机：queued → running → completed / failed；拆解确认流程有 awaiting_confirmation
TASK_STATUSES = ("queued", "running", "completed", "failed", "awaiting_confirmation")


class TaskRecord(BaseModel):
    task_id: str
    status: str = "completed"
    progress: str | None = Field(default=None, description="执行阶段：analyzing / generating_reviewing / exporting")
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    created_by: str | None = Field(default=None, description="创建人（登录用户名），任务归属精确到人")
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
    # ---- 生成质量核心闭环（生成质量核心需求设计）----
    point_review_log: list[dict] = Field(default_factory=list, description="测试点审核留痕：逐条/批量 通过/驳回/修改/删除")
    case_reviews: dict[str, dict] = Field(default_factory=dict, description="用例审核状态机：uid -> {status, comment, reject_count, locked}")
    fix_log: list[dict] = Field(default_factory=list, description="驳回定点修改 Diff 留痕（需求三十四）：修改前 vs 修改后")
    coverage: dict | None = Field(default=None, description="测试维度覆盖矩阵（需求八）：维度 -> 已覆盖/未覆盖/不适用/待确认")
    dup_report: dict | None = Field(default=None, description="重复检查结果（需求十一~十三）：疑似重复对与人工处置")
    reuse_hints: list[dict] = Field(default_factory=list, description="历史用例复用提示（需求二十九）：高度相关的正式用例")
    requirement_diff: dict | None = Field(default=None, description="需求变更差异分析（需求四十~四十五）：变化类型与受影响资产")
    quality: dict | None = Field(default=None, description="AI 自检评分（需求五十八）：仅作参考，不自动通过")
    rules: list[dict] = Field(default_factory=list, description="规则快照（需求三十九）：本次生成注入的团队/项目规则")
    executions: list[dict] = Field(default_factory=list, description="用例执行轮次与逐条执行记录（执行留痕）")


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
            record.progress = None  # 进度是进程内状态，重启后一律清除
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

    def cancel(self, task_id: str) -> bool:
        """取消后台任务（任务管理）：中断执行并标记失败留痕。

        仅对后台队列中的任务有效；前台同步请求中的执行无法从此处中断。
        """
        record = self._records.get(task_id)
        job = self._jobs.get(task_id)
        if record is None or record.status not in ("queued", "running") or job is None:
            return False
        job.cancel()
        self._jobs.pop(task_id, None)
        record.status = "failed"
        record.error = "任务已被用户取消"
        record.progress = None
        self._persist()
        logger.info("任务 {} 已被用户取消", task_id)
        return True

    def submit(self, task_id: str, job: Callable[[], Awaitable[None]]) -> None:
        """将任务放入后台执行；job 自身负责更新最终状态。"""
        self.set_progress(task_id, status="queued")

        async def _run() -> None:
            self.set_progress(task_id, status="running")
            logger.info("后台任务开始执行：{}", task_id)
            try:
                await job()
                logger.info("后台任务完成：{}", task_id)
            except Exception as e:  # 后台任务兜底：任何未捕获异常标记失败
                logger.exception("后台任务失败：{}（{}）", task_id, e)
                record = self.get(task_id)
                if record is not None:
                    record.status = "failed"
                    record.error = str(e)
                    record.progress = None
                    self.save(record)
            finally:
                self._jobs.pop(task_id, None)

        self._jobs[task_id] = asyncio.create_task(_run())

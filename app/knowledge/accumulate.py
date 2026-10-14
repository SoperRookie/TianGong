"""知识自动沉淀：任务 / 需求过程中的产物自动进项目知识库，让下一次生成"认识这个业务"。

沉淀节点（都在已有的人工确认动作之后，不需要用户多做任何操作）：
- 需求原文（新建任务上传 / 粘贴的需求；需求中心的原文、附件与链接解析完成后）→ 需求文档库
- 审核通过的测试点（连同设计需求页来源）→ 玩法与业务规则库：这是对业务最精炼的描述
- 评审通过 / 人工定稿的用例、离线终稿 → 测试用例库（标记「任务沉淀」，与手动导入的历史用例同库）
- 人工确认后的 AI 需求分析结果 → 规则 / 边界 / 状态 / 权限进玩法与业务规则库，功能点 / 流程 / 前置 / 依赖 / 风险进需求文档库

规则：
- 按来源替换：同一任务 / 需求重复确认只覆盖自己那份，不堆积；任务 / 需求删除时联动清理，恢复时重新沉淀
- 全部后台执行、任何故障只记日志，绝不阻塞确认 / 评审主链路
- 项目设置 knowledge_auto=false 或全局 knowledge_auto_ingest=false 时不沉淀
- 存量项目：管理员可一键「回溯沉淀」，把上线前的历史任务 / 需求按同样规则补进知识库（后台执行、可查进度、可重复执行）
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from loguru import logger

from app.config import get_settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ref_line(point: dict) -> str:
    ref = str(point.get("ref") or "").strip()
    return f"（需求来源：{ref}）" if ref else ""


def render_point_chunks(test_points: list[dict]) -> list[str]:
    """审核通过的测试点按模块渲染为知识切片：一个模块一片（短模块合并由切片器处理）。"""
    chunks: list[str] = []
    for entry in test_points:
        pts = [p for p in entry.get("points") or [] if isinstance(p, dict) and str(p.get("point", "")).strip()]
        if not pts:
            continue
        lines = [f"【模块】{entry.get('module', '')}（已确认的测试点，反映该模块的业务规则与验证要点）"]
        for p in pts:
            dim = f"[{p['dimension']}] " if p.get("dimension") else ""
            lines.append(f"- {dim}{p['point']}{_ref_line(p)}")
        chunks.append("\n".join(lines))
    return chunks


# 已确认的需求分析：哪些分析项进规则库、哪些进需求文档库
ANALYSIS_RULE_KEYS = ("rules", "boundaries", "state_changes", "permissions")
ANALYSIS_DOC_KEYS = ("features", "preconditions", "normal_flows", "exception_flows", "dependencies", "risks")


def render_analysis_chunks(analysis: dict, keys: tuple[str, ...], title: str) -> list[str]:
    """已确认的分析结果按分析项渲染为知识切片：一个分析项一片。"""
    from app.agents.prompts import REQUIREMENT_ANALYSIS_LABELS

    chunks = []
    for key in keys:
        items = [str(x).strip() for x in (analysis.get(key) or []) if str(x).strip()]
        if items:
            chunks.append(f"【需求「{title}」· {REQUIREMENT_ANALYSIS_LABELS[key]}】（AI 分析、人工确认）\n" + "\n".join(f"- {x}" for x in items))
    return chunks


def approved_points(test_points: list[dict]) -> list[dict]:
    """只取审核通过的测试点；没有审核状态的旧任务（确认即通过）全部视为通过。"""
    out = []
    for entry in test_points or []:
        pts = [p for p in entry.get("points") or [] if isinstance(p, dict)]
        keep = [p for p in pts if p.get("status") in (None, "", "approved")]
        if keep:
            out.append({"module": entry.get("module", ""), "points": keep})
    return out


def approved_cases(record) -> list[dict]:
    cases = (record.result or {}).get("cases") or []
    reviews = record.case_reviews or {}
    return [c for c in cases if (reviews.get(str(c.get("uid") or "")) or {}).get("status") == "approved"]


class KnowledgeAccumulator:
    """沉淀编排：判定是否启用、解析模块路径、后台执行并跟踪作业（测试可 drain 等待）。"""

    def __init__(self, app):
        self.app = app
        self.jobs: set[asyncio.Task] = set()
        self._backfill_doc = None
        self.backfills: dict[str, dict] = {}  # project -> 最近一次回溯状态（running / done / failed）

    # ---- 开关与上下文 ----

    def enabled(self, project: str | None) -> bool:
        if not project or not get_settings().knowledge_auto_ingest:
            return False
        p = self.app.state.projects.get(project)
        return bool(p) and bool(p.get("knowledge_auto", True))

    def _service(self):
        from app.api.routes import _knowledge_service

        return _knowledge_service(self.app)

    def module_path(self, module_id: str | None) -> str:
        if not module_id:
            return ""
        try:
            store = self.app.state.modules
            return store.path(module_id) if store.get(module_id) else ""
        except Exception:
            return ""

    def task_scope(self, record) -> tuple[str | None, str]:
        ctx = record.context or {}
        return ctx.get("project"), self.module_path(ctx.get("module_id"))

    # ---- 后台调度 ----

    def schedule(self, label: str, job: Callable[[], Awaitable[None]]) -> None:
        async def _run() -> None:
            try:
                await job()
            except Exception as e:  # 沉淀故障绝不影响主链路
                logger.warning("知识沉淀失败（{}）：{}", label, e)

        try:
            task = asyncio.get_running_loop().create_task(_run())
        except RuntimeError:  # 无事件循环（同步调用场景）：直接放弃，不阻塞
            return
        self.jobs.add(task)
        task.add_done_callback(self.jobs.discard)

    async def drain(self) -> None:
        """等待全部沉淀作业结束（测试与关停用）。"""
        while self.jobs:
            await asyncio.gather(*list(self.jobs), return_exceptions=True)

    # ---- 各类沉淀 ----

    async def ingest_text(self, project: str, module: str, source: str, text: str, category: str,
                          origin: str, origin_ref: str, by: str | None = None):
        if not self.enabled(project) or not (text or "").strip():
            return None
        doc = await self._service().ingest_text(
            text, source=source, category=category, space=project, module=module,
            created_by=by, origin=origin, origin_ref=origin_ref,
        )
        logger.info("知识沉淀：{}「{}」{} 片 → {}（{}）", project, source, doc.chunk_count, category, origin_ref)
        return doc

    async def ingest_requirement(self, record, by: str | None = None):
        """任务的需求原文 → 需求文档库（需求中心发起的任务由需求实体沉淀，这里跳过避免重复）。"""
        ctx = record.context or {}
        if ctx.get("requirement_id"):
            return None
        project, module = self.task_scope(record)
        return await self.ingest_text(project, module, f"任务 {record.task_id} · 需求原文", ctx.get("requirement", ""),
                                      "requirement_docs", "task", record.task_id, by)

    async def ingest_points(self, record, by: str | None = None):
        """审核通过的测试点 → 玩法与业务规则库（整任务按来源替换）。"""
        project, module = self.task_scope(record)
        if not self.enabled(project):
            return None
        chunks = render_point_chunks(approved_points((record.analysis or {}).get("test_points") or []))
        source = f"任务 {record.task_id} · 已确认测试点"
        service = self._service()
        if not chunks:
            service._drop_same_source(project, "business_rules", source)
            service.invalidate_cache()
            return None
        doc = await service.ingest_chunks(chunks, source=source, category="business_rules", space=project,
                                          module=module, created_by=by, origin="task", origin_ref=record.task_id)
        logger.info("知识沉淀：{}「{}」{} 个模块的测试点（{}）", project, source, doc.chunk_count, record.task_id)
        return doc

    async def ingest_cases(self, record, by: str | None = None, cases: list[dict] | None = None,
                           label: str = "评审通过用例"):
        """评审通过的用例（或离线终稿）→ 测试用例库（整任务按来源替换）。"""
        from app.knowledge.importers import render_case_chunk

        project, module = self.task_scope(record)
        if not self.enabled(project):
            return None
        cases = approved_cases(record) if cases is None else cases
        source = f"任务 {record.task_id} · {label}"
        service = self._service()
        if not cases:
            service._drop_same_source(project, "test_cases", source)
            service.invalidate_cache()
            return None
        doc = await service.ingest_chunks([render_case_chunk(c) for c in cases], source=source, category="test_cases",
                                          space=project, module=module, created_by=by, origin="task",
                                          origin_ref=record.task_id)
        logger.info("知识沉淀：{}「{}」{} 条用例（{}）", project, source, doc.chunk_count, record.task_id)
        return doc

    async def ingest_final_cases(self, record, by: str | None = None):
        """离线终稿：重新解析回传时保存的终稿文件（任务目录 final/）→ 测试用例库；文件已不在则跳过。"""
        from app.knowledge.importers import parse_cases_file

        review = record.offline_review or {}
        if not review.get("files"):
            return None
        output_dir = Path(self.app.state.tasks.output_dir)
        paths: list[Path] = [output_dir / rel for rel in (review.get("paths") or [])]
        if not paths:  # 旧记录没存路径：按原文件名在终稿目录里找最近一次落盘的那份
            for name in review["files"]:
                found = sorted((output_dir / record.task_id / "final" / "uploads").glob(f"*_{name}"),
                               key=lambda x: x.stat().st_mtime)
                if found:
                    paths.append(found[-1])
        cases: list[dict] = []
        for path in paths:
            if path.is_file():
                try:
                    cases.extend(await asyncio.to_thread(parse_cases_file, path))
                except Exception as e:
                    logger.warning("知识沉淀：任务 {} 终稿文件 {} 解析失败，跳过：{}", record.task_id, path.name, e)
        if not cases:
            return None
        return await self.ingest_cases(record, by, cases=cases, label="离线终稿")

    async def ingest_requirement_entity(self, item: dict, by: str | None = None) -> None:
        """需求中心的需求实体：原文 + 已解析附件 / 链接逐个入需求文档库。"""
        project = item.get("project")
        if not self.enabled(project) or item.get("deleted_at"):
            return
        module = self.module_path(item.get("module_id"))
        rid = item["req_id"]
        title = item.get("title") or rid
        if (item.get("raw_text") or "").strip():
            await self.ingest_text(project, module, f"需求 {title} · 原文", item["raw_text"], "requirement_docs",
                                   "requirement", rid, by)
        for att in item.get("attachments") or []:
            if att.get("parsed") and (att.get("text") or "").strip():
                name = att.get("source_url") or att.get("filename") or att.get("att_id")
                await self.ingest_text(project, module, f"需求 {title} · {name}", att["text"], "requirement_docs",
                                       "requirement", rid, by)
        await self.ingest_requirement_analysis(item, by)

    def _analysis_sources(self, item: dict) -> dict[str, str]:
        title = item.get("title") or item["req_id"]
        return {"business_rules": f"需求 {title} · 已确认分析（规则 / 边界 / 状态 / 权限）",
                "requirement_docs": f"需求 {title} · 已确认分析（功能 / 流程 / 依赖 / 风险）"}

    async def ingest_requirement_analysis(self, item: dict, by: str | None = None) -> int:
        """人工确认后的 AI 需求分析结果 → 知识库（以确认时的快照为准，按来源替换）；未确认则清掉旧沉淀。返回入库文档数。"""
        project = item.get("project")
        if not self.enabled(project) or item.get("deleted_at"):
            return 0
        confirmed = item.get("analysis_confirmed") or {}
        analysis = confirmed.get("analysis") or {}
        module = self.module_path(item.get("module_id"))
        title = item.get("title") or item["req_id"]
        service = self._service()
        n = 0
        for category, keys in (("business_rules", ANALYSIS_RULE_KEYS), ("requirement_docs", ANALYSIS_DOC_KEYS)):
            source = self._analysis_sources(item)[category]
            chunks = render_analysis_chunks(analysis, keys, title) if confirmed else []
            if not chunks:
                service._drop_same_source(project, category, source)
                service.invalidate_cache()
                continue
            doc = await service.ingest_chunks(chunks, source=source, category=category, space=project, module=module,
                                              created_by=by, origin="requirement", origin_ref=item["req_id"])
            logger.info("知识沉淀：{}「{}」{} 片（{}）", project, source, doc.chunk_count, item["req_id"])
            n += 1
        return n

    def forget(self, origin_ref: str) -> None:
        try:
            n = self._service().forget_origin(origin_ref)
            if n:
                logger.info("知识沉淀清理：{} 的 {} 篇自动沉淀文档已删除", origin_ref, n)
        except Exception as e:
            logger.warning("知识沉淀清理失败（{}）：{}", origin_ref, e)

    async def reingest_task(self, record, by: str | None = None) -> int:
        """任务恢复 / 回溯：需求原文、测试点、评审通过用例、离线终稿全部重新沉淀；返回入库文档数。"""
        docs = [await self.ingest_requirement(record, by), await self.ingest_points(record, by),
                await self.ingest_cases(record, by), await self.ingest_final_cases(record, by)]
        return sum(1 for d in docs if d is not None)

    # ---- 存量回溯 ----

    def _backfill_store(self):
        if self._backfill_doc is None:
            from app.db import DocStore

            self._backfill_doc = DocStore("knowledge_backfills")
            try:
                self.backfills = {k: v for k, v in self._backfill_doc.load_all().items()}
                for v in self.backfills.values():  # 上次进程中断留下的「进行中」视为失败
                    if v.get("status") == "running":
                        v.update(status="failed", error="服务重启，回溯中断，请重新执行", finished_at=v.get("finished_at") or _now())
            except Exception as e:
                logger.warning("读取回溯记录失败：{}", e)
        return self._backfill_doc

    def backfill_status(self, project: str) -> dict | None:
        self._backfill_store()
        return self.backfills.get(project)

    def backfill_running(self, project: str) -> bool:
        st = self.backfill_status(project)
        return bool(st and st.get("status") == "running")

    def _save_backfill(self, project: str) -> None:
        try:
            self._backfill_store().put(project, self.backfills[project])
        except Exception as e:
            logger.warning("保存回溯记录失败：{}", e)

    def backfill_targets(self, project: str) -> dict:
        """回溯范围预览：未删除且已出结果的任务数、未删除的需求数。"""
        tasks = [r for r in self.app.state.tasks.list(limit=100000, project=project)
                 if r.status not in ("queued", "running", "failed")]
        reqs = self.app.state.requirements.list(project=project)
        return {"tasks": len(tasks), "requirements": len(reqs)}

    async def backfill_project(self, project: str, by: str | None = None) -> dict:
        """回溯沉淀一个项目：按任务创建顺序逐个重新沉淀（按来源替换，可重复执行），再沉淀需求实体。
        进度写入 backfills[project]，任何单个任务失败只计数不中断。"""
        self._backfill_store()
        tasks = sorted([r for r in self.app.state.tasks.list(limit=100000, project=project)
                        if r.status not in ("queued", "running", "failed")], key=lambda r: r.created_at)
        reqs = self.app.state.requirements.list(project=project)
        st = self.backfills[project] = {
            "project": project, "status": "running", "by": by, "started_at": _now(), "finished_at": None,
            "total": len(tasks) + len(reqs), "done": 0, "tasks": len(tasks), "requirements": len(reqs),
            "docs": 0, "errors": 0, "error": None, "current": "",
        }
        self._save_backfill(project)
        try:
            for r in tasks:
                st["current"] = f"任务 {r.task_id}"
                try:
                    st["docs"] += await self.reingest_task(r, by)
                except Exception as e:
                    st["errors"] += 1
                    logger.warning("回溯沉淀：任务 {} 失败：{}", r.task_id, e)
                st["done"] += 1
            for item in reqs:
                st["current"] = f"需求 {item.get('title') or item['req_id']}"
                try:
                    before = len(self._service().store.list_docs(project))
                    await self.ingest_requirement_entity(item, by)
                    st["docs"] += max(0, len(self._service().store.list_docs(project)) - before)
                except Exception as e:
                    st["errors"] += 1
                    logger.warning("回溯沉淀：需求 {} 失败：{}", item["req_id"], e)
                st["done"] += 1
            st["status"] = "done"
        except Exception as e:
            st["status"] = "failed"
            st["error"] = str(e)
            logger.warning("回溯沉淀：项目 {} 中断：{}", project, e)
        finally:
            st["current"] = ""
            st["finished_at"] = _now()
            self._save_backfill(project)
        logger.info("回溯沉淀完成：{} 任务 {} / 需求 {} → 入库 {} 篇，失败 {}", project, st["tasks"], st["requirements"],
                    st["docs"], st["errors"])
        return st

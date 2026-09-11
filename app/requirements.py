"""需求中心（完整需求 5 章 / 21 章）：需求实体、原始需求分层保护、AI 分析结果与待确认事项、追溯。

- 需求实体：挂项目 / 版本 / 模块；标题必填；原文永久保留（raw_text，任何 AI 结果不得覆盖）；
  解析结果（attachments[].text）、AI 分析结果（analysis）、人工补充（description）各层独立保存；
- 待确认事项（5.5）：AI 分析产出的 open_questions 逐条确认后才允许发起测试设计（核心规则 4）；
- 追溯（21 章）：需求 → 任务（测试点/用例）→ 计划 → 执行，由 routes 侧按任务上下文聚合；
- 存量迁移：项目化之前的任务按「任务 = 一条需求」自动建需求并回填 task.context.requirement_id。
"""

from __future__ import annotations

import re

import uuid
from datetime import datetime, timezone

REQ_STATUSES = {
    "draft": "草稿",              # 已录入，尚未 AI 分析
    "analyzed": "已分析",         # AI 分析完成，无待确认事项
    "pending_confirm": "待确认",  # 有未确认的待确认事项
    "confirmed": "已确认",        # 待确认事项全部确认，可发起测试设计
    "designing": "设计中",        # 已发起测试设计任务
    "done": "已完成",             # 人工标记完成
    "archived": "已归档",
}
SOURCE_TYPES = {"manual": "手工录入", "file": "文件上传", "mixed": "文本+文件", "migrated": "历史任务迁移"}


class RequirementError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hex8() -> str:
    return uuid.uuid4().hex[:8]


class RequirementStore:
    def __init__(self):
        from app.db import DocStore

        self._doc = DocStore("requirements")
        self._items: dict[str, dict] = self._doc.load_all()
        # 后台解析中的附件遇服务重启会永远停在「解析中」：启动时标为失败并给出可重解析的提示
        for item in self._items.values():
            dirty = False
            for att in item.get("attachments") or []:
                if att.get("parsing"):
                    att.update(parsing=False, parsed=False, error="服务重启导致解析中断，请点「重新解析」")
                    dirty = True
            if dirty:
                self._persist(item)

    def _persist(self, item: dict) -> None:
        self._doc.put(item["req_id"], item)

    # ---- 查询 ----

    def get(self, req_id: str) -> dict | None:
        return self._items.get(req_id)

    def list(self, project: str | None = None, include_deleted: bool = False) -> list[dict]:
        items = [r for r in self._items.values()
                 if (project is None or r["project"] == project) and (include_deleted or not r.get("deleted_at"))]
        return sorted(items, key=lambda r: r["created_at"], reverse=True)

    def by_task(self, task_id: str) -> dict | None:
        for r in self._items.values():
            if task_id in r.get("tasks", []):
                return r
        return None

    # ---- 维护 ----

    def create(
        self, project: str, title: str, raw_text: str, created_by: str | None = None,
        description: str = "", source_type: str = "manual", attachments: list[dict] | None = None,
        version_id: str | None = None, module_id: str | None = None, created_at: str | None = None,
        has_files: bool = False,
    ) -> dict:
        title = (title or "").strip()
        if not project:
            raise RequirementError("需求必须归属项目")
        if not title:
            raise RequirementError("需求标题不能为空")
        if not (raw_text or "").strip() and not attachments and not has_files:
            raise RequirementError("需求原文不能为空（粘贴文本或上传文件）")
        item = {
            "req_id": _hex8(), "project": project, "title": title,
            "version_id": version_id or None, "module_id": module_id or None,
            "raw_text": raw_text or "", "description": (description or "").strip(),
            "source_type": source_type if source_type in SOURCE_TYPES else "manual",
            "attachments": attachments or [],
            "analysis": None, "analysis_meta": None, "questions": [],
            "status": "draft", "tasks": [],
            "created_by": created_by, "created_at": created_at or _now(),
            "updated_by": created_by, "updated_at": _now(), "deleted_at": None, "deleted_by": None,
        }
        self._items[item["req_id"]] = item
        self._persist(item)
        return item

    def update(self, req_id: str, operator: str | None = None, **fields) -> dict:
        item = self._get_alive(req_id)
        for key, value in fields.items():
            if value is None:
                continue
            if key == "title":
                value = value.strip()
                if not value:
                    raise RequirementError("需求标题不能为空")
            if key == "status" and value not in REQ_STATUSES:
                raise RequirementError(f"未知需求状态: {value}")
            if key not in ("title", "description", "version_id", "module_id", "status"):
                raise RequirementError(f"不允许修改字段: {key}")
            item[key] = value.strip() if isinstance(value, str) and key != "status" else value
        item["updated_by"], item["updated_at"] = operator, _now()
        self._persist(item)
        return item

    def add_attachments(self, req_id: str, attachments: list[dict], operator: str | None = None) -> dict:
        item = self._get_alive(req_id)
        item["attachments"].extend(attachments)
        if item["source_type"] == "manual":
            item["source_type"] = "mixed" if item["raw_text"].strip() else "file"
        item["updated_by"], item["updated_at"] = operator, _now()
        self._persist(item)
        return item

    def replace_attachment(self, req_id: str, att_id: str, parsed: dict) -> dict:
        item = self._get_alive(req_id)
        for att in item["attachments"]:
            if att["att_id"] == att_id:
                att.update(parsed)
                break
        else:
            raise RequirementError(f"附件不存在: {att_id}")
        self._persist(item)
        return item

    def set_analysis(self, req_id: str, analysis: dict, meta: dict) -> dict:
        """写入 AI 分析结果（不触碰原文）；待确认事项合并：已确认的保留，新问题追加。"""
        item = self._get_alive(req_id)
        item["analysis"] = {k: v for k, v in analysis.items() if k not in ("model_name", "chunks")}
        item["analysis_meta"] = meta
        existing = {q["question"]: q for q in item["questions"]}
        questions = []
        for q in analysis.get("open_questions") or []:
            if q in existing:
                questions.append(existing[q])
            else:
                questions.append({"q_id": _hex8(), "question": q, "status": "open",
                                  "answer": "", "by": None, "at": None, "source": "ai"})
        # 人工补充的问题保留
        questions.extend(q for q in item["questions"] if q.get("source") == "manual" and q["question"] not in
                         {x["question"] for x in questions})
        item["questions"] = questions
        self._refresh_status(item)
        self._persist(item)
        return item

    def add_question(self, req_id: str, question: str, operator: str | None = None) -> dict:
        item = self._get_alive(req_id)
        question = (question or "").strip()
        if not question:
            raise RequirementError("待确认事项不能为空")
        item["questions"].append({"q_id": _hex8(), "question": question, "status": "open",
                                  "answer": "", "by": operator, "at": _now(), "source": "manual"})
        self._refresh_status(item)
        self._persist(item)
        return item

    def answer_question(self, req_id: str, q_id: str, answer: str, operator: str | None = None,
                        reopen: bool = False) -> dict:
        item = self._get_alive(req_id)
        for q in item["questions"]:
            if q["q_id"] == q_id:
                if reopen:
                    q.update({"status": "open"})
                else:
                    if not (answer or "").strip():
                        raise RequirementError("请填写确认结论")
                    q.update({"status": "confirmed", "answer": answer.strip(), "by": operator, "at": _now()})
                break
        else:
            raise RequirementError(f"待确认事项不存在: {q_id}")
        self._refresh_status(item)
        self._persist(item)
        return item

    def open_questions(self, item: dict) -> list[dict]:
        return [q for q in item.get("questions", []) if q["status"] == "open"]

    def link_task(self, req_id: str, task_id: str) -> dict:
        item = self._get_alive(req_id)
        if task_id not in item["tasks"]:
            item["tasks"].append(task_id)
        if item["status"] in ("draft", "analyzed", "pending_confirm", "confirmed"):
            item["status"] = "designing"
        item["updated_at"] = _now()
        self._persist(item)
        return item

    def delete(self, req_id: str, operator: str | None = None) -> dict:
        item = self._get_alive(req_id)
        item["deleted_at"], item["deleted_by"] = _now(), operator
        self._persist(item)
        return item

    def merge(self, src_id: str, into_id: str, operator: str | None = None, tasks=None) -> dict:
        """合并需求（重复导入治理）：src 的关联任务 / 附件 / 待确认事项并入 into，src 逻辑删除并标记 merged_into。
        原文不合并也不丢失：src 记录仍在回收站，可查看；into 的原文保持不变。"""
        src, into = self._get_alive(src_id), self._get_alive(into_id)
        if src_id == into_id:
            raise RequirementError("不能合并到自身")
        if src["project"] != into["project"]:
            raise RequirementError("只能合并同一项目内的需求")
        for tid in src.get("tasks") or []:
            if tid not in into["tasks"]:
                into["tasks"].append(tid)
            rec = tasks.get(tid) if tasks is not None else None
            if rec is not None:
                rec.context = {**(rec.context or {}), "requirement_id": into_id, "requirement_title": into["title"]}
                tasks.save(rec)
        into["attachments"].extend(src.get("attachments") or [])
        seen = {q["question"] for q in into.get("questions") or []}
        for q in src.get("questions") or []:
            if q["question"] not in seen:
                into.setdefault("questions", []).append(q)
                seen.add(q["question"])
        if (src.get("description") or "").strip() and src["description"].strip() not in (into.get("description") or ""):
            into["description"] = ((into.get("description") or "").rstrip() + "\n\n【合并自 " + src["title"] + "】\n" + src["description"].strip()).strip()
        into.setdefault("merged_from", []).append({"req_id": src_id, "title": src["title"], "by": operator, "at": _now()})
        if into["status"] == "draft" and into["tasks"]:
            into["status"] = "designing"
        into["updated_by"], into["updated_at"] = operator, _now()
        src["tasks"], src["attachments"] = [], []
        src["merged_into"] = into_id
        src["deleted_at"], src["deleted_by"] = _now(), operator
        self._persist(into)
        self._persist(src)
        return into

    def restore(self, req_id: str) -> dict:
        item = self._items.get(req_id)
        if item is None or not item.get("deleted_at"):
            raise RequirementError(f"回收站中无此需求: {req_id}")
        item["deleted_at"], item["deleted_by"] = None, None
        self._persist(item)
        return item

    def rename_project(self, old: str, new: str) -> None:
        for r in self._items.values():
            if r["project"] == old:
                r["project"] = new
                self._persist(r)

    def purge_project(self, project: str) -> int:
        """清理项目下已逻辑删除的需求（删除空项目时调用）。"""
        import shutil

        from app.config import get_settings

        gone = [k for k, r in self._items.items() if r["project"] == project and r.get("deleted_at")]
        for k in gone:
            self._items.pop(k)
            self._doc.remove(k)
            shutil.rmtree(get_settings().outputs_dir / "requirements" / k, ignore_errors=True)
        return len(gone)

    # ---- 内部 ----

    def _get_alive(self, req_id: str) -> dict:
        item = self._items.get(req_id)
        if item is None or item.get("deleted_at"):
            raise RequirementError(f"需求不存在: {req_id}")
        return item

    def _refresh_status(self, item: dict) -> None:
        """分析/确认阶段的状态派生；设计中/已完成/已归档不回退。"""
        if item["status"] in ("designing", "done", "archived"):
            return
        if item["analysis"] is None:
            item["status"] = "draft"
        elif self.open_questions(item):
            item["status"] = "pending_confirm"
        else:
            item["status"] = "confirmed" if item["questions"] else "analyzed"


def design_brief(item: dict) -> str:
    """发起测试设计时喂给拆解/生成 Agent 的需求文本：原文 + 人工补充 + 已确认结论（原文不改）。"""
    from app.agents.prompts import REQUIREMENT_ANALYSIS_LABELS

    parts = [item["raw_text"].strip()]
    for att in item.get("attachments", []):
        if att.get("text"):
            parts.append(f"【文件：{att['filename']}】\n{att['text']}")
    if item.get("description", "").strip():
        parts.append(f"【人工补充说明】\n{item['description'].strip()}")
    confirmed = [q for q in item.get("questions", []) if q["status"] == "confirmed" and q.get("answer")]
    if confirmed:
        parts.append("【待确认事项的确认结论（以此为准）】\n" + "\n".join(
            f"- 问：{q['question']}\n  确认：{q['answer']}" for q in confirmed))
    analysis = item.get("analysis") or {}
    keep = [k for k in ("rules", "boundaries", "state_changes", "permissions") if analysis.get(k)]
    if keep:
        parts.append("【AI 需求分析要点（已经人工过目）】\n" + "\n".join(
            f"{REQUIREMENT_ANALYSIS_LABELS[k]}：" + "；".join(analysis[k]) for k in keep))
    return "\n\n".join(p for p in parts if p)


_FILENAME_RE = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|txt|md|markdown|png|jpe?g|xmind|csv)$", re.I)
_HEADING_RE = re.compile(r"^\s*#{1,3}\s*(.+?)\s*$")


def derive_title(raw_text: str, fallback: str) -> str:
    """从需求原文推导标题：优先 Markdown 一级/二级标题，其次第一行有意义的正文；都没有则用 fallback（文件名）。"""
    for line in (raw_text or "").splitlines()[:40]:
        line = line.strip()
        if not line or line.startswith("【文件："):
            continue
        m = _HEADING_RE.match(line)
        if m:
            heading = m.group(1).lstrip("# ").strip()
            if 2 <= len(heading) <= 60:
                return heading
    if fallback and _FILENAME_RE.search(fallback):
        return fallback  # 有文件名且原文无标题：保留文件名
    for line in (raw_text or "").splitlines()[:10]:
        line = line.strip()
        if line and not line.startswith("【文件：") and not line.startswith("#"):
            return line[:60]
    return fallback


def repair_migrated_titles(requirements: RequirementStore, tasks=None) -> int:
    """存量修复（幂等）：迁移来的需求标题若仍是文件名，改为正文标题；关联任务的 requirement_title 一并更新。"""
    fixed = 0
    for item in requirements.list(include_deleted=True):
        title_now = item.get("title") or ""
        if item.get("source_type") != "migrated" or not (_FILENAME_RE.search(title_now) or title_now.startswith("#")):
            continue
        title = derive_title(item.get("raw_text") or "", item["title"])
        if title == item["title"]:
            continue
        item["title"] = title
        requirements._persist(item)
        if tasks is not None:
            for tid in item.get("tasks") or []:
                rec = tasks.get(tid)
                if rec is not None and (rec.context or {}).get("requirement_id") == item["req_id"]:
                    rec.context = {**(rec.context or {}), "requirement_title": title}
                    tasks.save(rec)
        fixed += 1
    return fixed


def migrate_task(record, requirements: RequirementStore, tasks=None) -> bool:
    """单个历史任务 → 需求实体（「一个任务 = 一条需求」）并回填关联；已迁移 / 无项目 / 无需求文本则跳过。"""
    ctx = record.context or {}
    if ctx.get("requirement_id") or not ctx.get("project") or not (ctx.get("requirement") or "").strip():
        return False
    filename = next((s for s in record.sources if s and s != "text"), "")
    title = derive_title(ctx["requirement"], filename or record.task_id)
    item = requirements.create(
        ctx["project"], title, ctx["requirement"], created_by=record.created_by,
        source_type="migrated", created_at=record.created_at,
    )
    item["status"] = "designing"
    item["tasks"] = [record.task_id]
    requirements._persist(item)
    ctx["requirement_id"] = item["req_id"]
    ctx["requirement_title"] = title
    record.context = ctx
    if tasks is not None:
        tasks.save(record)
    return True


def migrate_tasks(tasks, requirements: RequirementStore) -> int:
    """存量迁移（M2）：项目化之前的任务按「一个任务 = 一条需求」建需求实体并回填关联，幂等。

    无项目的遗留任务不迁移（仅系统管理员可见，待通过「归属项目」接口指定项目后再迁）。
    """
    return sum(1 for record in tasks.list(limit=1000000) if migrate_task(record, requirements, tasks))

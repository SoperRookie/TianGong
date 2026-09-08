"""需求中心（完整需求 5 章 / 21 章）：需求实体、原始需求分层保护、AI 分析结果与待确认事项、追溯。

- 需求实体：挂项目 / 版本 / 模块；标题必填；原文永久保留（raw_text，任何 AI 结果不得覆盖）；
  解析结果（attachments[].text）、AI 分析结果（analysis）、人工补充（description）各层独立保存；
- 待确认事项（5.5）：AI 分析产出的 open_questions 逐条确认后才允许发起测试设计（核心规则 4）；
- 追溯（21 章）：需求 → 任务（测试点/用例）→ 计划 → 执行，由 routes 侧按任务上下文聚合；
- 存量迁移：项目化之前的任务按「任务 = 一条需求」自动建需求并回填 task.context.requirement_id。
"""

from __future__ import annotations

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


def migrate_tasks(tasks, requirements: RequirementStore) -> int:
    """存量迁移（M2）：项目化之前的任务按「一个任务 = 一条需求」建需求实体并回填关联，幂等。

    无项目的遗留任务不迁移（仅系统管理员可见，待归属项目后再迁）。
    """
    migrated = 0
    for record in tasks.list(limit=1000000):
        ctx = record.context or {}
        if ctx.get("requirement_id") or not ctx.get("project") or not (ctx.get("requirement") or "").strip():
            continue
        title = next((s for s in record.sources if s and s != "text"), "") or \
            (ctx["requirement"].strip().splitlines()[0][:60] if ctx["requirement"].strip() else record.task_id)
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
        tasks.save(record)
        migrated += 1
    return migrated

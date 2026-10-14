"""项目画像：一份结构化的项目业务认知（背景 / 角色 / 核心流程 / 术语 / 关键规则 / 易错点）。

- 来源：AI 从项目已沉淀的需求原文、已确认测试点、评审通过用例、待确认事项的确认结论中提炼为草稿，
  管理员确认后才生效（可在确认前逐项修改）；也可完全手工维护。
- 用途：生效版本注入拆解 / 生成 / 查漏 / 评审 / 需求分析全链路（独立预算，不占知识库配额），
  让每次生成都带着对这个业务的既有认知，而不是从零理解。
- 自动刷新：项目每新增若干任务自动生成一版新草稿（增量更新，已确认内容只增补不推翻），等待确认。
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.db import DocStore

SECTIONS: dict[str, str] = {
    "background": "业务背景",
    "roles": "角色与权限",
    "flows": "核心业务流程",
    "glossary": "术语表",
    "rules": "关键业务规则",
    "risks": "易错点与历史问题",
}
_LIST_SECTIONS = ("roles", "flows", "rules", "risks")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_profile(raw: dict | None) -> dict:
    """把 AI 输出 / 人工编辑的画像规整为固定结构：background 为文本，其余为字符串列表，glossary 为 {term, meaning}。"""
    raw = raw if isinstance(raw, dict) else {}
    out: dict = {"background": str(raw.get("background") or "").strip()}
    for key in _LIST_SECTIONS:
        items = raw.get(key) or []
        if isinstance(items, str):
            items = [ln.strip(" -•") for ln in items.splitlines()]
        out[key] = list(dict.fromkeys(str(x).strip() for x in items if str(x).strip()))[:60]
    glossary = []
    seen = set()
    raw_gl = raw.get("glossary") or []
    if isinstance(raw_gl, dict):
        raw_gl = [{"term": k, "meaning": v} for k, v in raw_gl.items()]
    if isinstance(raw_gl, str):
        raw_gl = [ln for ln in raw_gl.splitlines() if ln.strip()]
    for g in raw_gl:
        if isinstance(g, str):
            term, _, meaning = g.replace("：", ":").partition(":")
            g = {"term": term, "meaning": meaning}
        if not isinstance(g, dict):
            continue
        term = str(g.get("term") or "").strip()
        meaning = str(g.get("meaning") or "").strip()
        if term and term not in seen:
            seen.add(term)
            glossary.append({"term": term, "meaning": meaning})
    out["glossary"] = glossary[:120]
    return out


def is_empty_profile(profile: dict | None) -> bool:
    if not profile:
        return True
    return not (profile.get("background") or any(profile.get(k) for k in (*_LIST_SECTIONS, "glossary")))


def render_profile(profile: dict | None, budget_chars: int = 2500, description: str = "") -> str:
    """渲染为注入 Prompt 的文本（按预算截断：背景 → 术语 → 规则 → 流程 → 角色 → 易错点 的优先级）。"""
    profile = normalize_profile(profile)
    parts: list[str] = []
    if description.strip():
        parts.append(f"项目描述：{description.strip()}")
    if profile["background"]:
        parts.append(f"业务背景：{profile['background']}")
    if profile["glossary"]:
        parts.append("术语表：\n" + "\n".join(f"- {g['term']}：{g['meaning']}" if g["meaning"] else f"- {g['term']}"
                                            for g in profile["glossary"]))
    for key in ("rules", "flows", "roles", "risks"):
        if profile[key]:
            parts.append(f"{SECTIONS[key]}：\n" + "\n".join(f"- {x}" for x in profile[key]))
    text = "\n\n".join(parts)
    if len(text) > budget_chars:
        text = text[: budget_chars - 1].rstrip() + "…"
    return text


class ProfileStore:
    """每个项目一条记录：生效版本 + 待确认草稿 + 版本历史。"""

    def __init__(self):
        self._doc = DocStore("project_profiles")
        self._items: dict[str, dict] = {k: v for k, v in self._doc.load_all().items()}

    def get(self, project: str) -> dict | None:
        return self._items.get(project)

    def _ensure(self, project: str) -> dict:
        item = self._items.get(project)
        if item is None:
            item = self._items[project] = {
                "project": project, "active": None, "version": 0, "confirmed_by": None, "confirmed_at": None,
                "draft": None, "draft_meta": None, "extracted_task_count": 0, "history": [],
            }
        return item

    def _save(self, project: str) -> None:
        self._doc.put(project, self._items[project])

    def save_draft(self, project: str, draft: dict, meta: dict) -> dict:
        item = self._ensure(project)
        item["draft"] = normalize_profile(draft)
        item["draft_meta"] = {**meta, "at": _now()}
        if "task_count" in meta:
            item["extracted_task_count"] = int(meta["task_count"])
        self._save(project)
        return item

    def discard_draft(self, project: str) -> dict:
        item = self._ensure(project)
        item["draft"] = None
        item["draft_meta"] = None
        self._save(project)
        return item

    def confirm(self, project: str, profile: dict | None, by: str | None, source: str = "draft") -> dict:
        """确认生效：profile 为空则直接采用当前草稿；传入则以传入（管理员编辑后的）内容为准。"""
        item = self._ensure(project)
        final = normalize_profile(profile if profile is not None else item.get("draft"))
        item["active"] = final
        item["version"] = int(item.get("version") or 0) + 1
        item["confirmed_by"] = by
        item["confirmed_at"] = _now()
        item["draft"] = None
        item["draft_meta"] = None
        item["history"] = ([*item.get("history", []), {"version": item["version"], "by": by, "at": item["confirmed_at"],
                                                        "source": source}])[-20:]
        self._save(project)
        return item

    def mark_extracted(self, project: str, task_count: int) -> None:
        item = self._ensure(project)
        item["extracted_task_count"] = int(task_count)
        self._save(project)

    def render(self, project: str | None, budget_chars: int, description: str = "") -> str | None:
        if not project:
            return None
        item = self._items.get(project)
        active = item.get("active") if item else None
        if is_empty_profile(active) and not description.strip():
            return None
        return render_profile(active, budget_chars, description=description) or None

    def rename_project(self, old: str, new: str) -> None:
        item = self._items.pop(old, None)
        if item is None:
            return
        item["project"] = new
        self._items[new] = item
        self._doc.remove(old)
        self._save(new)

    def drop_project(self, project: str) -> None:
        if self._items.pop(project, None) is not None:
            self._doc.remove(project)


# ---- 提炼素材：从项目已有产物里收集（新近优先、按预算截断）----


def gather_material(app, project: str, budget_chars: int) -> tuple[str, dict]:
    """收集画像提炼素材：项目描述与模块树、需求（原文 / 附件 / 确认结论 / AI 分析要点）、
    已确认测试点、评审通过用例标题。返回 (素材文本, 统计)。"""
    from app.knowledge.accumulate import approved_cases, approved_points
    from app.requirements import design_brief

    parts: list[str] = []
    stats = {"requirements": 0, "tasks": 0, "points": 0, "cases": 0, "task_count": 0}
    used = 0

    def add(text: str) -> bool:
        nonlocal used
        text = (text or "").strip()
        if not text:
            return True
        room = budget_chars - used
        if room <= 200:
            return False
        if len(text) > room:
            text = text[: room - 1].rstrip() + "…"
        parts.append(text)
        used += len(text) + 2
        return True

    p = app.state.projects.get(project) or {}
    head = [f"项目：{project}"]
    if p.get("business_line"):
        head.append(f"业务线：{p['business_line']}")
    if p.get("description"):
        head.append(f"项目描述：{p['description']}")
    try:
        mods = [app.state.modules.path(m["module_id"]) for m in app.state.modules.list(project)]
        if mods:
            head.append("模块树：" + "、".join(m for m in mods if m))
    except Exception:
        pass
    add("\n".join(head))

    tasks = [r for r in app.state.tasks.list(limit=100000, project=project) if r.status != "failed"]
    stats["task_count"] = len(tasks)
    seen_req: set[str] = set()
    # 需求实体（新近优先）：原文 + 附件 + 确认结论 + AI 分析要点
    for item in sorted(app.state.requirements.list(project=project), key=lambda x: x.get("updated_at") or "", reverse=True):
        seen_req.add(item["req_id"])
        stats["requirements"] += 1
        if not add(f"【需求：{item.get('title') or item['req_id']}】\n{design_brief(item)}"):
            break
    # 任务：未挂需求实体的任务的需求原文（摘要）+ 已确认测试点 + 评审通过用例标题
    for r in tasks:
        ctx = r.context or {}
        stats["tasks"] += 1
        block = []
        if not ctx.get("requirement_id") and (ctx.get("requirement") or "").strip():
            block.append(f"【任务 {r.task_id} 需求原文（节选）】\n{ctx['requirement'][:3000]}")
        # 拆解确认任务取审核通过的点；直接生成的任务没有审核环节，拆解结果（已用于生成）全部可用
        pts = approved_points((r.analysis or {}).get("test_points") or (r.result or {}).get("test_points") or [])
        if pts:
            lines = []
            for entry in pts:
                for pt in entry["points"]:
                    stats["points"] += 1
                    ref = f"（{pt['ref']}）" if pt.get("ref") else ""
                    lines.append(f"- {entry['module']}：{pt.get('point', '')}{ref}")
            block.append(f"【任务 {r.task_id} 已确认测试点】\n" + "\n".join(lines[:200]))
        cases = approved_cases(r)
        if cases:
            stats["cases"] += len(cases)
            block.append(f"【任务 {r.task_id} 评审通过用例】\n" + "\n".join(
                f"- {c.get('module', '')} / {c.get('title', '')}" for c in cases[:150]))
        if block and not add("\n".join(block)):
            break
    return "\n\n".join(parts), stats


def should_auto_extract(app, project: str | None) -> bool:
    """自动提炼时机：项目开启自动沉淀、没有待确认草稿、且自上次提炼以来新增任务数达到阈值（首次只要有一个任务）。"""
    from app.config import get_settings

    settings = get_settings()
    if not project or not settings.profile_auto_extract:
        return False
    p = app.state.projects.get(project)
    if not p or not p.get("knowledge_auto", True):
        return False
    item = app.state.profiles.get(project) or {}
    if item.get("draft"):
        return False
    count = len([r for r in app.state.tasks.list(limit=100000, project=project) if r.status != "failed"])
    last = int(item.get("extracted_task_count") or 0)
    need = 1 if (last == 0 and is_empty_profile(item.get("active"))) else max(1, settings.profile_refresh_tasks)
    return count - last >= need

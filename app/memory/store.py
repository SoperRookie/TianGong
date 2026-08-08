"""长期记忆（模块八）：用户偏好记忆（F-8-2）与项目记忆（F-8-3）。

- 可见可控（F-8-6）：所有记忆可列出/编辑/删除/清空；任务记录保存本次注入的记忆快照（引用可见）。
- 检索注入（F-8-7）：生成任务时检索 当前项目记忆 + 用户偏好 注入生成 Agent，
  使用独立预算（建议 ≤ 上下文的 5%-10%），不占三大知识库 5:3:2 配额。
- 使用习惯沉淀（F-8-2 的确定性部分）：常用模板/常用模型按使用频次自动固化为偏好
  （作为界面默认值，不注入 Prompt）；跨任务重复出现的修订指令固化为偏好并注入
  生成 Prompt（后续任务主动满足）。基于评审留痕的修改习惯学习（F-8-4）与
  LLM 自动提炼（F-8-5）属后续里程碑，不在此实现。

当前为单用户部署，"用户"维度即本机用户；多用户/项目权限隔离（F-8-8）随账号体系接入。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

SCOPES = ("user", "project")

_SCOPE_LABELS = {"user": "用户偏好", "project": "项目记忆"}
_USAGE_LABELS = {"template": "模板", "model": "模型"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MemoryEntry(BaseModel):
    memory_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    scope: str  # user / project
    project: str | None = None  # project 维度必填
    content: str
    source: str = "manual"  # manual：用户手工维护；usage：使用习惯自动沉淀
    kind: str | None = Field(default=None, description="usage 型记忆的去重键（同键 upsert）")
    inject: bool = Field(default=True, description="是否注入生成 Prompt（模板/模型偏好仅作界面默认值）")
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    hits: int = Field(default=0, description="被检索注入次数（时效管理 F-8-9 的数据基础）")


class MemoryStore:
    def __init__(self, storage_path: Path, pref_threshold: int = 3, revision_threshold: int = 2):
        """pref_threshold：模板/模型使用满 N 次固化为默认偏好；
        revision_threshold：修订指令跨任务重复 N 次固化并注入 Prompt。
        阈值经 TIANGONG_MEMORY_PREF_THRESHOLD / TIANGONG_MEMORY_REVISION_THRESHOLD 配置。"""
        self._path = storage_path
        self._thresholds = {
            "template": max(1, pref_threshold),
            "model": max(1, pref_threshold),
            "revision": max(1, revision_threshold),
        }
        self._entries: dict[str, MemoryEntry] = {}
        self._usage: dict[str, dict[str, int]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        for item in raw.get("memories", []):
            entry = MemoryEntry.model_validate(item)
            self._entries[entry.memory_id] = entry
        self._usage = raw.get("usage", {})

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"memories": [e.model_dump() for e in self._entries.values()], "usage": self._usage}
        self._path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    # ---- 可见可控（F-8-6）----

    def add(
        self,
        content: str,
        scope: str = "user",
        project: str | None = None,
        source: str = "manual",
        kind: str | None = None,
        inject: bool = True,
    ) -> MemoryEntry:
        if scope not in SCOPES:
            raise ValueError(f"未知记忆维度: {scope}（可用 user/project）")
        if scope == "project" and not (project or "").strip():
            raise ValueError("项目记忆必须指定项目名")
        content = content.strip()
        if not content:
            raise ValueError("记忆内容不能为空")
        project = project.strip() if scope == "project" else None
        if kind:  # usage 型记忆按 kind 去重更新，避免同一习惯堆积多条
            for entry in self._entries.values():
                if entry.kind == kind and entry.scope == scope and entry.project == project:
                    entry.content, entry.updated_at = content, _now()
                    self._persist()
                    return entry
        entry = MemoryEntry(
            scope=scope, project=project, content=content, source=source, kind=kind, inject=inject
        )
        self._entries[entry.memory_id] = entry
        self._persist()
        return entry

    def list(self, scope: str | None = None, project: str | None = None) -> list[MemoryEntry]:
        entries = [
            e
            for e in self._entries.values()
            if (scope is None or e.scope == scope) and (project is None or e.project == project)
        ]
        return sorted(entries, key=lambda e: e.updated_at, reverse=True)

    def get(self, memory_id: str) -> MemoryEntry | None:
        return self._entries.get(memory_id)

    def update(self, memory_id: str, content: str) -> MemoryEntry | None:
        """记忆纠错：用户可直接改写记忆内容。"""
        entry = self._entries.get(memory_id)
        if entry is None:
            return None
        content = content.strip()
        if not content:
            raise ValueError("记忆内容不能为空")
        entry.content, entry.updated_at = content, _now()
        self._persist()
        return entry

    def delete(self, memory_id: str) -> bool:
        existed = self._entries.pop(memory_id, None) is not None
        if existed:
            self._persist()
        return existed

    def clear(self, scope: str | None = None, project: str | None = None) -> int:
        """一键清空（可按维度/项目过滤）；清空用户偏好时同步清零使用计数。"""
        victims = [e.memory_id for e in self.list(scope, project)]
        for memory_id in victims:
            self._entries.pop(memory_id, None)
        if scope in (None, "user"):
            self._usage = {}
        if victims or scope in (None, "user"):
            self._persist()
        return len(victims)

    # ---- 使用习惯沉淀（F-8-2 确定性部分）----

    def record_usage(self, kind: str, value: str) -> None:
        """记录一次模板/模型/修订指令的使用，达到阈值自动固化为偏好记忆。"""
        value = (value or "").strip()
        if kind not in self._thresholds or not value:
            return
        key = _normalize(value) if kind == "revision" else value
        counter = self._usage.setdefault(kind, {})
        counter[key] = counter.get(key, 0) + 1
        count = counter[key]
        if count < self._thresholds[kind]:
            self._persist()
            return
        if kind == "revision":
            self.add(
                f"该用户在历史任务中反复提出的修订要求（生成时请主动满足）：{value}",
                source="usage",
                kind=f"revision:{key}",
            )
        else:
            # 仅最高频者固化为默认值；不注入 Prompt，由界面/接口作为默认选项
            if count == max(counter.values()):
                label = _USAGE_LABELS[kind]
                self.add(
                    f"常用{label}：{value}（累计使用 {count} 次，作为默认{label}）",
                    source="usage",
                    kind=f"pref:{kind}",
                    inject=False,
                )

    def defaults(self) -> dict:
        """按使用频次导出的默认偏好（常用模板/常用模型），供任务创建界面预选。"""
        out: dict = {}
        for kind, field in (("template", "template_id"), ("model", "model")):
            counter = self._usage.get(kind, {})
            if counter:
                top, count = max(counter.items(), key=lambda kv: kv[1])
                if count >= self._thresholds[kind]:
                    out[field] = top
        return out

    # ---- 检索注入（F-8-7）----

    def retrieve(
        self, query: str, project: str | None = None, budget_chars: int = 600
    ) -> tuple[str | None, list[dict]]:
        """检索相关记忆并在独立预算内装填，返回（注入文本, 记忆快照）。

        项目记忆（当前项目）优先于用户偏好；组内按与需求的相关度、更新时间排序。
        命中注入的记忆累计 hits，供时效管理（F-8-9）降权归档使用。
        """
        groups = (
            [e for e in self.list(scope="project", project=project) if e.inject] if project else [],
            [e for e in self.list(scope="user") if e.inject],
        )
        qgrams = _bigrams(query)
        lines: list[str] = []
        snapshot: list[dict] = []
        used = 0
        for group in groups:
            group.sort(key=lambda e: (_relevance(qgrams, e.content), e.updated_at), reverse=True)
            for entry in group:
                line = f"[{_SCOPE_LABELS[entry.scope]}] {entry.content}"
                if used + len(line) > budget_chars:
                    continue
                used += len(line)
                lines.append(line)
                entry.hits += 1
                snapshot.append(
                    {
                        "memory_id": entry.memory_id,
                        "scope": entry.scope,
                        "project": entry.project,
                        "source": entry.source,
                        "content": entry.content,
                        "chars": len(line),
                    }
                )
        if snapshot:
            self._persist()  # hits 计数落盘
        return ("\n".join(lines) if lines else None), snapshot


def _normalize(text: str) -> str:
    """修订指令归一化：去空白与标点后小写，用于跨任务重复识别。"""
    return re.sub(r"[\s\W_]+", "", text).lower()


def _bigrams(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text)
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


def _relevance(query_grams: set[str], content: str) -> float:
    grams = _bigrams(content)
    if not grams:
        return 0.0
    return len(query_grams & grams) / len(grams)

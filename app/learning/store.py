"""学习规则库（需求三十六~三十九）：规则候选 → 人工确认 → 正式规则 → 反哺生成。

- 学习来源：审核通过后的人工修改留痕（在线评审 review_log、离线终稿 diff、修订指令）；
- 不无限自动学习：LLM 只产出候选，须负责人确认后才生效（status: candidate → active）；
- 规则有适用范围（需求三十九）：system（所有项目）/ team / project / module，
  生成时按当前项目过滤注入。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

RULE_SCOPES = ("system", "team", "project", "module")
RULE_STATUSES = ("candidate", "active", "ignored")

_SCOPE_LABELS = {"system": "系统级", "team": "团队级", "project": "项目级", "module": "模块级"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text).lower()


class Rule(BaseModel):
    rule_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    content: str
    scope: str = "project"          # system / team / project / module
    project: str | None = None      # project/module 级必填
    module: str | None = None       # module 级必填
    status: str = "candidate"       # candidate（待确认）/ active（生效）/ ignored（忽略）
    evidence: str = ""              # 依据（需求三十八：如"过去12条中10条人工增加该测试"）
    occurrences: int = 0
    confidence: str = "中"          # 高 / 中 / 低
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class RuleStore:
    def __init__(self, storage_path: Path):
        self._path = storage_path
        self._rules: dict[str, Rule] = {}
        self._load()

    def _load(self) -> None:
        from app.db import DocStore, load_with_migration

        self._doc = DocStore("rules")
        raw = load_with_migration(self._doc, self._path, lambda data: {"doc": data}).get("doc") or {}
        for item in raw.get("rules", []):
            rule = Rule.model_validate(item)
            self._rules[rule.rule_id] = rule

    def _persist(self) -> None:
        self._doc.put("doc", {"rules": [r.model_dump() for r in self._rules.values()]})

    def add_candidates(self, candidates: list[dict], project: str | None = None) -> list[Rule]:
        """入库规则候选，按归一化内容去重（已存在的仅更新依据与次数）。"""
        added: list[Rule] = []
        index = {_normalize(r.content): r for r in self._rules.values()}
        for c in candidates:
            content = str(c.get("content", "")).strip()
            if not content:
                continue
            key = _normalize(content)
            if key in index:
                rule = index[key]
                rule.occurrences = max(rule.occurrences, int(c.get("occurrences", 0) or 0))
                rule.evidence = str(c.get("evidence", rule.evidence))
                rule.updated_at = _now()
                continue
            scope_hint = str(c.get("scope_hint", "project"))
            rule = Rule(
                content=content,
                scope=scope_hint if scope_hint in RULE_SCOPES else "project",
                project=project,
                evidence=str(c.get("evidence", "")),
                occurrences=int(c.get("occurrences", 0) or 0),
                confidence=str(c.get("confidence", "中")),
            )
            self._rules[rule.rule_id] = rule
            index[key] = rule
            added.append(rule)
        if added:
            self._persist()
        return added

    def rename_project(self, old: str, new: str) -> None:
        changed = False
        for r in self._rules.values():
            if r.project == old:
                r.project = new
                changed = True
        if changed:
            self._persist()

    def list(self, status: str | None = None, project: str | None = None) -> list[Rule]:
        rules = [
            r for r in self._rules.values()
            if (status is None or r.status == status)
            and (project is None or r.project in (None, project) or r.scope in ("system", "team"))
        ]
        return sorted(rules, key=lambda r: r.updated_at, reverse=True)

    def get(self, rule_id: str) -> Rule | None:
        return self._rules.get(rule_id)

    def confirm(
        self, rule_id: str, scope: str, project: str | None = None, module: str | None = None
    ) -> Rule:
        """负责人确认候选生效（需求三十八：加入项目规则/团队规则），并定适用范围。"""
        rule = self._rules.get(rule_id)
        if rule is None:
            raise KeyError(f"规则不存在: {rule_id}")
        if scope not in RULE_SCOPES:
            raise ValueError(f"未知规则范围: {scope}（可用 {'/'.join(RULE_SCOPES)}）")
        if scope in ("project", "module") and not (project or "").strip():
            raise ValueError(f"{_SCOPE_LABELS[scope]}规则必须指定项目名")
        if scope == "module" and not (module or "").strip():
            raise ValueError("模块级规则必须指定模块名")
        rule.scope = scope
        rule.project = project.strip() if project else None
        rule.module = module.strip() if module else None
        rule.status = "active"
        rule.updated_at = _now()
        self._persist()
        return rule

    def ignore(self, rule_id: str) -> Rule:
        rule = self._rules.get(rule_id)
        if rule is None:
            raise KeyError(f"规则不存在: {rule_id}")
        rule.status = "ignored"
        rule.updated_at = _now()
        self._persist()
        return rule

    def update(self, rule_id: str, content: str) -> Rule:
        rule = self._rules.get(rule_id)
        if rule is None:
            raise KeyError(f"规则不存在: {rule_id}")
        content = content.strip()
        if not content:
            raise ValueError("规则内容不能为空")
        rule.content = content
        rule.updated_at = _now()
        self._persist()
        return rule

    def delete(self, rule_id: str) -> bool:
        existed = self._rules.pop(rule_id, None) is not None
        if existed:
            self._persist()
        return existed

    def active_rules(self, project: str | None = None) -> list[Rule]:
        """当前生效且适用的规则：system/team 全局适用；project/module 按项目匹配。"""
        result = []
        for rule in self._rules.values():
            if rule.status != "active":
                continue
            if rule.scope in ("system", "team") or (project and rule.project == project):
                result.append(rule)
        return sorted(result, key=lambda r: RULE_SCOPES.index(r.scope))

    def render(self, project: str | None = None, budget_chars: int = 800) -> tuple[str | None, list[dict]]:
        """渲染注入文本（预算内装填）与规则快照（留痕引用可见）。"""
        lines: list[str] = []
        snapshot: list[dict] = []
        used = 0
        for rule in self.active_rules(project):
            label = _SCOPE_LABELS[rule.scope]
            if rule.scope == "module" and rule.module:
                label += f"·{rule.module}"
            line = f"[{label}] {rule.content}"
            if used + len(line) > budget_chars:
                continue
            used += len(line)
            lines.append(line)
            snapshot.append({
                "rule_id": rule.rule_id, "scope": rule.scope, "project": rule.project,
                "module": rule.module, "content": rule.content, "chars": len(line),
            })
        return ("\n".join(lines) if lines else None), snapshot

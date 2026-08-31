"""项目实体存储（项目管理）：名称为自然键（任务上下文以名称引用）。

- 历史兼容：任务里已出现过的项目名在列表时自动注册（ensure）；
- 改名会联动更新所有引用该项目的任务；删除仅允许空项目（有任务的项目不可删）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


class ProjectError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProjectStore:
    def __init__(self, storage_path: Path):
        from app.db import DocStore

        self._path = storage_path  # 旧文件：仅用于首启迁移
        self._doc = DocStore("projects")
        self._projects: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        from app.db import load_with_migration

        self._projects = load_with_migration(
            self._doc, self._path,
            lambda raw: {p["name"]: p for p in raw.get("projects", [])},
        )

    def _persist(self) -> None:
        self._doc.replace_all(self._projects)

    def list(self) -> list[dict]:
        return list(self._projects.values())

    def get(self, name: str) -> dict | None:
        return self._projects.get(name)

    def ensure(self, names: list[str], created_by: str | None = None) -> None:
        """自动注册（历史任务中的项目名 / 接口直传的新名称），已存在的跳过。"""
        added = False
        for name in names:
            name = (name or "").strip()
            if not name or name in self._projects:
                continue
            self._projects[name] = {
                "name": name, "description": "",
                "created_by": created_by, "created_at": _now(),
            }
            added = True
        if added:
            self._persist()

    def create(self, name: str, description: str = "", created_by: str | None = None) -> dict:
        name = (name or "").strip()
        if not name:
            raise ProjectError("项目名不能为空")
        if name in self._projects:
            raise ProjectError(f"项目已存在: {name}")
        self._projects[name] = {
            "name": name, "description": (description or "").strip(),
            "created_by": created_by, "created_at": _now(),
        }
        self._persist()
        return self._projects[name]

    def update(self, name: str, new_name: str | None = None, description: str | None = None) -> dict:
        project = self._projects.get(name)
        if project is None:
            raise ProjectError(f"项目不存在: {name}")
        if new_name is not None and (new_name := new_name.strip()) and new_name != name:
            if new_name in self._projects:
                raise ProjectError(f"项目已存在: {new_name}")
            self._projects.pop(name)
            project["name"] = new_name
            self._projects[new_name] = project
        if description is not None:
            project["description"] = description.strip()
        self._persist()
        return project

    def delete(self, name: str) -> None:
        if name not in self._projects:
            raise ProjectError(f"项目不存在: {name}")
        self._projects.pop(name)
        self._persist()

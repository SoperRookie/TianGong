"""项目实体与项目底座（完整需求 3.2 / 3.4 / 4 章）。

- 项目：名称为自然键（任务/计划上下文以名称引用），字段：编码/描述/负责人/状态
  （进行中 active / 暂停 paused / 已归档 archived）/成员与项目角色/审计字段；
  改名联动更新所有引用（任务、计划、版本、模块、个人偏好）；删除仅允许空项目。
- 成员与角色（3.4）：members = {username: project_role}，系统级角色与项目级角色分离；
- 个人偏好：收藏与最近访问（3.2 收藏 / 最近访问 / 快速切换）；
- 版本（4.1）：名称/编码/描述/开始/计划结束/实际结束/状态；
- 模块树（4.2）：每项目独立、最多 5 级，排序/移动/改父级/逻辑删除，被引用禁止物理删除。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.permissions import PROJECT_ROLES

PROJECT_STATUSES = {"active": "进行中", "paused": "暂停", "archived": "已归档"}
VERSION_STATUSES = {"not_started": "未开始", "in_progress": "进行中",
                    "done": "已完成", "archived": "已归档"}
MODULE_MAX_DEPTH = 5
RECENT_LIMIT = 10


class ProjectError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hex8() -> str:
    return uuid.uuid4().hex[:8]


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
        for p in self._projects.values():
            self._normalize(p)

    @staticmethod
    def _normalize(p: dict) -> dict:
        """老数据补默认字段（项目实体化之前只有名称/描述/创建人）。"""
        p.setdefault("code", "")
        p.setdefault("description", "")
        p.setdefault("owner", p.get("created_by") or "")
        p.setdefault("status", "active")
        p.setdefault("members", {})
        if p.get("created_by") and p["created_by"] not in p["members"]:
            p["members"][p["created_by"]] = "project_admin"
        p.setdefault("updated_at", p.get("created_at", _now()))
        p.setdefault("updated_by", p.get("created_by"))
        return p

    def _persist(self) -> None:
        self._doc.replace_all(self._projects)

    # ---- 查询 ----

    def list(self, include_archived: bool = True) -> list[dict]:
        items = list(self._projects.values())
        if not include_archived:
            items = [p for p in items if p["status"] != "archived"]
        return items

    def get(self, name: str) -> dict | None:
        return self._projects.get(name)

    def role_of(self, name: str, username: str | None) -> str | None:
        p = self._projects.get(name)
        if p is None or not username:
            return None
        return p["members"].get(username)

    def projects_of(self, username: str) -> list[dict]:
        """用户所属项目及其项目角色（3.1「查看所属项目」/ 3.4）。"""
        return [
            {"project": p["name"], "role": p["members"][username], "status": p["status"]}
            for p in self._projects.values() if username in p["members"]
        ]

    # ---- 维护 ----

    def ensure(self, names: list[str], created_by: str | None = None) -> None:
        """自动注册（历史任务中的项目名 / 管理员直传的新名称），已存在的跳过。"""
        added = False
        for name in names:
            name = (name or "").strip()
            if not name or name in self._projects:
                continue
            self._projects[name] = self._normalize({
                "name": name, "created_by": created_by, "created_at": _now(),
            })
            added = True
        if added:
            self._persist()

    def create(
        self, name: str, description: str = "", created_by: str | None = None,
        code: str = "", owner: str = "",
    ) -> dict:
        name = (name or "").strip()
        if not name:
            raise ProjectError("项目名不能为空")
        if name in self._projects:
            raise ProjectError(f"项目已存在: {name}")
        code = (code or "").strip()
        if code and any(p["code"] == code for p in self._projects.values()):
            raise ProjectError(f"项目编码已存在: {code}")
        project = self._normalize({
            "name": name, "code": code, "description": (description or "").strip(),
            "owner": (owner or "").strip() or (created_by or ""),
            "created_by": created_by, "created_at": _now(),
        })
        if project["owner"]:
            project["members"].setdefault(project["owner"], "project_admin")
        self._projects[name] = project
        self._persist()
        return project

    def update(
        self, name: str, new_name: str | None = None, description: str | None = None,
        code: str | None = None, owner: str | None = None, status: str | None = None,
        operator: str | None = None,
    ) -> dict:
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
        if code is not None:
            code = code.strip()
            if code and any(p["code"] == code and p is not project for p in self._projects.values()):
                raise ProjectError(f"项目编码已存在: {code}")
            project["code"] = code
        if owner is not None:
            project["owner"] = owner.strip()
            if project["owner"]:
                project["members"].setdefault(project["owner"], "project_admin")
        if status is not None:
            if status not in PROJECT_STATUSES:
                raise ProjectError(f"未知项目状态: {status}（可用 {'/'.join(PROJECT_STATUSES)}）")
            project["status"] = status
        project["updated_at"] = _now()
        project["updated_by"] = operator
        self._persist()
        return project

    def delete(self, name: str) -> None:
        if name not in self._projects:
            raise ProjectError(f"项目不存在: {name}")
        self._projects.pop(name)
        self._persist()

    # ---- 成员（3.4）----

    def set_member(self, name: str, username: str, role: str, operator: str | None = None) -> dict:
        project = self._projects.get(name)
        if project is None:
            raise ProjectError(f"项目不存在: {name}")
        if role not in PROJECT_ROLES:
            raise ProjectError(f"未知项目角色: {role}（可用 {'/'.join(PROJECT_ROLES)}）")
        username = (username or "").strip()
        if not username:
            raise ProjectError("成员用户名不能为空")
        if (project["members"].get(username) == "project_admin" and role != "project_admin"
                and self._admin_count(project) <= 1):
            raise ProjectError("项目至少保留一名项目管理员")
        project["members"][username] = role
        project["updated_at"], project["updated_by"] = _now(), operator
        self._persist()
        return project

    def remove_member(self, name: str, username: str, operator: str | None = None) -> dict:
        project = self._projects.get(name)
        if project is None:
            raise ProjectError(f"项目不存在: {name}")
        if username not in project["members"]:
            raise ProjectError(f"{username} 不是项目成员")
        if project["members"][username] == "project_admin" and self._admin_count(project) <= 1:
            raise ProjectError("项目至少保留一名项目管理员")
        project["members"].pop(username)
        project["updated_at"], project["updated_by"] = _now(), operator
        self._persist()
        return project

    def remove_user_everywhere(self, username: str) -> None:
        """用户被删除时清理其成员关系（保留负责人字段文本作历史）。"""
        changed = False
        for p in self._projects.values():
            if username in p["members"]:
                p["members"].pop(username)
                changed = True
        if changed:
            self._persist()

    @staticmethod
    def _admin_count(project: dict) -> int:
        return sum(1 for r in project["members"].values() if r == "project_admin")


class UserPrefStore:
    """个人偏好：项目收藏与最近访问（按用户名一文档）。"""

    def __init__(self):
        from app.db import DocStore

        self._doc = DocStore("user_prefs")
        self._prefs: dict[str, dict] = self._doc.load_all()

    def get(self, username: str) -> dict:
        p = self._prefs.get(username) or {"favorites": [], "recent": []}
        return {"favorites": list(p.get("favorites", [])), "recent": list(p.get("recent", []))}

    def _save(self, username: str, pref: dict) -> None:
        self._prefs[username] = pref
        self._doc.put(username, pref)

    def toggle_favorite(self, username: str, project: str) -> bool:
        pref = self.get(username)
        if project in pref["favorites"]:
            pref["favorites"].remove(project)
            fav = False
        else:
            pref["favorites"].append(project)
            fav = True
        self._save(username, pref)
        return fav

    def record_visit(self, username: str, project: str) -> None:
        pref = self.get(username)
        pref["recent"] = [r for r in pref["recent"] if r["project"] != project]
        pref["recent"].insert(0, {"project": project, "at": _now()})
        pref["recent"] = pref["recent"][:RECENT_LIMIT]
        self._save(username, pref)

    def rename_project(self, old: str, new: str) -> None:
        for username, pref in list(self._prefs.items()):
            changed = False
            if old in pref.get("favorites", []):
                pref["favorites"] = [new if f == old else f for f in pref["favorites"]]
                changed = True
            for r in pref.get("recent", []):
                if r["project"] == old:
                    r["project"] = new
                    changed = True
            if changed:
                self._save(username, pref)

    def drop_project(self, name: str) -> None:
        for username, pref in list(self._prefs.items()):
            if name in pref.get("favorites", []) or any(r["project"] == name for r in pref.get("recent", [])):
                pref["favorites"] = [f for f in pref["favorites"] if f != name]
                pref["recent"] = [r for r in pref["recent"] if r["project"] != name]
                self._save(username, pref)


class VersionStore:
    """项目版本（4.1）。"""

    def __init__(self):
        from app.db import DocStore

        self._doc = DocStore("versions")
        self._items: dict[str, dict] = self._doc.load_all()

    def list(self, project: str) -> list[dict]:
        items = [v for v in self._items.values() if v["project"] == project]
        return sorted(items, key=lambda v: v["created_at"], reverse=True)

    def get(self, version_id: str) -> dict | None:
        return self._items.get(version_id)

    def create(self, project: str, name: str, code: str = "", description: str = "",
               start_date: str = "", planned_end: str = "", actual_end: str = "",
               status: str = "not_started", created_by: str | None = None) -> dict:
        name = (name or "").strip()
        if not name:
            raise ProjectError("版本名称不能为空")
        if any(v["project"] == project and v["name"] == name for v in self._items.values()):
            raise ProjectError(f"版本已存在: {name}")
        if status not in VERSION_STATUSES:
            raise ProjectError(f"未知版本状态: {status}")
        item = {
            "version_id": _hex8(), "project": project, "name": name,
            "code": (code or "").strip(), "description": (description or "").strip(),
            "start_date": (start_date or "").strip(), "planned_end": (planned_end or "").strip(),
            "actual_end": (actual_end or "").strip(), "status": status,
            "created_by": created_by, "created_at": _now(), "updated_at": _now(),
        }
        self._items[item["version_id"]] = item
        self._doc.put(item["version_id"], item)
        return item

    def update(self, version_id: str, **fields) -> dict:
        item = self._items.get(version_id)
        if item is None:
            raise ProjectError(f"版本不存在: {version_id}")
        for key, value in fields.items():
            if value is None:
                continue
            if key == "status" and value not in VERSION_STATUSES:
                raise ProjectError(f"未知版本状态: {value}")
            if key == "name":
                value = value.strip()
                if not value:
                    raise ProjectError("版本名称不能为空")
                if any(v["project"] == item["project"] and v["name"] == value
                       and v["version_id"] != version_id for v in self._items.values()):
                    raise ProjectError(f"版本已存在: {value}")
            item[key] = value.strip() if isinstance(value, str) else value
        item["updated_at"] = _now()
        self._doc.put(version_id, item)
        return item

    def delete(self, version_id: str) -> dict:
        item = self._items.pop(version_id, None)
        if item is None:
            raise ProjectError(f"版本不存在: {version_id}")
        self._doc.remove(version_id)
        return item

    def rename_project(self, old: str, new: str) -> None:
        for v in self._items.values():
            if v["project"] == old:
                v["project"] = new
                self._doc.put(v["version_id"], v)

    def drop_project(self, name: str) -> None:
        for vid in [k for k, v in self._items.items() if v["project"] == name]:
            self._items.pop(vid)
            self._doc.remove(vid)


class ModuleStore:
    """项目模块树（4.2）：扁平存储（parent_id / order），树形按需组装。"""

    def __init__(self):
        from app.db import DocStore

        self._doc = DocStore("modules")
        self._items: dict[str, dict] = self._doc.load_all()

    # ---- 查询 ----

    def get(self, module_id: str) -> dict | None:
        return self._items.get(module_id)

    def list(self, project: str, include_deleted: bool = False) -> list[dict]:
        items = [m for m in self._items.values() if m["project"] == project
                 and (include_deleted or not m.get("deleted_at"))]
        return sorted(items, key=lambda m: (m["order"], m["created_at"]))

    def path(self, module_id: str) -> str:
        names = []
        seen = set()
        cur = self._items.get(module_id)
        while cur and cur["module_id"] not in seen:
            seen.add(cur["module_id"])
            names.append(cur["name"])
            cur = self._items.get(cur["parent_id"]) if cur["parent_id"] else None
        return "/".join(reversed(names))

    def depth(self, module_id: str | None) -> int:
        d = 0
        cur = self._items.get(module_id) if module_id else None
        while cur:
            d += 1
            cur = self._items.get(cur["parent_id"]) if cur["parent_id"] else None
        return d

    def descendants(self, module_id: str) -> list[str]:
        out, stack = [], [module_id]
        while stack:
            cur = stack.pop()
            for m in self._items.values():
                if m["parent_id"] == cur and m["module_id"] not in out:
                    out.append(m["module_id"])
                    stack.append(m["module_id"])
        return out

    def tree(self, project: str, include_deleted: bool = False) -> list[dict]:
        items = self.list(project, include_deleted=include_deleted)
        by_parent: dict[str | None, list[dict]] = {}
        for m in items:
            by_parent.setdefault(m["parent_id"], []).append(m)
        ids = {m["module_id"] for m in items}

        def build(parent_id: str | None, depth: int) -> list[dict]:
            return [
                {**m, "path": self.path(m["module_id"]), "depth": depth,
                 "children": build(m["module_id"], depth + 1)}
                for m in by_parent.get(parent_id, [])
            ]

        # 父级已被逻辑删除的节点在过滤视图里挂到根，避免"消失"
        roots = build(None, 1)
        for m in items:
            if m["parent_id"] and m["parent_id"] not in ids:
                roots.append({**m, "path": self.path(m["module_id"]), "depth": 1,
                              "children": build(m["module_id"], 2)})
        return roots

    # ---- 维护 ----

    def _persist(self, item: dict) -> None:
        self._doc.put(item["module_id"], item)

    def _check_name(self, project: str, parent_id: str | None, name: str, exclude: str | None = None) -> None:
        for m in self._items.values():
            if (m["project"] == project and m["parent_id"] == parent_id and m["name"] == name
                    and not m.get("deleted_at") and m["module_id"] != exclude):
                raise ProjectError(f"同级已存在模块: {name}")

    def create(self, project: str, name: str, parent_id: str | None = None,
               description: str = "", created_by: str | None = None) -> dict:
        name = (name or "").strip()
        if not name:
            raise ProjectError("模块名称不能为空")
        if "/" in name:
            raise ProjectError("模块名称不能包含 /")
        parent_id = parent_id or None
        if parent_id:
            parent = self._items.get(parent_id)
            if parent is None or parent["project"] != project or parent.get("deleted_at"):
                raise ProjectError("父级模块不存在")
            if self.depth(parent_id) >= MODULE_MAX_DEPTH:
                raise ProjectError(f"模块层级最多 {MODULE_MAX_DEPTH} 级")
        self._check_name(project, parent_id, name)
        siblings = [m for m in self._items.values()
                    if m["project"] == project and m["parent_id"] == parent_id]
        item = {
            "module_id": _hex8(), "project": project, "name": name, "parent_id": parent_id,
            "description": (description or "").strip(),
            "order": (max((m["order"] for m in siblings), default=-1) + 1),
            "created_by": created_by, "created_at": _now(), "updated_at": _now(),
            "deleted_at": None, "deleted_by": None,
        }
        self._items[item["module_id"]] = item
        self._persist(item)
        return item

    def update(self, module_id: str, name: str | None = None, description: str | None = None,
               parent_id: str | None = ..., operator: str | None = None) -> dict:
        """改名 / 改描述 / 移动（parent_id 传 None 表示移到根，不传表示不动）。"""
        item = self._items.get(module_id)
        if item is None or item.get("deleted_at"):
            raise ProjectError(f"模块不存在: {module_id}")
        if parent_id is not ...:
            parent_id = parent_id or None
            if parent_id == module_id or parent_id in self.descendants(module_id):
                raise ProjectError("不能把模块移动到自身或其子模块下")
            if parent_id:
                parent = self._items.get(parent_id)
                if parent is None or parent["project"] != item["project"] or parent.get("deleted_at"):
                    raise ProjectError("目标父级模块不存在")
                # 移动后子树最深层级不得超过上限
                sub_depth = 1 + max((self.depth(d) - self.depth(module_id) for d in self.descendants(module_id)), default=0)
                if self.depth(parent_id) + sub_depth > MODULE_MAX_DEPTH:
                    raise ProjectError(f"移动后层级超过 {MODULE_MAX_DEPTH} 级")
            if parent_id != item["parent_id"]:
                self._check_name(item["project"], parent_id, name.strip() if name else item["name"], exclude=module_id)
                siblings = [m for m in self._items.values()
                            if m["project"] == item["project"] and m["parent_id"] == parent_id]
                item["parent_id"] = parent_id
                item["order"] = max((m["order"] for m in siblings), default=-1) + 1
        if name is not None:
            name = name.strip()
            if not name:
                raise ProjectError("模块名称不能为空")
            if "/" in name:
                raise ProjectError("模块名称不能包含 /")
            self._check_name(item["project"], item["parent_id"], name, exclude=module_id)
            item["name"] = name
        if description is not None:
            item["description"] = description.strip()
        item["updated_at"] = _now()
        item["updated_by"] = operator
        self._persist(item)
        return item

    def reorder(self, project: str, parent_id: str | None, ordered_ids: list[str]) -> list[dict]:
        parent_id = parent_id or None
        siblings = {m["module_id"]: m for m in self.list(project) if m["parent_id"] == parent_id}
        if set(ordered_ids) != set(siblings):
            raise ProjectError("排序列表必须恰好包含该层级全部模块")
        for i, mid in enumerate(ordered_ids):
            siblings[mid]["order"] = i
            self._persist(siblings[mid])
        return [siblings[m] for m in ordered_ids]

    def delete(self, module_id: str, operator: str | None = None) -> list[dict]:
        """逻辑删除（含子树）。"""
        item = self._items.get(module_id)
        if item is None or item.get("deleted_at"):
            raise ProjectError(f"模块不存在: {module_id}")
        removed = []
        for mid in [module_id, *self.descendants(module_id)]:
            m = self._items[mid]
            if m.get("deleted_at"):
                continue
            m["deleted_at"], m["deleted_by"] = _now(), operator
            self._persist(m)
            removed.append(m)
        return removed

    def restore(self, module_id: str) -> dict:
        item = self._items.get(module_id)
        if item is None or not item.get("deleted_at"):
            raise ProjectError(f"回收站中无此模块: {module_id}")
        parent = self._items.get(item["parent_id"]) if item["parent_id"] else None
        if parent is not None and parent.get("deleted_at"):
            item["parent_id"] = None  # 父级仍在回收站：恢复到根
        self._check_name(item["project"], item["parent_id"], item["name"], exclude=module_id)
        item["deleted_at"], item["deleted_by"] = None, None
        self._persist(item)
        return item

    def purge(self, module_id: str, referenced: Callable[[str], bool]) -> dict:
        """物理删除：仅限已逻辑删除且（含子树）未被需求/用例引用的模块。"""
        item = self._items.get(module_id)
        if item is None:
            raise ProjectError(f"模块不存在: {module_id}")
        for mid in [module_id, *self.descendants(module_id)]:
            m = self._items[mid]
            if not m.get("deleted_at"):
                raise ProjectError("请先删除（逻辑删除）后再永久删除")
            if referenced(self.path(mid)) or referenced(m["name"]):
                raise ProjectError(f"模块「{self.path(mid)}」仍被用例引用，禁止物理删除")
        for mid in [*self.descendants(module_id), module_id]:
            self._items.pop(mid, None)
            self._doc.remove(mid)
        return item

    def rename_project(self, old: str, new: str) -> None:
        for m in self._items.values():
            if m["project"] == old:
                m["project"] = new
                self._persist(m)

    def drop_project(self, name: str) -> None:
        for mid in [k for k, v in self._items.items() if v["project"] == name]:
            self._items.pop(mid)
            self._doc.remove(mid)

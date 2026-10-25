"""需求中心（完整需求 5 章 / 21 章）：需求实体、原始需求分层保护、AI 分析结果与待确认事项、追溯。

- 需求实体：挂项目 / 版本 / 模块；标题必填；原文永久保留（raw_text，任何 AI 结果不得覆盖）；
  解析结果（attachments[].text）、AI 分析结果（analysis）、人工补充（description）各层独立保存；
- 待确认事项（5.5）：AI 分析产出的 open_questions 逐条确认后才允许发起测试设计（核心规则 4）；
- 追溯（21 章）：需求 → 任务（测试点/用例）→ 计划 → 执行，由 routes 侧按任务上下文聚合；
- 存量迁移：项目化之前的任务按「任务 = 一条需求」自动建需求并回填 task.context.requirement_id。
"""

from __future__ import annotations

import json as _json

import re

import uuid
from datetime import datetime, timezone

EXTRACTION_HISTORY = 10   # 整理稿历史版本数（页面可查看 / 对比 / 恢复）
REQ_STATUSES = {
    "draft": "草稿",              # 已录入，尚未 AI 分析
    "analyzed": "已分析",         # AI 分析完成，无待确认事项
    "pending_confirm": "待确认",  # 有未确认的待确认事项
    "confirmed": "已确认",        # 待确认事项全部确认，可发起测试设计
    "designing": "设计中",        # 已发起测试设计任务
    "done": "已完成",             # 人工标记完成
    "archived": "已归档",
}
SOURCE_TYPES = {"manual": "手工录入", "file": "文件上传", "link": "链接抓取", "mixed": "文本+文件",
                "migrated": "历史任务迁移"}


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
            if item.get("extracting"):
                item["extracting"] = False
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
            raise RequirementError("需求原文不能为空（粘贴文本、上传文件或填写链接）")
        item = {
            "req_id": _hex8(), "project": project, "title": title,
            "version_id": version_id or None, "module_id": module_id or None,
            "raw_text": raw_text or "", "description": (description or "").strip(),
            "source_type": source_type if source_type in SOURCE_TYPES else "manual",
            "attachments": attachments or [],
            "analysis": None, "analysis_meta": None, "analysis_confirmed": None, "questions": [],
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
            item["source_type"] = "mixed" if item["raw_text"].strip() else ("link" if all(a.get("source_url") for a in attachments) else "file")
        item["updated_by"], item["updated_at"] = operator, _now()
        self._persist(item)
        return item

    def remove_attachment(self, req_id: str, att_id: str, operator: str | None = None) -> dict:
        """删掉一个附件记录（换新版本文档前删旧的）。原文不动；整理稿 / 分析按输入指纹自然变成过期。"""
        item = self._get_alive(req_id)
        before = len(item["attachments"])
        item["attachments"] = [a for a in item["attachments"] if a["att_id"] != att_id]
        if len(item["attachments"]) == before:
            raise RequirementError(f"附件不存在: {att_id}")
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

    # ---- 需求提取（分析前置的整理稿）----

    def set_extracting(self, req_id: str, flag: bool, error: str | None = None) -> dict:
        item = self._get_alive(req_id)
        item["extracting"] = bool(flag)
        item["extraction_error"] = error
        self._persist(item)
        return item

    def _archive_extraction(self, item: dict) -> None:
        """当前整理稿进历史（最近 EXTRACTION_HISTORY 版），供版本页面查看 / 对比 / 恢复。"""
        if item.get("extraction"):
            hist = item.setdefault("extraction_history", [])
            hist.append(item["extraction"])
            del hist[:-EXTRACTION_HISTORY]

    def set_extraction(self, req_id: str, data: dict, meta: dict) -> dict:
        """写入 AI 提取的整理稿（正文 + 逐条结构）；上一版留档。整理稿换版后分析指纹变化，分析结果标过期。"""
        item = self._get_alive(req_id)
        self._archive_extraction(item)
        previous = (item.get("extraction_history") or [None])[-1]
        meta = {**meta, "rev": 0}
        item["extraction"] = {"text": data["text"], "meta": meta, "edited": None,
                              "modules": data.get("modules"), "dropped": data.get("dropped") or [], "reviewed": None,
                              "ledger": data.get("ledger") or {"stats": None, "decisions": {}}}
        item["extracting"], item["extraction_error"] = False, None
        self._inherit_ledger(item, previous)
        self._render_text(item)
        self._persist(item)
        return item

    def _inherit_sources(self, item: dict, previous: dict | None) -> tuple[list[dict], list[tuple[dict, dict, list[dict], float]]]:
        """可以沿用底稿决定的来源，按优先级排：本需求的上一版整理稿（材料大体没变、换版不该把人点过的丢掉）在前，
        然后是同项目里材料重合 ≥ INHERIT_MIN_OVERLAP 的其他需求（含回收站里的——重复导入后把旧的删了 / 合并了也算），
        重合度高的在前。返回 (本需求材料的单元清单, [(来源说明, 整理稿, 该来源材料的单元清单, 重合度)])。"""
        from app.agents.ledger import INHERIT_MIN_OVERLAP, build_units, has_human_decisions, unit_overlap

        units = build_units(source_materials(item))
        out: list[tuple[dict, dict, list[dict], float]] = []
        if has_human_decisions(previous):
            out.append(({"req_id": item["req_id"], "title": item.get("title") or "", "self": True}, previous, units, 1.0))
        scored = []
        for other in self.list(item["project"], include_deleted=True):
            if other["req_id"] == item["req_id"] or not has_human_decisions(other.get("extraction")):
                continue
            theirs = build_units(source_materials(other))
            ratio = unit_overlap(units, theirs)
            if ratio >= INHERIT_MIN_OVERLAP:
                scored.append((ratio, other, theirs))
        scored.sort(key=lambda x: -x[0])
        out.extend(({"req_id": o["req_id"], "title": o.get("title") or ""}, o["extraction"], th, round(r, 3)) for r, o, th in scored)
        return units, out

    def _inherit_ledger(self, item: dict, previous: dict | None) -> None:
        """新整理稿落下后，把人做过的底稿决定按原句沿用过来（同一句话上次人已经判过，不该再问一遍）：
        人工收录过、这版又没收进来的句子按原句收进「页面-人工收录」；人工排除过的（这版没有正式摘录）记人工排除，
        兜底收进来的条目一并撤掉；需复核已看过的沿用已看过。先用的优先，每句只沿用一次；模型自己的判定不沿用。
        沿用摘要记在 extraction.ledger.inherited，条目 / 判定上带 inherited={req_id, title} 可追溯。"""
        from app.agents.extraction import extraction_markdown
        from app.agents.ledger import STATUS_EX, STATUS_IN, BY_MODEL, account, human_decisions, page_of, source_of, unit_key

        ex = item["extraction"]
        if not ex.get("modules"):
            return
        units, sources = self._inherit_sources(item, previous)
        if not sources:
            return
        led = ex.setdefault("ledger", {"stats": None, "decisions": {}})
        decisions = led.setdefault("decisions", {})
        rows = {r["uid"]: r for r in account(units, ex.get("text") or "", decisions)["units"]}
        own_by_uid: dict[str, list[tuple[dict, dict]]] = {}
        for m in ex["modules"]:
            for it in m["items"]:
                if it.get("unit"):
                    own_by_uid.setdefault(it["unit"], []).append((m, it))
        done: set[str] = set()
        summary: list[dict] = []
        now = _now()
        for src, src_ex, src_units, ratio in sources:
            H = human_decisions(src_ex, src_units)
            n = {"include": 0, "exclude": 0, "checked": 0}
            tag = {"req_id": src["req_id"], "title": src["title"]}
            for u in units:
                key = unit_key(u["text"])
                if key in done:
                    continue
                row = rows[u["uid"]]
                own = [pair for x in u["uids"] for pair in own_by_uid.get(x, [])]
                if key in H["include"]:
                    if row["status"] == STATUS_IN:
                        continue   # 这版模型自己收进来了，不用人工收录
                    name = f"{page_of(u['path'])}-人工收录"
                    mod = next((m for m in ex["modules"] if m["name"] == name), None)
                    if mod is None:
                        mod = {"name": name, "items": []}
                        ex["modules"].append(mod)
                    mod["items"].append({"text": u["text"], "sources": [source_of(u["path"])], "manual": True, "unit": u["uid"],
                                         "verified": True, "id": self._unit_item_id(name, u["text"]),
                                         "review": {"status": "included", "by": H["include"][key].get("who"), "at": now},
                                         "inherited": tag})
                    for x in u["uids"]:
                        decisions.pop(x, None)
                    done.add(key)
                    n["include"] += 1
                elif key in H["exclude"]:
                    if row["status"] == STATUS_IN and any(not it.get("forced") for _, it in own):
                        continue   # 这版正式摘录了这句：收录优先，人要排除得再点一次
                    if row["status"] == STATUS_IN and not own:
                        continue   # 稿里逐字有但没有带编号的条目：不知道删哪条，不动
                    for mod, it in own:
                        mod["items"].remove(it)
                        if not mod["items"]:
                            ex["modules"].remove(mod)
                    for x in u["uids"]:
                        decisions.pop(x, None)
                        own_by_uid.pop(x, None)
                    decisions[u["uid"]] = {"r": H["exclude"][key]["r"], "by": "user", "who": H["exclude"][key].get("who"),
                                           "at": now, "inherited": tag}
                    done.add(key)
                    n["exclude"] += 1
                elif key in H["checked"]:
                    if row["status"] != STATUS_EX or row.get("by") != BY_MODEL or row.get("checked"):
                        continue
                    k = next((x for x in u["uids"] if x in decisions), u["uid"])
                    decisions[k] = {**decisions.get(k, {"r": row.get("reason") or "", "by": BY_MODEL}),
                                    "checked": {**H["checked"][key], "inherited": tag}}
                    done.add(key)
                    n["checked"] += 1
            if any(n.values()):
                summary.append({**src, "overlap": ratio, **n})
        if not summary:
            return
        led["inherited"] = summary
        ex["text"] = extraction_markdown(ex["modules"], ex.get("dropped"))
        self._recount(ex)
        ex["meta"]["ledger"] = self._refresh_ledger(item)["stats"]

    @staticmethod
    def _render_text(item: dict) -> None:
        """有逐条结构时按层级重生成整理稿正文（带文头：标题 / 来源 / 校验）；整篇改写过的正文不动。"""
        from app.agents.extraction import extraction_markdown

        ex = item["extraction"]
        if ex.get("modules"):
            tidy_modules(item)
            ex["text"] = extraction_markdown(ex["modules"], ex.get("dropped"), head=extraction_head(item))
            ex["meta"]["chars"] = len(ex["text"])

    @staticmethod
    def _refresh_ledger(item: dict) -> dict:
        """按当前整理稿正文重新核算底稿（每个材料单元的去向），统计写回 extraction.ledger.stats；返回核算结果。"""
        from app.agents.ledger import account, build_units

        ex = item["extraction"]
        led = ex.setdefault("ledger", {"stats": None, "decisions": {}})
        acct = account(build_units(source_materials(item)), ex.get("text") or "", led.get("decisions") or {})
        led["stats"], led["image"] = acct["stats"], acct["image"]
        return acct

    def ledger_view(self, req_id: str) -> dict:
        """底稿核对页数据：每个单元的编号、来源、原句、去向（收录 / 排除 + 原因与判定方 / 待处理），按页面分组。"""
        from app.agents.ledger import group_by_page

        item = self._get_alive(req_id)
        if not item.get("extraction"):
            raise RequirementError("还没有整理稿")
        acct = self._refresh_ledger(item)
        flags = {it["unit"]: ("forced" if it.get("forced") else "manual") for m in item["extraction"].get("modules") or []
                 for it in m["items"] if it.get("unit") and (it.get("forced") or it.get("manual"))}
        index = {a["filename"]: a["controls"] for a in item.get("attachments", []) if a.get("controls")}
        for u in acct["units"]:
            for x in u["uids"]:
                if x in flags:
                    u[flags[x]] = True
            ctl = (index.get(u["path"].split(" › ")[0]) or {}).get(u["uid"].split(".")[0])
            if ctl:
                u["ctl"] = ctl   # 原型控件类型 / 坐标 / 隐藏态：人看底稿时分辨表格单元格、流程框、隐藏面板
        checks = [{"filename": a["filename"], "ok": a["check"].get("ok", False), "missing": a["check"].get("missing", 0),
                   "pages": len(a["check"].get("pages") or [])}
                  for a in item.get("attachments", []) if a.get("check")]
        return {"stats": acct["stats"], "image": acct["image"], "pages": group_by_page(acct["units"]),
                "checks": checks, "rev": item["extraction"]["meta"].get("rev", 0)}

    def review_ledger_unit(self, req_id: str, uid: str, action: str, reason: str | None = None,
                           operator: str | None = None) -> dict:
        """底稿核对：include 把排除 / 待处理的单元按原句收进整理稿（标人工收录）；exclude 把兜底 / 人工收录 / 待处理的单元
        记为人工排除并撤掉对应条目；confirm 把模型排除的「需复核」单元标为人已看过（维持排除）。
        整理稿里本来就逐字有的单元不能排除（它确实在稿里，要删得删那条摘录）。"""
        from app.agents.extraction import extraction_markdown
        from app.agents.ledger import STATUS_EX, STATUS_IN, build_units, page_of, source_of

        item = self._get_alive(req_id)
        ex = item.get("extraction") or {}
        if not ex.get("modules"):
            raise RequirementError("这份整理稿没有逐条结构（整篇改写过或旧版本提取），不能按底稿核对；可重新提取")
        unit = next((u for u in build_units(source_materials(item)) if uid in u["uids"]), None)
        if unit is None:
            raise RequirementError("底稿里没有这个单元（材料可能已变化）")
        uid = unit["uid"]   # 去重单元用首次出现的编号做键
        led = ex.setdefault("ledger", {"stats": None, "decisions": {}})
        decisions = led.setdefault("decisions", {})
        own = [(m, it) for m in ex["modules"] for it in m["items"] if it.get("unit") in unit["uids"]]
        if action == "confirm":
            row = next((u for u in self._refresh_ledger(item)["units"] if u["uid"] == uid), None)
            if not row or row["status"] != STATUS_EX:
                raise RequirementError("只有排除的单元需要确认")
            key = next((x for x in unit["uids"] if x in decisions), uid)
            decisions[key] = {**decisions.get(key, {"r": row.get("reason") or "", "by": row.get("by") or "model"}),
                              "checked": {"by": operator, "at": _now()}}
            led["stats"], led["image"] = (acct := self._refresh_ledger(item))["stats"], acct["image"]
            item["updated_by"], item["updated_at"] = operator, _now()
            self._persist(item)
            return item
        if action == "include":
            if not own:
                name = f"{page_of(unit['path'])}-人工收录"
                mod = next((m for m in ex["modules"] if m["name"] == name), None)
                if mod is None:
                    mod = {"name": name, "items": []}
                    ex["modules"].append(mod)
                mod["items"].append({"text": unit["text"], "sources": [source_of(unit["path"])], "manual": True, "unit": uid,
                                     "verified": True, "id": self._unit_item_id(name, unit["text"]),
                                     "review": {"status": "included", "by": operator, "at": _now()}})
            for x in unit["uids"]:
                decisions.pop(x, None)
        elif action == "exclude":
            status = next((u["status"] for u in self._refresh_ledger(item)["units"] if u["uid"] == uid), None)
            if status == STATUS_IN and not own:
                raise RequirementError("这句已在整理稿里逐字出现，不能排除；要去掉请删除对应的摘录条目")
            for mod, it in own:
                mod["items"].remove(it)
                if not mod["items"]:
                    ex["modules"].remove(mod)
            for x in unit["uids"]:
                decisions.pop(x, None)
            decisions[uid] = {"r": (reason or "").strip()[:80] or "人工排除", "by": "user", "who": operator, "at": _now()}
        else:
            raise RequirementError(f"未知操作：{action}")
        ex["text"] = extraction_markdown(ex["modules"], ex.get("dropped"))
        self._after_item_change(item, operator)
        return item

    @staticmethod
    def _recount(ex: dict) -> None:
        """逐条结构变了之后重算 meta 里的条目统计（条数 / 核验 / 模块数 / 字数）。"""
        ex["meta"]["checked"] = sum(len(m["items"]) for m in ex["modules"])
        ex["meta"]["unverified"] = sum(1 for m in ex["modules"] for x in m["items"] if x.get("verified") is False)
        ex["meta"]["verified"] = ex["meta"]["checked"] - ex["meta"]["unverified"]
        ex["meta"]["modules"] = len(ex["modules"])
        ex["meta"]["chars"] = len(ex["text"])

    @staticmethod
    def _unit_item_id(module: str, text: str) -> str:
        from app.agents.extraction import _item_id

        return _item_id(module, text)

    def _after_item_change(self, item: dict, operator: str | None) -> None:
        """逐条 / 底稿改动后的公共收尾：统计、rev +1（分析指纹变化）、审核标记作废、底稿重算、落库。"""
        ex = item["extraction"]
        ex["meta"]["rev"] = int(ex["meta"].get("rev") or 0) + 1
        self._recount(ex)
        ex["reviewed"] = None
        self._refresh_ledger(item)
        self._render_text(item)
        item["updated_by"], item["updated_at"] = operator, _now()
        self._persist(item)

    def edit_extraction(self, req_id: str, text: str, operator: str | None = None) -> dict:
        """整篇人工改写整理稿：逐条结构失效（modules 清空），edited 记录改动者与时间。"""
        item = self._get_alive(req_id)
        if not item.get("extraction"):
            raise RequirementError("还没有整理稿可修改，请先提取需求")
        text = (text or "").strip()
        if not text:
            raise RequirementError("整理稿不能为空；不需要整理稿请点「删除整理稿」")
        ex = item["extraction"]
        ex["text"] = text
        ex["modules"], ex["reviewed"] = None, None
        ex["edited"] = {"by": operator, "at": _now()}
        ex["meta"]["rev"] = int(ex["meta"].get("rev") or 0) + 1
        self._refresh_ledger(item)
        item["updated_by"], item["updated_at"] = operator, _now()
        self._persist(item)
        return item

    def review_extraction_item(self, req_id: str, item_id: str, action: str, text: str | None = None,
                               verified: bool | None = None, operator: str | None = None) -> dict:
        """逐条审核：ok 核对通过 / edit 修改文本（调用方传入重新核验结果）/ delete 删除。改动后整理稿正文按结构重生成，
        rev +1（分析指纹变化），已完成的审核标记作废。"""
        from app.agents.extraction import extraction_markdown

        item = self._get_alive(req_id)
        ex = item.get("extraction") or {}
        if not ex.get("modules"):
            raise RequirementError("这份整理稿没有逐条结构（整篇改写过或旧版本提取），不能逐条审核；可重新提取")
        hit = None
        for mod in ex["modules"]:
            for it in mod["items"]:
                if it.get("id") == item_id:
                    hit = (mod, it)
        if hit is None:
            raise RequirementError("条目不存在，可能已被删除")
        mod, it = hit
        if action == "ok":
            it["review"] = {"status": "ok", "by": operator, "at": _now()}
        elif action == "edit":
            text = (text or "").strip()
            if not text:
                raise RequirementError("条目内容不能为空；不需要请删除")
            it["text"] = text
            if verified is not None:
                it["verified"] = verified
            it["review"] = {"status": "edited", "by": operator, "at": _now()}
            if it.pop("doubt", None):
                it["doubt_resolved"] = True   # 人改过这条就算疑点处理过了：整理稿与预览不再标〔疑点〕，疑点清单也不再列
        elif action == "delete":
            mod["items"].remove(it)
            if not mod["items"]:
                ex["modules"].remove(mod)
        else:
            raise RequirementError(f"未知操作：{action}")
        ex["text"] = extraction_markdown(ex["modules"], ex.get("dropped"))
        self._after_item_change(item, operator)
        return item

    def finish_extraction_review(self, req_id: str, operator: str | None = None) -> dict:
        """标记整理稿已审核：⚠ 条目都得有核对结论（通过 / 修改 / 删除），底稿里不能有待处理的单元（每句要么收录要么有排除原因）。"""
        item = self._get_alive(req_id)
        ex = item.get("extraction") or {}
        if not ex:
            raise RequirementError("还没有整理稿")
        pending = sum(1 for m in ex.get("modules") or [] for x in m["items"] if x.get("verified") is False and not x.get("review"))
        if pending:
            raise RequirementError(f"还有 {pending} 条未逐字找到的条目没有核对，请先逐条处理（核对通过 / 修改 / 删除）")
        st = self._refresh_ledger(item)["stats"] or {}
        if st.get("todo"):
            raise RequirementError(f"底稿里还有 {st['todo']} 句材料原句既没收录也没有排除原因，请在「底稿核对」里处理（收录 / 排除）")
        if st.get("review_open"):
            raise RequirementError(f"底稿里还有 {st['review_open']} 句模型排除的需复核句没有看过，请在「底稿核对 › 需复核」里逐条确认或收录")
        ex["reviewed"] = {"by": operator, "at": _now(), "rev": ex["meta"].get("rev", 0)}
        item["updated_by"], item["updated_at"] = operator, _now()
        self._persist(item)
        return item

    def restore_extraction(self, req_id: str, index: int, operator: str | None = None) -> dict:
        """把历史版本恢复为当前整理稿：当前版本进历史，恢复的版本记 restored_from 并换新 meta.id（分析指纹变化）。"""
        item = self._get_alive(req_id)
        hist = item.get("extraction_history") or []
        if index < 0 or index >= len(hist):
            raise RequirementError("历史版本不存在")
        old = hist[index]
        self._archive_extraction(item)
        restored = _json.loads(_json.dumps(old))
        restored["meta"] = {**restored.get("meta", {}), "id": uuid.uuid4().hex[:8],
                            "restored_from": old.get("meta", {}).get("id"), "restored_by": operator, "restored_at": _now(),
                            "rev": int(restored.get("meta", {}).get("rev") or 0) + 1}
        restored["reviewed"] = None
        item["extraction"] = restored
        self._refresh_ledger(item)
        item["updated_by"], item["updated_at"] = operator, _now()
        self._persist(item)
        return item

    def clear_extraction(self, req_id: str, operator: str | None = None) -> dict:
        item = self._get_alive(req_id)
        self._archive_extraction(item)
        item["extraction"] = None
        item["updated_by"], item["updated_at"] = operator, _now()
        self._persist(item)
        return item

    def set_analysis(self, req_id: str, analysis: dict, meta: dict) -> dict:
        """写入 AI 分析结果（不触碰原文）；待确认事项合并：已确认的保留，新问题追加。"""
        item = self._get_alive(req_id)
        if item.get("analysis"):  # 历次分析留档（最近 10 次），页面可回看对比
            hist = item.setdefault("analysis_history", [])
            hist.append({"analysis": item["analysis"], "meta": item.get("analysis_meta")})
            del hist[:-10]
        item["analysis"] = {k: v for k, v in analysis.items() if k not in ("model_name", "chunks")}
        item["analysis_meta"] = meta
        item["analysis_edits"] = []  # 新一版分析：上一版的人工修改留痕随历史走
        existing = {q["question"]: q for q in item["questions"]}
        history = self._confirmed_question_pool(item)
        questions = []
        used: set[str] = set()
        for q in analysis.get("open_questions") or []:
            meta = q if isinstance(q, dict) else {"question": str(q)}
            text = str(meta.get("question", "")).strip()
            if not text:
                continue
            # 同一需求里问过的问题：原文相同，或换了说法但说的是同一件事（相似度 ≥ QUESTION_SIM_AUTO 且数字一致）→ 沿用原记录
            old = existing.get(text) or find_similar_question(text, item["questions"], QUESTION_SIM_AUTO, same_numbers=True)[0]
            if old is not None:
                if old["q_id"] in used:
                    continue   # 这次分析把同一件事问了两遍：只留一条
                used.add(old["q_id"])
                old.setdefault("level", meta.get("level") or "must")
                old.setdefault("reason", meta.get("reason", "")); old.setdefault("assumption", meta.get("assumption", ""))
                if meta.get("module") and not old.get("module"):   # 旧问题没定过模块、这次定到了就补上
                    old["module"] = str(meta["module"])
                questions.append(old)
            else:
                new = {"q_id": _hex8(), "question": text, "status": "open",
                       "level": meta.get("level") if meta.get("level") in ("must", "suggest") else "must",
                       "reason": str(meta.get("reason", "")), "assumption": str(meta.get("assumption", "")),
                       "module": str(meta.get("module") or ""),
                       "answer": "", "by": None, "at": None, "source": "ai",
                       # 核验降级 / 复核补充的条目：记下来源项与原文本，确认成立后可一键采纳回分析结果
                       "kind": meta.get("kind") or "question", "key": meta.get("key"),
                       "text": str(meta.get("text") or ""), "quote": str(meta.get("quote") or "")}
                # 同项目其他需求（同一份文档重新上传、新版本）已经确认过的同一问题：结论直接带进来，标来源、可重新打开；
                # 像但没到自动沿用门槛的只作参考，页面预填、人点确认才算
                hit, score = find_similar_question(text, history, QUESTION_SIM_HINT)
                if hit is not None:
                    src = {"req_id": hit["_req_id"], "title": hit["_title"], "question": hit["question"], "score": round(score, 2)}
                    if score >= QUESTION_SIM_AUTO and _same_numbers(text, hit["question"]):
                        new.update(status="confirmed", answer=hit["answer"], by=hit.get("by"), at=hit.get("at") or _now(),
                                   inherited=src, module=new["module"] or str(hit.get("module") or ""))
                    else:
                        new["suggested"] = {**src, "answer": hit["answer"], "by": hit.get("by")}
                questions.append(new)
        # 分析结果里的待确认事项统一存成问题文本，便于展示与比对
        item["analysis"]["open_questions"] = [q["question"] for q in questions]
        # 人工补充的问题保留
        questions.extend(q for q in item["questions"] if q.get("source") == "manual" and q["question"] not in
                         {x["question"] for x in questions})
        item["questions"] = questions
        self._refresh_status(item)
        self._persist(item)
        return item

    def _confirmed_question_pool(self, item: dict) -> list[dict]:
        """同项目其他需求（含回收站里的）已确认、有结论的待确认事项，供新分析沿用；每条带 _req_id / _title 记来源。
        新的需求在前（同一问题多条需求都确认过时，以最近的结论为准）。"""
        pool: list[dict] = []
        for other in self.list(item["project"], include_deleted=True):
            if other["req_id"] == item["req_id"]:
                continue
            for q in other.get("questions") or []:
                if q.get("status") == "confirmed" and (q.get("answer") or "").strip():
                    pool.append({**q, "_req_id": other["req_id"], "_title": other.get("title") or ""})
        return pool

    def confirm_analysis(self, req_id: str, by: str | None) -> dict:
        """人工确认当前分析结果：快照留存（知识沉淀以快照为准），重新分析后需再次确认才会更新知识库。"""
        item = self._get_alive(req_id)
        if not item.get("analysis"):
            raise RequirementError("还没有分析结果可确认")
        item["analysis_confirmed"] = {
            "by": by, "at": _now(), "analysis_id": _analysis_id(item),
            "analysis": {k: [dict(x) if isinstance(x, dict) else x for x in v]
                         for k, v in item["analysis"].items() if k != "open_questions" and isinstance(v, list)},
        }
        self._persist(item)
        return item

    def unconfirm_analysis(self, req_id: str) -> dict:
        item = self._get_alive(req_id)
        item["analysis_confirmed"] = None
        self._persist(item)
        return item

    @staticmethod
    def analysis_confirmed_stale(item: dict) -> bool:
        """已确认的是旧一版分析（之后又重新分析过）。"""
        c = item.get("analysis_confirmed") or {}
        return bool(c) and _analysis_id(item) != c.get("analysis_id")

    def edit_analysis_item(self, req_id: str, key: str, action: str, index: int | None = None,
                           text: str | None = None, operator: str | None = None, quote: str | None = None) -> dict:
        """人工修改分析结果（edit / delete / add）：条目文本改了就留痕（analysis_edits），
        修改后的结果是评测集标准答案与学习规则的素材；修改会让已确认的版本变为「旧版本」需重新确认。"""
        from app.agents.analysis import ITEM_KEYS, as_item, normalize, quote_found

        item = self._get_alive(req_id)
        analysis = item.get("analysis")
        if not analysis:
            raise RequirementError("还没有分析结果可修改")
        if key not in ITEM_KEYS:
            raise RequirementError(f"不可修改的分析项: {key}")
        items = [as_item(x) for x in analysis.get(key) or []]
        text = (text or "").strip()
        log = {"key": key, "action": action, "by": operator, "at": _now()}
        if action == "add":
            if not text:
                raise RequirementError("内容不能为空")
            quote = (quote or "").strip()   # 采纳降级事项时带回原 quote，并按原文重新核验，不丢溯源
            verified = quote_found(quote, normalize(design_brief({**item, "analysis": None, "questions": []}))) if quote else None
            items.append({"text": text, "quote": quote, "verified": verified, "manual": True})
            log["after"] = text
        else:
            if index is None or not (0 <= index < len(items)):
                raise RequirementError("条目不存在（可能已被修改，请刷新）")
            before = items[index]
            log["before"] = before["text"]
            if action == "delete":
                items.pop(index)
            elif action == "edit":
                if not text:
                    raise RequirementError("内容不能为空")
                if text == before["text"]:
                    return item
                items[index] = {**before, "text": text, "manual": True}
                items[index].pop("review", None)  # 人工改过的条目不再带复核意见
                log["after"] = text
            elif action == "accept_fix":  # 采纳复核给出的改正
                fix = ((before.get("review") or {}).get("fix") or "").strip()
                if not fix:
                    raise RequirementError("该条目没有可采纳的复核改正")
                items[index] = {**before, "text": fix, "manual": True}
                items[index].pop("review", None)
                log.update(action="edit", after=fix, via="review")
            elif action == "dismiss_review":  # 人工看过复核意见，认为原条目没问题：留痕（谁、何时、驳回了什么意见）
                review = before.get("review") or {}
                items[index] = {k: v for k, v in before.items() if k != "review"}
                log.update(review_verdict=review.get("verdict"), review_note=review.get("note"))
            else:
                raise RequirementError(f"未知操作: {action}")
        analysis[key] = items
        if log is not None:
            item.setdefault("analysis_edits", []).append(log)
        item["updated_by"], item["updated_at"] = operator, _now()
        self._persist(item)
        return item

    def adopt_question(self, req_id: str, q_id: str, operator: str | None = None) -> dict:
        """核验降级 / 复核补充的待确认事项经人工确认成立：原条目采纳回对应分析项，问题记为已确认。"""
        item = self._get_alive(req_id)
        q = next((x for x in item["questions"] if x["q_id"] == q_id), None)
        if q is None:
            raise RequirementError(f"待确认事项不存在: {q_id}")
        if not q.get("key") or not q.get("text"):
            raise RequirementError("该事项不是从分析条目降级来的，无法采纳")
        if not item.get("analysis"):
            raise RequirementError("还没有分析结果")
        self.edit_analysis_item(req_id, q["key"], "add", text=q["text"], operator=operator, quote=q.get("quote"))
        q.update({"status": "confirmed", "answer": "确认成立，已采纳回分析结果", "by": operator, "at": _now()})
        self._refresh_status(item)
        self._persist(item)
        return item

    def add_question(self, req_id: str, question: str, operator: str | None = None, level: str = "must",
                     assumption: str = "", module: str = "", ref: str = "") -> dict:
        """人工补充 / 盲区转入的待确认事项；module 为所属页面 / 模块（盲区转入时沿用盲区的模块与出处 ref）。"""
        item = self._get_alive(req_id)
        question = (question or "").strip()
        if not question:
            raise RequirementError("待确认事项不能为空")
        q = {"q_id": _hex8(), "question": question, "status": "open",
             "level": level if level in ("must", "suggest") else "must", "reason": "",
             "assumption": (assumption or "").strip(), "module": (module or "").strip(),
             "answer": "", "by": operator, "at": _now(), "source": "manual"}
        if (ref or "").strip():
            q["ref"] = ref.strip()
        item["questions"].append(q)
        self._refresh_status(item)
        self._persist(item)
        return item

    def set_question_level(self, req_id: str, q_id: str, level: str, assumption: str | None = None,
                           operator: str | None = None) -> dict:
        """调整待确认事项等级：must（必须确认后才能生成）/ suggest（可带假设继续）。"""
        if level not in ("must", "suggest"):
            raise RequirementError("等级只能是 must 或 suggest")
        item = self._get_alive(req_id)
        q = next((x for x in item["questions"] if x["q_id"] == q_id), None)
        if q is None:
            raise RequirementError(f"待确认事项不存在: {q_id}")
        q["level"] = level
        if assumption is not None:
            q["assumption"] = assumption.strip()
        q["level_by"], q["level_at"] = operator, _now()
        self._refresh_status(item)
        self._persist(item)
        return item

    def answer_question(self, req_id: str, q_id: str, answer: str, operator: str | None = None,
                        reopen: bool = False) -> dict:
        item = self._get_alive(req_id)
        for q in item["questions"]:
            if q["q_id"] == q_id:
                q.pop("inherited", None); q.pop("suggested", None)   # 人自己定了：沿用 / 参考的历史结论标记撤掉
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

    def blocking_questions(self, item: dict) -> list[dict]:
        """必须确认的未确认事项：不确认不能发起测试设计；建议级可带假设继续。"""
        return [q for q in self.open_questions(item) if (q.get("level") or "must") == "must"]

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
        elif self.blocking_questions(item):
            item["status"] = "pending_confirm"
        else:
            item["status"] = "confirmed" if item["questions"] else "analyzed"


def _analysis_id(item: dict) -> str | None:
    """标识某一版分析结果：优先 meta.id，旧记录退化为分析时间；人工修改过 n 次则带上 +n（修改即新版本）。"""
    meta = item.get("analysis_meta") or {}
    base = meta.get("id") or meta.get("at")
    # 只数改了内容的操作：看过复核意见认为原条目没问题（dismiss_review）只留痕，不算新版本
    edits = sum(1 for x in item.get("analysis_edits") or [] if x.get("action") in ("edit", "delete", "add"))
    return f"{base}+{edits}" if base and edits else base


def tidy_modules(item: dict) -> None:
    """按当前规则收口逐条结构（老整理稿也适用，不用重新提取）：疑似草稿只认独立区域、流程框文字不与流程行重复。原地改。"""
    from app.agents.extraction import dedupe_flow_nodes, enforce_draft
    from app.agents.ledger import island_keys

    modules = (item.get("extraction") or {}).get("modules")
    if modules:
        enforce_draft(modules, island_keys(source_materials(item)))
        dedupe_flow_nodes(modules)


def extraction_head(item: dict) -> dict:
    """整理稿文头：标题、来源（附件与页面清单）、校验结论（逐字回查、底稿去向、原型自校验），与《连赢挑战赛_需求提取》的文头一致。"""
    ex = item.get("extraction") or {}
    meta, ls = ex.get("meta") or {}, (ex.get("ledger") or {}).get("stats") or {}
    srcs = ["需求原文"] if (item.get("text") or "").strip() else []
    checks = []
    for a in item.get("attachments") or []:
        if not a.get("parsed"):
            continue
        chk = a.get("check") or {}
        pages = chk.get("pages") or []
        srcs.append(f"`{a.get('filename', '')}`" + (f"（Axure 原型导出，共 {len(pages)} 个页面：{' / '.join(str(p.get('page', '')) for p in pages)}）" if pages else ""))
        if pages:
            checks.append(f"`{a.get('filename', '')}` 整页字符自校验 " + ("0 缺字" if chk.get("ok") else f"缺 {chk.get('missing', '?')} 字") + f"（{len(pages)} 页）")
    parts = []
    if meta.get("checked") is not None:
        parts.append(f"整理稿 {meta.get('checked')} 条逐字回查材料，未找到 {meta.get('unverified') or 0} 条")
    if ls:
        parts.append(f"底稿 {ls.get('total', 0)} 句（去重）：收录 {ls.get('in', 0)}、排除 {ls.get('ex', 0)}、待处理 {ls.get('todo', 0)}")
    parts.extend(checks)
    return {"title": item.get("title") or "", "sources": "；".join(srcs) or "需求原文", "check": "；".join(parts) or "—"}


def source_materials(item: dict) -> str:
    """用户上传的全部原始材料：原文 + 每个已解析附件 / 链接的文本（需求提取的输入；没有整理稿时也是分析与设计的输入）。"""
    parts = [item["raw_text"].strip()]
    for att in item.get("attachments", []):
        if att.get("text"):
            label = f"链接：{att['source_url']}" if att.get("source_url") else f"文件：{att['filename']}"
            parts.append(f"【{label}】\n{att['text']}")
    return "\n\n".join(p for p in parts if p)


def attachment_fingerprint(item: dict) -> list[str]:
    """已解析附件的 id@解析时间：追加 / 重新解析 / 删除附件都会变（整理稿与分析结果的过期判定依据）。"""
    return sorted(f"{a['att_id']}@{a.get('parsed_at') or ''}" for a in item.get("attachments", []) if a.get("parsed"))


def extraction_wanted(item: dict) -> bool:
    """需要先提取再分析的需求：有已解析出文本的附件（材料散落在多份文件 / 原型 / 截图里）。纯手填原文的直接用原文。"""
    return any(a.get("parsed") and a.get("text") for a in item.get("attachments", []))


def extraction_current(item: dict) -> bool:
    """整理稿是否基于当前材料：附件指纹一致才算；追加 / 重新解析 / 删除附件后整理稿过期，分析与设计退回原始材料。"""
    ex = item.get("extraction") or {}
    return bool(ex.get("text")) and (ex.get("meta") or {}).get("inputs") == attachment_fingerprint(item)


def extraction_status(item: dict) -> str:
    if item.get("extracting"):
        return "running"
    if not item.get("extraction"):
        return "none"
    return "current" if extraction_current(item) else "outdated"



QUESTION_SIM_AUTO = 0.6    # 待确认事项相似度：到这个值且数字一致，算同一问题（线上同一需求里不同问题之间最高 0.57，且那几对本就是一问两提）
QUESTION_SIM_HINT = 0.4    # 到这个值只作「历史结论参考」预填，人点确认才算
_QUESTION_MIN_CHARS = 10   # 规范化后不足这个字数的问题只认原文相同（短句字对两两重合太容易撞）
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else ({s} if s else set())


def question_similarity(a: str, b: str) -> float:
    """两个问题说的是不是同一件事：规范化（去标点空白、全角转半角、小写）后字对（相邻两字）的 Dice 系数；
    原文相同为 1。模型重新分析常把同一问题换个说法（「按操作人、页签还是全系统计时」/「按操作人、活动、页签还是全系统计算」），
    逐字相等认不出来，字对重合对中文改写够稳。"""
    from app.agents.analysis import normalize

    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if min(len(na), len(nb)) < _QUESTION_MIN_CHARS:
        return 0.0
    A, B = _bigrams(na), _bigrams(nb)
    return 2 * len(A & B) / (len(A) + len(B))


def _same_numbers(a: str, b: str) -> bool:
    """两个问题里出现的数字一样（「前 10 名」和「前 20 名」说法再像也不是同一问题）。"""
    import unicodedata

    return set(_NUM_RE.findall(unicodedata.normalize("NFKC", a))) == set(_NUM_RE.findall(unicodedata.normalize("NFKC", b)))


def find_similar_question(text: str, candidates: list[dict], min_score: float, same_numbers: bool = False) -> tuple[dict | None, float]:
    """candidates（每条带 question）里与 text 最像的一条及相似度；没到 min_score 返回 (None, 0)。
    same_numbers=True 时还要求数字一致。几条同分时取先出现的（候选按优先级排好）。"""
    best, best_score = None, 0.0
    for q in candidates:
        sc = question_similarity(text, q.get("question") or "")
        if sc > best_score and sc >= min_score and (not same_numbers or _same_numbers(text, q.get("question") or "")):
            best, best_score = q, sc
    return best, best_score


def _q_module(q: dict) -> str:
    """待确认事项的所属模块写进拆解上下文：「问（活动列表-基本配置）：…」，没有模块的还是「问：…」。"""
    return f"（{q['module']}）" if q.get("module") else ""

def design_brief(item: dict) -> str:
    """发起测试设计 / AI 分析时喂给 Agent 的需求文本：整理稿（或原始材料）+ 人工补充 + 已确认结论（原文不改）。

    有基于当前材料的整理稿时以整理稿代替原文与附件文本：噪音已剔除、重复已合并，分析的 quote 核验也对照整理稿。
    """
    from app.agents.analysis import item_text, review_pending
    from app.agents.prompts import REQUIREMENT_ANALYSIS_LABELS

    if extraction_current(item):
        n = len([a for a in item.get("attachments", []) if a.get("parsed") and a.get("text")])
        head = f"【需求整理稿】（AI 从原文与 {n} 份附件逐字摘录并按模块归整" + ("，人工修改过" if item["extraction"].get("edited") else "") + "）"
        parts = [f"{head}\n{item['extraction']['text']}"]
    else:
        parts = [source_materials(item)]
    if item.get("description", "").strip():
        parts.append(f"【人工补充说明】\n{item['description'].strip()}")
    confirmed = [q for q in item.get("questions", []) if q["status"] == "confirmed" and q.get("answer")]
    if confirmed:
        parts.append("【待确认事项的确认结论（以此为准）】\n" + "\n".join(
            f"- 问{_q_module(q)}：{q['question']}\n  确认：{q['answer']}" for q in confirmed))
    assumed = [q for q in item.get("questions", []) if q["status"] == "open" and (q.get("level") or "must") == "suggest"]
    if assumed:
        parts.append("【未确认的建议级事项（按下列假设处理，并在用例 remark 中注明假设）】\n" + "\n".join(
            f"- 问{_q_module(q)}：{q['question']}\n  假设：{q.get('assumption') or '按行业惯例处理'}" for q in assumed))
    analysis = item.get("analysis") or {}
    # 独立复核标了理解错 / 无依据、人还没处理的条目不进上下文：拆解 Agent 会把它当「人工过目」的事实
    lines = []
    for k in ("rules", "boundaries", "state_changes", "permissions"):
        vals = [item_text(x) for x in analysis.get(k) or [] if item_text(x) and not review_pending(x)]
        if vals:
            lines.append(f"{REQUIREMENT_ANALYSIS_LABELS[k]}：" + "；".join(vals))
    if lines:
        parts.append("【AI 需求分析要点（已经人工过目）】\n" + "\n".join(lines))
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


_SOURCE_REF_RE = re.compile(r"（出处：(.+?)）\s*$")
# 回填规则版本：1 依据 / 出处 / 章节号；2 加按文字定位；3 文字定位收紧（标题 ≥ 3 字且唯一、正文重合阈值 20%）
_MODULE_BACKFILL_V = 3


def backfill_question_modules(requirements: RequirementStore) -> int:
    """存量回填（幂等）：本版之前的待确认事项没有 module 字段，启动时按与新数据同样的规则补一次：
    有原文依据的（核验降级 / 复核补充）按依据所在章节，盲区转入的按问题末尾「（出处：…）」，其余按问题文字里的章节号；
    定不到的也写成空串并记 module_v，下次启动不再算；定位规则升级时抬高 _MODULE_BACKFILL_V，回填过的会按新规则再算一次
    （模型 / 人工给的模块没有 module_v，不动）。返回补上模块名的条数。"""
    from app.agents.analysis import section_module
    from app.tasks.blind_spots import ref_to_module

    filled = 0
    for item in requirements.list(include_deleted=True):
        # 没有 module 键的（本版之前的数据）、以及上次回填用旧规则算过的（有 module_v 且小于当前版本）；
        # 模型 / 人工给的模块没有 module_v，不动
        todo = [q for q in item.get("questions") or []
                if "module" not in q or (q.get("module_v") is not None and q["module_v"] < _MODULE_BACKFILL_V)]
        if not todo:
            continue
        source = design_brief({**item, "analysis": None, "questions": []})
        for q in todo:
            m = _SOURCE_REF_RE.search(q.get("question") or "")
            if q.get("quote"):
                module = section_module(source, quote=q["quote"], text=q.get("text") or "")
            elif m:
                module = ref_to_module(m.group(1), item.get("title") or "")
                q.setdefault("ref", m.group(1))
            else:
                module = section_module(source, text=f"{q.get('question') or ''} {q.get('reason') or ''}")
            q["module"], q["module_v"] = module, _MODULE_BACKFILL_V
            filled += bool(module)
        requirements._persist(item)
    return filled


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

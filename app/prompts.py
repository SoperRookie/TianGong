"""Prompt 版本化管理（完整需求 15 章）。

- 全部 Prompt 纳管：拆解/生成/评审/定点修正/查漏/查重/测试点修改/用例修改/需求变更/需求分析/学习分析/
  测试点补充/图片理解，以及注入块（知识/规则/记忆）；
- 每个 Prompt 一条记录：版本链 [{version_no, content, status: active/draft/archived, note, created_by, created_at}]，
  内置默认为 v1；新建版本为草稿，激活后生效（可随时回滚到任一历史版本=再激活）；
- 占位符校验：新版本必须包含默认版本的全部 {占位符}，防止 .format 失败；
- 取用登记：prompt_text(key) 返回生效内容并登记 (key, version) 到调用上下文，调用日志与任务留痕据此记录版本。
"""

from __future__ import annotations

import string
from datetime import datetime, timezone

from app.agents import prompts as P

PROMPT_DEFS: list[dict] = [
    {"key": "analyst", "name": "需求拆解（测试点）", "kind": "system", "default": P.ANALYST_SYSTEM,
     "where": "任务创建 → 拆解测试点"},
    {"key": "requirement_analysis", "name": "需求分析（11 项）", "kind": "system", "default": P.REQUIREMENT_ANALYSIS_SYSTEM,
     "where": "需求中心 → AI 需求分析"},
    {"key": "generator", "name": "用例生成", "kind": "system", "default": P.GENERATOR_SYSTEM,
     "where": "确认测试点 → 生成用例 / 定点修正", "placeholders": ["template_spec"]},
    {"key": "reviewer", "name": "用例评审", "kind": "system", "default": P.REVIEWER_SYSTEM,
     "where": "生成后独立评审（≤3 轮）"},
    {"key": "fix_instruction", "name": "评审问题定点修正指令", "kind": "user", "default": P.FIX_INSTRUCTION,
     "where": "评审未通过 → 生成 Agent 定点修正", "placeholders": ["issues", "cases"]},
    {"key": "gap_check", "name": "覆盖查漏", "kind": "system", "default": P.GAP_CHECK_SYSTEM,
     "where": "拆解后自动查漏 / 手动查漏"},
    {"key": "dup_judge", "name": "测试点语义查重", "kind": "system", "default": P.DUP_JUDGE_SYSTEM,
     "where": "拆解后自动查重 / 手动查重"},
    {"key": "point_fix", "name": "测试点驳回修改", "kind": "system", "default": P.POINT_FIX_SYSTEM,
     "where": "测试点驳回 → AI 定点修改提案"},
    {"key": "case_fix", "name": "用例驳回修改", "kind": "system", "default": P.CASE_FIX_SYSTEM,
     "where": "用例驳回 → AI 定点修改提案 / 需求变更更新", "placeholders": ["template_spec"]},
    {"key": "requirement_diff", "name": "需求变更分析", "kind": "system", "default": P.REQ_DIFF_SYSTEM,
     "where": "任务 → 需求变更"},
    {"key": "learning", "name": "修改习惯学习", "kind": "system", "default": P.LEARNING_SYSTEM,
     "where": "学习规则 → 分析修改习惯"},
    {"key": "point_supplement", "name": "测试点人工补充", "kind": "system",
     "default": "你是资深测试分析师，负责按用户要求补充测试点。一个测试点对应一个明确验证目标。",
     "where": "测试点审核 → AI 补充测试点"},
    {"key": "vision_image", "name": "图片理解（Vision）", "kind": "user", "default": None,
     "where": "需求图片 / 文档内嵌图片解析"},
    {"key": "knowledge_cases_block", "name": "注入块：历史用例知识", "kind": "block", "default": P.KNOWLEDGE_CASES_BLOCK,
     "where": "拆解与评审阶段注入", "placeholders": ["knowledge"]},
    {"key": "knowledge_refs_block", "name": "注入块：需求/规则知识", "kind": "block", "default": P.KNOWLEDGE_REFS_BLOCK,
     "where": "生成阶段注入", "placeholders": ["knowledge"]},
    {"key": "rules_block", "name": "注入块：团队/项目规则", "kind": "block", "default": P.RULES_BLOCK,
     "where": "生成阶段注入", "placeholders": ["rules"]},
    {"key": "memory_block", "name": "注入块：偏好记忆", "kind": "block", "default": P.MEMORY_BLOCK,
     "where": "生成阶段注入", "placeholders": ["memories"]},
]


class PromptError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _placeholders(text: str) -> set[str]:
    out = set()
    for _, field, _, _ in string.Formatter().parse(text or ""):
        if field:
            out.add(field.split(".")[0].split("[")[0])
    return out


class PromptStore:
    def __init__(self):
        from app.db import DocStore

        self._doc = DocStore("prompts")
        self._items: dict[str, dict] = self._doc.load_all()
        self._seed()

    def _seed(self) -> None:
        from app.parsers.image import VISION_PROMPT

        for d in PROMPT_DEFS:
            default = d["default"] if d["default"] is not None else VISION_PROMPT
            item = self._items.get(d["key"])
            if item is None:
                item = {
                    "key": d["key"], "name": d["name"], "kind": d["kind"], "where": d["where"],
                    # 只有经 .format 注入的 Prompt 声明占位符；其余内容里的花括号是字面 JSON 示例
                    "placeholders": sorted(d.get("placeholders") or []),
                    "versions": [{"version_no": 1, "content": default, "status": "active", "note": "内置默认",
                                  "created_by": None, "created_at": _now()}],
                    "active_version": 1,
                }
                self._items[d["key"]] = item
                self._doc.put(d["key"], item)
            else:  # 元信息随代码更新，版本内容不动
                item.update({"name": d["name"], "kind": d["kind"], "where": d["where"]})

    # ---- 读取 ----

    def list(self) -> list[dict]:
        out = []
        for d in PROMPT_DEFS:
            item = self._items[d["key"]]
            active = self.active(d["key"])
            out.append({k: item[k] for k in ("key", "name", "kind", "where", "placeholders", "active_version")}
                       | {"versions": len(item["versions"]), "customized": item["active_version"] != 1,
                          "drafts": sum(1 for v in item["versions"] if v["status"] == "draft"),
                          "updated_at": max(v["created_at"] for v in item["versions"]),
                          "preview": active["content"][:160]})
        return out

    def get(self, key: str) -> dict:
        item = self._items.get(key)
        if item is None:
            raise PromptError(f"Prompt 不存在: {key}")
        return item

    def active(self, key: str) -> dict:
        item = self.get(key)
        return next(v for v in item["versions"] if v["version_no"] == item["active_version"])

    def text(self, key: str) -> tuple[str, int]:
        v = self.active(key)
        return v["content"], v["version_no"]

    # ---- 维护 ----

    def create_version(self, key: str, content: str, note: str = "", by: str | None = None,
                       activate: bool = False) -> dict:
        item = self.get(key)
        content = (content or "").rstrip()
        if not content.strip():
            raise PromptError("Prompt 内容不能为空")
        required = set(item["placeholders"])
        if required:  # 经 .format 注入的 Prompt：占位符必须与默认版本一致，字面花括号须写成 {{ }}
            try:
                have = _placeholders(content)
            except ValueError as e:
                raise PromptError(f"花括号格式错误：{e}（字面花括号请写成 {{{{ }}}}）")
            missing = required - have
            if missing:
                raise PromptError(f"缺少必需占位符: {', '.join('{' + m + '}' for m in sorted(missing))}")
            extra = have - required
            if extra:
                raise PromptError(f"出现未定义的占位符: {', '.join('{' + m + '}' for m in sorted(extra))}（字面花括号请写成 {{{{ }}}}）")
        no = max(v["version_no"] for v in item["versions"]) + 1
        version = {"version_no": no, "content": content, "status": "draft", "note": (note or "").strip(),
                   "created_by": by, "created_at": _now()}
        item["versions"].append(version)
        if activate:
            self._activate(item, no, by)
        self._doc.put(key, item)
        return version

    def activate(self, key: str, version_no: int, by: str | None = None) -> dict:
        item = self.get(key)
        self._activate(item, version_no, by)
        self._doc.put(key, item)
        return item

    def _activate(self, item: dict, version_no: int, by: str | None) -> None:
        target = next((v for v in item["versions"] if v["version_no"] == version_no), None)
        if target is None:
            raise PromptError(f"版本不存在: v{version_no}")
        for v in item["versions"]:
            if v["status"] == "active":
                v["status"] = "archived"
        target["status"] = "active"
        target["activated_by"], target["activated_at"] = by, _now()
        item["active_version"] = version_no

    def archive(self, key: str, version_no: int) -> dict:
        item = self.get(key)
        target = next((v for v in item["versions"] if v["version_no"] == version_no), None)
        if target is None:
            raise PromptError(f"版本不存在: v{version_no}")
        if target["status"] == "active":
            raise PromptError("生效中的版本不能归档，请先激活其他版本")
        target["status"] = "archived"
        self._doc.put(key, item)
        return item


_current: PromptStore | None = None


def set_current(store: PromptStore | None) -> None:
    global _current
    _current = store


def prompt_text(key: str) -> str:
    """取生效 Prompt 并登记版本到调用上下文；未初始化（脚本/单测直接调用）时回退内置默认。"""
    from app.llm.calllog import note_prompt

    d = next(x for x in PROMPT_DEFS if x["key"] == key)
    if _current is None:
        from app.parsers.image import VISION_PROMPT

        content, version = (d["default"] if d["default"] is not None else VISION_PROMPT), 1
    else:
        content, version = _current.text(key)
    note_prompt(key, version, d["name"], d["kind"])
    return content

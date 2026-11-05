"""Prompt 版本化管理（完整需求 15 章）。

- 全部 Prompt 纳管：拆解/生成/评审/定点修正/查漏/查重/测试点修改/用例修改/需求变更/需求分析/需求分析复核/评测裁判/学习分析/
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
    {"key": "point_extend", "name": "经验延伸（需求之外的场景）", "kind": "system", "default": P.POINT_EXTEND_SYSTEM,
     "where": "拆解测试点之后的独立一步 / 测试点页「经验延伸」：按资深测试经验补需求没写到的并发 / 中断 / 跨天 / 越权 / 脏数据场景，每条带依据"},
    {"key": "point_combine", "name": "模块级组合延伸", "kind": "system", "default": P.POINT_COMBINE_SYSTEM,
     "where": "拆解 / 延伸 / 查漏之后自动（配置 point_combine）/ 测试点页「组合延伸」：每个模块一次，按判定表 / 场景串联 / 状态×角色×端 / 规则优先级 / 数据流转找单条规则叠加后的组合场景，每条带依据与来源编号"},
    {"key": "requirement_outline", "name": "长需求模块大纲", "kind": "system", "default": P.REQUIREMENT_OUTLINE_SYSTEM,
     "where": "长需求分片拆解前 → 全文模块大纲"},
    {"key": "e2e_scenarios", "name": "端到端场景（跨端主线）", "kind": "system", "default": P.E2E_SCENARIOS_SYSTEM,
     "where": "拆解测试点前（配置 point_e2e）：通读需求找跨端业务主线（商户后台配置 → H5 参与 → 游戏后台结算 → 商户报表），注入各分片拆解作参考，场景点落成「端到端」测试点"},
    {"key": "terminal_classify", "name": "整理稿章节归端", "kind": "system", "default": P.TERMINAL_CLASSIFY_SYSTEM,
     "where": "需求提取完成后（配置 requirement_extract_terminals）：按顶层章节判 H5 / 游戏后台管理 / 商户后台 / 通用，三端汇总与按端拆解据此；提取页可人工改"},
    {"key": "blind_spot_verify", "name": "分片盲区复核", "kind": "system", "default": P.BLIND_SPOT_VERIFY_SYSTEM,
     "where": "长需求分片拆解后 → 用其他分片原文复核盲区（已说明的不算盲区）"},
    {"key": "requirement_extract", "name": "需求提取（分析前置）", "kind": "system", "default": P.REQUIREMENT_EXTRACT_SYSTEM,
     "where": "需求中心 → 附件解析完成后自动 / 手动提取：从全部材料逐字摘录需求内容归整为整理稿，AI 分析与测试设计以整理稿为准"},
    {"key": "requirement_extract_gap", "name": "需求提取补漏", "kind": "system", "default": P.REQUIREMENT_EXTRACT_GAP_SYSTEM,
     "where": "需求提取之后：程序对照出整理稿没覆盖的材料原句，交模型判「补进 / 跳过」，最多两轮"},
    {"key": "requirement_extract_crosscheck", "name": "需求提取交叉核对", "kind": "system", "default": P.REQUIREMENT_EXTRACT_CROSSCHECK_SYSTEM,
     "where": "需求提取最后一步：整稿条目清单送模型找跨页面 / 跨文档矛盾与疑似遗留草稿（只加疑点与草稿标记，不改条目）"},
    {"key": "requirement_analysis", "name": "需求分析（11 项）", "kind": "system", "default": P.REQUIREMENT_ANALYSIS_SYSTEM,
     "where": "需求中心 → AI 需求分析（每条带原文依据 quote，程序逐条核验）"},
    {"key": "requirement_analysis_review", "name": "需求分析独立复核", "kind": "system",
     "default": P.REQUIREMENT_ANALYSIS_REVIEW_SYSTEM,
     "where": "需求中心 → AI 需求分析后的第二轮：对照原文挑理解错 / 无依据 / 遗漏"},
    {"key": "analysis_judge", "name": "需求分析评测裁判", "kind": "system", "default": P.ANALYSIS_JUDGE_SYSTEM,
     "where": "需求分析评测 → 标准答案与 AI 输出逐条语义对齐"},
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
    {"key": "point_compare", "name": "人工与 AI 测试点对比", "kind": "system", "default": P.POINT_COMPARE_SYSTEM,
     "where": "测试点审核 → 人工对比"},
    {"key": "point_supplement", "name": "测试点人工补充", "kind": "system", "default": P.POINT_SUPPLEMENT_SYSTEM,
     "where": "测试点审核 → AI 补充测试点：按用户一句话要求只补新增，带 ref / 依据"},
    {"key": "point_review", "name": "测试点独立评审", "kind": "system", "default": P.POINT_REVIEW_SYSTEM,
     "where": "拆解 / 延伸 / 查漏之后自动（配置 point_review）/ 测试点页「AI 评审」：逐条挑合写 / 不可验证 / 数值改写 / ref 对不上 / UI 外观 / 超出需求 / 维度不符，标到条目上"},
    {"key": "case_dup_judge", "name": "用例语义查重", "kind": "system", "default": P.CASE_DUP_JUDGE_SYSTEM,
     "where": "用例页「重复检查」：文字初筛候选对交模型判重复 / 可合并 / 不重复，人工处置"},
    {"key": "question_suggest", "name": "待确认事项推荐答案", "kind": "system", "default": P.QUESTION_SUGGEST_SYSTEM,
     "where": "需求中心 → AI 分析完成后自动（配置 question_suggest）/ 待确认事项页「推荐答案」：按原文 › 历史结论 › 行业惯例给每条未确认事项一个确定答案 + 依据 + 风险，人确认才算"},
    {"key": "plan_case_select", "name": "测试计划用例挑选", "kind": "system", "default": P.PLAN_CASE_SELECT_SYSTEM,
     "where": "计划页「AI 挑选」：按目标（冒烟 / 全量回归 / 变更影响回归 / 模块专项）、预算、变更摘要、重点模块从本项目正式用例里挑集合，草稿人勾选后加入"},
    {"key": "exec_triage", "name": "执行失败归因与缺陷单", "kind": "system", "default": P.EXEC_TRIAGE_SYSTEM,
     "where": "计划执行页「AI 归因」（单条 / 本轮全部失败）：用例 + 失败说明 + 截图理解 + 对应需求原文 → 五类归因（用例问题 / 系统缺陷 / 环境 / 数据 / 需求变更）+ 系统缺陷时起草缺陷单，人采纳分类才算"},
    {"key": "test_report", "name": "测试报告总结", "kind": "system", "default": P.TEST_REPORT_SYSTEM,
     "where": "计划执行页「生成报告」：范围 + 程序统计的执行数据（总体 / 模块 / 优先级）+ 失败与阻塞清单（带归因与缺陷单）+ 未关闭待确认事项 → 结论（可发布 / 有条件发布 / 不可发布）、摘要、风险、遗留、建议与整份 markdown；结论与数据对不上由程序校正"},
    {"key": "dependency_infer", "name": "依赖关系识别", "kind": "system", "default": P.DEPENDENCY_INFER_SYSTEM,
     "where": "知识库 → 依赖关系 → AI 识别（需求功能依赖 / 用例依赖链路）"},
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
    {"key": "profile_block", "name": "注入块：项目画像", "kind": "block", "default": P.PROFILE_BLOCK,
     "where": "拆解 / 生成 / 评审 / 查漏 / 补充测试点 / 需求分析全链路注入（项目画像确认生效后）", "placeholders": ["profile"]},
    {"key": "profile_extract", "name": "项目画像提炼", "kind": "system", "default": P.PROFILE_EXTRACT_SYSTEM,
     "where": "项目 → 项目画像 → AI 提炼（自动 / 手动），草稿经管理员确认后生效"},
    # 共享片段：多条 Prompt 共用的枚举 / 清单 / 格式只写一份，正文里用「【共享:名称】」引用，prompt_text 取用时展开；改这里全部生效
    {"key": "shared_dimensions", "name": "共享片段：测试维度枚举", "kind": "shared", "default": P.SHARED_DIMENSIONS,
     "where": "正文写「【共享:维度枚举】」处展开：拆解 / 经验延伸 / 组合延伸 / 查漏 / AI 补充"},
    {"key": "shared_ui_exclusion", "name": "共享片段：UI 外观排除清单", "kind": "shared", "default": P.SHARED_UI_EXCLUSION,
     "where": "正文写「【共享:UI外观清单】」处展开：拆解 / 查漏 / AI 补充 / 测试点评审 / 用例生成 / 用例评审"},
    {"key": "shared_ref_format", "name": "共享片段：ref 格式", "kind": "shared", "default": P.SHARED_REF_FORMAT,
     "where": "正文写「【共享:ref格式】」处展开：拆解 / 经验延伸 / 组合延伸 / 查漏 / AI 补充"},
]

# 共享片段标记名 → 登记键
SHARED_KEYS = {"维度枚举": "shared_dimensions", "UI外观清单": "shared_ui_exclusion", "ref格式": "shared_ref_format"}


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
            else:
                # 元信息随代码更新；内置默认版本（version 1）的内容也随代码更新：
                # 未自定义过的 Prompt 立即生效，已自定义的保留管理员的生效版本、只刷新 v1 供回滚参考
                item.update({"name": d["name"], "kind": d["kind"], "where": d["where"]})
                v1 = next((v for v in item["versions"] if v["version_no"] == 1), None)
                if v1 is not None and v1.get("note", "").startswith("内置默认") and v1["content"] != default:
                    v1["content"] = default
                    v1["note"] = "内置默认（随代码更新）"
                    v1["created_at"] = _now()
                    self._doc.put(d["key"], item)

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

    STATES = {"builtin": "内置", "customized": "已定制", "draft": "有草稿"}

    def _state_of(self, row: dict) -> set[str]:
        states = {"customized" if row["customized"] else "builtin"}
        if row["drafts"]:
            states.add("draft")
        return states

    def _haystack(self, row: dict) -> str:
        item = self._items[row["key"]]
        texts = [row["key"], row["name"], row["where"], *row["placeholders"]]
        texts += [v["content"] for v in item["versions"]] + [v.get("note") or "" for v in item["versions"]]
        return "\n".join(texts).lower()

    def query(self, kind: str = "", state: str = "", keyword: str = "", page: int = 1, page_size: int = 0) -> dict:
        """列表筛选：类型 / 定制状态 / 关键词（名称、键、场景、占位符、各版本内容与说明），计数按全集统计。
        page_size=0 不分页。"""
        rows = self.list()
        kind_counts = {k: sum(1 for r in rows if r["kind"] == k) for k in ("system", "user", "block", "shared")}
        state_counts = {k: sum(1 for r in rows if k in self._state_of(r)) for k in self.STATES}
        if kind:
            rows = [r for r in rows if r["kind"] == kind]
        if state:
            rows = [r for r in rows if state in self._state_of(r)]
        if keyword.strip():
            kw = keyword.strip().lower()
            rows = [r for r in rows if kw in self._haystack(r)]
        total = len(rows)
        if page_size:
            pages = max(1, -(-total // page_size))
            page = max(1, min(page, pages))
            rows = rows[(page - 1) * page_size:page * page_size]
        return {"prompts": rows, "total": total, "page": page if page_size else 1, "page_size": page_size,
                "kind_counts": kind_counts, "state_counts": state_counts, "states": self.STATES}

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

    def shared_blocks(self) -> dict[str, str]:
        """共享片段当前生效的正文（标记名 → 文本），供展开。"""
        return {mark: self.active(key)["content"] for mark, key in SHARED_KEYS.items() if key in self._items}

    def expanded(self, key: str, content: str | None = None) -> str:
        """Prompt 正文展开共享片段后的全文（管理页展示、模型实际收到的就是它）。"""
        return P.expand_shared(self.active(key)["content"] if content is None else content, self.shared_blocks())

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
    """取生效 Prompt 并登记版本到调用上下文；未初始化（脚本/单测直接调用）时回退内置默认。
    正文里的「【共享:名称】」标记在这里展开成共享片段的生效正文（共享片段的版本也一并登记）。"""
    from app.llm.calllog import note_prompt

    d = next(x for x in PROMPT_DEFS if x["key"] == key)
    if _current is None:
        from app.parsers.image import VISION_PROMPT

        content, version = (d["default"] if d["default"] is not None else VISION_PROMPT), 1
    else:
        content, version = _current.text(key)
    note_prompt(key, version, d["name"], d["kind"])
    if d["kind"] != "shared" and P.SHARED_MARK_RE.search(content):
        blocks = {}
        for mark, skey in SHARED_KEYS.items():
            if _current is None:
                blocks[mark] = P.SHARED_BLOCKS[mark]
            else:
                blocks[mark], sv = _current.text(skey)
                note_prompt(skey, sv, "共享片段", "block")
        content = P.expand_shared(content, blocks)
    return content

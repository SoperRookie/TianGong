"""三角色最小编排（M1）：拆解 → 生成 → 评审 → 修正回环（≤3 轮）→ 输出。

有向图预定义（LangGraph StateGraph），非自由对话；
生成与评审强制分离，评审不合格项带具体问题定点打回。
"""

import json
import re

from langgraph.graph import END, StateGraph
from loguru import logger
from pydantic import ValidationError

from app.agents.json_utils import LLMOutputError, extract_json
from app.agents.context import profile_block
from app.agents.prompts import DATA_GUARD, wrap_data
from app.prompts import prompt_text
from app.agents.state import MAX_REVIEW_ROUNDS, OrchestrationState
from app.llm.client import LLMClient
from app.templates import CustomTemplate, TestCase, builtin_default_template


def _dump(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1)


def _compact(data) -> str:
    """紧凑序列化：用于定点修正时的用例全集（省输入 token，也避免模型模仿缩进格式输出）。"""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


_CASE_ID_SEQ_RE = re.compile(r"^(.*?)(\d+)\s*$")


_UNSAFE_ID_RE = re.compile(r"[<>\"'\\\x00-\x1f]")


def renumber_case_ids(cases: list[dict]) -> None:
    """模块内重编号：各模块 case_id 从 001 连续。

    前缀由模块名派生（TC-{模块}-），避免模型把同一模块写成不同前缀造成跨模块重号；
    模块名中的引号/尖括号等会进入页面内联属性的字符一律替换，最后做全局唯一兜底。
    """
    counters: dict[str, int] = {}
    seen: set[str] = set()
    for case in cases:
        module = _UNSAFE_ID_RE.sub("_", str(case.get("module", ""))).strip() or "未分组"
        case["module"] = module
        counters[module] = counters.get(module, 0) + 1
        m = _CASE_ID_SEQ_RE.match(str(case.get("case_id", "")))
        prefix = m.group(1) if (m and module in m.group(1)) else f"TC-{module}-"
        prefix = _UNSAFE_ID_RE.sub("_", prefix)
        cid = f"{prefix}{counters[module]:03d}"
        while cid in seen:  # 兜底：绝不允许重号（重号会让审核操作作用到错误用例）
            counters[module] += 1
            cid = f"{prefix}{counters[module]:03d}"
        seen.add(cid)
        case["case_id"] = cid


# UI 检查类用例（只看外观、不验证业务结果）不在生成范围内：提示词已禁止，这里按标题做保守兜底过滤。
# 只匹配明确的外观词；「显示 / 展示 / 文案 / 置灰 / 隐藏」等常与业务结果同现的词不列入，避免误删。
_UI_CHECK_RE = re.compile(
    r"样式|布局|排版|字体|字号|字重|颜色|配色|色值|对齐|间距|边距|像素|px\b|圆角|阴影|透明度|"
    r"图标(?:显示|样式|大小|尺寸)|动效|动画效果|过渡效果|分辨率|机型适配|屏幕适配|横竖屏|"
    r"UI\s*(?:检查|展示|显示|效果|样式)|界面(?:展示|显示|效果)(?:正常|正确)|页面(?:展示|显示|效果)(?:正常|正确)|"
    r"视觉|美观|高亮(?:样式|颜色)|响应式",
    re.IGNORECASE,
)


def is_ui_check_case(case: dict) -> bool:
    """标题命中外观检查词即判为 UI 检查类（标题是测试目的的浓缩，步骤 / 预期里出现外观词不算）。"""
    return bool(_UI_CHECK_RE.search(str(case.get("title", ""))))


def drop_ui_check_cases(cases: list[dict]) -> tuple[list[dict], list[dict]]:
    """返回 (保留, 剔除)。剔除后由调用方重编号。"""
    kept, removed = [], []
    for case in cases:
        (removed if is_ui_check_case(case) else kept).append(case)
    return kept, removed


def merge_fix(current: list[dict], data: dict) -> list[dict]:
    """增量合并定点修正结果（PRD 增量更新）：改动用例按 case_id 覆盖原用例，
    其余原样保留（不依赖模型复述——大用例集下全量回传必然超输出上限）；
    新增用例追加，deleted 列表删除，最后统一重排编号。"""
    existing = {str(c.get("case_id")) for c in current}
    changed: dict[str, dict] = {}
    added: list[dict] = []
    for c in data.get("cases", []):
        if not isinstance(c, dict):
            continue
        cid = str(c.get("case_id"))
        # 只有编号存在于当前用例集才视为"修改"；其余一律按新增处理（模型算错编号不得覆盖原用例）
        if cid in existing:
            changed[cid] = c
        else:
            added.append(c)
    deleted = {str(x) for x in data.get("deleted", [])}
    merged: list[dict] = []
    for case in current:
        case_id = str(case.get("case_id"))
        if case_id in deleted:
            changed.pop(case_id, None)  # 既删又改：按删除处理，不再加回
            continue
        new = changed.get(case_id)
        if new is None:
            merged.append(case)
            continue
        # 模型只覆盖内容字段；uid / 来源测试点 / 来源类型 等系统字段保留，内容有变则版本 +1
        keep = {k: case[k] for k in ("uid", "point_ids", "source") if k in case}
        fused = {**case, **new, **keep}
        if _content(fused) != _content(case):
            fused["version"] = int(case.get("version", 1) or 1) + 1
        else:
            fused["version"] = case.get("version", 1)
        merged.append(fused)
    for c in added:
        c.setdefault("point_ids", [])
        c.setdefault("source", "ai")
        merged.append(c)
    renumber_case_ids(merged)
    return merged


def _content(case: dict) -> dict:
    return {k: v for k, v in case.items() if k not in ("uid", "version", "case_id")}


# 输出被 max_tokens 截断或格式异常时的节点级重试次数
_JSON_RETRIES = 1


async def _chat_json(llm: LLMClient, messages: list[dict], model: str | None, **overrides) -> tuple[dict, "object"]:
    """调用 LLM 并解析 JSON；解析失败自动重试（输出截断/格式异常兜底）。overrides 透传给 chat（如 reasoning_effort）。"""
    from app.llm.calllog import used_prompts

    last_error: Exception | None = None
    if messages and messages[0].get("role") == "system" and DATA_GUARD not in messages[0]["content"]:
        messages = [{**messages[0], "content": messages[0]["content"] + DATA_GUARD}, *messages[1:]]
    noted = dict(used_prompts.get())  # 重试时恢复 Prompt 登记，调用日志不丢版本归属
    for attempt in range(1 + _JSON_RETRIES):
        if attempt:
            used_prompts.set(dict(noted))
        result = await llm.chat(messages, model=model, **overrides)
        try:
            return extract_json(result.content), result
        except LLMOutputError as e:
            last_error = e
            if result.finish_reason == "length":
                logger.warning(
                    "模型输出被 max_tokens 截断（{}，输出 {} 字），不再原样重试——"
                    "请缩小单次生成范围或调大 max_tokens", result.model_name, len(result.content),
                )
                break
            logger.warning("模型输出 JSON 解析失败（{}，输出 {} 字），重试", result.model_name, len(result.content))
    raise last_error


_CASE_SEQ_RE = re.compile(r"(\d+)\s*$")


def rule_check(cases: list[dict], template: CustomTemplate | None = None) -> list[dict]:
    """规则校验（评审 Agent 的确定性部分）：模板合规（F-4-5）+ 编号唯一 + 模块内编号连续。"""
    issues: list[dict] = []
    seen_ids: set[str] = set()
    module_seqs: dict[str, list[int]] = {}
    for i, raw in enumerate(cases):
        case_id = str(raw.get("case_id", f"<第{i + 1}条>"))
        try:
            TestCase.model_validate(raw, context={"require_expected": True})
        except ValidationError as e:
            problems = "; ".join(err["msg"] for err in e.errors())
            issues.append({"case_id": case_id, "problem": f"模板校验不通过: {problems}"})
        if template is not None:
            issues.extend({"case_id": case_id, "problem": p} for p in _template_check(raw, template))
        if case_id in seen_ids:
            issues.append({"case_id": case_id, "problem": "用例编号重复"})
        seen_ids.add(case_id)
        m = _CASE_SEQ_RE.search(case_id)
        if m:
            module_seqs.setdefault(str(raw.get("module", "")), []).append(int(m.group(1)))
    for module, seqs in module_seqs.items():
        expected = list(range(1, len(seqs) + 1))
        if sorted(seqs) != expected:
            issues.append(
                {
                    "case_id": f"<模块:{module}>",
                    "problem": f"模块「{module}」用例编号不连续（应为 001-{len(seqs):03d}，实际序号 {sorted(seqs)}），请重新整理编号",
                }
            )
    return issues


def _template_check(raw: dict, template: CustomTemplate) -> list[str]:
    """自定义模板约束（F-4-5）：优先级枚举、必填自定义列、自定义列取值枚举。"""
    problems: list[str] = []
    priority = str(raw.get("priority", "")).upper()
    allowed = template.priority_enum()
    if priority and priority not in allowed:
        problems.append(f"优先级 {priority} 不在模板允许范围 {allowed}")
    extras = raw.get("extras") or {}
    for col in template.custom_columns():
        value = str(extras.get(col.name, "") or "").strip()
        if col.required and not value:
            problems.append(f"模板必填字段「{col.name}」缺失，请写入 extras[\"{col.name}\"]")
        if value and col.enum_values and value not in col.enum_values:
            problems.append(f"字段「{col.name}」取值 {value} 不在枚举 {col.enum_values} 中")
    return problems


async def outline_requirement(llm: LLMClient, requirement: str, model: str | None) -> list[str]:
    """长需求的全文大纲：先用整篇文本列出功能模块清单，分片拆解时注入，保证模块命名一致、不漏模块。"""
    data, _ = await _chat_json(
        llm,
        [
            {"role": "system", "content": prompt_text("requirement_outline")},
            {"role": "user", "content": wrap_data("需求原文", requirement)},
        ],
        model,
        reasoning_effort="low",
    )
    return [str(m).strip() for m in data.get("modules", []) if str(m).strip()]


def _blind_spots(data: dict) -> list[str]:
    """盲区列表：兼容键名别名与对象形态（{"description": ...}）。"""
    keys = ("blind_spots", "blindSpots", "blind_spot", "盲区", "需求盲区")
    raw = next((data[k] for k in keys if isinstance(data.get(k), list)), None)
    if raw is None:  # 与模块列表一样兼容一层 result / data 包装
        for w in ("result", "data", "output", "analysis", "response"):
            inner = data.get(w)
            if isinstance(inner, dict):
                raw = next((inner[k] for k in keys if isinstance(inner.get(k), list)), None)
                if raw is not None:
                    break
    out: list[str] = []
    raw = raw or []
    for b in raw:
        if isinstance(b, dict):
            b = next((v for k in ("description", "desc", "point", "text", "content", "question", "盲区") if isinstance((v := b.get(k)), str)), "")
        if isinstance(b, (str, int, float)) and str(b).strip():
            out.append(str(b).strip())
    return out


async def analyze_requirement(
    llm: LLMClient, requirement: str, model: str | None, knowledge_cases: str | None = None,
    outline: list[str] | None = None, chunk_no: int = 0, chunk_total: int = 0, allow_empty: bool = False,
    req_title: str | None = None,
) -> dict:
    """需求分析 Agent：测试点拆解 + 盲区识别（拆解确认流程 F-3-3 亦单独调用）。

    knowledge_cases：测试用例库检索结果，拆解阶段注入做覆盖度查漏（PRD 检索时机约束）。
    outline / chunk_no：长需求分片拆解时注入全文模块大纲与分片序号，模块命名与大纲一致、只拆本片内容。
    req_title：多条需求合并设计时本片所属的需求标题，模块名与 ref 都带它作前缀（两份整理稿章节号各自从 1 起，不带就分不清）。
    allow_empty：分片模式下允许本片为空（目录 / 修订记录 / 术语表等不含可测内容），返回空测试点而不报错；
    非分片模式下空结果先重试一次再报错（拆解是最贵的一步，偶发空输出不该直接让任务失败）。
    """
    user = wrap_data("需求原文", requirement)
    if req_title:
        user = (f"本片属于需求「{req_title}」（本次设计合并了多条需求，各自的章节编号都从 1 开始）：模块名统一带「{req_title}-」前缀，"
                f"ref 写成「{req_title} › 页面或章节 › 需求点」。\n") + user
    if outline:
        user = (f"这是一份长需求的第 {chunk_no}/{chunk_total} 片。全文模块大纲如下（模块命名必须与大纲一致；"
                f"只拆解本片实际包含的内容，本片未涉及的模块不要凭空生成；大纲只约束模块归属、不限制测试点数量，"
                f"本片里大纲没单列的小标题归入它所在页面的模块）：\n{wrap_data('全文模块大纲', chr(10).join(outline))}\n\n") + user
    user += profile_block()  # 项目画像：统一术语与模块命名，带着既有业务认知拆解
    if knowledge_cases:
        user += prompt_text("knowledge_cases_block").format(knowledge=wrap_data("历史用例知识", knowledge_cases))
    from app.tasks.points import extract_modules, normalize_points

    messages = [
        {"role": "system", "content": prompt_text("analyst")},
        {"role": "user", "content": user},
    ]
    modules: list[dict] = []
    for attempt in range(2):
        # 拆解是理解需求的关键一步：推理型模型用最高强度
        data, result = await _chat_json(llm, messages, model, reasoning_effort="high")
        modules = [m for m in normalize_points(extract_modules(data)) if m["points"]]
        if modules or allow_empty or attempt:
            break
        logger.warning("需求拆解返回空结果（{}），重试一次", result.model_name)
    logger.info(
        "需求分析完成：{} 个模块 / {} 个测试点 / {} 条盲区",
        len(modules), sum(len(m.get("points", [])) for m in modules), len(data.get("blind_spots", [])),
    )
    if not modules and allow_empty:
        logger.warning("分片 {}/{} 未拆出测试点（可能不含可测内容），跳过该片", chunk_no, chunk_total)
        return {"test_points": [], "blind_spots": _blind_spots(data), "model_name": result.model_name, "empty": True}
    if not modules:
        keys = "、".join(map(str, data.keys())) if isinstance(data, dict) else type(data).__name__
        head = (result.content or "").strip().replace("\n", " ")[:300]
        logger.error("需求拆解未解析出测试点：模型 {}，finish_reason={}，顶层键 [{}]，输出 {} 字：{}",
                     result.model_name, getattr(result, "finish_reason", None), keys, len(result.content or ""), head)
        raise LLMOutputError(
            f"需求拆解未输出任何测试点（模型 {result.model_name} 返回结构异常：顶层键 [{keys}]，"
            f"finish_reason={getattr(result, 'finish_reason', None)}；输出开头：{head[:120]}）"
        )
    return {
        "test_points": modules,
        "blind_spots": _blind_spots(data),
        "model_name": result.model_name,
    }


def build_graph(
    llm: LLMClient,
    template: CustomTemplate | None = None,
    knowledge_refs: str | None = None,
    knowledge_cases: str | None = None,
    memory_notes: str | None = None,
    rule_notes: str | None = None,
):
    """knowledge_refs：需求文档/规则库知识，生成前注入生成 Agent；
    knowledge_cases：历史用例，只注入拆解与评审 Agent（PRD 上下文隔离约束）；
    memory_notes：用户偏好与项目记忆（F-8-7），独立预算注入生成 Agent；
    rule_notes：学习规则库中已确认生效的团队/项目规则（需求三十九），注入生成 Agent。"""
    template = template or builtin_default_template()
    async def analyze(state: OrchestrationState) -> dict:
        analysis = await analyze_requirement(
            llm, state["requirement"], state.get("model"), knowledge_cases=knowledge_cases
        )
        trace = state.get("trace", []) + [{"agent": "需求分析", "model": analysis["model_name"]}]
        return {
            "test_points": analysis["test_points"],
            "blind_spots": analysis["blind_spots"],
            "trace": trace,
        }

    async def generate(state: OrchestrationState) -> dict:
        system = prompt_text("generator").format(template_spec=template.prompt_spec())
        if state.get("issues"):
            # 定点修正：携带评审问题与当前用例全集（紧凑格式），只回传改动部分
            user = prompt_text("fix_instruction").format(
                issues=_dump(state["issues"]), cases=_compact(state["cases"])
            )
            action = "定点修正"
        else:
            user = (
                f"需求内容：\n{wrap_data('需求原文', state['requirement'])}\n\n"
                f"测试点拆解结果：\n{wrap_data('测试点', _dump(state['test_points']))}\n\n"
                "请为上述全部测试点生成详细测试用例。"
            )
            if knowledge_refs:
                user += prompt_text("knowledge_refs_block").format(knowledge=wrap_data("需求/规则知识", knowledge_refs))
            action = "全量生成"
        user += profile_block()
        if memory_notes:
            user += prompt_text("memory_block").format(memories=wrap_data("偏好记忆", memory_notes))
        if rule_notes:
            user += prompt_text("rules_block").format(rules=wrap_data("团队规则", rule_notes))
        data, result = await _chat_json(
            llm,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            state.get("model"),
        )
        trace = state.get("trace", []) + [{"agent": "用例生成", "action": action, "model": result.model_name}]
        if action == "定点修正":
            cases = merge_fix(state["cases"], data)
            logger.info(
                "用例生成（定点修正）：改动 {} 条 / 删除 {} 条，合并后共 {} 条",
                len(data.get("cases", [])), len(data.get("deleted", [])), len(cases),
            )
        else:
            cases = [c for c in (data.get("cases") or []) if isinstance(c, dict)]
            if not cases:
                raise LLMOutputError("用例生成未输出任何用例（模型返回结构异常或被截断）")
            logger.info("用例生成（全量）：{} 条", len(cases))
        cases, removed = drop_ui_check_cases(cases)
        if removed:
            renumber_case_ids(cases)  # 剔除后保持模块内编号连续，否则规则校验会报编号不连续
            titles = [str(c.get("title", "")) for c in removed]
            logger.info("剔除 UI 检查类用例 {} 条：{}", len(removed), titles)
            trace.append({"agent": "UI 检查过滤", "removed": len(removed), "titles": titles})
            if not cases:
                raise LLMOutputError("用例生成结果全部为 UI 检查类用例（不在生成范围内），没有可用用例")
        return {"cases": cases, "trace": trace}

    async def review(state: OrchestrationState) -> dict:
        issues = rule_check(state["cases"], template)
        current_round = state.get("review_rounds", 0) + 1
        # 按模块并行的实例只评审自己负责的模块，遗漏判断限定范围，避免误报其他模块
        modules = [str(tp.get("module", "")) for tp in state.get("test_points", [])]
        scope = "、".join(m for m in modules if m and m != "(修订)")
        scope_line = (
            f"本次评审范围仅限模块「{scope}」：遗漏场景只评估该范围，其余模块由其他实例负责。\n\n"
            if scope else ""
        )
        prior = ""
        if state.get("issues"):
            prior = f"\n\n上一轮评审问题（本轮重点核对是否已修复）：\n{_dump(state['issues'])}"
        prior += profile_block()
        if knowledge_cases:
            # 历史用例注入评审 Agent 辅助覆盖度把关（PRD：用例库进评审与拆解，不进生成）
            prior += prompt_text("knowledge_cases_block").format(knowledge=wrap_data("历史用例知识", knowledge_cases))
        data, result = await _chat_json(
            llm,
            [
                {"role": "system", "content": prompt_text("reviewer")},
                {
                    "role": "user",
                    "content": (
                        f"{scope_line}本次为第 {current_round} 轮评审。\n\n"
                        f"需求内容：\n{wrap_data('需求原文', state['requirement'])}\n\n"
                        f"待评审用例：\n{wrap_data('待评审用例', _dump(state['cases']))}{prior}"
                    ),
                },
            ],
            state.get("reviewer_model") or state.get("model"),
            reasoning_effort="high",
        )
        missing = data.get("missing", [])
        if not data.get("passed", False):
            issues.extend(data.get("issues", []))
            # 遗漏场景转为可修正问题，让生成 Agent 补用例
            issues.extend({"case_id": "(新增)", "problem": f"补充遗漏场景: {m}"} for m in missing)
        rounds = state.get("review_rounds", 0) + 1
        passed = not issues
        trace = state.get("trace", []) + [
            {"agent": "评审", "model": result.model_name, "round": rounds, "passed": passed}
        ]
        update: dict = {
            "issues": issues,
            "missing": missing,
            "suggestions": state.get("suggestions", []) + data.get("suggestions", []),
            "passed": passed,
            "review_rounds": rounds,
            "trace": trace,
        }
        logger.info(
            "评审第 {} 轮：{}（问题 {} 条 / 遗漏 {} 条 / 建议 {} 条）",
            rounds, "通过" if passed else "打回", len(issues), len(missing), len(data.get("suggestions", [])),
        )
        if not passed and rounds >= MAX_REVIEW_ROUNDS:
            # 回环超限强制出稿，未解决项显式标注供人工重点关注
            update["unresolved"] = issues
            logger.warning("评审 {} 轮未收敛，强制出稿，未解决项 {} 条", rounds, len(issues))
        return update

    def decide(state: OrchestrationState) -> str:
        if state["passed"] or state["review_rounds"] >= MAX_REVIEW_ROUNDS:
            return "done"
        return "fix"

    graph = StateGraph(OrchestrationState)
    graph.add_node("analyze", analyze)
    graph.add_node("generate", generate)
    graph.add_node("review", review)
    # 已确认测试点的任务（拆解确认流程 F-3-3）直接从生成开始，不重复拆解
    graph.set_conditional_entry_point(
        lambda s: "generate" if s.get("test_points") else "analyze",
        {"analyze": "analyze", "generate": "generate"},
    )
    graph.add_edge("analyze", "generate")
    graph.add_edge("generate", "review")
    graph.add_conditional_edges("review", decide, {"fix": "generate", "done": END})
    return graph.compile()

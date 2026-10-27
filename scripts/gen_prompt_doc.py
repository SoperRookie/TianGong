"""重新生成 docs/Prompt清单与缺口设计.md 的第 0～3 部分（机制 / 链路 / 总表 / 逐条全文）：Prompt 全文从 app/prompts.py 登记表读取，
与代码一致；第 4 部分起（横向评审、缺口设计、落地顺序）是手写的，脚本原样保留。改了 Prompt 默认文本后运行：
    python3 scripts/gen_prompt_doc.py
新增 Prompt 时在 META 里补一行说明并加进 ORDER，否则脚本报错提醒。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.prompts import PROMPT_DEFS  # noqa: E402
from app.parsers.image import VISION_PROMPT  # noqa: E402

# key → (阶段, 调用位置, 推理强度 / 模型, 输入, 输出与程序兜底, 设计意图)
META = {
    "requirement_extract": ("需求中心 · 提取", "`app/agents/extraction.py` `run_requirement_extraction`，按「文件 › 章节」切单元并行调用", "medium；`TIANGONG_REQUIREMENT_EXTRACT_MODEL`（空则任务默认模型）",
        "单元原文（Axure 行带控件编号、流程行、独立区域 / 流程图标记）、项目画像；多份时注明第 i/N 份与页面路径", "`{modules:[{name, items:[{text, sources, doubt?, draft?}]}], dropped}`；程序 `_merge_modules` 去重合并、`enforce_block_names` 区块名必须来自材料、`verify_modules` 逐条对照材料、底稿 `account` 双向核验",
        "逐字摘录不改写，宁多勿漏；模块名「页面-区块」且区块名必须是材料里原有的小标题。"),
    "requirement_extract_gap": ("需求中心 · 提取补漏", "`extraction.py` `_gap_fill`，底稿待处理单元按页面分批（≤ 15000 字）并行，最多 2 轮", "medium",
        "待处理原句（带单元编号与「整理稿已有其中 N%」）+ 已有模块清单", "`{add:[{uid, module, text, sources, draft?}], skip:[{uid, reason}]}`；每句必须出现在 add / skip 之一，没判的程序按原句兜底收进「页面-待归类」",
        "判定标准只有一条：这句能不能直接支撑写用例。拿不准 add。"),
    "requirement_extract_crosscheck": ("需求中心 · 提取交叉核对", "`extraction.py` `_crosscheck`，整稿一次（≤ 15 万字），开关 `TIANGONG_REQUIREMENT_EXTRACT_CROSSCHECK`", "medium",
        "全部条目带 id", "`{doubts:[{id, doubt}], drafts:[{module, reason}]}`；只加疑点与草稿标记，不改条目；出错只跳过",
        "单页看不出的跨页面矛盾与遗留草稿，最后整稿过一遍。"),
    "requirement_analysis": ("需求中心 · AI 分析", "`app/agents/analysis.py` `run_requirement_analysis`，长文分片并行", "high",
        "整理稿（或原始材料）分片 + 大纲 + 画像 + 需求 / 规则知识", "11 项，前 10 项每条 `{text, quote}`；`quote_found` 程序逐条核验 quote，对不上的降级为待确认事项；open_questions 分 must / suggest、带 module",
        "严禁脑补：规则 / 边界 / 状态 / 权限四项无原文依据不得写，改写入待确认。"),
    "requirement_analysis_review": ("需求中心 · 分析复核", "`analysis.py` `review_analysis`，开关 `TIANGONG_ANALYSIS_REVIEW`", "high；`reviewer_model or model`",
        "原文 + 核验后的条目清单 + 画像", "`{issues:[{key, n, verdict: wrong|unsupported, fix, note}], missing:[{key, text, quote}]}`；意见标在条目上由人拍板，不改条目",
        "独立第二人只找理解错 / 无依据 / 遗漏三类，只报有把握的。"),
    "question_suggest": ("需求中心 · 推荐答案", "`quality.run_question_suggest` ← AI 分析完成后自动（配置 `TIANGONG_QUESTION_SUGGEST`）/ 待确认事项页「推荐答案」`POST /requirements/{id}/questions/suggest`；≤ 40 条一批并行", "默认；分析模型",
        "原文（整理稿 + 人工补充 + 已确认结论，不含 AI 分析要点）+ 本批未确认事项（question / reason / module / level / assumption）+ 同项目其他需求已确认的相似问题结论（问法相似 ≥ 0.3，每条 ≤ 3 条）+ 画像", "`{suggestions:[{index, answer, basis: 原文|历史结论|行业惯例, evidence, risk, confidence: 高|中|低}]}`；index 只认批内编号、一条一个答案、空答案丢；basis=原文 的 evidence 用 `quote_found` 核验，对不上降为「行业惯例」并标 `unverified`；写到事项 `suggested_answer`，页面预填、人确认才算，确认 / 采纳后撤掉；某批故障只丢该批",
        "答案必须是能直接写进预期结果的确定值；按原文 › 历史结论 › 行业惯例依次找，每条带选它的风险；不替需求方拍板「无需确认」。"),
    "analysis_judge": ("需求分析评测", "`app/analysis_eval.py` `judge_sample`", "high",
        "标准答案（人工确认）与 AI 输出", "`{项: {matched, wrong, extra, missing}}`；程序先把规范化相同的条目预配对，裁判漏报的补回；`score_alignment` 算准确率 / 捏造率 / 理解错率 / 遗漏率",
        "语义对齐而非文字对齐，归类不同的同一事实算匹配。"),
    "requirement_outline": ("测试设计 · 拆解前", "`graph.py` `outline_requirement`，仅当需求不是整理稿（标题大纲不足 3 个模块）时调用", "low",
        "全文（≤ 12 万字，超出取每片开头）", "`{modules:[...]}`；整理稿由 `outline_from_brief` 程序按标题派生，不调模型",
        "只列模块不拆点，给分片拆解统一模块命名。"),
    "analyst": ("测试设计 · 拆解", "`graph.py` `analyze_requirement` ← `service.run_analysis` / `_analyze_then_generate`，分片并行", "high",
        "需求分片（先过 `clean_manual_sections`：流程图围栏换说明行、人工收录小节清洗）+ 全文大纲 + 片号 + 需求前缀 + 画像 + 历史用例 / 缺陷知识", "`{modules:[{module, points:[{point, dimension, ref}]}], blind_spots:[{text, module, ref}]}`；`normalize_points` 兼容别名、`dedupe_module_points` 同模块同文去重、空结果重试一次、`coarse_warnings` 粗粒度提示",
        "一条测试点只验一个结果、不许合写；逐句覆盖宁多勿漏；不拆 UI 外观；模块 = 需求页面 / 功能点；每条带 ref 可回到需求页。"),
    "point_extend": ("测试设计 · 经验延伸", "`graph.py` `extend_points` ← `service._analyze_and_extend`（每片拆完紧接着）；测试点页「经验延伸」`POST /tasks/{id}/points/extend`；开关 `TIANGONG_POINT_EXTENSION`", "high",
        "本片原文 + 刚拆出的全部明文测试点 + 画像 + 历史用例 / 缺陷知识", "`{extensions:[{module, point, dimension, ref, basis}]}`；模块归位到已有模块、与已有点相似 ≥ 0.75 跳过、没 basis 跳过、总数 ≤ 明文点 1/3（≥ 3）；`source=derived` 待审核",
        "只延伸不复述、不给需求没有的功能加需求；每条带「为什么要测」；拆解与延伸分两次调用，职责分开。"),
    "point_combine": ("测试设计 · 组合延伸", "`quality.run_point_combine` ← 拆解后自动 `_run_point_quality_checks`（查漏之后、查重之前，配置 `TIANGONG_POINT_COMBINE`）/ 测试点页「组合延伸」`POST /tasks/{id}/points/combine`（body.modules 只跑指定模块）；每个模块（未驳回点 ≥ 3）一次调用，并发 6", "high",
        "该模块所在章节的原文（按 `gap_batches` 定位，对不上用整篇）+ 该模块全部测试点（tp_id / point / dimension / ref / basis）+ 画像", "`{combinations:[{point, dimension, ref, basis, from:[tp_id…]}]}`；模块固定为被处理的那个；`from` 只认本模块编号且 ≥ 2（不足的是单点不是组合）；没 basis 跳过；与本模块已有点相似 ≥ 0.75 跳过；每模块 ≤ 8 条；`source=combined` 带 `combo_from` 待审核；某模块故障只丢该模块",
        "单条规则各自成立不等于叠在一起也成立：条件组合 / 流程串联 / 状态×角色×端 / 规则优先级 / 数据流转五种方法只在模块内找交叉场景；宁少勿错、条条有依据。"),
    "blind_spot_verify": ("测试设计 · 盲区复核", "`quality.run_blind_spot_verify`，多片时每片对照全部盲区各一次", "默认",
        "一片原文 + 全部盲区清单", "`{explained:[{index, evidence}]}`；任一片判已说明即剔除并记 `resolved_blind_spots`",
        "前片标的盲区后片可能已说明；只有原文明确写出才算。"),
    "gap_check": ("测试设计 · 覆盖查漏", "`quality.run_gap_check` ← 拆解后自动 `_run_point_quality_checks` / 手动 `POST .../points/gap-check`；按章节分批（≤ 8000 字）并行", "默认",
        "本批原文 + 本批章节相关的测试点（模块名对章节、ref 章节号投票）+ 画像", "`{coverage:{维度: 已覆盖|未覆盖|不适用|待确认}, additions:[{module, point, dimension, ref, reason}]}`；`merge_coverage` 合并后 `apply_additions_to_coverage` 把补的算进去并记 `coverage_notes`；补点 `source=gap`，相似 ≥ 0.75 跳过，模块 `resolve_module` 归位",
        "只新增不改存量；逐句查漏为主、维度矩阵只是概览；不得为需求未涉及的功能编造。"),
    "dup_judge": ("测试设计 · 语义查重", "`quality.run_dup_judge`，bigram 候选（同页面 ≥ 0.45、跨页面 ≥ 0.7）按 40 对一批并行", "默认",
        "候选对（带两边模块）+ 需求", "`{duplicates:[{a, b, verdict, reason, suggestion}]}`；判不重复且同文的留下标「同文不同对象」；某批故障标待复核；AI 不删，人工处置",
        "同一规则在不同页面是两个测试对象；合写复述算重复、删合写的那条。"),
    "point_compare": ("测试设计 · 人工对比", "`quality.run_point_compare` ← 测试点页「人工对比」", "默认",
        "人工编写的测试点 + AI 测试点 + 需求", "`{matches, manual_only, ai_only:[{tp_id, reason, valuable}]}`；每条最多匹配一次",
        "评估 AI 覆盖度与遗漏；带 basis 的延伸点按场景合理性判价值。"),
    "point_supplement": ("测试设计 · AI 补充", "`routes.py` `POST .../points/add`（instruction 形态）", "默认",
        "需求 + 已有测试点 + 用户补充要求 + 画像", "`{additions:[{module, point, dimension, ref, reason}]}`；`add_points(source=supplement)`，相似跳过、模块归位；没 ref 的 reason 作「依据」",
        "只补要求范围内的、宁少勿错；有需求依据写 ref，经验场景写 reason；不复述、不越界、不写 UI。"),
    "point_review": ("测试设计 · 独立评审", "`quality.run_point_review` ← 拆解后自动 `_run_point_quality_checks`（查漏、查重之后，配置 `TIANGONG_POINT_REVIEW`）/ 手动 `POST .../points/ai-review`；按 `gap_batches` 分批并行", "默认",
        "本批原文 + 本批待审核的测试点（tp_id / point / dimension / ref / basis）+ 画像", "`{issues:[{tp_id, verdict, fix, note, quote}]}`；只收批内编号与七类判定、一条点一个意见；数值改写 / ref 对不上 / 超出需求的 quote 用 `quote_found` 核验，对不上标 `unverified`；`apply_ai_review` 标到 `p.ai_review`，不改测试点，人处理后撤掉",
        "宁少勿错，只报七类硬伤（合写 / 不可验证 / 数值改写 / ref 对不上 / UI 外观 / 超出需求 / 维度不符）；不改、不删、不补。"),
    "case_dup_judge": ("测试设计 · 用例查重", "`quality.run_case_dup_judge` ← 用例页「重复检查」`POST .../cases/dup-check`；`case_duplicate_candidates` 候选按 40 对一批并行", "默认",
        "候选对（两条用例的模块 / 标题 / 前置 / 步骤 / point_ids）+ 需求前 2000 字", "`{duplicates:[{a, b, verdict: 重复|可合并|不重复, reason, keep, merged_title}]}`；不重复不进列表、没判到待复核、一批故障全待复核；存 `dup_report.cases`，人工删 / 忽略 / 重开",
        "不同端 / 页面 / 角色 / 状态各验一次是两个对象；等价类不同类不算重复；AI 不删。"),
    "point_fix": ("测试设计 · 测试点驳回修改", "`quality.run_point_fix` ← 「AI 修改驳回项」，产出提案人工逐项确认", "默认",
        "被驳回测试点 + 结构化驳回信息（类型 / 原因 / 修改要求 / 范围）+ 需求", "`{fixes:[{tp_id, comment_type, action: split|modify|delete, point, dimension, split_into, note}], additions}`；`build_point_proposals` 只接受驳回列表内的 tp_id",
        "只处理被驳回项；修改要求优先级最高；按驳回类型选 split / modify / delete。"),
    "generator": ("测试设计 · 用例生成", "`graph.py` `generate` 节点 ← `service._run_single`，按模块并行（每模块一次调用）", "默认；任务模型",
        "需求原文（整份）+ 该模块测试点（tp_id / ref / basis）+ 需求与规则知识 + 画像 + 记忆 + 团队规则 + 模板字段规范 `template_spec`", "`{cases:[{case_id, module, title, priority, precondition, steps:[{action, expected}], keywords, remark, extras, point_ids}]}`；`drop_ui_check_cases` 剔 UI 用例、`renumber_case_ids`、`rule_check` 校验优先级与编号；定点修正走 `fix_instruction` + `merge_fix`",
        "六种方法论；一个点几条用例的判定规则；人话表达；触发条件必须可构造；延伸点按 basis 写用例、remark 标「经验延伸：」。"),
    "reviewer": ("测试设计 · 用例评审", "`graph.py` `review` 节点，≤ 3 轮回环，超限强制出稿标 unresolved", "默认；`reviewer_model or model`",
        "需求 + 本模块用例 + 历史用例 / 缺陷知识 + 画像", "`{passed, issues:[{case_id, problem}], missing:[...], suggestions:[...]}`；issues 与 missing 为空才通过",
        "区分阻断缺陷与建议避免评审不收敛；复审只核对上一轮问题；延伸用例不得以超出需求打回。"),
    "fix_instruction": ("测试设计 · 定点修正（user）", "`graph.py` `generate` 节点在有 issues 时作为 user 消息", "默认",
        "评审问题清单 + 当前用例全集（紧凑）", "`{cases:[改动与新增], deleted:[case_id]}`；`merge_fix` 按 case_id 合并，内容变化 version+1",
        "只回传有改动的用例，节省输出。"),
    "case_fix": ("测试设计 · 用例驳回修改 / 需求变更更新", "`quality.run_case_fix` ← 「AI 修改驳回项」；`routes.py` 需求变更后更新受影响用例", "默认",
        "被驳回用例 + 结构化驳回信息（可带指定字段 / 指定步骤）+ 需求；变更时为新版需求 + 变更清单 + 受影响用例", "`{fixes:[{case_id, comment_type, note}], cases:[...], deleted:[...]}`；`_restrict_case_fix` 对越权改动确定性还原、`merge_case_fix`",
        "只处理被驳回 / 受影响的用例；字段 / 步骤最小修改原则。"),
    "requirement_diff": ("测试设计 · 需求变更分析", "`quality.run_requirement_diff` ← 任务「需求变更」", "默认",
        "旧版需求 + 新版需求 + 现有测试点 / 用例编号", "`{changes:[{type, description, affected_points, affected_cases, action_hint}], new_requirements, summary}`",
        "变化类型八选一；删除只标记不建议自动删；受影响范围要具体不扩大。"),
    "plan_case_select": ("测试执行 · 计划挑选", "`quality.run_plan_case_select` ← 计划页「AI 挑选」`POST /plans/{id}/select`；候选 = 本项目已通过、不在计划内的正式用例，按 300 条一批并行", "默认",
        "候选精简视图（id / case_id / module / title / priority / keywords / point_ids / failed_recent）+ 目标（冒烟 / 全量回归 / 变更影响回归 / 模块专项）+ 预算 + 变更摘要（空则自动带任务的需求变更分析）+ 重点模块 + 画像；不带步骤", "`{selected:[{id, reason}], excluded_summary, coverage_note}`；只认批内 id、一条一次；合并后按 P0 › P1 › P2 › P3 再按模型顺序排、超预算截掉靠后的；冒烟目标每个有 P0 的模块至少保 1 条 P0（程序补、标 by=program）；某批故障只丢该批；草稿存 `plan.selection_draft`，人勾选后走 `POST .../cases` 加入",
        "在预算内覆盖风险最高的、不求全；四种目标各有选法；不得生成清单外的 id。"),
    "exec_triage": ("测试执行 · 失败归因", "`quality.run_exec_triage` ← 计划执行页「AI 归因」（行内单条 / 本轮全部失败）`POST /plans/{id}/runs/{rid}/triage`，每条一次调用并行；关联截图先走 `understand_image_bytes`（`EXEC_SHOT_PROMPT`，≤ 3 张）", "默认",
        "用例快照（标题 / 前置 / 步骤与预期 / 备注）+ 执行说明 + 来源测试点（tp_id / point / ref）+ 截图理解结果 + 用例所在模块的需求章节（`_module_texts` 定位，定不到用整篇前 8000 字）+ 画像", "`{triage:{category: 用例问题|系统缺陷|环境问题|数据问题|需求变更, sub, failed_step, confidence, reason}, bug:{title, severity, precondition, steps, expected, actual, scope}|null}`；category 枚举外按系统缺陷且置信度低、sub 只在用例问题下认三类、failed_step 收到步数内、bug 只在系统缺陷时留；`suggested_reason` 映射到 FAIL_REASONS，写在执行记录 `triage` 上，人「采纳」才改 `reason`（`POST .../results/{item}/triage/adopt`）",
        "先对照原文判用例的预期对不对，预期对实际错才是缺陷；证据不足置信度写低不编；缺陷单标题「模块 + 操作 + 现象」、步骤精简到失败那一步。"),
    "learning": ("学习规则", "`quality.run_learning_analysis` ← `POST /learning/analyze`，按项目分组各调一次", "默认",
        "该项目的人工修改留痕样本（≤ 80 条：评审修改 / 删除、离线终稿 diff、测试点驳回、修订指令、执行失败归因用例问题、需求分析人工修改）", "`{candidates:[{content, evidence, occurrences, confidence, scope_hint}]}`；`add_candidates` 同项目同内容去重，候选须管理员确认才生效",
        "只提炼出现 ≥ 2 次的模式；规则写成可直接注入生成的一句话指令。"),
    "profile_extract": ("项目画像", "`app/agents/profile.py` `run_profile_extract`（自动 / 手动），草稿经管理员确认后生效", "默认",
        "项目已沉淀的素材（需求、附件、已确认测试点、评审通过用例标题、确认结论）+ 当前画像", "`{background, roles, flows, glossary, rules, risks}`；每数组 ≤ 40 条",
        "只写素材里有依据的内容，禁止脑补行业通识；增补修正不无故删除。"),
    "dependency_infer": ("知识库 · 依赖识别", "`routes.py` 知识库 → 依赖关系 → AI 识别，结果为草稿人工确认", "默认",
        "同项目的需求 / 用例节点列表 `[编号] 标题 | 摘要`", "`{edges:[{from, to, relation, reason}]}`；只允许列表内编号",
        "只输出有明确依据的关系，不得编造编号。"),
    "vision_image": ("解析 · 图片理解（user）", "`app/parsers/image.py` `understand_image_bytes`（独立图片 / 文档内嵌图 / Axure 截图 / 扫描 PDF 整页），大图切块逐块理解", "Vision 模型自动路由",
        "图片 + 可选上下文一句", "Markdown 五节：图片类型 / 界面元素清单 / 交互与流程逻辑 / 业务规则与约束 / 图中文字原文；提取与底稿只取「图中文字原文」节为单元",
        "只描述图中实际存在的内容，无法辨认明确标注。"),
    "knowledge_cases_block": ("注入块", "拆解 / 延伸 / 评审阶段", "—", "历史用例与缺陷知识", "—", "只用于识别遗漏维度与历史缺陷重点，禁止照抄无关内容。"),
    "knowledge_refs_block": ("注入块", "生成 / 需求分析阶段", "—", "相关历史需求与业务规则", "—", "与当前需求冲突以当前需求为准并视为重点测试对象。"),
    "rules_block": ("注入块", "生成阶段", "—", "已确认生效的团队 / 项目规则（预算 800 字）", "—", "与需求明文冲突以需求为准。"),
    "memory_block": ("注入块", "生成阶段", "—", "用户偏好 + 项目记忆（预算 600 字）", "—", "用户反复提过的要求直接满足。"),
    "profile_block": ("注入块", "拆解 / 延伸 / 组合 / 生成 / 评审 / 查漏 / 补充 / 需求提取 / 需求分析 / 推荐答案", "—", "管理员确认生效的项目画像（≤ 2500 字）", "—", "统一术语与模块命名；画像与需求冲突以需求为准。"),
    "shared_dimensions": ("共享片段", "正文「【共享:维度枚举】」处展开：analyst / point_extend / point_combine / gap_check / point_supplement", "—", "—", "`prompt_text` 取用时 `expand_shared` 替换标记；管理页 `expanded` 字段显示展开全文", "十个维度只写一份，改一处全部生效。"),
    "shared_ui_exclusion": ("共享片段", "正文「【共享:UI外观清单】」处展开：analyst / gap_check / point_supplement / point_review / generator / reviewer", "—", "—", "同上", "UI 外观排除清单只写一份。"),
    "shared_ref_format": ("共享片段", "正文「【共享:ref格式】」处展开：analyst / point_extend / point_combine / gap_check / point_supplement", "—", "—", "同上", "ref 格式「页面或章节 › 需求点」只写一份。"),
}

ORDER = [
    ("一、需求中心", ["requirement_extract", "requirement_extract_gap", "requirement_extract_crosscheck", "requirement_analysis", "requirement_analysis_review", "question_suggest", "analysis_judge"]),
    ("二、测试设计：拆解与质量闭环", ["requirement_outline", "analyst", "point_extend", "point_combine", "blind_spot_verify", "gap_check", "dup_judge", "point_review", "point_compare", "point_supplement", "point_fix"]),
    ("三、测试设计：生成与评审", ["generator", "reviewer", "fix_instruction", "case_dup_judge", "case_fix", "requirement_diff"]),
    ("四、测试执行", ["plan_case_select", "exec_triage"]),
    ("五、沉淀与知识", ["learning", "profile_extract", "dependency_infer", "vision_image"]),
    ("六、注入块", ["knowledge_cases_block", "knowledge_refs_block", "rules_block", "memory_block", "profile_block"]),
    ("七、共享片段", ["shared_dimensions", "shared_ui_exclusion", "shared_ref_format"]),
]

defs = {d["key"]: d for d in PROMPT_DEFS}
assert set(defs) == set(META), set(defs) ^ set(META)

DATA_GUARD = __import__("app.agents.prompts", fromlist=["DATA_GUARD"]).DATA_GUARD

out: list[str] = []
w = out.append
w("# 天工 Prompt 清单与缺口设计（v2.1.4）\n")
w("> 本文第 1～3 部分由脚本从 `app/prompts.py` 登记表与 `app/agents/prompts.py` 生成，正文即代码里当前内置的默认版本（v1）。"
  "线上在「AI 中心 › Prompt 管理」里改过的 Prompt 以线上激活版本为准，点「恢复默认」回到这里的文本。第 4～5 部分（横向评审、缺口设计）为手写。\n")
w("## 0. Prompt 机制\n")
w("""- **登记与版本**：全部 Prompt 在 `app/prompts.py` 的 `PROMPT_DEFS` 登记（key / 名称 / kind / 调用位置 / 默认文本 / 占位符），`PromptStore` 为每条维护版本链（内置默认 v1 → 新建草稿 → 激活生效，可回滚到任一历史版本）。新版本必须包含默认版本的全部 `{占位符}`，否则 `.format` 会失败。
- **取用登记**：`prompt_text(key)` 返回生效内容并把 (key, version) 登记到调用上下文，任务 `prompt_versions` 与调用日志据此记录，复盘时能知道这条任务是哪个版本的 Prompt 跑出来的。
- **kind**：`system` 系统消息；`user` 用户消息模板（`fix_instruction`、`vision_image`）；`block` 注入块（追加在 user 消息末尾）；`shared` 共享片段——多条 Prompt 共用的枚举 / 清单 / 格式只写一份，正文里写「【共享:名称】」标记，`prompt_text` 取用时 `expand_shared` 展开成共享片段的生效正文（共享片段的版本一并登记），管理页 `expanded` 显示展开全文；下文第 3 部分贴的是带标记的原文。
- **策略句**：每条 Prompt 第一段固定一句「本步策略：宁多勿漏 / 宁少勿错」——提取、补漏、拆解、分析、查漏是宁多勿漏；延伸、组合、盲区复核、分析复核、查重、对比、评审、学习是宁少勿错。
- **数据边界**：每个 system 消息末尾自动追加 `DATA_GUARD`；外部内容（需求原文、知识、历史用例、审核意见、记忆、规则）一律用 `wrap_data(名称, 内容)` 包成 `<<<数据:名称>>> … <<<数据结束>>>`，Prompt 注入文字只当数据。
- **输出协议**：全部要求只输出 JSON；`_chat_json` 解析失败重试一次（推理耗尽预算、`finish_reason=length` 且无正文时也重试一次），`extract_json` 依次尝试代码块 / 第一个完整对象 / 数组包装 / 逐对象抢救。
- **推理强度**：拆解、延伸、需求分析、分析复核、评测裁判用 `high`；提取三步 `medium`；大纲 `low`；其余默认。GPT-6 / 5.6 / 5.5 温度只能 1，仅 5.4 系可设 0.3（见 `docs/GPT模型组合与预算.md`）。
- **模型选择**：任务级 `model`，评审 / 分析复核可单独 `reviewer_model`；提取可单独 `TIANGONG_REQUIREMENT_EXTRACT_MODEL`；图片走 Vision 路由。
""")
w("DATA_GUARD 原文：\n")
w("```text\n" + DATA_GUARD.strip() + "\n```\n")

w("## 1. 调用链路\n")
w("""```mermaid
flowchart LR
  A[附件解析] --> V[vision_image<br>图片理解]
  A --> E1[requirement_extract<br>逐字摘录]
  V --> E1
  E1 --> E2[requirement_extract_gap<br>底稿补漏 ≤2 轮]
  E2 --> E3[requirement_extract_crosscheck<br>整稿交叉核对]
  E3 --> R1[requirement_analysis<br>11 项带原文依据]
  R1 --> R2[requirement_analysis_review<br>独立复核]
  R2 -. 评测 .-> J[analysis_judge]
  E3 --> O[requirement_outline<br>非整理稿才调]
  O --> P1[analyst<br>明文测试点]
  R2 --> QS[question_suggest<br>推荐答案]
  P1 --> P2[point_extend<br>经验延伸]
  P2 --> B[blind_spot_verify]
  P2 --> G[gap_check<br>分批查漏]
  G --> CB[point_combine<br>模块级组合]
  CB --> D[dup_judge<br>查重]
  D --> PR[point_review<br>独立评审]
  PR --> H{人工审核}
  H -. 驳回 .-> PF[point_fix]
  H -. 人工对比 / 补充 .-> PC[point_compare / point_supplement]
  H --> GEN[generator<br>按模块生成]
  GEN --> REV[reviewer ≤3 轮]
  REV -. 不通过 .-> FIX[fix_instruction → generator]
  GEN --> CR{用例审核}
  CR -. 驳回 .-> CF[case_fix]
  CR -. 需求变更 .-> RD[requirement_diff → case_fix]
  CR --> PS[plan_case_select<br>AI 挑选] --> EX[计划执行] -. 失败 .-> TR[exec_triage<br>归因 + 缺陷单]
  CR --> L[learning<br>按项目提炼规则]
  L -. rules_block .-> GEN
  PRO[profile_extract] -. profile_block .-> P1
  PRO -. profile_block .-> GEN
  K[知识库] -. knowledge_cases_block .-> P1
  K -. knowledge_refs_block .-> GEN
  DEP[dependency_infer]
```
""")

w("## 2. 总表\n")
w("| key | 名称 | 阶段 | 调用位置 | 推理 / 模型 | 字数 |\n|---|---|---|---|---|---|")
for _, keys in ORDER:
    for k in keys:
        d = defs[k]; m = META[k]
        text = d["default"] if d["default"] is not None else VISION_PROMPT
        w(f"| `{k}` | {d['name']} | {m[0]} | {m[1]} | {m[2]} | {len(text)} |")
w("")

w("## 3. 逐条：说明与全文\n")
for title, keys in ORDER:
    w(f"### {title}\n")
    for k in keys:
        d = defs[k]; m = META[k]
        text = d["default"] if d["default"] is not None else VISION_PROMPT
        w(f"#### `{k}` · {d['name']}\n")
        w(f"- **kind**：{d['kind']}{'　**占位符**：' + '、'.join('`{' + p + '}`' for p in d.get('placeholders') or []) if d.get('placeholders') else ''}")
        w(f"- **阶段**：{m[0]}")
        w(f"- **调用位置**：{m[1]}")
        w(f"- **推理强度 / 模型**：{m[2]}")
        w(f"- **输入**：{m[3]}")
        w(f"- **输出与程序兜底**：{m[4]}")
        w(f"- **设计意图**：{m[5]}")
        w("")
        w("```text\n" + text.strip("\n") + "\n```\n")

DOC = ROOT / "docs/Prompt清单与缺口设计.md"
existing = DOC.read_text() if DOC.exists() else ""
mark = "## 4. "
tail = existing[existing.index(mark):] if mark in existing else Path(__file__).with_name("prompt_doc_tail.md").read_text()
w(tail)
DOC.write_text("\n".join(out))
print("written", sum(len(x) for x in out), "chars")

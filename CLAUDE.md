# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**在本仓库中始终使用中文回复用户。**

**Git 提交信息与 PR 描述中不得包含任何 AI 相关的署名或标识**（如 `Co-Authored-By: Claude`、`Generated with Claude Code` 等），提交信息使用中文。

## 项目状态

本仓库（tiangong）是"AI 测试用例生成 Agent（TestCase Agent）"项目，2026-07-13 开工，按 M1–M6 里程碑推进。**截至 2026-08-07：M1（多 Agent 最小集 + 基础链路）与 M2（XMind 导出、自定义模板、图片多模态、大文档分片、测试点拆解确认）功能已完成，进度领先排期约 2 周。** 下一里程碑为 M3（业务知识库 RAG + 知识管家 Agent，排期 08-24 起），其硬前置 **POC-R8 Embedding 选型（截止 08-21）尚未启动**，首批知识语料也待需求方提供。所有文档与开发沟通均使用中文。

核心文档：

- `docs/AI测试用例生成Agent需求文档.md` — PRD V2.1（多 Agent 架构版），是本项目的唯一需求来源。功能需求编号形如 F-x-y（如 F-7-3b），风险项编号形如 Rx，实现任何功能前先查对应条目。
- `docs/项目排期计划.md` — M1–M6 周级排期、POC 并行线、前置阻塞项。
- `docs/architecture/` — 4 张 PlantUML 架构图（.puml 源文件 + 预渲染 .svg/.png）：系统总体架构、多 Agent 编排流程、核心任务时序图、部署架构图。修改 .puml 后需重新渲染：`plantuml -tsvg *.puml`（或粘贴到 plantuml.com 校验）。
- `docs/xq/` — 本地内部需求 PDF（实测语料），**已在 .gitignore 中排除，不入库**。
- `docs/architecture/测试用例模版.xmind` — 团队实际用例模板样例（R1 已交付），XMind 2020+ ZEN 格式（zip 内含 content.json）。实现 F-5-1 XMind 输出时以此为准，其结构与 PRD 默认设定有差异：层级为「项目名称 → 一级功能模块 → 二级功能模块 → 测试点/用例标题 → 测试步骤 → 预期结果（步骤的子节点）」；优先级用节点 labels 承载；前置条件放在用例标题节点的 notes（备注）中，不是独立节点。**优先级已确认统一为 4 级 P0–P3**（2026-07-10 决策）：模板样例中出现的 p4 合并入 P3，内置默认模板与覆盖度统计均按 4 级设计；用户自定义模板若使用其他枚举，按 F-4-1/F-4-3 的自定义枚举处理。

## 系统架构（PRD 已锁定的设计决策）

系统是一个多 Agent 协作的测试用例生成平台：输入 PDF/Word/图片/文本需求，输出 XMind + Excel/CSV 用例文件。实现时以下决策已在评审中确认，不要偏离：

**六类 Agent，有向图编排（LangGraph），非自由对话**：

- 主控（Orchestrator）：用户对话、任务调度、多轮修订路由
- 需求分析：文档/图片理解、测试点拆解、按模块切分
- 用例生成：按模板 + 注入知识 + 记忆生成用例，可按模块并行多实例
- 评审（Critic）：与生成 Agent **强制分离**（独立 Prompt，可配不同模型），不合格项带具体问题定点打回，回环 ≤3 轮后强制出稿并标注未通过项
- 知识管家：三大知识库检索编排、配额分配、知识与需求冲突仲裁（冲突时以当前需求为准并标注为重点测试对象）、知识快照记录
- 学习：**离线异步**，与在线链路解耦（归因、Prompt 优化、记忆提炼）

**关键约束**：

- 上下文隔离：每个 Agent 只接收自身职责所需上下文；测试用例库的历史用例只注入评审 Agent 与拆解阶段，不进生成 Agent 上下文。
- 知识注入配额：三大知识库（测试用例库/需求文档库/玩法与业务规则库）按任务级 token 总预算 **5:3:2** 分配，支持让渡；记忆注入不占此配额，单独 ≤5%-10%。
- 检索时机分类不同：测试用例库在**拆解阶段**注入（覆盖度查漏），规则库与需求文档库在**生成前**注入。
- 每次任务记录完整 Agent 调用链路与知识快照（F-7-13），支撑归因。
- 自我学习三重防护不可裁剪（R11）：黄金评估集强制回归、重大变更管理员审批、Prompt 版本化可回滚。
- 评审闭环双通道（在线评审留痕 F-6-6 + 离线终稿回传 diff F-6-8）是度量与学习数据飞轮的前提，不可只做其一。

**模型接入（2026-07-10 决策，2026-08 已部分演进）**：LLM 适配层按多厂商设计——模型清单在 `config/models.yaml`（每条目含 provider、base_url、api_key_env、model、temperature、max_tokens、超时、supports_vision、fallbacks，OpenAI 兼容协议，密钥走环境变量/.env 不落明文），新增厂商只改配置不改代码。当前实际接入：**默认文本模型为采购的 DeepSeek 商用 token**（deepseek-chat，降级 deepseek-reasoner）；**Vision 已提前接入私有化模型**——内网 Ollama 部署的 qwen3-vl:30b（Windows + RTX 4090，派生别名 `qwen3-vl-16k` 固化 16K 上下文），图片类需求解析（F-2-3）已启用，图片自动路由至 Vision 模型（F-1-5）。**Embedding 已定型（2026-08-07，POC-R8 收口）：内网 Ollama 部署的 bge-m3（1024 维），商用 Embedding API 对比暂不进行**；首轮基线 Recall@5=0.833（评测脚本 `scripts/embedding_poc.py`）。模型微调 F-9-6 仍推迟；涉密兜底（限用内部模型）合规口径需与安全方确认；架构上不得写死"仅外部 API"或"仅私有化"的假设。

**技术栈（PRD 第 7 章建议选型）**：Python 3.12 + FastAPI、LangGraph 编排、LiteLLM/自研 LLM 适配层（统一 OpenAI 兼容协议，需支持私有化 vLLM/Ollama 与商用 token 两类来源、Vision 能力标识）、Milvus/Qdrant + Elasticsearch 混合检索、bge/m3e Embedding、Celery + Redis 任务队列、openpyxl 生成 Excel、XMind ZEN 格式（zip + content.json）生成脑图、Vue3/React 前端、Docker + K8s 部署。

## 工程与环境

- Python 3.12 + `.venv`，依赖清单在 `pyproject.toml`（FastAPI、LangGraph、Celery、PyMuPDF、openpyxl 等；dev 组含 pytest）。
- 启动服务：`uvicorn app.main:app --reload`；运行测试：`pytest`（tests/ 下按模块分文件，LLM 调用用 stubs 隔离）。
- 代码结构（`app/`）：`llm/`（多厂商适配层：registry + client，重试与降级）、`parsers/`（text/docx/pdf/image、分片 chunking、图文混排 enrich）、`agents/`（LangGraph 三角色编排 graph/state/prompts/service）、`templates/`（内置默认模板 + 自定义模板识别与模板库）、`exporters/`（xmind ZEN 格式 + Excel/CSV tabular）、`tasks/`（任务存储与终稿 diff）、`knowledge/`（三大知识库：入库/混合检索/知识管家 steward）、`memory/`（长期记忆：用户偏好/项目记忆、使用习惯沉淀、检索注入）、`api/`（FastAPI 路由）、`web/`（单页 Web 界面）。
- 模型配置：`config/models.yaml`；密钥经 `.env` 加载，不入库。POC 脚本在 `scripts/`（prompt_poc、vision_poc）。
- 分支：日常开发在 `dev`，PR 目标分支为 `main`。

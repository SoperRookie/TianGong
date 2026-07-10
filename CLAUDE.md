# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**在本仓库中始终使用中文回复用户。**

**Git 提交信息与 PR 描述中不得包含任何 AI 相关的署名或标识**（如 `Co-Authored-By: Claude`、`Generated with Claude Code` 等），提交信息使用中文。

## 项目状态

**尚未开始编码。** 本仓库（tiangong）目前只包含"AI 测试用例生成 Agent（TestCase Agent）"项目的需求与设计文档，全部为中文。开工日为 2026-07-13，代码实现将按 M1–M6 里程碑逐步落地。所有文档与后续开发沟通均使用中文。

核心文档：

- `docs/AI测试用例生成Agent需求文档.md` — PRD V2.1（多 Agent 架构版），是本项目的唯一需求来源。功能需求编号形如 F-x-y（如 F-7-3b），风险项编号形如 Rx，实现任何功能前先查对应条目。
- `docs/项目排期计划.md` — M1–M6 周级排期、POC 并行线、前置阻塞项。
- `docs/architecture/` — 4 张 PlantUML 架构图（.puml 源文件 + 预渲染 .svg/.png）：系统总体架构、多 Agent 编排流程、核心任务时序图、部署架构图。修改 .puml 后需重新渲染：`plantuml -tsvg *.puml`（或粘贴到 plantuml.com 校验）。
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

**模型接入（2026-07-10 决策）**：**首期不使用 GPU/私有化部署，所有模型（LLM 与 Embedding）均为外部采购 API；后期计划用 Ollama/vLLM 部署私有化大模型接入**（两者均提供 OpenAI 兼容接口，届时在模型配置文件中新增条目即可，架构上不得写死"仅外部 API"的假设）。首期影响：Embedding 走商用 API（DeepSeek 无 Embedding API，需另选如阿里云/智谱/OpenAI，POC-R8 首轮改为商用 API 间对比）；模型微调 F-9-6 推迟至私有化模型落地后再评估；部署架构首期无 GPU 层但保留扩展位；首期全部数据经外部 API 传输，涉密兜底（限用内部模型）待私有化落地后才可用，合规口径需与安全方确认。首期使用**采购的 DeepSeek 商用 token** 作为默认模型；LLM 适配层从第一版起就按多厂商设计——模型清单放在配置文件中（每个模型条目含 provider、base_url、api_key、model 名称、temperature、max_tokens、超时、是否支持 Vision 等，OpenAI 兼容协议，密钥不落明文），新增厂商只改配置不改代码。注意 DeepSeek 当前不具备 Vision 能力，图片类需求解析（F-2-3）需等接入多模态模型后启用，或先走 OCR 兜底链路。

**技术栈（PRD 第 7 章建议选型）**：Python 3.12 + FastAPI、LangGraph 编排、LiteLLM/自研 LLM 适配层（统一 OpenAI 兼容协议，需支持私有化 vLLM/Ollama 与商用 token 两类来源、Vision 能力标识）、Milvus/Qdrant + Elasticsearch 混合检索、bge/m3e Embedding、Celery + Redis 任务队列、openpyxl 生成 Excel、XMind ZEN 格式（zip + content.json）生成脑图、Vue3/React 前端、Docker + K8s 部署。

## 环境

- 仓库根目录已有 `.venv`（CPython 3.12），尚无 `pyproject.toml`/依赖清单——开始编码时需先建立工程脚手架（M1-W1：FastAPI + LangGraph + Celery）。
- 分支：日常开发在 `dev`，PR 目标分支为 `main`。

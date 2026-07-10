# 架构图说明

基于《AI 测试用例生成 Agent 需求文档》PRD V2.1（多 Agent 架构版）设计，PlantUML 源文件与预渲染 SVG 一一对应。

| 文件 | 图类型 | 内容 |
|---|---|---|
| `01-系统总体架构.puml` | 组件图 | 七层分层架构：用户交互层、多 Agent 协作层（六类 Agent）、能力支撑层（解析/LLM 适配/输出）、知识与记忆层、自我学习数据飞轮、平台基础服务、模型服务 |
| `02-多Agent编排流程.puml` | 活动图 | LangGraph 有向图编排全流程：拆解 → 知识编排 → 并行生成 → 评审 ≤3 轮修正回环 → 输出 → 评审闭环（在线/离线双路径）→ 学习 Agent 离线异步 |
| `03-核心任务时序图.puml` | 时序图 | 单次任务中各 Agent 与知识库/记忆/LLM 适配层的调用时序，含修正回环 loop、多轮修订 opt、双路径评审 alt |
| `04-部署架构图.puml` | 部署图 | 内网 K8s 集群：Nginx 接入、无状态应用层（API/Agent Worker/学习 Worker）、中间件存储层、GPU 模型层、可观测性；商用 API 受控出网 |

## 渲染方式

- **在线**：源文件内容粘贴到 <https://www.plantuml.com/plantuml>（4 个文件均已通过该服务器语法校验）
- **VS Code**：安装 PlantUML 插件（jebbs.plantuml），`Alt+D` 预览
- **本地 CLI**：`brew install plantuml`，然后 `plantuml -tsvg *.puml`
- 同目录下的 `.svg` 为已渲染版本，可直接用浏览器打开

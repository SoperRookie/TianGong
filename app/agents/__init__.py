# 多 Agent 协作层（LangGraph 有向图编排）。
# M1 最小集：拆解 → 生成 → 评审 → 修正回环（≤3 轮）→ 输出。
#
# 包级导出按需加载（PEP 562）：app.prompts 只需要 app.agents.prompts 的常量，
# 若在这里直接 import service，会沿 service → graph → app.prompts 形成循环导入（单独运行解析器测试时触发）。
__all__ = ["AnalysisResult", "GenerationResult", "run_analysis", "run_generation",
           "run_requirement_analysis", "run_revision"]


def __getattr__(name: str):
    if name in __all__:
        from app.agents import service

        return getattr(service, name)
    raise AttributeError(f"module 'app.agents' has no attribute {name!r}")

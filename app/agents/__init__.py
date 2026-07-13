# 多 Agent 协作层（LangGraph 有向图编排）。
# M1 最小集：拆解 → 生成 → 评审 → 修正回环（≤3 轮）→ 输出。
from app.agents.service import AnalysisResult, GenerationResult, run_analysis, run_generation

__all__ = ["AnalysisResult", "GenerationResult", "run_analysis", "run_generation"]

"""知识管家 Agent（F-7-6）：三大知识库检索编排、任务级配额分配与让渡、知识快照。

配额：任务级注入预算按 测试用例库:需求文档库:玩法与业务规则库 = 5:3:2 分配（PRD 锁定），
某类命中不足时余量按比例让渡给其他类。注入时机分类差异化（PRD 锁定）：
- 测试用例库 → 拆解阶段（覆盖度查漏）与评审 Agent；不进生成 Agent 上下文
- 需求文档库、玩法与业务规则库 → 生成前注入生成 Agent

每次检索产出知识快照（F-7-13 的知识侧留痕），随任务记录落库支撑归因。
"""

from pydantic import BaseModel, Field

from app.knowledge.schemas import CATEGORIES, SearchHit
from app.knowledge.service import KnowledgeService

# 配额比例（PRD 锁定 5:3:2，不可改）
QUOTA_SHARES: dict[str, int] = {
    "test_cases": 5,
    "requirement_docs": 3,
    "business_rules": 2,
}

# 拆解/评审阶段注入的分类 与 生成前注入的分类（PRD 上下文隔离约束）
ANALYSIS_CATEGORIES = ("test_cases",)
GENERATION_CATEGORIES = ("requirement_docs", "business_rules")

# 每类候选检索条数：足量候选是让渡的前提
_CANDIDATES_PER_CATEGORY = 12


class KnowledgeBundle(BaseModel):
    """一次编排的产出：按分类的命中切片 + 快照留痕。"""

    hits: dict[str, list[SearchHit]] = Field(default_factory=dict)
    snapshot: list[dict] = Field(default_factory=list, description="知识快照（F-7-13）")

    @property
    def empty(self) -> bool:
        return not any(self.hits.values())

    def render(self) -> str:
        """渲染为注入 Prompt 的参考资料文本，带引用来源（F-7-5 引用可见）。"""
        parts: list[str] = []
        for category, hits in self.hits.items():
            if not hits:
                continue
            parts.append(f"### {CATEGORIES[category]}")
            parts.extend(
                f"[{h.source} 第{h.chunk_index + 1}片] {h.text}" for h in hits
            )
        return "\n\n".join(parts)


class KnowledgeSteward:
    def __init__(self, service: KnowledgeService, budget_chars: int = 6000):
        self.service = service
        self.budget_chars = budget_chars

    async def for_analysis(self, query: str, space: str | None = None) -> KnowledgeBundle:
        """拆解阶段：只注入测试用例库（历史用例覆盖度查漏），占总预算的 5/10。"""
        return await self._gather(query, ANALYSIS_CATEGORIES, space, stage="analysis")

    async def for_generation(self, query: str, space: str | None = None) -> KnowledgeBundle:
        """生成前：注入需求文档库与规则库，共占总预算的 5/10，两类间可让渡。"""
        return await self._gather(query, GENERATION_CATEGORIES, space, stage="generation")

    async def _gather(
        self, query: str, categories: tuple[str, ...], space: str | None, stage: str
    ) -> KnowledgeBundle:
        total_shares = sum(QUOTA_SHARES.values())
        candidates: dict[str, list[SearchHit]] = {}
        for category in categories:
            candidates[category] = await self.service.search(
                query, top_k=_CANDIDATES_PER_CATEGORY, category=category, space=space
            )

        budgets = {
            c: self.budget_chars * QUOTA_SHARES[c] // total_shares for c in categories
        }
        bundle = KnowledgeBundle()
        # 首轮按各自配额装填；余量按配额比例让渡给仍有候选的分类，直至无候选或无余量
        leftover = 0
        for category in categories:
            used = self._fill(bundle, category, candidates[category], budgets[category])
            leftover += budgets[category] - used
        while leftover > 0:
            remaining = [c for c in categories if candidates[c]]
            if not remaining:
                break
            shares = sum(QUOTA_SHARES[c] for c in remaining)
            consumed = 0
            for category in remaining:
                extra = leftover * QUOTA_SHARES[category] // shares
                consumed += self._fill(bundle, category, candidates[category], extra)
            if consumed == 0:  # 余量不足以装入任何候选切片
                break
            leftover -= consumed

        for category in categories:
            for hit in bundle.hits.get(category, []):
                bundle.snapshot.append(
                    {
                        "stage": stage,
                        "category": category,
                        "doc_id": hit.doc_id,
                        "source": hit.source,
                        "chunk_index": hit.chunk_index,
                        "score": hit.score,
                        "chars": len(hit.text),
                    }
                )
        return bundle

    @staticmethod
    def _fill(
        bundle: KnowledgeBundle, category: str, candidates: list[SearchHit], budget: int
    ) -> int:
        """从候选队列头部装填切片直至预算耗尽，返回实际消耗字符数。"""
        used = 0
        selected = bundle.hits.setdefault(category, [])
        while candidates and used + len(candidates[0].text) <= budget:
            hit = candidates.pop(0)
            selected.append(hit)
            used += len(hit.text)
        return used

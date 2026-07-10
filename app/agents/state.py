"""编排状态定义。"""

from typing import TypedDict

MAX_REVIEW_ROUNDS = 3  # 生成-评审修正回环上限，超限强制出稿（PRD 异常流程）


class ReviewIssue(TypedDict):
    case_id: str
    problem: str


class OrchestrationState(TypedDict, total=False):
    # 输入
    requirement: str
    model: str | None           # 生成/拆解使用的模型（任务级切换）
    reviewer_model: str | None  # 评审模型，可与生成不同（生成-评审分离）

    # 需求分析产物
    test_points: list[dict]     # [{module, points[]}]
    blind_spots: list[str]

    # 生成与评审产物
    cases: list[dict]           # TestCase 字典列表
    issues: list[ReviewIssue]   # 当前轮评审问题（含规则校验）
    missing: list[str]          # 评审发现的遗漏场景
    passed: bool
    review_rounds: int          # 已完成评审轮数
    unresolved: list[ReviewIssue]  # 回环超限仍未解决的问题（强制出稿标注）

    # 调用链路留痕（PRD 4.1a 规则 6 的最小实现）
    trace: list[dict]

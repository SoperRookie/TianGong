"""知识管家与混合检索测试：BM25 长尾召回、RRF 融合、5:3:2 配额与让渡、注入时机隔离。"""

import json

import pytest

from app.agents.service import run_analysis, run_generation
from app.knowledge import KnowledgeService, KnowledgeSteward, KnowledgeStore
from app.knowledge.hybrid import bm25_scores, rrf_merge, tokenize
from tests.stubs import ANALYST_REPLY, StubLLM, generator_reply, make_case, review_reply
from tests.test_knowledge import StubEmbedder, _VOCAB


@pytest.fixture
def service() -> KnowledgeService:
    store = KnowledgeStore(":memory:", dimensions=len(_VOCAB))
    return KnowledgeService(store, StubEmbedder(), chunk_max_chars=200)


# ---- 混合检索 ----


def test_tokenize_chinese_bigram_and_ascii_words():
    assert "限红" in tokenize("桌台限红规则")
    assert "abc" in tokenize("测试ABC模块")
    assert tokenize("红") == ["红"]


def test_bm25_finds_rare_keyword():
    docs = ["赔率与投注说明", "荷官可尝试重新识别牌面", "牌型判定规则"]
    scores = bm25_scores("扫牌失败后重新识别", docs)
    assert scores.index(max(scores)) == 1


def test_rrf_merge_prefers_consensus():
    fused = rrf_merge([[1, 2, 3], [2, 1, 4]])
    assert fused[0][0] in (1, 2)
    assert {cid for cid, _ in fused} == {1, 2, 3, 4}


async def test_hybrid_search_recalls_keyword_only_chunk(service):
    # "荷官"与"识别"都不在词表向量桩件的词汇中 → 纯向量检索必然失手，混合检索靠 BM25 兜底
    await service.ingest_text(
        "荷官开牌时系统无法识别，可尝试重新识别牌面。", source="流程.md", category="business_rules"
    )
    await service.ingest_text("赔率投注结算规则若干。", source="赔率.md", category="business_rules")
    hits = await service.search("扫牌失败怎么重新识别", top_k=2, mode="hybrid")
    assert hits and hits[0].source == "流程.md"


# ---- 知识管家配额与让渡 ----


def _long(text: str, n: int = 120) -> str:
    return text + "。" + "补充说明" * ((n - len(text)) // 4)


@pytest.fixture
async def stocked_service(service):
    for i in range(4):
        await service.ingest_text(
            _long(f"历史用例{i}：验证登录密码与赔率投注 第{i}组"), source=f"用例{i}.md", category="test_cases"
        )
        await service.ingest_text(
            _long(f"历史需求{i}：登录密码与赔率投注的需求描述 第{i}组"), source=f"需求{i}.md",
            category="requirement_docs",
        )
        await service.ingest_text(
            _long(f"业务规则{i}：赔率投注结算与桌台限红 第{i}组"), source=f"规则{i}.md",
            category="business_rules",
        )
    return service


async def test_analysis_stage_only_test_cases(stocked_service):
    steward = KnowledgeSteward(stocked_service, budget_chars=2000)
    bundle = await steward.for_analysis("赔率投注")
    assert set(bundle.hits) == {"test_cases"}
    assert bundle.hits["test_cases"]
    assert all(s["stage"] == "analysis" for s in bundle.snapshot)


async def test_generation_stage_quota_split(stocked_service):
    steward = KnowledgeSteward(stocked_service, budget_chars=2000)
    bundle = await steward.for_generation("赔率投注")
    assert set(bundle.hits) == {"requirement_docs", "business_rules"}
    # 需求文档库配额 3 份 > 规则库 2 份：注入字符数应更多
    req_chars = sum(len(h.text) for h in bundle.hits["requirement_docs"])
    rule_chars = sum(len(h.text) for h in bundle.hits["business_rules"])
    assert req_chars >= rule_chars > 0
    # 总注入不超过该阶段两类配额之和
    assert req_chars + rule_chars <= 2000


async def test_quota_ceded_when_category_empty(service):
    # 只有规则库有内容：需求文档库配额应让渡给规则库
    for i in range(8):
        await service.ingest_text(
            _long(f"业务规则{i}：赔率投注与桌台限红 第{i}组"), source=f"规则{i}.md",
            category="business_rules",
        )
    steward = KnowledgeSteward(service, budget_chars=3000)
    bundle = await steward.for_generation("赔率投注")
    rule_chars = sum(len(h.text) for h in bundle.hits["business_rules"])
    own_budget = 3000 * 2 // 10
    assert rule_chars > own_budget  # 超出自身配额 → 发生了让渡
    assert bundle.hits.get("requirement_docs", []) == []


async def test_snapshot_records_provenance(stocked_service):
    steward = KnowledgeSteward(stocked_service, budget_chars=2000)
    bundle = await steward.for_generation("赔率投注")
    assert bundle.snapshot
    entry = bundle.snapshot[0]
    assert {"stage", "category", "doc_id", "source", "chunk_index", "score", "chars"} <= set(entry)


# ---- 注入时机与上下文隔离（PRD 锁定约束）----


async def test_knowledge_cases_reach_analyst_and_reviewer_not_generator():
    llm = StubLLM([ANALYST_REPLY, generator_reply(make_case()), review_reply(True)])
    await run_generation(
        "登录需求",
        llm=llm,
        knowledge_refs="【历史需求】登录需支持验证码",
        knowledge_cases="【历史用例】验证密码错误锁定",
    )
    analyst_msg = llm.calls[0]["messages"][1]["content"]
    generator_msg = llm.calls[1]["messages"][1]["content"]
    reviewer_msg = llm.calls[2]["messages"][1]["content"]
    assert "验证密码错误锁定" in analyst_msg
    # 生成 Agent：只见需求/规则库知识，不见历史用例（上下文隔离）
    assert "登录需支持验证码" in generator_msg
    assert "验证密码错误锁定" not in generator_msg
    assert "验证密码错误锁定" in reviewer_msg


async def test_run_analysis_accepts_knowledge():
    llm = StubLLM([ANALYST_REPLY])
    await run_analysis("登录需求", llm=llm, knowledge_cases="【历史用例】验证锁定策略")
    assert "验证锁定策略" in llm.calls[0]["messages"][1]["content"]


async def test_conflict_arbitration_wording_injected():
    llm = StubLLM([ANALYST_REPLY, generator_reply(make_case()), review_reply(True)])
    await run_generation("登录需求", llm=llm, knowledge_refs="旧规则")
    assert "以当前需求为准" in llm.calls[1]["messages"][1]["content"]

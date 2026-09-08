"""知识库模块测试：切片、入库检索回路、分类/空间过滤、API 集成。"""

import math

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.knowledge import KnowledgeService, KnowledgeStore, split_knowledge
from app.knowledge.schemas import InvalidCategoryError
from app.llm.embeddings import EmbeddingConfig, EmbeddingRegistry

# 词表向量桩件：按关键词命中构造向量，保证"语义相近→余弦相似度高"的测试前提
_VOCAB = ["登录", "密码", "赔率", "投注", "牌型", "荷官", "桌台", "结算"]


class StubEmbedder:
    def __init__(self):
        self.registry = EmbeddingRegistry(
            default_embedding="stub",
            configs=[
                EmbeddingConfig(
                    name="stub",
                    provider="stub",
                    base_url="http://stub",
                    model="stub",
                    dimensions=len(_VOCAB),
                )
            ],
        )
        self.calls: list[list[str]] = []

    async def embed(self, texts, model=None):
        self.calls.append(list(texts))
        vectors = []
        for text in texts:
            v = [float(text.count(word)) for word in _VOCAB]
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            vectors.append([x / norm for x in v])
        return vectors


@pytest.fixture
def service() -> KnowledgeService:
    store = KnowledgeStore(":memory:", dimensions=len(_VOCAB))
    return KnowledgeService(store, StubEmbedder(), chunk_max_chars=200)


def test_split_knowledge_merges_short_chunks():
    text = "# 标题\n\n" + "正文内容较长，" * 30 + "\n\n# 短节\n短"
    chunks = split_knowledge(text, max_chars=100)
    assert all(len(c) >= 50 for c in chunks[:-1])
    assert "".join(chunks).count("正文内容较长") == 30


async def test_ingest_and_search_roundtrip(service):
    await service.ingest_text(
        "赔率表：一对赔率4.8，两对赔率2.1，投注按桌台限红执行。", source="赔率规则.md",
        category="business_rules",
    )
    await service.ingest_text(
        "登录用例：输入正确密码登录成功；密码错误提示重试。", source="历史用例.md",
        category="test_cases",
    )
    hits = await service.search("投注赔率如何结算", top_k=3)
    assert hits and hits[0].source == "赔率规则.md"
    assert hits[0].category == "business_rules"


async def test_search_category_and_space_filter(service):
    await service.ingest_text("赔率与投注规则", source="a.md", category="business_rules", space="game1")
    await service.ingest_text("赔率相关历史用例", source="b.md", category="test_cases", space="game2")
    only_cases = await service.search("赔率", category="test_cases")
    assert {h.category for h in only_cases} == {"test_cases"}
    only_game1 = await service.search("赔率", space="game1")
    assert {h.space for h in only_game1} == {"game1"}


async def test_ingest_rejects_unknown_category(service):
    with pytest.raises(InvalidCategoryError):
        await service.ingest_text("内容", source="x", category="wrong")


async def test_delete_doc_removes_hits(service):
    doc = await service.ingest_text("荷官开牌与桌台流程", source="c.md", category="requirement_docs")
    assert service.store.delete_doc(doc.doc_id) is True
    assert service.store.delete_doc(doc.doc_id) is False
    assert await service.search("荷官") == []
    assert service.store.list_docs() == []


def test_store_dimension_mismatch_raises(tmp_path):
    KnowledgeStore(tmp_path / "kb", dimensions=8)
    with pytest.raises(ValueError, match="维度"):
        KnowledgeStore(tmp_path / "kb", dimensions=16)


def test_embedding_registry_from_yaml(tmp_path):
    cfg = tmp_path / "models.yaml"
    cfg.write_text(
        """
default_embedding: bge
embeddings:
  - name: bge
    provider: ollama
    base_url: http://x/v1
    model: bge-m3
    dimensions: 1024
""",
        encoding="utf-8",
    )
    registry = EmbeddingRegistry.from_yaml(cfg)
    assert registry.get().dimensions == 1024
    assert registry.list_public()[0]["is_default"] is True


async def test_knowledge_api_roundtrip(service):
    from app.main import app

    async with LifespanManager(app):
        app.state.knowledge = service
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/knowledge/docs",
                data={"category": "business_rules", "text": "牌型判定与赔率结算规则", "source": "规则.md", "level": "public"},
            )
            assert resp.status_code == 200, resp.text
            doc_id = resp.json()["ingested"][0]["doc_id"]

            resp = await client.post("/api/v1/knowledge/search", json={"query": "赔率"})
            assert resp.status_code == 200
            assert resp.json()["hits"][0]["doc_id"] == doc_id

            resp = await client.get("/api/v1/knowledge/docs")
            assert len(resp.json()["documents"]) == 1

            resp = await client.delete(f"/api/v1/knowledge/docs/{doc_id}")
            assert resp.status_code == 200

            resp = await client.post(
                "/api/v1/knowledge/docs", data={"category": "bad", "text": "x"}
            )
            assert resp.status_code == 400

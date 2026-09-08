"""知识库服务：入库流水线（解析 → 切片 → 向量化 → 入库）与语义检索（F-7-1/3）。"""

import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.knowledge.chunker import split_knowledge
from app.knowledge.schemas import (
    CATEGORIES,
    DEFAULT_SPACE,
    InvalidCategoryError,
    KnowledgeDoc,
    SearchHit,
)
from app.knowledge.store import KnowledgeStore
from app.llm.embeddings import EmbeddingClient
from app.parsers import parse_file, parse_text


class KnowledgeService:
    def __init__(
        self,
        store: KnowledgeStore,
        embedder: EmbeddingClient,
        chunk_max_chars: int = 600,
    ):
        self.store = store
        self.embedder = embedder
        self.chunk_max_chars = chunk_max_chars

    async def ingest_file(
        self, path: str | Path, category: str, space: str = DEFAULT_SPACE, **meta
    ) -> KnowledgeDoc:
        doc = parse_file(path)
        return await self._ingest(doc.full_text, source=doc.source, category=category, space=space, **meta)

    async def ingest_text(
        self, text: str, source: str, category: str, space: str = DEFAULT_SPACE, **meta
    ) -> KnowledgeDoc:
        parsed = parse_text(text)
        return await self._ingest(parsed.full_text, source=source, category=category, space=space, **meta)

    async def ingest_cases(self, path: str | Path, space: str = DEFAULT_SPACE, **meta) -> KnowledgeDoc:
        """历史用例入库（F-7-2）：Excel/CSV/XMind → 测试用例库，每条用例一个切片。"""
        from app.knowledge.importers import parse_cases_file, render_case_chunk

        path = Path(path)
        chunks = [render_case_chunk(c) for c in parse_cases_file(path)]
        vectors = await self.embedder.embed(chunks)
        record = self._new_doc(source=path.name, category="test_cases", space=space, chunk_count=len(chunks), **meta)
        self.store.upsert_chunks(record, chunks, vectors)
        return record

    async def _ingest(self, text: str, source: str, category: str, space: str, **meta) -> KnowledgeDoc:
        if category not in CATEGORIES:
            raise InvalidCategoryError(category)
        chunks = split_knowledge(text, self.chunk_max_chars)
        if not chunks:
            raise ValueError(f"文档 {source} 无有效内容，未入库")
        vectors = await self.embedder.embed(chunks)
        self._check_dims(vectors)
        # 同空间同分类同来源重复入库：先删旧文档，避免重复切片挤占检索配额
        for old in self.store.list_docs(space, category):
            if old.source == source:
                self.store.delete_doc(old.doc_id)
        record = self._new_doc(source=source, category=category, space=space, chunk_count=len(chunks), **meta)
        self.store.upsert_chunks(record, chunks, vectors)
        return record

    def _check_dims(self, vectors: list[list[float]]) -> None:
        expected = self.embedder.registry.get().dimensions
        if vectors and len(vectors[0]) != expected:
            raise ValueError(f"Embedding 返回维度 {len(vectors[0])} 与配置 {expected} 不一致，请检查模型配置")

    def _new_doc(self, source: str, category: str, space: str, chunk_count: int,
                 level: str | None = None, module: str = "", created_by: str | None = None) -> KnowledgeDoc:
        from app.knowledge.schemas import DEFAULT_SPACE as _DEF, PUBLIC_SPACE

        if level is None:
            level = "public" if space in (_DEF, PUBLIC_SPACE) else ("module" if module else "project")
        return KnowledgeDoc(
            doc_id=uuid.uuid4().hex[:12],
            space=space,
            level=level,
            module=module or "",
            created_by=created_by,
            category=category,
            source=source,
            chunk_count=chunk_count,
            embedding_model=self.embedder.registry.default_embedding,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    async def search(
        self,
        query: str,
        top_k: int = 5,
        category: str | None = None,
        space: str | None = None,
        mode: str = "hybrid",
    ) -> list[SearchHit]:
        """检索知识切片。mode: hybrid（向量+关键词 RRF 融合，F-7-3b，默认）/ vector。"""
        if category is not None and category not in CATEGORIES:
            raise InvalidCategoryError(category)
        [vector] = await self.embedder.embed([query])
        vector_hits = self.store.search(
            vector, top_k=top_k if mode == "vector" else top_k * 3, category=category, space=space
        )
        if mode == "vector":
            return vector_hits
        return self._fuse(query, vector_hits, top_k, category, space)

    def _fuse(
        self,
        query: str,
        vector_hits: list[SearchHit],
        top_k: int,
        category: str | None,
        space: str | None,
    ) -> list[SearchHit]:
        from app.knowledge.hybrid import bm25_scores, rrf_merge

        corpus = self.store.iter_chunks(category=category, space=space)
        if not corpus:
            return vector_hits[:top_k]
        by_key = {(h.doc_id, h.chunk_index): i for i, h in enumerate(corpus)}

        keyword_scores = bm25_scores(query, [h.text for h in corpus])
        keyword_ranking = [
            i for i in sorted(range(len(corpus)), key=lambda i: -keyword_scores[i])
            if keyword_scores[i] > 0
        ][: top_k * 3]
        vector_ranking = [
            by_key[(h.doc_id, h.chunk_index)]
            for h in vector_hits
            if (h.doc_id, h.chunk_index) in by_key
        ]
        fused = rrf_merge([vector_ranking, keyword_ranking])
        results = []
        for idx, score in fused[:top_k]:
            hit = corpus[idx].model_copy(update={"score": round(score, 4)})
            results.append(hit)
        return results

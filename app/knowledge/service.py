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
        self, path: str | Path, category: str, space: str = DEFAULT_SPACE
    ) -> KnowledgeDoc:
        doc = parse_file(path)
        return await self._ingest(doc.full_text, source=doc.source, category=category, space=space)

    async def ingest_text(
        self, text: str, source: str, category: str, space: str = DEFAULT_SPACE
    ) -> KnowledgeDoc:
        parsed = parse_text(text)
        return await self._ingest(parsed.full_text, source=source, category=category, space=space)

    async def _ingest(self, text: str, source: str, category: str, space: str) -> KnowledgeDoc:
        if category not in CATEGORIES:
            raise InvalidCategoryError(category)
        chunks = split_knowledge(text, self.chunk_max_chars)
        if not chunks:
            raise ValueError(f"文档 {source} 无有效内容，未入库")
        vectors = await self.embedder.embed(chunks)
        record = KnowledgeDoc(
            doc_id=uuid.uuid4().hex[:12],
            space=space,
            category=category,
            source=source,
            chunk_count=len(chunks),
            embedding_model=self.embedder.registry.default_embedding,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self.store.upsert_chunks(record, chunks, vectors)
        return record

    async def search(
        self,
        query: str,
        top_k: int = 5,
        category: str | None = None,
        space: str | None = None,
    ) -> list[SearchHit]:
        if category is not None and category not in CATEGORIES:
            raise InvalidCategoryError(category)
        [vector] = await self.embedder.embed([query])
        return self.store.search(vector, top_k=top_k, category=category, space=space)

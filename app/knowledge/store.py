"""知识向量库：Qdrant 本地嵌入式模式（PRD 7 章选型），切换服务端部署只改初始化参数。

文档台账（KnowledgeDoc）落 JSON 文件，向量与切片正文存 Qdrant payload。
"""

import json
import uuid
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

from app.knowledge.schemas import KnowledgeDoc, SearchHit

_COLLECTION = "knowledge"

# doc_id + 切片序号 → 稳定 UUID，重复入库同一文档时覆盖而非重复
_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


class KnowledgeStore:
    def __init__(self, location: str | Path, dimensions: int):
        """location 为目录路径（持久化）或 ':memory:'（测试用）。"""
        if location == ":memory:":
            self._client = QdrantClient(":memory:")
            self._docs_path: Path | None = None
        else:
            base = Path(location)
            base.mkdir(parents=True, exist_ok=True)
            self._client = QdrantClient(path=str(base / "qdrant"))
            self._docs_path = base / "docs.json"
        self.dimensions = dimensions
        self._docs: dict[str, KnowledgeDoc] = self._load_docs()
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        if not self._client.collection_exists(_COLLECTION):
            self._client.create_collection(
                collection_name=_COLLECTION,
                vectors_config=VectorParams(size=self.dimensions, distance=Distance.COSINE),
            )
            return
        current = self._client.get_collection(_COLLECTION).config.params.vectors.size
        if current != self.dimensions:
            raise ValueError(
                f"向量库维度 {current} 与当前 Embedding 模型维度 {self.dimensions} 不一致；"
                "更换 Embedding 模型后需重建知识库（删除 data/knowledge 后重新入库）"
            )

    # ---- 文档台账 ----

    def _load_docs(self) -> dict[str, KnowledgeDoc]:
        if self._docs_path is None or not self._docs_path.exists():
            return {}
        raw = json.loads(self._docs_path.read_text(encoding="utf-8"))
        return {d["doc_id"]: KnowledgeDoc.model_validate(d) for d in raw}

    def _save_docs(self) -> None:
        if self._docs_path is None:
            return
        data = [d.model_dump() for d in self._docs.values()]
        self._docs_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def list_docs(self, space: str | None = None, category: str | None = None) -> list[KnowledgeDoc]:
        docs = list(self._docs.values())
        if space:
            docs = [d for d in docs if d.space == space]
        if category:
            docs = [d for d in docs if d.category == category]
        return sorted(docs, key=lambda d: d.created_at, reverse=True)

    def get_doc(self, doc_id: str) -> KnowledgeDoc | None:
        return self._docs.get(doc_id)

    # ---- 切片写入与检索 ----

    def upsert_chunks(
        self, doc: KnowledgeDoc, chunks: list[str], vectors: list[list[float]]
    ) -> None:
        if len(chunks) != len(vectors):
            raise ValueError(f"切片数 {len(chunks)} 与向量数 {len(vectors)} 不一致")
        points = [
            PointStruct(
                id=str(uuid.uuid5(_NAMESPACE, f"{doc.doc_id}:{i}")),
                vector=vec,
                payload={
                    "doc_id": doc.doc_id,
                    "source": doc.source,
                    "category": doc.category,
                    "space": doc.space,
                    "chunk_index": i,
                    "text": chunk,
                },
            )
            for i, (chunk, vec) in enumerate(zip(chunks, vectors))
        ]
        self._client.upsert(collection_name=_COLLECTION, points=points)
        self._docs[doc.doc_id] = doc
        self._save_docs()

    def search(
        self,
        vector: list[float],
        top_k: int = 5,
        category: str | None = None,
        space: str | None = None,
    ) -> list[SearchHit]:
        conditions = []
        if category:
            conditions.append(FieldCondition(key="category", match=MatchValue(value=category)))
        if space:
            conditions.append(FieldCondition(key="space", match=MatchValue(value=space)))
        result = self._client.query_points(
            collection_name=_COLLECTION,
            query=vector,
            limit=top_k,
            query_filter=Filter(must=conditions) if conditions else None,
        )
        return [
            SearchHit(
                text=p.payload["text"],
                score=p.score,
                doc_id=p.payload["doc_id"],
                source=p.payload["source"],
                category=p.payload["category"],
                space=p.payload["space"],
                chunk_index=p.payload["chunk_index"],
            )
            for p in result.points
        ]

    def iter_chunks(
        self, category: str | None = None, space: str | None = None
    ) -> list[SearchHit]:
        """遍历范围内全部切片（关键词侧检索用）。当前规模全量扫描可行；
        语料到万级后关键词侧迁移 Elasticsearch（PRD 选型），此接口即废弃。"""
        conditions = []
        if category:
            conditions.append(FieldCondition(key="category", match=MatchValue(value=category)))
        if space:
            conditions.append(FieldCondition(key="space", match=MatchValue(value=space)))
        hits: list[SearchHit] = []
        offset = None
        while True:
            points, offset = self._client.scroll(
                collection_name=_COLLECTION,
                scroll_filter=Filter(must=conditions) if conditions else None,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            hits.extend(
                SearchHit(
                    text=p.payload["text"],
                    score=0.0,
                    doc_id=p.payload["doc_id"],
                    source=p.payload["source"],
                    category=p.payload["category"],
                    space=p.payload["space"],
                    chunk_index=p.payload["chunk_index"],
                )
                for p in points
            )
            if offset is None:
                return hits

    def delete_doc(self, doc_id: str) -> bool:
        if doc_id not in self._docs:
            return False
        self._client.delete(
            collection_name=_COLLECTION,
            points_selector=Filter(
                must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]
            ),
        )
        del self._docs[doc_id]
        self._save_docs()
        return True

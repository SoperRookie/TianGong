"""知识向量库：Qdrant 本地嵌入式模式（PRD 7 章选型），切换服务端部署只改初始化参数。

文档台账（KnowledgeDoc）落 JSON 文件，向量与切片正文存 Qdrant payload。
"""

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
        if self._docs_path is None:  # :memory:（测试）：不入库，进程内即可
            return {}
        from app.db import DocStore, load_with_migration

        self._doc_table = DocStore("knowledge_docs")
        raw = load_with_migration(
            self._doc_table, self._docs_path,
            lambda data: {d["doc_id"]: d for d in data},
        )
        docs = {}
        for k, d in raw.items():
            if "level" not in d:  # 迁移期：历史 default 空间视同公共层，其余视同项目层
                d["level"] = "public" if d.get("space") in ("default", "public") else "project"
            docs[k] = KnowledgeDoc.model_validate(d)
        return docs

    def _save_docs(self, *doc_ids: str, removed: str | None = None) -> None:
        if self._docs_path is None:
            return
        if removed:
            self._doc_table.remove(removed)
        for did in (doc_ids or tuple(self._docs)):
            if did in self._docs:
                self._doc_table.put(did, self._docs[did].model_dump())

    def list_docs(self, space: str | None = None, category: str | None = None,
                  level: str | None = None) -> list[KnowledgeDoc]:
        docs = list(self._docs.values())
        if space:
            docs = [d for d in docs if d.space == space]
        if level:
            docs = [d for d in docs if d.level == level]
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
                    "level": doc.level,
                    "module": doc.module,
                    "chunk_index": i,
                    "text": chunk,
                },
            )
            for i, (chunk, vec) in enumerate(zip(chunks, vectors))
        ]
        self._client.upsert(collection_name=_COLLECTION, points=points)
        self._docs[doc.doc_id] = doc
        self._save_docs(doc.doc_id)

    @staticmethod
    def _scope_filter(category: str | None, space: str | None):
        """检索范围（16 章三层）：space 指定项目时 = 该项目（含模块层）+ 公共层（public / 历史 default）；
        space 为 None 时不限（仅管理员全局检索用）。项目知识绝不跨项目命中。"""
        from app.knowledge.schemas import DEFAULT_SPACE, PUBLIC_SPACE

        must = []
        if category:
            must.append(FieldCondition(key="category", match=MatchValue(value=category)))
        should = None
        if space:
            should = [FieldCondition(key="space", match=MatchValue(value=s))
                      for s in dict.fromkeys([space, PUBLIC_SPACE, DEFAULT_SPACE])]
        if not must and not should:
            return None
        return Filter(must=must or None, should=should)

    def search(
        self,
        vector: list[float],
        top_k: int = 5,
        category: str | None = None,
        space: str | None = None,
    ) -> list[SearchHit]:
        result = self._client.query_points(
            collection_name=_COLLECTION,
            query=vector,
            limit=top_k,
            query_filter=self._scope_filter(category, space),
        )
        return [
            SearchHit(
                text=p.payload["text"],
                score=p.score,
                doc_id=p.payload["doc_id"],
                source=p.payload["source"],
                category=p.payload["category"],
                space=p.payload["space"],
                level=p.payload.get("level", "project"),
                module=p.payload.get("module", ""),
                chunk_index=p.payload["chunk_index"],
            )
            for p in result.points
        ]

    def iter_chunks(
        self, category: str | None = None, space: str | None = None
    ) -> list[SearchHit]:
        """遍历范围内全部切片（关键词侧检索用）。当前规模全量扫描可行；
        语料到万级后关键词侧迁移 Elasticsearch（PRD 选型），此接口即废弃。"""
        hits: list[SearchHit] = []
        offset = None
        while True:
            points, offset = self._client.scroll(
                collection_name=_COLLECTION,
                scroll_filter=self._scope_filter(category, space),
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
                    level=p.payload.get("level", "project"),
                    module=p.payload.get("module", ""),
                    chunk_index=p.payload["chunk_index"],
                )
                for p in points
            )
            if offset is None:
                return hits

    def rename_space(self, old: str, new: str) -> int:
        """项目改名联动：台账 space 与向量切片 payload 一并更新。"""
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        docs = [d for d in self._docs.values() if d.space == old]
        if not docs:
            return 0
        for d in docs:
            d.space = new
        self._save_docs(*[d.doc_id for d in docs])
        self._client.set_payload(
            collection_name=_COLLECTION, payload={"space": new},
            points=Filter(must=[FieldCondition(key="space", match=MatchValue(value=old))]),
        )
        return len(docs)

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
        self._save_docs(removed=doc_id)
        return True

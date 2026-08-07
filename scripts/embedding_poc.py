"""POC-R8：Embedding 检索质量评测（M3 选型硬前置，须 08-21 前出结论）。

用 docs/xq 真实需求文档建语料（切片粒度与生产一致），以中文改述查询 + 关键词
判定相关性，对比 models.yaml 中 embeddings 段配置的各模型：

    .venv/bin/python scripts/embedding_poc.py                 # 评测全部已配置模型
    .venv/bin/python scripts/embedding_poc.py --models bge-m3-local

指标：Recall@1 / Recall@5（前 K 命中率）、MRR@10、向量化吞吐。
商用 API 对比：在 models.yaml 取消注释对应条目并配置密钥后重跑即可。
报告输出至 outputs/embedding_poc_report.json。
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import BASE_DIR, get_settings  # noqa: E402
from app.knowledge import split_knowledge  # noqa: E402
from app.llm.embeddings import EmbeddingClient, EmbeddingRegistry  # noqa: E402
from app.parsers import parse_file  # noqa: E402

CORPUS_DIR = BASE_DIR / "docs" / "xq"

# 评测查询：改述式提问（避免与原文完全同词），expected 为 AND 关键词组的 OR 列表——
# 命中切片包含任一组的全部关键词即判相关。
EVAL_QUERIES: list[dict] = [
    {"q": "一对牌型的赔付倍数是多少", "expected": [["一对", "4.8"], ["一对", "赔率"]]},
    {"q": "网络故障导致牌局没完成会怎样", "expected": [["故障", "取消"]]},
    {"q": "皇家同花顺和普通同花顺怎么区分", "expected": [["皇家同花顺"]]},
    {"q": "荷官扫牌失败后的处理流程", "expected": [["重新识别"], ["无法识别"]]},
    {"q": "走地德州一共发几门手牌", "expected": [["6门", "手牌"], ["六门", "手牌"]]},
    {"q": "每局游戏分成哪几个下注阶段", "expected": [["阶段", "投注"], ["阶段", "下注"]]},
    {"q": "桌台限红是什么意思", "expected": [["限红"]]},
    {"q": "首页进入游戏的按钮上要展示什么", "expected": [["进入游戏", "桌台名称"]]},
    {"q": "高牌的胜负判定规则", "expected": [["高牌"]]},
    {"q": "公共牌什么时候翻开", "expected": [["公共牌"]]},
    {"q": "提前胜出是什么规则", "expected": [["提前胜出"]]},
    {"q": "荷官风采页面展示荷官哪些信息", "expected": [["荷官风采"], ["查看详情"]]},
]


def build_corpus(chunk_chars: int) -> list[dict]:
    """解析语料 PDF 并按生产切片粒度切分（不走 Vision，纯文本层评测）。"""
    chunks: list[dict] = []
    for pdf in sorted(CORPUS_DIR.glob("*.pdf")):
        doc = parse_file(pdf)
        for i, chunk in enumerate(split_knowledge(doc.full_text, chunk_chars)):
            chunks.append({"source": pdf.name[:8], "chunk_index": i, "text": chunk})
    return chunks


def is_relevant(text: str, expected: list[list[str]]) -> bool:
    return any(all(kw in text for kw in group) for group in expected)


async def evaluate_model(
    client: EmbeddingClient, name: str, corpus: list[dict], top_k: int
) -> dict:
    texts = [c["text"] for c in corpus]
    start = time.monotonic()
    corpus_vecs = np.array(await client.embed(texts, model=name))
    embed_secs = time.monotonic() - start
    query_vecs = np.array(await client.embed([q["q"] for q in EVAL_QUERIES], model=name))

    # 余弦相似度检索
    corpus_norm = corpus_vecs / np.linalg.norm(corpus_vecs, axis=1, keepdims=True)
    query_norm = query_vecs / np.linalg.norm(query_vecs, axis=1, keepdims=True)
    scores = query_norm @ corpus_norm.T

    recall1 = recall5 = 0
    mrr = 0.0
    details = []
    for qi, item in enumerate(EVAL_QUERIES):
        ranked = np.argsort(-scores[qi])[:top_k]
        rel = [is_relevant(corpus[ci]["text"], item["expected"]) for ci in ranked]
        first_hit = rel.index(True) + 1 if True in rel else None
        recall1 += bool(first_hit == 1)
        recall5 += bool(first_hit and first_hit <= 5)
        mrr += 1 / first_hit if first_hit else 0
        details.append(
            {
                "query": item["q"],
                "first_hit_rank": first_hit,
                "top1_source": corpus[ranked[0]]["source"],
                "top1_preview": corpus[ranked[0]]["text"][:60].replace("\n", " "),
            }
        )

    n = len(EVAL_QUERIES)
    return {
        "model": name,
        "dimensions": int(corpus_vecs.shape[1]),
        "recall@1": round(recall1 / n, 3),
        "recall@5": round(recall5 / n, 3),
        f"mrr@{top_k}": round(mrr / n, 3),
        "corpus_chunks": len(corpus),
        "embed_throughput_chunks_per_s": round(len(corpus) / embed_secs, 1),
        "details": details,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="POC-R8 Embedding 检索质量评测")
    parser.add_argument("--models", nargs="*", default=None, help="待评测模型名，缺省评测全部")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--chunk-chars", type=int, default=None, help="切片大小，缺省与生产配置一致")
    args = parser.parse_args()

    settings = get_settings()
    registry = EmbeddingRegistry.from_yaml(settings.models_config_path)
    client = EmbeddingClient(registry)
    names = args.models or [m["name"] for m in registry.list_public()]
    chunk_chars = args.chunk_chars or settings.knowledge_chunk_chars

    corpus = build_corpus(chunk_chars)
    print(f"语料: {CORPUS_DIR}（{len(corpus)} 个切片，切片≤{chunk_chars}字）| 查询: {len(EVAL_QUERIES)} 条\n")

    reports = []
    for name in names:
        print(f"== 评测 {name} ==")
        report = await evaluate_model(client, name, corpus, args.top_k)
        reports.append(report)
        for key in ("dimensions", "recall@1", "recall@5", f"mrr@{args.top_k}", "embed_throughput_chunks_per_s"):
            print(f"  {key}: {report[key]}")
        misses = [d for d in report["details"] if d["first_hit_rank"] is None]
        weak = [d for d in report["details"] if d["first_hit_rank"] and d["first_hit_rank"] > 1]
        if misses:
            print("  未命中查询:", "；".join(d["query"] for d in misses))
        if weak:
            print("  非首位命中:", "；".join(f"{d['query']}(第{d['first_hit_rank']}位)" for d in weak))
        print()

    out = BASE_DIR / "outputs" / "embedding_poc_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完整报告已写入 {out}")


if __name__ == "__main__":
    asyncio.run(main())

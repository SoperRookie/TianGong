"""混合检索的关键词侧（F-7-3b）：中文字符二元组 BM25 + RRF 排名融合。

不引入分词依赖：中文用字符 bigram 召回即可覆盖低频专有词（如"限红""重新识别"），
这正是纯向量检索的长尾短板。语料增长到万级切片后按 PRD 迁移 Elasticsearch，
本模块的 RRF 融合逻辑保持不变。
"""

import math
import re
from collections import Counter

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[一-鿿]")

_BM25_K1 = 1.5
_BM25_B = 0.75

# RRF 常数：排名靠后的贡献衰减速度，60 为通用经验值
_RRF_K = 60


def tokenize(text: str) -> list[str]:
    """英文/数字按词、中文按字符二元组（单字文本退化为单字）。"""
    units = _TOKEN_RE.findall(text.lower())
    tokens: list[str] = []
    i = 0
    while i < len(units):
        if len(units[i]) > 1:  # 英文单词/数字整体成词
            tokens.append(units[i])
            i += 1
            continue
        if i + 1 < len(units) and len(units[i + 1]) == 1:
            tokens.append(units[i] + units[i + 1])
        else:
            tokens.append(units[i])
        i += 1
    return tokens


def bm25_scores(query: str, docs: list[str]) -> list[float]:
    """返回 query 对每个 doc 的 BM25 得分（未命中为 0）。"""
    doc_tokens = [tokenize(d) for d in docs]
    doc_freqs = [Counter(t) for t in doc_tokens]
    n = len(docs)
    avgdl = (sum(len(t) for t in doc_tokens) / n) if n else 0.0

    scores = [0.0] * n
    for term in set(tokenize(query)):
        df = sum(1 for f in doc_freqs if term in f)
        if df == 0:
            continue
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        for i, freqs in enumerate(doc_freqs):
            tf = freqs.get(term, 0)
            if tf == 0:
                continue
            dl = len(doc_tokens[i]) or 1
            scores[i] += idf * tf * (_BM25_K1 + 1) / (
                tf + _BM25_K1 * (1 - _BM25_B + _BM25_B * dl / avgdl)
            )
    return scores


def rrf_merge(rankings: list[list[int]], k: int = _RRF_K) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion：融合多路排名（元素为候选 id 的有序列表）。

    返回 (id, 融合得分) 按得分降序；只出现在单路的候选同样保留。
    """
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking, 1):
            fused[cid] = fused.get(cid, 0.0) + 1 / (k + rank)
    return sorted(fused.items(), key=lambda kv: -kv[1])

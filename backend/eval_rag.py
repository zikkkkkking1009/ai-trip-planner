"""R3 评测：BM25 基线 vs 向量重排（golden 30 条，命中率@1/@5 + MRR）。

口径（ROADMAP R3，延续「发现瓶颈→假设→改造→对照」）：
- 检索全库（不做城市过滤——golden query 大多不含城市名，对两种方法公平）；
- BM25 = rag.ask 现线基线；向量 = 语料文本 + 查询各取 embedding，余弦全库排序；
- embedding 走 LLM_FAST_* 通道（OpenAI 兼容 /embeddings，模型 embedding-3），
  **语料向量一次性缓存进 data/rag_embed_cache.json**（gitignore），重跑零花费；
- 指标函数为纯函数，有单测（test_rag_eval.py），embedding 调用不进测试。

用法：cd backend && python eval_rag.py   →  backend/eval_rag.json
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openai import OpenAI

BACKEND = Path(__file__).resolve().parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from rag import ask, build_corpus  # noqa: E402

GOLDEN_FILE = BACKEND / "rag_golden.json"
OUT_FILE = BACKEND / "eval_rag.json"
CACHE_FILE = BACKEND.parent / "data" / "rag_embed_cache.json"
EMBED_MODEL = "embedding-3"
BATCH = 32

log = logging.getLogger(__name__)


def load_golden() -> list[dict[str, str]]:
    raw = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    entries: list[dict[str, str]] = raw["entries"]
    assert len({e["q"] for e in entries}) == len(entries), "golden query 重复"
    return entries


def metrics(ranks: list[int | None]) -> dict:
    """rank（1 起）列表 → 命中率@1/@5 与 MRR；None = 未命中。"""
    n = len(ranks)
    hit1 = sum(1 for r in ranks if r == 1)
    hit5 = sum(1 for r in ranks if r is not None and r <= 5)
    mrr = sum(1.0 / r for r in ranks if r is not None)
    return {"n": n, "hit1_pct": round(hit1 / n * 100, 1),
            "hit5_pct": round(hit5 / n * 100, 1),
            "mrr": round(mrr / n, 3)}


def rank_of(golden: dict[str, str], results: list[dict]) -> int | None:
    """golden 实体在结果列表中的名次（1 起）；city+name 双匹配才算命中。"""
    for i, r in enumerate(results, 1):
        if r["name"] == golden["name"] and r["city"] == golden["city"]:
            return i
    return None


def eval_bm25(golden: list[dict[str, str]]) -> dict:
    ranks = []
    for e in golden:
        res = ask(e["q"], k=10)["results"]
        ranks.append(rank_of(e, res))
    return {"method": "BM25+bigram（现线基线）", "ranks": ranks, **metrics(ranks)}


def _llm_fast() -> tuple[OpenAI, str]:
    from commute import load_env_file
    for k, v in load_env_file().items():     # 先落 .env 再读（顺序反了必假报未配置）
        os.environ.setdefault(k, v)
    import openai
    api_key = os.environ.get("LLM_FAST_API_KEY") or os.environ.get("LLM_API_KEY")
    base_url = os.environ.get("LLM_FAST_BASE_URL") or os.environ.get("LLM_BASE_URL")
    if not api_key or not base_url:
        raise RuntimeError("未配置 LLM_FAST_*/LLM_* 通道，无法取向量")
    return openai.OpenAI(api_key=api_key, base_url=base_url, timeout=30), EMBED_MODEL


def _embed_cached(client: OpenAI, model: str, texts: list[str]) -> list[list[float]]:
    """带磁盘缓存的批量取向量：同文本永不二次计费。"""
    cache: dict[str, list[float]] = {}
    if CACHE_FILE.exists():
        cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    missing = [t for t in texts if t not in cache]
    print(f"  向量缓存命中 {len(texts) - len(missing)}/{len(texts)}，需新取 {len(missing)} 条")
    for i in range(0, len(missing), BATCH):
        batch = missing[i:i + BATCH]
        for attempt in (1, 2, 3):
            try:
                resp = client.embeddings.create(model=model, input=batch)
                for t, d in zip(batch, resp.data):
                    cache[t] = d.embedding
                break
            except Exception as e:
                if attempt == 3:
                    raise
                print(f"  embed 批次失败（{e}），重试 {attempt}/2…")
                time.sleep(2 * attempt)
        print(f"  已取 {min(i + BATCH, len(missing))}/{len(missing)}")
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache), encoding="utf-8")
    return [cache[t] for t in texts]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def eval_vector(golden: list[dict[str, str]]) -> dict:
    docs = build_corpus()
    texts = [d["text"] for d in docs]
    client, model = _llm_fast()
    doc_vecs = _embed_cached(client, model, texts)
    q_vecs = _embed_cached(client, model, [e["q"] for e in golden])
    ranks = []
    for e, qv in zip(golden, q_vecs):
        scored = sorted(zip(docs, doc_vecs), key=lambda p: -_cosine(qv, p[1]))
        order = [d for d, _ in scored]
        ranks.append(rank_of(e, order))
    return {"method": f"向量余弦（{model}，无重排直接全库）", "ranks": ranks, **metrics(ranks)}


def main() -> None:
    golden = load_golden()
    print(f"golden {len(golden)} 条")
    bm = eval_bm25(golden)
    print(f"BM25 : hit@1={bm['hit1_pct']}% hit@5={bm['hit5_pct']}% MRR={bm['mrr']}")
    try:
        vec = eval_vector(golden)
        print(f"向量 : hit@1={vec['hit1_pct']}% hit@5={vec['hit5_pct']}% MRR={vec['mrr']}")
    except Exception as e:
        # 向量臂失败不影响 BM25 基线——降级为 error 记录继续落盘（典型为外部配额问题，非代码缺陷）
        log.warning("向量臂不可用，降级为 error 记录：%s", e)
        vec = {"method": "向量余弦", "error": f"不可用：{e}"}
        print(f"向量 : 不可用（{e}）—— BM25 结果照常落盘")
    out = {"golden_n": len(golden), "bm25": bm, "vector": vec}
    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {OUT_FILE}")


if __name__ == "__main__":
    main()

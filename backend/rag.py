"""R1 检索基线：BM25 + 字符 bigram（零新依赖、零 Key、离线可测）——RAG 骨架。

ROADMAP R1（2026-09-30 立项）：中厂评审指出「工程扎实但 AI 偏浅」，语料现成
（38 城预置景点 + 美食种子 ≈ 600 篇），先上**零新依赖**的检索基线，把「了解 RAG」
升级成「实践 RAG」；向量重排对照（R3）与引用核查（R2，复用 aligner）后续增量。

为什么 bigram：中文无空格分词，jieba 是新增依赖；字符 bigram 对专名检索
（兵马俑 / 乳扇 / 鼓浪屿）召回稳定，600 篇语料下 BM25 毫秒级。
"""
from __future__ import annotations

import math
import re
from functools import lru_cache

from demo_data import DEMO_SPOTS
from models import Spot
from food_seeds import FOOD_SEEDS

_K1, _B = 1.5, 0.75          # Okapi BM25 标准参数
_PUNCT = re.compile(r"[\s·、，,。.\-—()（）【】\[\]!！?？:：;；\"'`~～]+")
_K_LIMIT = 10


def tokenize(text: str) -> list[str]:
    """清洗后切字符 bigram（单字输入降级为单字 token）。"""
    s = _PUNCT.sub("", text)
    if len(s) < 2:
        return [s] if s else []
    return [s[i:i + 2] for i in range(len(s) - 1)]


def build_corpus() -> list[dict[str, str]]:
    """语料文档：{type, city, name, text, source}（每次新建，测试可用）。"""
    docs: list[dict[str, str]] = []
    for city, spots in DEMO_SPOTS.items():
        for s in spots:
            docs.append({
                "type": "景点", "city": city, "name": s.name,
                "text": " ".join(x for x in (s.name, s.desc, city) if x),
                "source": "预置景点库",
            })
    for city, foods in FOOD_SEEDS.items():
        for name, intro in foods:
            docs.append({
                "type": "美食", "city": city, "name": name,
                "text": " ".join(x for x in (name, intro, city) if x),
                "source": "美食种子库",
            })
    return docs


class _Index:
    """BM25 索引：名字权重 ×2（查询意图主要落在专名上）。"""

    def __init__(self, docs: list[dict[str, str]]):
        self.docs = docs
        self.tfs: list[dict[str, int]] = []
        self.dls: list[int] = []
        df: dict[str, int] = {}
        for d in docs:
            tf: dict[str, int] = {}
            for tok in tokenize(d["name"]) * 2 + tokenize(d["text"]):
                tf[tok] = tf.get(tok, 0) + 1
            self.tfs.append(tf)
            self.dls.append(sum(tf.values()))
            for tok in tf:
                df[tok] = df.get(tok, 0) + 1
        self.n = len(docs)
        self.avgdl = (sum(self.dls) / self.n) if self.n else 0.0
        self.idf = {t: math.log((self.n - d + 0.5) / (d + 0.5) + 1.0)
                    for t, d in df.items()}

    def score(self, q_toks: list[str]) -> list[float]:
        scores = [0.0] * self.n
        for tok in set(q_toks):
            idf = self.idf.get(tok)
            if idf is None:
                continue
            qtf = q_toks.count(tok)
            for i, tf in enumerate(self.tfs):
                f = tf.get(tok)
                if not f:
                    continue
                denom = f + _K1 * (1 - _B + _B * self.dls[i] / self.avgdl)
                scores[i] += idf * qtf * f * (_K1 + 1) / denom
        return scores


@lru_cache(maxsize=1)
def _index_cached() -> _Index:
    return _Index(build_corpus())


# ---- R2 引用核查：每条引用必须落到库内可对齐实体（反幻觉闸门）----
# 语料当前 100% 来自库内，"必过"是机制旁证；闸门真正兜底的是未来
# 语料混入外部文本（攻略识别 / 网页抓取）的场景——对不上一律 verified=False。

def _norm(name: str) -> str:
    """归一化：剥括号内容（馆区/分馆后缀）→ 去标点空白 → 小写（对齐器同思路）。"""
    s = re.sub(r"[（(【\[].*?[)）\]】]", "", name)
    return _PUNCT.sub("", s).lower()


@lru_cache(maxsize=1)
def _entity_index() -> tuple[dict[tuple[str, str], Spot],
                             dict[tuple[str, str], None]]:
    """{(city, 归一名): Spot} 与 {(city, 归一名): None}（美食无坐标语义）。"""
    spots: dict[tuple[str, str], Spot] = {}
    for city, spot_list in DEMO_SPOTS.items():
        for s in spot_list:
            spots.setdefault((city, _norm(s.name)), s)
    foods: dict[tuple[str, str], None] = {}
    for city, food_list in FOOD_SEEDS.items():
        for name, _intro in food_list:
            foods.setdefault((city, _norm(name)), None)
    return spots, foods


def verify_citation(doc: dict[str, object]) -> dict[str, object]:
    """单条引用核查：景点 → 库内对齐出真实坐标；美食 → 库内对齐即可。

    对不上库内实体一律 verified=False（宁可标失败，不假装可核查）。
    """
    city = str(doc.get("city", ""))
    name = str(doc.get("name", ""))
    spots, foods = _entity_index()
    key = (city, _norm(name))
    if doc.get("type") == "美食":
        ok = key in foods
        return {"verified": ok, "lat": None, "lon": None}
    spot = spots.get(key)
    if spot is None:
        return {"verified": False, "lat": None, "lon": None}
    return {"verified": True, "lat": float(spot.lat), "lon": float(spot.lon)}


def ask(q: str, city: str | None = None, k: int = 5) -> dict:
    """检索问答：返回带来源引用的结果（R2 将在条目上再做库内对齐核查）。"""
    from cities import normalize_city
    q_toks = tokenize(q)
    index = _index_cached()
    picked = list(range(index.n))
    city_n = normalize_city(city) if city else ""
    if city_n:
        picked = [i for i in picked if index.docs[i]["city"] == city_n]
    scores = index.score(q_toks)
    scored = [(index.docs[i], scores[i]) for i in picked if scores[i] > 0]
    scored.sort(key=lambda x: -x[1])
    kk = max(1, min(k, _K_LIMIT))
    results = [{
        "type": d["type"], "city": d["city"], "name": d["name"],
        "text": d["text"], "source": d["source"], "score": round(s, 3),
    } for d, s in scored[:kk]]
    for r in results:                       # R2：引用核查（verified + 坐标落地）
        r.update(verify_citation(r))
    return {
        "q": q, "city": city_n or None, "k": kk, "results": results,
        "verified_count": sum(1 for r in results if r["verified"]),
        "index": {"docs": index.n, "cities": len(DEMO_SPOTS)},
    }

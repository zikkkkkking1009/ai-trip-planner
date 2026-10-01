"""R3 评测的纯函数单测：指标 / 名次判定 / 余弦——embedding 调用不进测试。"""
from __future__ import annotations

import pytest

from eval_rag import _cosine, load_golden, metrics, rank_of


def test_metrics_full_and_partial():
    m = metrics([1, 3, None])            # 第1条第1名、第2条第3名、第3条脱靶
    assert m == {"n": 3, "hit1_pct": 33.3, "hit5_pct": 66.7, "mrr": 0.444}
    assert metrics([None, None])["mrr"] == 0.0
    assert metrics([1, 1])["hit1_pct"] == 100.0


def test_rank_of_requires_city_and_name():
    res = [{"name": "洪崖洞", "city": "重庆"}, {"name": "洪崖洞", "city": "大理"}]
    assert rank_of({"name": "洪崖洞", "city": "重庆"}, res) == 1
    assert rank_of({"name": "洪崖洞", "city": "大理"}, res) == 2
    assert rank_of({"name": "不存在", "city": "西安"}, res) is None


def test_cosine_basics():
    assert _cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert _cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert _cosine([0, 0], [1, 1]) == 0.0      # 零向量不抛错


def test_load_golden_sane():
    entries = load_golden()
    assert 25 <= len(entries) <= 35, f"golden {len(entries)} 条，应 25~35"
    for e in entries:
        assert {"q", "city", "name"} <= set(e)
    # golden 实体必须真的在语料库里（甄别错误当场暴露）
    from rag import build_corpus
    corpus_keys = {(d["city"], d["name"]) for d in build_corpus()}
    missing = [(e["city"], e["name"]) for e in entries
               if (e["city"], e["name"]) not in corpus_keys]
    assert not missing, f"golden 引用了库外实体：{missing}"

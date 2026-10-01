"""R2 引用核查测试：每条检索引用必须落到库内可对齐实体（反幻觉闸门）。

设计（ROADMAP R2）：景点引用 → 库内对齐出**真实坐标**（verified + lat/lon）；
美食引用 → 库内对齐（无坐标，verified 即可）；对不上的引用 verified=False。
语料当前 100% 来自库内，所以"必过"是机制正确性的旁证——该闸门真正的价值在
未来语料混入外部文本（攻略识别/网页抓取）时兜底。
"""
from __future__ import annotations

import pytest

from rag import ask, verify_citation


def _all_results(queries: list[str]) -> list[dict]:
    out = []
    for q in queries:
        out.extend(ask(q, k=5)["results"])
    return out


# ---- 景点引用：verified + 真实坐标 ----

def test_spot_citations_carry_coordinates():
    res = ask("兵马俑")["results"]
    top = res[0]
    assert top["verified"] is True
    assert isinstance(top["lat"], float) and isinstance(top["lon"], float)
    assert 30 < top["lat"] < 40 and 100 < top["lon"] < 115   # 落在中国域内


def test_every_spot_citation_verified_across_queries():
    for r in _all_results(["兵马俑", "洪崖洞", "莫高窟", "云冈石窟", "布达拉宫"]):
        assert r["type"] == "景点"
        assert r["verified"] is True, f"{r['name']} 未通过库内对齐"
        assert r["lat"] is not None and r["lon"] is not None


# ---- 美食引用：verified 即可（无坐标语义）----

def test_food_citations_verified_without_coords():
    for r in _all_results(["乳扇", "老友粉", "驴肉黄面"]):
        assert r["type"] == "美食"
        assert r["verified"] is True, f"{r['name']} 未通过库内对齐"
        assert "lat" not in r or r["lat"] is None


# ---- 机制：对不上的引用必须拒绝（反幻觉闸门的真值路径）----

def test_verify_rejects_unaligned_entity():
    fake = {"type": "景点", "city": "西安", "name": "不存在的假景点"}
    assert verify_citation(fake)["verified"] is False


def test_verify_normalizes_punctuation_and_suffix():
    # 市后缀与标点差异不应影响对齐
    d = {"type": "景点", "city": "西安", "name": "秦始皇兵马俑博物馆（分馆）"}
    assert verify_citation(d)["verified"] is True


# ---- 响应层：核查统计 ----

def test_ask_response_carries_verified_count():
    res = ask("古城", city="大同")
    assert res["verified_count"] == len(res["results"])
    assert res["verified_count"] > 0

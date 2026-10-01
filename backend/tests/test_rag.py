"""R1 检索基线测试：BM25 + 字符 bigram 的语料完整性 / 排序正确性 / 引用字段 / 边界。

R1 范围（ROADMAP）：语料 = 38 城预置景点 + 美食种子；/ask 带引用返回；
引用核查（R2）与向量重排对照（R3）不在本文件。
"""
from __future__ import annotations

import pytest

from rag import ask, build_corpus, tokenize


# ---- 语料完整性 ----

def test_corpus_nonempty_and_typed():
    docs = build_corpus()
    assert len(docs) >= 300, f"语料 {len(docs)} 篇，景点+美食不该少于 300"
    kinds = {d["type"] for d in docs}
    assert kinds == {"景点", "美食"}


def test_corpus_covers_all_demo_cities():
    docs = build_corpus()
    cities = {d["city"] for d in docs}
    from cities import DEMO_CITIES
    assert set(DEMO_CITIES) <= cities, f"缺城市：{set(DEMO_CITIES) - cities}"


def test_tokenize_bigram():
    assert tokenize("兵马俑") == ["兵马", "马俑"]
    assert tokenize("a 火锅") == ["a火", "火锅"]      # 空白被清掉后跨字符切
    assert tokenize("") == []
    assert tokenize("！？。") == []                    # 纯标点


# ---- 检索正确性 ----

def test_ask_famous_spot_ranks_top():
    res = ask("秦始皇兵马俑")
    assert res["results"], "兵马俑查不到"
    assert res["results"][0]["name"] == "秦始皇兵马俑博物馆"
    assert res["results"][0]["type"] == "景点"


def test_ask_food_ranks_top():
    res = ask("乳扇")
    assert res["results"], "乳扇查不到"
    assert res["results"][0]["name"] == "乳扇"
    assert res["results"][0]["type"] == "美食"
    assert res["results"][0]["city"] == "大理"


def test_ask_city_filter_restricts_results():
    res = ask("古城", city="大同")
    assert res["results"], "大同古城查不到"
    assert all(r["city"] == "大同" for r in res["results"])


def test_ask_partial_query_still_fuzzy():
    res = ask("熊猫")        # 名字是「成都大熊猫繁育研究基地」，子串应召回
    names = [r["name"] for r in res["results"]]
    assert any("熊猫" in n for n in names)


# ---- 引用字段（R1 核心：每条带来源）----

def test_results_carry_citation_fields():
    res = ask("莫高窟")
    top = res["results"][0]
    assert {"type", "city", "name", "source", "score"} <= set(top)
    assert top["source"] in ("预置景点库", "美食种子库")
    assert top["score"] > 0


# ---- 边界 ----

def test_ask_empty_and_garbage():
    assert ask("")["results"] == []
    assert ask("！？。")["results"] == []
    assert ask("完全不存在的词组xyzzy")["results"] == []


def test_ask_k_clamped():
    assert len(ask("古城", k=99)["results"]) <= 10
    assert len(ask("古城", k=1)["results"]) <= 1


def test_ask_unknown_city_returns_empty():
    assert ask("兵马俑", city="火星")["results"] == []

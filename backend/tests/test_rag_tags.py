"""R6 标签过滤集成测试：语料带结构化字段、ask(tag=) 过滤、意图解析跨模块一致性。"""
from __future__ import annotations

from corpus_fields import SPOT_TAGS
from rag import ask, build_corpus
from rag_intent import parse_intent


# ---- 语料字段 ----

def test_corpus_docs_carry_structured_fields():
    docs = {str(d["name"]): d for d in build_corpus()}
    bmy = docs["秦始皇兵马俑博物馆"]
    assert "博物馆" in bmy["tags"] and "历史文化" in bmy["tags"]
    assert bmy["stay_min"] == bmy["stay_min"]          # 结构化停留存在
    assert "ticket" in bmy and "ticket_known" in bmy
    food = docs["乳扇"]
    assert food["tags"] == ["美食"] and food["ticket"] is None


# ---- ask(tag=) 过滤 ----

def test_ask_tag_filter_respects_tag():
    res = ask("兵马俑", tag="博物馆", k=5)["results"]
    assert res, "过滤后不应为空（兵马俑本体就是博物馆）"
    assert all("博物馆" in r["tags"] for r in res), "tag 过滤必须全部命中标签"


def test_ask_tag_filter_can_empty():
    # 兵马俑不是海滨——结构上就不该有结果，宁可空也不给不沾边的
    assert ask("兵马俑", tag="海滨", k=5)["results"] == []


def test_rain_indoor_intent_not_rejected():
    # 「下雨天」原会被气象拒答词表拦下；带了室内意图就应转成室内标签检索
    out = ask("下雨天能去哪，有室内的地方吗", k=5)
    assert not out.get("abstain"), "找室内选项不该被气象拒答"
    assert out["results"], "室内意图应有结果"


def test_intent_tags_subset_of_taxonomy():
    from corpus_fields import SPOT_TAGS as _T
    for q in ("免费景点", "带孩子", "下雨天室内", "看夜景", "爬山", "美食小吃", ""):
        assert set(parse_intent(q)) <= set(_T), q


def test_golden_intent_file_valid():
    import json
    from pathlib import Path
    data = json.loads((Path(__file__).parent.parent / "rag_golden_intent.json")
                      .read_text(encoding="utf-8"))
    assert len(data["queries"]) >= 14
    assert set(data["tags"]) == set(SPOT_TAGS)
    for row in data["queries"]:
        assert row["tag"] in SPOT_TAGS and row["q"].strip()

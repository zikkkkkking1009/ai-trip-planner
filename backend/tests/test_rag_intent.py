"""rag_intent.parse_intent 单测：单/多意图命中、专名不误触发、边界与标签一致性守门。"""
from __future__ import annotations

import pytest

from rag_intent import parse_intent

# 守门清单：与 corpus_fields.SPOT_TAGS 一致的 13 个标签（字面写出，防标签体系漂移）。
SPOT_TAGS = [
    "免费", "亲子", "夜景", "室内", "自然风光", "历史文化", "登山徒步",
    "古镇街区", "博物馆", "寺庙宗教", "海滨", "演出", "美食",
]


@pytest.mark.parametrize(
    ("query", "expected_tag"),
    [
        ("西安有什么免费景点", "免费"),
        ("适合带孩子玩的地方", "亲子"),
        ("下雨天能去哪", "室内"),
        ("晚上想看夜景", "夜景"),
        ("想爬山", "登山徒步"),
        ("想去看博物馆", "博物馆"),
        ("海边玩水", "海滨"),
        ("当地有什么特色小吃", "美食"),
    ],
)
def test_single_intent(query: str, expected_tag: str) -> None:
    """单意图：每个标签族至少一条真实问法能命中对应标签。"""
    tags = parse_intent(query)
    assert expected_tag in tags, (query, tags)


def test_multi_intent_free_and_family() -> None:
    """多意图：「免费又适合孩子的」须同时命中 免费+亲子。"""
    tags = parse_intent("免费又适合孩子的")
    assert {"免费", "亲子"} <= set(tags), tags


def test_multi_intent_order_follows_rule_table() -> None:
    """多意图按规则表顺序去重返回：亲子(规则2)→夜景(规则4)→美食(规则11)。"""
    assert parse_intent("晚上带孩子看夜景吃小吃") == ["亲子", "夜景", "美食"]


def test_proper_noun_spot_no_false_trigger() -> None:
    """专名查询不误触发：景点名仍走纯检索，不被意图规则污染。"""
    assert parse_intent("兵马俑") == []


def test_proper_noun_food_no_false_trigger() -> None:
    """专名查询不误触发：美食名同理（乳扇不含任何规则关键词）。"""
    assert parse_intent("乳扇") == []


def test_empty_query() -> None:
    """边界：空串返回 []。"""
    assert parse_intent("") == []


def test_punctuation_only_query() -> None:
    """边界：纯标点/空白不命中任何标签。"""
    assert parse_intent("！？。，、；：（）") == []
    assert parse_intent("?!.,;: ") == []
    assert parse_intent("   ") == []


def test_results_always_subset_of_spot_tags() -> None:
    """一致性断言：任何返回值都必须 ⊆ 13 标签清单（覆盖命中与不命中两类输入）。"""
    queries = [
        "西安有什么免费景点", "适合带孩子玩的地方", "下雨天能去哪", "晚上想看夜景",
        "想爬山", "古镇老街怎么逛", "看展去哪", "烧香祈福的地方", "海边赶海",
        "看一场演出", "有什么好吃的", "山水风景好的地方", "免费又适合孩子的",
        "晚上带孩子看夜景吃小吃", "兵马俑", "乳扇", "", "!!!",
    ]
    for q in queries:
        assert set(parse_intent(q)) <= set(SPOT_TAGS), q


def test_every_rule_is_reachable() -> None:
    """每条规则至少 1 个关键词能独立触发其标签（防死规则）。"""
    probes = {
        "免费": "穷游怎么玩",
        "亲子": "带娃去哪",
        "室内": "雨天去哪",
        "夜景": "夜市推荐",
        "登山徒步": "远足路线",
        "古镇街区": "老街逛逛",
        "博物馆": "看展",
        "寺庙宗教": "烧香祈福",
        "海滨": "沙滩玩水",
        "演出": "看一场表演",
        "美食": "有什么好吃的",
        "自然风光": "山水风景",
    }
    for tag, q in probes.items():
        assert tag in parse_intent(q), (tag, q, parse_intent(q))


def test_deterministic_pure_function() -> None:
    """确定性：纯函数无状态，同一输入重复解析结果一致。"""
    assert parse_intent("免费又适合孩子的") == parse_intent("免费又适合孩子的")


# ---- 集成期补充（ZCode）：历史文化规则 + 跨模块一致性 ----

def test_history_rule():
    assert "历史文化" in parse_intent("对历史文化感兴趣，有没有古迹类的景点值得去")
    assert "历史文化" in parse_intent("想看看文物古迹")


def test_intent_tags_subset_of_taxonomy():
    from corpus_fields import SPOT_TAGS
    for q in ("免费景点", "带孩子", "下雨天", "爬山", "兵马俑", ""):
        assert set(parse_intent(q)) <= set(SPOT_TAGS), q

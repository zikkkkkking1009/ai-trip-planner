"""A8 测试：挂在主名后面的子景点后缀（与 A7 的尾部子景点对称）。

A7 管的是「长前缀 + 别名在尾部」（北京国际雕塑公园-远望紫禁城）；
A8 管的是「查询名 + 分隔符 + 短后缀」（成都大熊猫繁育研究基地-熊猫塔、杜甫草堂-杜陵村）。

关键不是"能不能识别子景点"，而是**不能误伤合法的上级 POI 扩展名**：
「故宫」→「故宫博物院」、「大雁塔」→「大雁塔文化休闲景区」都是正常对齐，
如果把这类也判成子景点，会把本来正确的对齐打下去（回归）。
"""
from __future__ import annotations

import pytest

from aligner import (_is_appended_subvenue, _is_tailed_subvenue, containment,
                     text_similarity)


@pytest.mark.parametrize("alias,candidate", [
    ("成都大熊猫繁育研究基地", "成都大熊猫繁育研究基地-熊猫塔"),
    ("杜甫草堂", "杜甫草堂-杜陵村"),
    ("西湖", "西湖-断桥残雪"),
    ("故宫", "故宫-太和殿"),
    ("天安门广场", "天安门广场（升旗台）"),
    ("秦始皇兵马俑博物馆", "秦始皇兵马俑博物馆·一号坑"),
])
def test_detects_appended_subvenue(alias, candidate):
    assert _is_appended_subvenue(alias, candidate), f"未识别出子景点：{candidate}"


@pytest.mark.parametrize("alias,candidate", [
    # 合法扩展名：无分隔符（这是正常的"简称 → 全称"）
    ("故宫", "故宫博物院"),
    ("大雁塔", "大雁塔文化休闲景区"),
    ("西湖", "西湖风景名胜区"),
    # 后缀过长 → 更像主景区全称而非子点
    ("大熊猫基地", "大熊猫基地旅游度假区游客服务中心"),
    # 完全相等 / 反向包含
    ("杜甫草堂", "杜甫草堂"),
    ("杜甫草堂博物馆", "杜甫草堂"),
])
def test_does_not_flag_legit_extensions(alias, candidate):
    assert not _is_appended_subvenue(alias, candidate), \
        f"误判为子景点（会打掉正确对齐）：{alias} vs {candidate}"


def test_tailed_and_appended_are_symmetric_but_distinct():
    """两种子景点结构互不误触发：一个在尾部、一个在前部。"""
    assert _is_tailed_subvenue("远望紫禁城", "北京国际雕塑公园-远望紫禁城")
    assert not _is_appended_subvenue("远望紫禁城", "北京国际雕塑公园-远望紫禁城")
    assert _is_appended_subvenue("杜甫草堂", "杜甫草堂-杜陵村")
    assert not _is_tailed_subvenue("杜甫草堂", "杜甫草堂-杜陵村")


def test_similarity_and_containment_both_zero_for_subvenue():
    """两个入口都要压住——只改一个的话，另一个仍会给高分（A8 的 bug 来源就是 containment）。"""
    alias, cand = "成都大熊猫繁育研究基地", "成都大熊猫繁育研究基地-熊猫塔"
    assert text_similarity(alias, cand) == 0.0
    assert containment(alias, cand) == 0.0, \
        "containment 若给 0.85，子景点会压过主景区（这正是 A8 修复的问题）"


def test_containment_still_scores_legit_extension():
    """修复不能把正常包含关系一起打掉。

    注意别把断言写错：短名 vs 长名的**字符相似度天然不高**（「故宫」vs「故宫博物院」
    只有 0.41），主判据其实是 containment=0.85。这里只验证"没被压到 0"。
    """
    assert containment("故宫", "故宫博物院") == 0.85
    assert text_similarity("故宫", "故宫博物院") > 0.3, "不该被子景点规则压到 0"


def test_exact_match_still_wins():
    """主景区条目存在时，它必须比子景点得分高。"""
    alias = "成都大熊猫繁育研究基地"
    main = containment(alias, alias)                       # 完全匹配
    sub = containment(alias, alias + "-熊猫塔")             # 子点
    assert main == 1.0 and sub == 0.0 and main > sub

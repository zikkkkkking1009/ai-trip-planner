"""城市表与归一化测试：多城市泛化的地基。

这些测试存在的理由：泛化问题最危险的形态是**静默错误**——
用错城市搜 POI、坐标落到别的城市，系统不报错但结果是错的。
所以这里把「归一化」「未知城市的处理」都锁成断言。
"""
from __future__ import annotations

import pytest

from cities import (CITY_ADCODES, CITY_CENTERS, DEFAULT_CITY, DEMO_CITIES,
                    city_center, is_known_city, normalize_city)


def test_all_demo_cities_have_center_and_adcode():
    """每个可演示城市都必须有中心坐标与 adcode（否则兜底坐标会退回西安）。"""
    for city in DEMO_CITIES:
        assert city in CITY_CENTERS, f"{city} 缺中心坐标"
        assert city in CITY_ADCODES, f"{city} 缺 adcode"
        lat, lon = CITY_CENTERS[city]
        assert 18 <= lat <= 54, f"{city} 纬度越界：{lat}"      # 中国纬度范围
        assert 73 <= lon <= 135, f"{city} 经度越界：{lon}"     # 中国经度范围


def test_default_city_is_demo_city():
    assert DEFAULT_CITY in DEMO_CITIES


@pytest.mark.parametrize("raw,expected", [
    ("成都", "成都"),
    ("成都市", "成都"),
    ("  北京  ", "北京"),
    ("杭州市", "杭州"),
    ("西安市", "西安"),
    ("", ""),
    (None, ""),
])
def test_normalize_city(raw, expected):
    assert normalize_city(raw) == expected


def test_city_center_known_and_unknown():
    assert city_center("成都") == CITY_CENTERS["成都"]
    assert city_center("成都市") == CITY_CENTERS["成都"]   # 带后缀也应命中
    # 未知城市必须返回 None —— 调用方据此提示用户，而不是静默用别的城市
    assert city_center("火星") is None
    assert city_center("") is None
    assert city_center(None) is None
    assert is_known_city("火星") is False
    assert is_known_city("北京") is True


def test_city_center_tolerates_dirty_value():
    """LLM 可能返回「成都 4 日游」这类脏值，应能捞出已知城市。"""
    assert city_center("成都 4 日游") == CITY_CENTERS["成都"]


def test_centers_are_distinct():
    """五个城市中心不能重合（否则说明数据抄错了）。"""
    values = list(CITY_CENTERS.values())
    assert len(set(values)) == len(values)

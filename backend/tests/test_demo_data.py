"""演示数据测试：多城市数据的完整性与「坐标不错配」守卫。

最关键的一条是 `test_spots_lie_near_their_city`：
如果哪天有人手工改坐标、或抓取脚本串了城市，这个测试会立刻报错——
这正是本次改造要根除的那类静默错误（粘成都攻略得到西安坐标）。
"""
from __future__ import annotations

import pytest

from cities import DEMO_CITIES, city_center, normalize_city
from demo_data import (DEMO_SPOTS, XI_AN_SPOTS, demo_cities, demo_spots)

# 允许的最大偏移（度）：城市辖区内景点距离市中心一般 < 1.5 度（约 160km）
MAX_OFFSET_DEG = 1.5


def test_xi_an_spots_alias_kept_for_backwards_compat():
    """Xi_AN_SPOTS 被 8 处引用（evaluation / run_demo / 测试），不能被重命名。"""
    assert len(XI_AN_SPOTS) == 14
    assert DEMO_SPOTS["西安"] is XI_AN_SPOTS


def test_demo_cities_listed():
    cities = demo_cities()
    assert cities == [c for c in DEMO_CITIES if c in DEMO_SPOTS]
    assert len(cities) >= 5, "至少应有 5 个可演示城市"


@pytest.mark.parametrize("city", DEMO_CITIES)
def test_each_city_has_enough_spots(city):
    spots = demo_spots(city)
    assert len(spots) >= 8, f"{city} 只有 {len(spots)} 个景点，不足以排多日行程"


@pytest.mark.parametrize("city", DEMO_CITIES)
def test_spots_lie_near_their_city(city):
    """每个景点坐标必须落在该城市中心附近 —— 防止「坐标串城」的静默错误。"""
    center = city_center(city)
    assert center is not None
    lat_c, lon_c = center
    for s in demo_spots(city):
        assert abs(s.lat - lat_c) <= MAX_OFFSET_DEG, \
            f"{city} 的「{s.name}」纬度 {s.lat} 偏离城市中心 {lat_c}"
        assert abs(s.lon - lon_c) <= MAX_OFFSET_DEG, \
            f"{city} 的「{s.name}」经度 {s.lon} 偏离城市中心 {lon_c}"


@pytest.mark.parametrize("city", DEMO_CITIES)
def test_spot_fields_are_sane(city):
    for s in demo_spots(city):
        assert 30 <= s.stay_min <= 480, f"{s.name} 停留时长异常：{s.stay_min}"
        assert 0 <= s.score <= 10, f"{s.name} 评分越界：{s.score}"
        assert s.ticket >= 0
        assert 0 <= s.open_h < s.close_h <= 24, f"{s.name} 开放时间异常"
        assert s.name.strip(), "景点名不能为空"


def test_unknown_city_returns_empty_not_default():
    """未知城市必须返回空列表：不能静默拿别的城市数据顶上。"""
    assert demo_spots("火星") == []
    assert demo_spots("") == []
    assert demo_spots(None) == []


def test_city_name_suffix_tolerated():
    assert len(demo_spots("成都市")) == len(demo_spots("成都"))


def test_source_ids_unique_within_city():
    for city in DEMO_CITIES:
        ids = [s.source_id for s in demo_spots(city)]
        assert len(ids) == len(set(ids)), f"{city} 的 source_id 有重复"


def test_no_coordinate_duplicates_within_city():
    """同城景点不能共用一个坐标（抓取串行时容易把上一条的坐标带进来）。"""
    for city in DEMO_CITIES:
        seen = [(round(s.lat, 4), round(s.lon, 4)) for s in demo_spots(city)]
        assert len(seen) == len(set(seen)), f"{city} 存在重复坐标：{seen}"

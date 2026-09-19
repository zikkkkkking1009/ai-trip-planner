"""城市中心表：多城市泛化的基础设施。

**为什么需要它**：抽取阶段的 LLM 只给景点名、给不出可信坐标，需要一个兜底坐标
（拿到对齐结果前的占位）。原先这个兜底被写死成西安坐标（34.26, 108.94），
意味着「粘一段成都攻略」会得到一堆西安坐标的景点——而且**不报错**，是静默错误。

**坐标来源**：高德地理编码 API（GCJ-02），2026-09-19 由 `tools/build_demo_data.py`
抓取，adcode 记录在旁便于复核。不要手工改这些数字——需要新增城市时跑那个脚本。

用法：
    from cities import city_center, normalize_city, DEMO_CITIES
    center = city_center("成都市")      # → (30.572961, 104.066301)
"""
from __future__ import annotations

import re

# 城市 → (纬度, 经度)，高德地理编码真实值
CITY_CENTERS: dict[str, tuple[float, float]] = {
    "西安": (34.343207, 108.939645),   # adcode=610100
    "成都": (30.572961, 104.066301),   # adcode=510100
    "北京": (39.904179, 116.407387),   # adcode=110000
    "杭州": (30.246566, 120.209903),   # adcode=330100
    "重庆": (29.562680, 106.551787),   # adcode=500000
}

CITY_ADCODES: dict[str, str] = {
    "西安": "610100", "成都": "510100", "北京": "110000",
    "杭州": "330100", "重庆": "500000",
}

# demo 数据覆盖的城市（零 Key 可演示）；任意其他城市仍可通过「粘贴攻略 + 高德对齐」使用
DEMO_CITIES: tuple[str, ...] = ("西安", "成都", "北京", "杭州", "重庆")

# 兜底城市：仅在城市完全无法确定时使用（前端会提示用户选择）
DEFAULT_CITY = "西安"

_CITY_SUFFIX = re.compile(r"(市|地区|特别行政区|自治州|自治县)$")


def normalize_city(name: str | None) -> str:
    """城市名归一化：去空白、去「市」等后缀。

    LLM 抽取、用户输入、前端下拉可能给出「成都市」「北京市」，统一成「成都」「北京」。
    """
    if not name:
        return ""
    return _CITY_SUFFIX.sub("", str(name).strip())


def city_center(city: str | None) -> tuple[float, float] | None:
    """取城市中心坐标；未知城市返回 None（调用方需决定降级策略，不要静默用别的城市）。"""
    key = normalize_city(city)
    if not key:
        return None
    if key in CITY_CENTERS:
        return CITY_CENTERS[key]
    # 容错：给出「西安市」这类带后缀但库里有基名的，或「成都 4 日游」这类脏值
    for known, center in CITY_CENTERS.items():
        if known in key:
            return center
    return None


def is_known_city(city: str | None) -> bool:
    return city_center(city) is not None

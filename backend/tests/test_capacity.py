"""规划前容量预估测试：算术口径 / NN 装箱 / 酒店锚点方向性 / 空列表边界。

capacity_estimate 是纯算术（零网络零求解器）——估算有误差，只断言方向与结构，
不断言具体分钟数。
"""
from __future__ import annotations

import pytest

from models import Hotel, PlanRequest, Spot
from solver import capacity_estimate


def _spot(i: int, lat: float, lon: float, stay: int = 60) -> Spot:
    return Spot(source_id=i, name=f"景点{i}", lat=lat, lon=lon,
                stay_min=stay, score=1.0)


def _req(spots, days=2, start=9.0, end=18.0, hotel=None) -> PlanRequest:
    return PlanRequest(city="西安", days=days, daily_start_h=start,
                       daily_end_h=end, spots=spots, hotel=hotel)


# ---- 装得下 ----

def test_fits_with_room_to_spare():
    spots = [_spot(1, 34.260, 108.940), _spot(2, 34.261, 108.941), _spot(3, 34.262, 108.942)]
    out = capacity_estimate(_req(spots))
    assert out["fits"] is True
    assert out["likely_planned"] == 3
    assert out["likely_unplanned"] == []
    assert out["shortage_min"] == 0
    assert out["needed_min"] > 180          # 3×60 停留 + 通勤
    assert out["available_min"] == 1080.0   # 2 天 × 9 小时


# ---- 装不下：报缺口、报名单、给建议 ----

def test_shortage_reports_unplanned_and_advice():
    spots = [_spot(i, 34.26 + i * 0.001, 108.94 + i * 0.001) for i in range(1, 6)]
    out = capacity_estimate(_req(spots, days=1, start=9.0, end=12.0))
    assert out["fits"] is False
    assert out["shortage_min"] > 0
    assert 1 <= out["likely_planned"] <= 4
    assert len(out["likely_unplanned"]) == 5 - out["likely_planned"]
    assert out["needed_min"] > out["available_min"]
    assert "可能" in out["message"], "估算有误差，措辞必须是「可能」级"


# ---- 酒店锚点：住得远会拉长通勤（方向性） ----

def test_hotel_anchor_increases_needed():
    spots = [_spot(i, 34.26 + i * 0.001, 108.94 + i * 0.001) for i in range(1, 6)]
    without = capacity_estimate(_req(spots))["needed_min"]
    far_hotel = Hotel(name="远郊酒店", lat=34.45, lon=108.75)
    with_hotel = capacity_estimate(_req(spots, hotel=far_hotel))["needed_min"]
    assert with_hotel > without


# ---- 边界 ----

def test_empty_spots_raises():
    with pytest.raises(ValueError):
        capacity_estimate(_req([]))


def test_single_spot_tight_window_still_estimates():
    # 30 分钟窗口装不下 60 分钟停留——单景点也要给出明确的「装不下」与名单
    out = capacity_estimate(_req([_spot(1, 34.26, 108.94, stay=60)],
                                 days=1, start=9.0, end=9.5))
    assert out["fits"] is False
    assert out["likely_planned"] == 0
    assert out["likely_unplanned"] == ["景点1"]

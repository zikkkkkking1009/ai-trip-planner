"""排期求解器单元测试。"""
import pytest

from demo_data import XI_AN_SPOTS
from models import PlanRequest
from solver import Solver, commute_min, haversine_km


def _req(days: int = 3, budget: float | None = 500) -> PlanRequest:
    return PlanRequest(city="西安", days=days, budget=budget,
                       spots=[s.model_copy() for s in XI_AN_SPOTS])


def test_haversine_known_distance():
    # 西安钟楼-兵马俑直线约 30km（允许粗略误差）
    d = haversine_km(34.2610, 108.9420, 34.3847, 109.2785)
    assert 25 < d < 40


def test_commute_min_has_floor():
    a, b = XI_AN_SPOTS[0], XI_AN_SPOTS[1]
    assert commute_min(a, b) >= 10.0


def test_all_planned_spots_within_time_windows():
    req = _req(days=3, budget=None)
    days, unplanned, cost, _ = Solver(req).solve()
    for d in days:
        for v in d.spots:
            s = next(s for s in req.spots if s.name == v.name)
            assert v.depart_h <= s.close_h + 1e-6, f"{v.name} 超过关门时间"
            assert v.depart_h <= req.daily_end_h + 1e-6
            assert v.arrive_h >= req.daily_start_h - 1e-6


def test_budget_is_hard_constraint():
    # 预算压到 200：任何一天的门票总和都不该让总价超 200
    req = _req(days=2, budget=200)
    days, unplanned, cost, _ = Solver(req).solve()
    assert cost <= 200 + 1e-6
    # 被放弃的景点应标注原因
    reasons = {u.reason for u in unplanned}
    assert reasons <= {"预算不足", "时间窗装不下"}
    assert any(u.reason == "预算不足" for u in unplanned)


def test_solution_uses_all_days_when_spots_abundant():
    req = _req(days=3, budget=None)
    days, unplanned, _, _ = Solver(req).solve()
    empty = [d for d in days if not d.spots]
    # 14 个景点 3 天，除非时间窗卡死，不应有空天
    assert not empty or len(unplanned) >= 3


def test_solver_is_deterministic():
    r1 = Solver(_req()).solve()
    r2 = Solver(_req()).solve()
    assert r1 == r2


def test_hotel_anchor_counts_into_commute_and_window():
    from models import Hotel
    spots = [s for s in XI_AN_SPOTS if s.name in ("钟楼", "回民街")]
    # 酒店设在兵马俑附近（离市区 ~25km），每天往返酒店的长途必须计入
    req = PlanRequest(city="西安", days=1,
                      spots=[s.model_copy() for s in spots],
                      hotel=Hotel(name="远郊酒店", lat=34.38, lon=109.28))
    days, unplanned, cost, _ = Solver(req).solve()
    d = days[0]
    assert d.spots, "景点应被排入"
    assert d.commute_min > 60, "酒店往返通勤应计入当天通勤"
    for v in d.spots:
        assert v.depart_h <= req.daily_end_h + 1e-6

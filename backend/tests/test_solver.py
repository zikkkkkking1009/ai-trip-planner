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


def _tight_req():
    """紧张实例：景点多、天数少（CP-SAT 对照显示旧贪心在这类实例上 gap 20%+）。"""
    return PlanRequest(city="西安", days=2, budget=300,
                       daily_start_h=9.0, daily_end_h=18.0,
                       spots=[s.model_copy() for s in XI_AN_SPOTS[:13]])


def test_multistart_not_worse_than_single_start(monkeypatch):
    """多起点随机重启的结果不得劣于单起点贪心（第 0 轮即保底，skipped 也不能更差）。"""
    from solver import SCORE_OBJ_W, Solver

    req = _tight_req()
    days_m, _, _, score_m = Solver(req).solve()
    obj_m = SCORE_OBJ_W * score_m - sum(d.commute_min for d in days_m)

    import solver as solver_mod
    monkeypatch.setattr(solver_mod, "MULTISTART_ITERS", 1)   # 常量在模块级
    days_s, _, _, score_s = Solver(req).solve()
    obj_s = SCORE_OBJ_W * score_s - sum(d.commute_min for d in days_s)

    # 容差 1.0：内部择优用未取整通勤，而这里用的是 DayPlan 里取整到 0.1 分钟的值，
    # 会产生 0.1 量级的假性反向差异；真实回归的量级是数千（gap 20% ≈ 数千分）。
    assert obj_m >= obj_s - 1.0


def test_multistart_is_reproducible():
    """固定随机种子：同一输入两次求解结果完全一致（实验可复现）。"""
    from solver import Solver

    req = _tight_req()
    a = Solver(req).solve()
    b = Solver(req).solve()
    assert a == b


def test_multistart_respects_time_budget():
    """时间预算：即便景点数达上限，单次求解也要明显快于交互阈值。"""
    import time

    from solver import Solver
    req = PlanRequest(city="西安", days=3, budget=None,
                      daily_start_h=9.0, daily_end_h=18.0,
                      spots=[s.model_copy() for s in XI_AN_SPOTS])
    t0 = time.perf_counter()
    Solver(req).solve()
    assert time.perf_counter() - t0 < 2.0, "交互式排期应在 2 秒内返回"

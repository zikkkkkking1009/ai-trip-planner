"""CP-SAT 精确求解器测试（缺少 ortools 时自动跳过，保证 CI 不依赖它）。"""
import pytest

pytest.importorskip("ortools", reason="ortools 未安装（仅实验环境需要）")

from demo_data import XI_AN_SPOTS
from models import PlanRequest
from solver import Solver
from solver_cpsat import SCORE_W, CPSatSolver


def _req(n=8, days=2):
    return PlanRequest(city="西安", days=days, budget=500,
                       daily_start_h=9.0, daily_end_h=18.0,
                       spots=[s.model_copy() for s in XI_AN_SPOTS[:n]])


def test_cpsat_solves_and_covers_constraints():
    """基本正确性：能求解、时间窗与预算不越界。"""
    req = _req()
    days, unplanned, cost, score = CPSatSolver(req, time_limit=5).solve()
    assert days, "应至少排入一个景点"
    assert all(v.depart_h <= req.daily_end_h + 1e-6 for d in days for v in d.spots)
    assert cost <= (req.budget or 1e9) + 1e-6


def test_cpsat_not_worse_than_heuristic_with_floor():
    """下界约束：以启发式目标值为下界时，返回值保证不差于启发式。"""
    req = _req(n=10)
    days_h, _, _, score_h = Solver(req).solve()
    comm_h = sum(d.commute_min for d in days_h)
    floor = SCORE_W * score_h - comm_h

    cs = CPSatSolver(req, time_limit=2, warm_start=days_h, target_floor=floor)
    days_c, _, _, score_c = cs.solve()
    assert days_c, "应返回可行解"
    comm_c = sum(d.commute_min for d in days_c)
    assert SCORE_W * score_c - comm_c >= floor - 1e-6


def test_cpsat_warm_start_does_not_break_model():
    """暖启动的 hint 必须合法：重复 hint 同一变量会让模型变成 MODEL_INVALID。"""
    req = _req(n=9)
    days_h, *_ = Solver(req).solve()
    cs = CPSatSolver(req, time_limit=2, warm_start=days_h)
    days_c, *_ = cs.solve()
    assert cs.status_name in ("OPTIMAL", "FEASIBLE"), cs.status_name
    assert days_c

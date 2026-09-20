"""A2 偏好权重测试。

最重要的两条：
1. **balanced 必须与加偏好前的旧口径完全一致**（否则所有历史实验数据都失效）
2. **每种偏好必须在对应指标上真的产生差异**（权重是标定出来的，不是拍脑袋；
   曾踩过：只调 `_objective` 权重而不加"主动放弃景点"算子，偏好完全没效果）
"""
from __future__ import annotations

import pytest

import editor
from demo_data import XI_AN_SPOTS
from models import PlanRequest, Spot
from solver import (MORE_SPOTS_EXTRA_H, PREFERENCES, Solver,
                    normalize_preference)


def _req(pref: str = "balanced", **kw) -> PlanRequest:
    return PlanRequest(city="西安", days=2, budget=500,
                       spots=[s.model_copy() for s in XI_AN_SPOTS],
                       preference=pref, **kw)


def _solve(pref: str):
    s = Solver(_req(pref))
    days, unplanned, cost, score = s.solve()
    return {
        "commute": sum(d.commute_min for d in days),
        "cost": cost,
        "score": score,
        "planned": sum(len(d.spots) for d in days),
        "solver": s,
    }


def test_normalize_preference_falls_back_to_default():
    assert normalize_preference("乱写的") == "balanced"
    assert normalize_preference("LESS_WALK") == "balanced"   # 大小写敏感 → 回退
    assert normalize_preference(None) == "balanced"
    assert normalize_preference("less_walk") == "less_walk"


def test_balanced_matches_legacy_objective():
    """balanced 的目标值必须等于旧口径 `1000·收益 − 通勤`（无回归）。"""
    s = Solver(_req("balanced"))
    s.solve()
    legacy = 1000 * s._planned_score() - s._global_commute()
    assert abs(s._objective() - legacy) < 1e-6
    assert PREFERENCES["balanced"]["commute"] == 1.0
    assert PREFERENCES["balanced"]["cost"] == 0.0


def test_less_walk_reduces_commute():
    """少走路：通勤必须明显下降，且不能把景点砍太多。"""
    base, less = _solve("balanced"), _solve("less_walk")
    assert less["commute"] < base["commute"], \
        f"少走路没有减少通勤：{base['commute']} → {less['commute']}"
    assert less["planned"] >= base["planned"] - 2, \
        "少走路把景点砍太多了（权重标定过大）"


def test_save_money_reduces_ticket_cost():
    """省钱：总门票必须明显下降。"""
    base, save = _solve("balanced"), _solve("save_money")
    assert save["cost"] < base["cost"] * 0.85, \
        f"省钱没有明显降门票：{base['cost']} → {save['cost']}"


def test_more_spots_extends_window_and_keeps_spots():
    """多玩：放宽时间窗（不是靠调权重——调权重实测无效）。"""
    s = Solver(_req("more_spots"))
    assert abs(s.req.daily_end_h - (18.0 + MORE_SPOTS_EXTRA_H)) < 1e-9
    base, more = _solve("balanced"), _solve("more_spots")
    assert more["planned"] >= base["planned"], "多玩没有排出更多景点"


def test_more_spots_does_not_mutate_caller_request():
    """放宽时间窗只能改求解器内的副本，不能污染调用方传入的 req。"""
    req = _req("more_spots")
    Solver(req)
    assert req.daily_end_h == 18.0, "外部传入的 req 被改了（会影响后续重排）"


def test_pref_op_leaves_spots_untouched():
    """pref 操作只登记说明，不改景点列表（真正切换偏好在上层写回请求参数）。"""
    base = [Spot(source_id=i, name=f"景点{i}", lat=34.0 + i / 100, lon=108.9,
                 stay_min=60, score=8.0, ticket=0, open_h=9.0, close_h=18.0)
            for i in range(3)]
    spots, changes = editor.apply_ops(base, [{"op": "pref", "value": "less_walk"}])
    assert len(spots) == len(base)
    assert any("偏好" in c for c in changes)


def test_all_preferences_are_calibrated():
    """每种偏好的权重都必须被标定过（不是 0/1 这种"等于没设"的值）。"""
    for name in ("less_walk", "save_money"):
        w = PREFERENCES[name]
        assert max(w["commute"], w["cost"]) >= 50, \
            f"{name} 的权重过小，实测不会产生可感知差异（标定要求 ≥50）"

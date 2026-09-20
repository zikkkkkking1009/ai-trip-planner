"""N3b 稳健排程测试：缓冲机制必须"按内缩窗口排、按原始窗口判定"。

这里守住的是一条**踩过坑才明白**的语义（第一版实现搞错了）：
求解器总会把可用时间窗填到 ~93%，所以如果"排程"和"判定"都用收窄后的窗口，
缓冲等于不存在——实测 15.5→14.0 时按时概率只从 3% 升到 7%，远达不到 70%。
正确做法：**按 13.5 时收工排，但仍按 15.5 时判定**。

用桩 solve_fn / sim_fn 精确验证：
- 不要求稳妥 → 完全不干预（且不丢 unplanned/cost/score）
- 首轮就达标 → 不重排（曾因局部变量未初始化而 UnboundLocalError）
- 不达标 → 缓冲单调增大，且**判定始终用原始 req**
- 到上限仍不达标 → 如实报告（不许假装达标）
- 缓冲量参考 P90 超时（固定 30 分钟步长实测收敛太慢）
"""
from __future__ import annotations

from types import SimpleNamespace

from robustness import robustify


def _req(end_h=18.0, start_h=9.0, rob=0.8):
    """带 model_copy 的轻量 req 替身（模拟收窄窗口后的新 req）。"""
    def model_copy(update=None):
        new_end = (update or {}).get("daily_end_h", end_h)
        return _req(end_h=new_end, start_h=start_h, rob=rob)
    return SimpleNamespace(daily_start_h=start_h, daily_end_h=end_h, robustness=rob,
                           model_copy=model_copy)


class _Day:
    def __init__(self, p90):
        self.return_p90 = p90


class _Sim:
    """桩模拟：概率可控；per_day 的 P90 驱动自适应缓冲。"""

    def __init__(self, prob, p90=None):
        self.on_time_prob = prob
        self.per_day = [_Day(p90)] if p90 is not None else []


def _solve_recording():
    seen = []

    def solve(r):
        seen.append(r.daily_end_h)
        return (["dp"], ["un"], 100.0, 50.0)
    return solve, seen


def test_no_target_means_no_intervention():
    req = _req(rob=0.0)
    solve, seen = _solve_recording()
    days, un, c, s, info = robustify(req, ["orig"], ["u0"], 7.0, 3.0, None, solve)
    assert info == {}
    assert seen == []
    assert (days, un, c, s) == (["orig"], ["u0"], 7.0, 3.0)


def test_first_round_meets_target_keeps_values():
    """首轮达标是最常见路径——绝不能把 unplanned/cost/score 覆盖成空值（曾崩过）。"""
    req = _req(rob=0.8)
    solve, seen = _solve_recording()
    days, un, c, s, info = robustify(req, ["orig"], ["u0"], 7.0, 3.0,
                                     lambda d, r: _Sim(0.9), solve)
    assert info["reached"] is True and info["rounds"] == 1
    assert seen == [], "已达标不该重排"
    assert (days, un, c, s) == (["orig"], ["u0"], 7.0, 3.0)


def test_judges_with_original_request_but_schedules_with_buffer():
    """核心语义：判定用原始 18.0，排程用内缩窗口（否则缓冲等于不存在）。"""
    req = _req(end_h=18.0, rob=0.8)
    solve, seen = _solve_recording()
    judged = []

    def sim(days, r):
        judged.append(r.daily_end_h)
        return _Sim(0.3 if len(judged) < 3 else 0.9, p90=19.0)

    days, un, c, s, info = robustify(req, ["orig"], [], 0.0, 0.0, sim, solve)
    assert all(abs(j - 18.0) < 1e-9 for j in judged), \
        f"判定必须始终用原始时间窗 18.0，实际 {judged}"
    assert seen and all(w < 18.0 for w in seen), f"排程应使用内缩窗口，实际 {seen}"
    assert info["reached"] is True
    assert info["solve_end_h"] < 18.0 and info["buffer_h"] > 0


def test_buffer_grows_monotonically_and_stops_at_floor():
    req = _req(start_h=9.0, end_h=15.0, rob=0.9)     # 内缩下限 = 9+5 = 14.0
    solve, seen = _solve_recording()
    days, un, c, s, info = robustify(req, ["orig"], [], 0.0, 0.0,
                                     lambda d, r: _Sim(0.05, p90=20.0), solve)
    assert info["reached"] is False
    assert "未达标" in info["note"]
    assert all(w >= 14.0 - 1e-9 for w in seen), f"排程窗口越过了下限：{seen}"
    assert info["solve_end_h"] >= 14.0 - 1e-9
    assert all(seen[i] >= seen[i + 1] - 1e-9 for i in range(len(seen) - 1)), \
        "缓冲应当单调增大（排程窗口单调不增）"


def test_max_rounds_respected():
    req = _req(end_h=23.0, rob=0.9)
    solve, seen = _solve_recording()
    *_rest, info = robustify(req, ["orig"], [], 0.0, 0.0,
                             lambda d, r: _Sim(0.01, p90=30.0), solve, max_rounds=2)
    assert info["rounds"] == 2
    assert len(seen) == 2, f"max_rounds=2 时最多重排 2 次，实际 {len(seen)}"


def test_adaptive_buffer_uses_p90_overrun():
    """缓冲量参考 P90 超时，而不是固定 30 分钟（固定步长实测收敛太慢）。"""
    req = _req(end_h=18.0, rob=0.9)
    solve, seen = _solve_recording()
    robustify(req, ["orig"], [], 0.0, 0.0,
              lambda d, r: _Sim(0.1, p90=19.5), solve, step_h=0.5)
    assert seen, "应该重排过"
    assert 18.0 - seen[0] >= 1.4, \
        f"应按 P90 超时量（1.5h）留缓冲，实际只留 {18.0 - seen[0]:.2f}h"

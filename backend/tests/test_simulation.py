"""N3 稳健性模拟测试：验证统计性质，而不是"跑一遍没报错"。

模拟器的正确性只能靠**性质**来检验：
- 无波动 → 必然按时（概率=1）
- 波动越大 → 按时概率单调下降
- 固定种子 → 结果完全可复现
- 紧凑行程里，砍掉最长停留的景点应当显著提升按时概率（风险点识别有效）
- 早到景点要等开门（否则"排太早"的隐性成本会被低估）
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from models import DayPlan, Spot, VisitedSpot
from simulation import NoiseModel, simulate

HOTEL = SimpleNamespace(lat=34.26, lon=108.94)


def commute_10min(a, b) -> float:
    """桩通勤：每次 10 分钟。

    ⚠️ 刻意**校验参数形态**：真实 `commute_min` 收的是带 .lat/.lon 的对象。
    第一版桩写成 `def commute_10min(_a, _b)` 什么都能收，于是"传元组"的接口不匹配
    在单测里全绿、到真实服务上才 500（AttributeError: 'tuple' object has no attribute 'lat'）。
    """
    for x in (a, b):
        assert hasattr(x, "lat") and hasattr(x, "lon"), \
            f"通勤函数应接收带 lat/lon 的对象，收到的是 {type(x).__name__}"
    return 10.0


def make_spot(name: str, stay: int, open_h: float = 8.0) -> Spot:
    return Spot(source_id=0, name=name, lat=34.26, lon=108.94,
                stay_min=stay, score=8.0, ticket=0, open_h=open_h, close_h=22.0)


def make_day(day_no: int, specs: list[tuple[str, int]]) -> DayPlan:
    """按 (名字, 停留分钟) 造一个 DayPlan（arrive/depart 只用于兜底，模拟器会用 spot_by_name）。"""
    spots, t = [], 9.0
    for name, stay in specs:
        arrive = t
        t += stay / 60.0
        spots.append(VisitedSpot(name=name, arrive_h=round(arrive, 2),
                                 depart_h=round(t, 2), ticket=0))
    return DayPlan(day=day_no, spots=spots, commute_min=10.0 * (len(specs) + 1),
                   cost=0.0, active_min=float(sum(s for _n, s in specs)))


def _req(start=9.0, end=18.0):
    return SimpleNamespace(daily_start_h=start, daily_end_h=end, hotel=HOTEL)


def test_no_noise_means_certain():
    """零波动 → 只要纸面可行就必然按时（概率 1）。"""
    day = make_day(1, [("A", 60), ("B", 60)])
    spots = {n: make_spot(n, s) for n, s in [("A", 60), ("B", 60)]}
    res = simulate([day], spots, _req(), commute_10min, runs=200,
                   noise=NoiseModel(stay_low=1.0, stay_high=1.0, commute_sigma=0.0))
    assert res.on_time_prob == 1.0
    assert res.per_day[0].on_time_prob == 1.0


def test_more_noise_lowers_on_time_probability():
    """波动越大，按时概率越低（单调性）——这是模拟器"有反应"的基本证据。"""
    # 3×90 分钟停留 + 4×10 分钟通勤 = 310 分钟 → 约 14:10 返回，距 15:00 有 50 分钟余量，
    # 正好能体现"小波动赶得上、大波动赶不上"（用 4 个景点会必然超时，两边概率都是 0 无法比较）
    specs = [("A", 90), ("B", 90), ("C", 90)]
    day = make_day(1, specs)
    spots = {n: make_spot(n, s) for n, s in specs}
    small = simulate([day], spots, _req(end=15.0), commute_10min, runs=400,
                     noise=NoiseModel(stay_low=0.95, stay_high=1.05, commute_sigma=0.02))
    large = simulate([day], spots, _req(end=15.0), commute_10min, runs=400,
                     noise=NoiseModel(stay_low=0.5, stay_high=2.0, commute_sigma=0.5))
    assert small.on_time_prob > large.on_time_prob, \
        f"波动更大反而更按时：{small.on_time_prob} vs {large.on_time_prob}"


def test_deterministic_with_fixed_seed():
    specs = [("A", 120), ("B", 120), ("C", 120)]
    day = make_day(1, specs)
    spots = {n: make_spot(n, s) for n, s in specs}
    a = simulate([day], spots, _req(end=16.0), commute_10min, runs=300, seed=7)
    b = simulate([day], spots, _req(end=16.0), commute_10min, runs=300, seed=7)
    assert a.on_time_prob == b.on_time_prob
    assert [d.return_p90 for d in a.per_day] == [d.return_p90 for d in b.per_day]
    # 不同种子应当（大概率）给出不同估计
    c = simulate([day], spots, _req(end=16.0), commute_10min, runs=300, seed=8)
    assert (a.on_time_prob, a.per_day[0].return_p90) != (c.on_time_prob, c.per_day[0].return_p90)


def test_identifies_risk_spot_in_tight_schedule():
    """紧凑行程：砍掉最长停留的景点应显著提升按时概率。"""
    specs = [("短景点", 40), ("超长景点", 300)]
    day = make_day(1, specs)
    spots = {n: make_spot(n, s) for n, s in specs}
    res = simulate([day], spots, _req(end=15.0), commute_10min, runs=500,
                   noise=NoiseModel(stay_low=0.8, stay_high=1.3, commute_sigma=0.2))
    assert res.on_time_prob < 0.99, "这个行程本应很紧张"
    assert res.risks, "没有识别出任何风险点"
    assert res.risks[0].name == "超长景点", f"风险点排序不对：{res.risks}"
    assert res.risks[0].drop_gain_pp > res.risks[-1].drop_gain_pp


def test_empty_day_is_always_on_time():
    day = DayPlan(day=1, spots=[], commute_min=0.0, cost=0.0, active_min=0.0)
    res = simulate([day], {}, _req(), commute_10min, runs=100)
    assert res.on_time_prob == 1.0
    assert res.per_day[0].return_p50 < 9.1     # 直接返回，几乎不花时间


def test_respects_open_hour_early_arrival():
    """早到要等开门：开门时间很晚的景点会吃掉时间（不建模就会低估风险）。"""
    specs = [("晚开门", 60)]
    spots = {n: make_spot(n, s, open_h=14.0) for n, s in specs}
    day = make_day(1, specs)
    res_wait = simulate([day], spots, _req(end=17.0), commute_10min, runs=100,
                        noise=NoiseModel(stay_low=1.0, stay_high=1.0, commute_sigma=0.0,
                                         respect_open_hour=True))
    res_ignore = simulate([day], spots, _req(end=17.0), commute_10min, runs=100,
                          noise=NoiseModel(stay_low=1.0, stay_high=1.0, commute_sigma=0.0,
                                           respect_open_hour=False))
    # 等开门版本应更晚返回
    assert res_wait.per_day[0].return_p50 > res_ignore.per_day[0].return_p50


def test_all_days_must_be_on_time():
    """整体概率 = 所有天都按时；单独某天全中、另一天必超时 → 整体为 0。"""
    easy = make_day(1, [("A", 30)])
    tight = make_day(2, [("B", 480)])
    spots = {"A": make_spot("A", 30), "B": make_spot("B", 480)}
    res = simulate([easy, tight], spots, _req(end=15.0), commute_10min, runs=200)
    assert res.per_day[0].on_time_prob == 1.0
    assert res.per_day[1].on_time_prob < 0.5
    assert res.on_time_prob < 0.5, "整体概率不该被宽松的那天掩盖"


def test_result_carries_noise_params_for_reproducibility():
    """结果里要带上噪声参数与种子——否则"这个概率是怎么算出来的"无从追溯。"""
    day = make_day(1, [("A", 60)])
    spots = {"A": make_spot("A", 60)}
    res = simulate([day], spots, _req(), commute_10min, runs=100, seed=99)
    assert res.seed == 99
    assert set(res.noise) == {"stay_low", "stay_high", "commute_sigma", "respect_open_hour"}

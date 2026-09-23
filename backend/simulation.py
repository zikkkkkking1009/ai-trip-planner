"""N3 行程稳健性模拟（蒙特卡洛）：这个行程"能按时走完"的概率有多大？

**为什么需要它**：确定性求解器给出的是一条"纸面最优"的行程——但真实的停留时长与
路况都会波动。用户真正关心的是「我会不会赶不上回酒店 / 会不会太赶」，
而不是一个漂亮的目标值。所以对已排好的行程做蒙特卡洛，输出：
1. 按时完成概率（每天都要在 daily_end_h 前回到酒店）
2. 每天的返回时间分布（P50 / P90）
3. **风险点排名**：哪个景点最该砍——用"去掉它能把按时概率提升多少"来衡量

**噪声模型（参数可调，来源写清楚）**：
- 停留时长：三角分布 triangular(0.6, 1.0, 1.6) × stay_min。
  最可能值取计划值；旅游场景的停留时长变异系数约 0.3，故悲观侧给到 1.6×。
- 通勤时长：对数正态 exp(N(ln(planned), σ))，σ 默认 0.25。
  用对数正态是因为通勤时间**只能为正且右偏**（大多数人正常、少数遇到严重拥堵）。
- 早到等开门：到达时间早于景点 open_h 时按 open_h 进入（真实约束，也是"排太早"的隐性成本）。

**风险点识别用"共同随机数"（CRN）**：所有候选（基线 vs 去掉某景点）复用同一批随机样本，
因此两者的差异只来自"去掉那个景点"本身，对比方差大幅降低——否则 1000 次抽样下
概率估计的标准误约 1.5 个百分点，会把 1~2 个百分点的真实差异淹没。

**参数局限**：σ 与三角分布端点是**估计值**，不是从真实日志标定的。它们决定绝对概率，
但风险点的**相对排序**对参数不敏感（这一点在 docs/experiments.md 实验七里做了敏感性检验）。
"""
from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

DEFAULT_RUNS = 1000
DEFAULT_SEED = 42


@dataclass(frozen=True)
class NoiseModel:
    """波动模型参数（可调；默认值见模块 docstring 的说明）。"""
    stay_low: float = 0.6       # 停留时长：乐观倍数
    stay_high: float = 1.6      # 停留时长：悲观倍数
    commute_sigma: float = 0.25  # 通勤：对数正态标准差（右偏程度）
    # 若为 True，模拟里会考虑景点开放时间（早到需等开门）
    respect_open_hour: bool = True


@dataclass
class DaySimulation:
    day: int
    on_time_prob: float          # 该天在 end_h 前回到酒店的概率
    return_p50: float            # 返回时间中位数（小时）
    return_p90: float
    return_worst: float
    expected_overrun_min: float  # 期望超时分钟（只统计超时样本）


@dataclass
class RiskSpot:
    """风险点：去掉它能带来多少按时概率提升。"""
    day: int
    name: str
    drop_gain_pp: float          # 概率提升（百分点）
    stay_min: float              # 计划停留时长


@dataclass
class SimulationResult:
    runs: int
    on_time_prob: float          # 所有天都按时完成的概率
    per_day: list[DaySimulation] = field(default_factory=list)
    risks: list[RiskSpot] = field(default_factory=list)
    noise: dict = field(default_factory=dict)
    seed: int = DEFAULT_SEED


def _commute_table(plan_days, spot_by_name, hotel, commute_fn) -> list[list[float]]:
    """每天的"分段通勤矩阵"：[[酒店→s1, s1→s2, ..., sn→酒店], ...]（分钟）。"""
    table = []
    for day in plan_days:
        seq = [spot_by_name[s.name] for s in day.spots if s.name in spot_by_name]
        if not seq:
            table.append([])
            continue
        pts = seq
        legs = []
        prev = hotel
        for s in pts:
            if prev is None:
                legs.append(0.0)          # 没设住宿 anchor → 当天第一段无通勤
            else:
                legs.append(float(commute_fn(prev, s)))
            prev = s
        if prev is not None and hotel is not None:
            legs.append(float(commute_fn(prev, hotel)))
        table.append(legs)
    return table


def simulate(plan_days, spot_by_name: dict, req, commute_fn,
             runs: int = DEFAULT_RUNS, seed: int = DEFAULT_SEED,
             noise: NoiseModel | None = None) -> SimulationResult:
    """对已排行程做蒙特卡洛。

    plan_days:   list[DayPlan]（求解器输出）
    spot_by_name: {名字: Spot}（要拿坐标与 stay_min；不在表里的景点按计划停留时长兜底）
    req:         PlanRequest（要 daily_start_h / daily_end_h / days）
    commute_fn:  (a, b) -> 分钟；测试可注入桩函数
    """
    noise = noise or NoiseModel()
    hotel = getattr(req, "hotel", None)
    rng = random.Random(seed)
    legs_by_day = _commute_table(plan_days, spot_by_name, hotel, commute_fn)

    # 每天的景点序列（只保留在 spot_by_name 里的）+ 计划停留时长
    days_seq: list[list[tuple[str, float, float]]] = []   # (name, stay_min, open_h)
    for day in plan_days:
        seq = []
        for v in day.spots:
            s = spot_by_name.get(v.name)
            stay = float(s.stay_min) if s else max(10.0, (v.depart_h - v.arrive_h) * 60.0)
            open_h = float(s.open_h) if s else 0.0
            seq.append((v.name, stay, open_h))
        days_seq.append(seq)

    n_days = len(days_seq)

    def draw() -> list[list[tuple[list[float], list[float]]]]:
        """为每轮抽一组样本：每天的 (通勤倍数, 停留倍数)。"""
        out = []
        for di in range(n_days):
            n_legs, n_spots = len(legs_by_day[di]), len(days_seq[di])
            cm = [math.exp(rng.gauss(0.0, noise.commute_sigma)) for _ in range(n_legs)]
            st = [rng.triangular(noise.stay_low, noise.stay_high, 1.0) for _ in range(n_spots)]
            out.append((cm, st))
        return out

    def eval_day(di: int, cm: list[float], st: list[float],
                 skip_spot: int | None = None) -> float:
        """返回该天的返回时刻（小时）。skip_spot：把该下标的停留时长视为 0（用于风险分析）。"""
        t = float(req.daily_start_h)
        seq = days_seq[di]
        legs = legs_by_day[di]
        for i, (_name, stay_min, open_h) in enumerate(seq):
            if legs:
                t += legs[i] * cm[i] / 60.0
            if i == skip_spot:
                continue
            if noise.respect_open_hour and open_h > 0 and t < open_h:
                t = open_h                      # 早到要等开门
            t += stay_min * st[i] / 60.0
        # 回酒店这一段**只在有住宿锚点时才存在**。
        # ⚠️ 2026-09-23 修：原写法是无条件的 `if legs:`，而 legs 在 hotel=None 时
        # 形如 [0, s1→s2, …, s_{n-1}→sn]（首段 0 表示没有出发点、末段就是最后两景点间），
        # 循环里已经加过 legs[-1]，这里再加一遍 ⇒ 末段通勤被算两次、返回时刻虚高一整段。
        # 后果：无酒店（演示默认路径）的按时概率被系统性低估，N3b 稳健排程据此过度内缩时间窗。
        if hotel is not None and legs:
            t += legs[-1] * cm[-1] / 60.0
        return t

    # ---- 1) 基线模拟 ----
    samples = [draw() for _ in range(runs)]
    returns: list[list[float]] = [[] for _ in range(n_days)]
    on_time_all = 0
    for smp in samples:
        ok = True
        for di in range(n_days):
            cm, st = smp[di]
            r = eval_day(di, cm, st)
            returns[di].append(r)
            if r > req.daily_end_h + 1e-9:
                ok = False
        on_time_all += 1 if ok else 0

    per_day: list[DaySimulation] = []
    for di in range(n_days):
        rs = sorted(returns[di])
        ok = sum(1 for r in rs if r <= req.daily_end_h + 1e-9)
        over = [(r - req.daily_end_h) * 60 for r in rs if r > req.daily_end_h + 1e-9]
        per_day.append(DaySimulation(
            day=plan_days[di].day,
            on_time_prob=round(ok / runs, 4),
            return_p50=round(rs[len(rs) // 2], 2) if rs else 0.0,
            return_p90=round(rs[int(len(rs) * 0.9)], 2) if rs else 0.0,
            return_worst=round(rs[-1], 2) if rs else 0.0,
            expected_overrun_min=round(sum(over) / len(over), 1) if over else 0.0,
        ))

    base_prob = on_time_all / runs

    # ---- 2) 风险点：在同一批样本下去掉某景点，看按时概率提升多少（共同随机数）----
    risks: list[RiskSpot] = []
    for di in range(n_days):
        for si, (name, stay_min, _open) in enumerate(days_seq[di]):
            if stay_min <= 0:
                continue
            ok_all = 0
            for smp in samples:
                ok = True
                for dj in range(n_days):
                    cm, st = smp[dj]
                    r = eval_day(dj, cm, st, skip_spot=si if dj == di else None)
                    if r > req.daily_end_h + 1e-9:
                        ok = False
                        break
                ok_all += 1 if ok else 0
            gain = (ok_all / runs - base_prob) * 100
            if gain > 0.05:      # 忽略噪声级别的差异
                risks.append(RiskSpot(day=plan_days[di].day, name=name,
                                      drop_gain_pp=round(gain, 1),
                                      stay_min=stay_min))
    risks.sort(key=lambda r: -r.drop_gain_pp)

    log.info("稳健性模拟完成：%d 次抽样，按时概率 %.1f%%，风险点 %d 个",
             runs, base_prob * 100, len(risks))
    return SimulationResult(
        runs=runs, on_time_prob=round(base_prob, 4), per_day=per_day,
        risks=risks[:5],
        noise={"stay_low": noise.stay_low, "stay_high": noise.stay_high,
               "commute_sigma": noise.commute_sigma,
               "respect_open_hour": noise.respect_open_hour},
        seed=seed,
    )

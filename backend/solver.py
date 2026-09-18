"""排期求解器：把一堆景点排进 N 天，最大化收益、控制通勤。

建模：带时间窗的定向问题（OPTW）。
- 每个景点有收益 score，访问它要花 stay_min + 通勤时间
- 约束：每天时间窗 [daily_start, daily_end]、景点开放时间窗 [open_h, close_h]
- 策略：L1 = 贪心构造 + 2-opt / 跨日搬运 优化（本文件实现）
        L2 = OR-Tools CP-SAT 对比最优解（TODO，见 README 路线图）

通勤目前用「haversine 距离 / 市内均速 + 固定开销」估算，
接口留好了：把 commute_min 换成高德路径规划 API 即可（记得加缓存）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

from models import DayPlan, PlanRequest, Spot, UnplannedSpot, VisitedSpot

# ---- 通勤估算参数（之后替换成真实 API）----
CITY_SPEED_KMH = 18.0       # 市内门到门均速（地铁+步行混合）
COMMUTE_OVERHEAD_MIN = 8.0  # 进出站/等车固定开销


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """球面距离（km）。"""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def commute_min(a: Spot, b: Spot) -> float:
    """两景点间通勤时长（分钟）。TODO: 换成高德路径规划 API + Redis 缓存。"""
    km = haversine_km(a.lat, a.lon, b.lat, b.lon)
    return max(10.0, km / CITY_SPEED_KMH * 60 + COMMUTE_OVERHEAD_MIN)


@dataclass
class _Seq:
    """一天内部的访问序列。commute_fn 可注入（高德真实数据 / 估算降级）。"""

    spots: list[Spot] = field(default_factory=list)
    commute_fn: Callable[[Spot, Spot], float] = commute_min

    def timeline(self, req: PlanRequest, hotel=None) -> list[tuple[Spot, float, float]] | None:
        """给定顺序模拟一天时间线，返回 [(spot, arrive_h, depart_h)]。

        hotel（住宿锚点）给定时：从酒店出发（首段通勤）且必须按时返回
        （末段通勤计入时间窗）。
        到早了等开门（start = max(arrive, open_h)）；
        任何景点 depart 超过 close_h 或返回时间超 daily_end_h → 不可行，返回 None。
        """
        t = req.daily_start_h
        out: list[tuple[Spot, float, float]] = []
        prev = hotel  # 有酒店时，第一段通勤从酒店算起
        for s in self.spots:
            if prev is not None:
                t += self.commute_fn(prev, s) / 60.0
            start = max(t, s.open_h)
            depart = start + s.stay_min / 60.0
            if depart > s.close_h + 1e-9 or depart > req.daily_end_h + 1e-9:
                return None
            out.append((s, t, depart))
            t = depart
            prev = s
        # 返程：最后景点 → 酒店，回程时间也受 daily_end 约束
        if hotel is not None and self.spots:
            if t + self.commute_fn(prev, hotel) / 60.0 > req.daily_end_h + 1e-9:
                return None
        return out

    def commute_total(self, req: PlanRequest, hotel=None) -> float:
        """序列可行时的纯通勤总分钟数（含酒店往返两段）。"""
        total, prev = 0.0, None
        if hotel is not None and self.spots:
            total += self.commute_fn(hotel, self.spots[0])
        for s in self.spots:
            if prev is not None:
                total += self.commute_fn(prev, s)
            prev = s
        if hotel is not None and self.spots:
            total += self.commute_fn(prev, hotel)
        return total


class Solver:
    def __init__(self, req: PlanRequest, commute_fn=None):
        self.req = req
        self.commute_fn = commute_fn or commute_min
        self.hotel = req.hotel  # 住宿锚点：每天的起点与终点
        self.days: list[_Seq] = [
            _Seq(commute_fn=self.commute_fn) for _ in range(req.days)]

    # ---------- 构造阶段：贪心插入 ----------
    def _try_insert(self, s: Spot, budget_left: float | None) -> bool:
        """尝试插到所有 (天, 位置)，选「该天累计通勤 / score」最小且可行的位置。

        budget_left 不为 None 时，预算是构造阶段的硬约束：门票装不进剩余
        预算的景点直接不参与插入（而不是排完再让校验器打脸）。
        """
        if budget_left is not None and s.ticket > budget_left + 1e-9:
            return False
        best: tuple[float, int, int] | None = None
        for di, day in enumerate(self.days):
            for pos in range(len(day.spots) + 1):
                day.spots.insert(pos, s)
                if day.timeline(self.req, self.hotel) is not None:
                    ratio = day.commute_total(self.req, self.hotel) / max(s.score, 0.01)
                    if best is None or ratio < best[0]:
                        best = (ratio, di, pos)
                day.spots.pop(pos)
        if best is None:
            return False
        self.days[best[1]].spots.insert(best[2], s)
        return True

    # ---------- 优化阶段 1：天内 2-opt ----------
    def _intra_2opt(self, di: int) -> bool:
        day = self.days[di]
        base = day.commute_total(self.req, self.hotel)
        improved = False
        n = len(day.spots)
        for i in range(n - 1):
            for j in range(i + 1, n):
                day.spots[i:j + 1] = reversed(day.spots[i:j + 1])
                if (day.timeline(self.req, self.hotel) is not None
                        and day.commute_total(self.req, self.hotel) < base - 1e-6):
                    base = day.commute_total(self.req, self.hotel)
                    improved = True
                else:
                    day.spots[i:j + 1] = reversed(day.spots[i:j + 1])
        return improved

    # ---------- 优化阶段 2：跨日搬运 ----------
    def _relocate(self) -> bool:
        """把景点搬到另一天，score 不变，目标：全局通勤下降。"""
        for src in self.days:
            for idx in range(len(src.spots)):
                s = src.spots[idx]
                src.spots.pop(idx)  # 从可行序列中移除子序列，剩余部分仍可行
                before = self._global_commute()
                placed = False
                for tgt in self.days:
                    if tgt is src:
                        continue
                    for pos in range(len(tgt.spots) + 1):
                        tgt.spots.insert(pos, s)
                        if (tgt.timeline(self.req, self.hotel) is not None
                                and self._global_commute() < before - 1e-6):
                            placed = True
                            break
                        tgt.spots.pop(pos)
                    if placed:
                        break
                if not placed:
                    src.spots.insert(idx, s)
                else:
                    return True
        return False

    def _global_commute(self) -> float:
        return sum(d.commute_total(self.req, self.hotel) for d in self.days)

    # ---------- 主入口 ----------
    def solve(self, progress_cb: Callable[[str, dict], None] | None = None
              ) -> tuple[list[DayPlan], list[UnplannedSpot], float, float]:
        """progress_cb(stage, info)：阶段回调，供异步任务系统推送进度。

        stage ∈ {构造, 优化, 完成}；不传则静默（同步调用方式不变）。
        """
        progress_cb = progress_cb or (lambda stage, info: None)

        # 1) 按 score 降序贪心插入；预算是构造阶段的硬约束
        progress_cb("构造", {"msg": f"贪心插入 {len(self.req.spots)} 个景点（预算硬约束生效）..."})
        unplanned: list[UnplannedSpot] = []
        spent = 0.0
        for s in sorted(self.req.spots, key=lambda x: -x.score):
            budget_left = None if self.req.budget is None else self.req.budget - spent
            if self._try_insert(s, budget_left):
                spent += s.ticket
            else:
                if budget_left is not None and s.ticket > budget_left + 1e-9:
                    reason = "预算不足"
                else:
                    reason = "时间窗装不下"
                unplanned.append(UnplannedSpot(name=s.name, reason=reason))
        progress_cb("构造", {"msg": f"已排入 {len(self.req.spots) - len(unplanned)} 个，"
                                    f"放弃 {len(unplanned)} 个"})

        # 2) 局部优化：2-opt 压通勤 + 跨日搬运，迭代至收敛（最多 3 轮）
        for rd in range(3):
            changed = any(self._intra_2opt(di) for di in range(len(self.days)))
            changed |= self._relocate()
            progress_cb("优化", {"msg": f"第 {rd + 1} 轮优化（2-opt + 跨日搬运），"
                                        f"当前总通勤 {self._global_commute():.0f} 分钟"})
            if not changed:
                break
        progress_cb("完成", {"msg": "求解完成"})

        # 3) 输出结构化结果
        day_plans: list[DayPlan] = []
        total_cost = total_score = 0.0
        for i, day in enumerate(self.days):
            tl = day.timeline(self.req, self.hotel) or []
            vspots: list[VisitedSpot] = []
            cost = active = comm = 0.0
            prev: Spot | None = self.hotel  # 通勤从酒店出发算起
            for s, arrive, depart in tl:
                if prev is not None:
                    comm += self.commute_fn(prev, s)
                vspots.append(VisitedSpot(
                    name=s.name, arrive_h=round(arrive, 2),
                    depart_h=round(depart, 2), ticket=s.ticket, desc=s.desc,
                    image=s.image, intro=s.intro))
                cost += s.ticket
                active += s.stay_min
                total_score += s.score
                prev = s
            if self.hotel is not None and prev is not None:
                comm += self.commute_fn(prev, self.hotel)  # 回酒店这段也算通勤
            total_cost += cost
            day_plans.append(DayPlan(
                day=i + 1, spots=vspots, commute_min=round(comm, 1),
                cost=round(cost, 1), active_min=round(active, 0)))
        return day_plans, unplanned, round(total_cost, 1), round(total_score, 1)

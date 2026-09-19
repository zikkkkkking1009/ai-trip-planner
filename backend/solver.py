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

import logging
import math
import random
import time
from dataclasses import dataclass, field
from typing import Callable

from models import DayPlan, PlanRequest, Spot, UnplannedSpot, VisitedSpot

log = logging.getLogger(__name__)

# ---- 通勤估算参数（之后替换成真实 API）----
CITY_SPEED_KMH = 18.0       # 市内门到门均速（地铁+步行混合）
COMMUTE_OVERHEAD_MIN = 8.0  # 进出站/等车固定开销

# ---- 多起点随机重启（multi-start）参数 ----
# 背景：纯「分数降序贪心」确定性太强，在紧张实例（景点多、天数少）上会先装满高分景点，
# 把后续高收益组合挤掉。CP-SAT 对照实验显示这类实例 gap 可达 22~24%，
# 而随机重启 + 保留最优能把 gap 压到 0~1%（实测见 docs/experiments.md 实验四）。
SCORE_OBJ_W = 1000              # 目标权重：收益优先、通勤为次（与 CP-SAT 对照同口径）
MULTISTART_MIN_SPOTS = 7        # 景点数少于此 → 单次贪心（实测已达最优）
MULTISTART_ITERS = 600          # 随机重启次数上限（实测 200→600 可把最差实例 gap 24%→0%）
MULTISTART_TIME_BUDGET_S = 1.5  # 时间预算硬上限：实测均值 410ms / 最大 882ms，交互可接受
MULTISTART_SEED = 42            # 固定种子：同输入同输出，实验可复现

# ---- N2 地理聚类（构造顺序候选轮）----
# 背景：纯分数贪心会把「城东一个、城西一个」交错喂进来，插入判据只看当天通勤增量，
# 容易把同一天的景点排得跨区折返。加一轮「按地理位置分组」的构造顺序作为候选，
# 让地理相近的景点连续进入贪心 → 更容易落到同一天（每天玩一个区域）。
#
# ⚠️ **实测结论：无增量收益，因此默认关闭**（2026-09-20，`eval_cluster.py` 50 场景同口径对照：
#    通勤占比 0.1049 → 0.1049，逐场景 0 改善 / 0 变差 / 50 持平）。归因：
#    ① 目标函数 `1000·收益 − 通勤` 里通勤权重极弱（1 分钟 ≈ 0.001 分），
#       任何"牺牲收益换通勤"的构造成果都会被目标值判为更差；
#    ② 600 轮随机抖动 + 2-opt + 跨日搬运已经吃掉了这部分顺序空间。
#    **什么条件下值得重新评估**：目标函数改为「通勤权重可调」（功能池 A2 偏好开关）之后——
#    那时"少走路"模式会真正奖励地理聚集。代码与实验脚本都保留，`USE_GEO_CLUSTER=True` 即可复测。
USE_GEO_CLUSTER = False
CLUSTER_MAX_ITER = 25           # k-means 迭代上限（收敛即提前退出）


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
        self.last_elapsed_ms: float = 0.0   # 上次 solve() 耗时，供实验脚本与日志取值
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

    # ---------- 多起点构造：构造 → 局部优化 → 择优 ----------
    def _planned_score(self) -> float:
        return sum(s.score for d in self.days for s in d.spots)

    def _objective(self) -> float:
        """目标值：收益为主、通勤为次（与 CP-SAT 对照同一口径）。"""
        return SCORE_OBJ_W * self._planned_score() - self._global_commute()

    def _construct(self, order: list[Spot]) -> list[UnplannedSpot]:
        """按给定顺序贪心插入（预算硬约束生效），返回未安排列表。"""
        unplanned: list[UnplannedSpot] = []
        spent = 0.0
        for s in order:
            budget_left = None if self.req.budget is None else self.req.budget - spent
            if self._try_insert(s, budget_left):
                spent += s.ticket
            else:
                reason = ("预算不足"
                          if budget_left is not None and s.ticket > budget_left + 1e-9
                          else "时间窗装不下")
                unplanned.append(UnplannedSpot(name=s.name, reason=reason))
        return unplanned

    def _optimize(self, max_rounds: int = 3) -> None:
        """天内 2-opt + 跨日搬运，迭代至收敛。"""
        for _ in range(max_rounds):
            changed = any(self._intra_2opt(di) for di in range(len(self.days)))
            changed |= self._relocate()
            if not changed:
                break

    def _snapshot(self) -> tuple[list[list[Spot]], list[UnplannedSpot]]:
        return [list(d.spots) for d in self.days], list(self._unplanned)

    def _restore(self, snap: tuple[list[list[Spot]], list[UnplannedSpot]]) -> None:
        day_spots, self._unplanned = snap
        self.days = [_Seq(spots=list(sp), commute_fn=self.commute_fn)
                     for sp in day_spots]

    @staticmethod
    def _perturbed_order(spots: list[Spot], rng: random.Random, k: int) -> list[Spot]:
        """给分数加抖动生成新顺序：小幅抖动偏利用，大幅抖动偏探索。"""
        amp = 0.5 + 3.0 * ((k % 4) / 3.0)          # 0.5 → 3.5 循环
        return sorted(spots, key=lambda s: -(s.score + rng.uniform(-amp, amp)))

    # ---------- N2：地理聚类构造顺序 ----------
    @staticmethod
    def _geo_clusters(spots: list[Spot], k: int,
                      max_iter: int = CLUSTER_MAX_ITER) -> list[list[Spot]]:
        """按经纬度做 k-means 聚类（纯标准库，零依赖）。

        **确定性初始化**：把景点按经度排序后等距取 k 个点作为初始中心——
        不用随机初始化是为了守住「同输入同输出」这条项目纪律（随机初始化会破坏可复现性）。
        距离用 haversine（与通勤估算同一套），跨区/跨城时比度数平方更合理。
        """
        if k <= 1 or len(spots) <= k:
            return [list(spots)]
        ordered = sorted(spots, key=lambda s: (s.lon, s.lat))
        n = len(ordered)
        centers = []
        for i in range(k):
            idx = min(n - 1, int((i + 0.5) * n / k))
            centers.append((ordered[idx].lat, ordered[idx].lon))

        labels = [-1] * n
        for _ in range(max_iter):
            changed = False
            for i, s in enumerate(ordered):
                best = min(range(k), key=lambda c: haversine_km(
                    s.lat, s.lon, centers[c][0], centers[c][1]))
                if labels[i] != best:
                    labels[i] = best
                    changed = True
            for c in range(k):
                members = [ordered[i] for i in range(n) if labels[i] == c]
                if members:
                    centers[c] = (sum(m.lat for m in members) / len(members),
                                  sum(m.lon for m in members) / len(members))
            if not changed:
                break

        clusters: list[list[Spot]] = [[] for _ in range(k)]
        for i, s in enumerate(ordered):
            clusters[labels[i] if labels[i] >= 0 else 0].append(s)
        return [c for c in clusters if c]

    @classmethod
    def _cluster_order(cls, spots: list[Spot], days: int) -> list[Spot]:
        """地理感知的构造顺序：先按簇分组，簇内按分数降序。

        簇间顺序按「簇内最高分」降序——强簇先排，避免先被弱簇占满时间窗。
        """
        clusters = cls._geo_clusters(spots, days)
        if len(clusters) <= 1:
            return []          # 只有一个簇说明聚类没带来新信息，不额外消耗一轮
        clusters.sort(key=lambda c: -max(s.score for s in c))
        return [s for c in clusters for s in sorted(c, key=lambda x: -x.score)]

    # ---------- 主入口 ----------
    def solve(self, progress_cb: Callable[[str, dict], None] | None = None
              ) -> tuple[list[DayPlan], list[UnplannedSpot], float, float]:
        """progress_cb(stage, info)：阶段回调，供异步任务系统推送进度。

        stage ∈ {构造, 优化, 完成}；不传则静默（同步调用方式不变）。

        策略：多起点随机重启（multi-start）——第 0 轮用「分数降序」（与旧版行为一致，
        作为保底），后续轮次用带抖动的顺序重跑构造 + 局部优化，保留目标值最优的一轮。
        景点数少（< MULTISTART_MIN_SPOTS）或已超时间预算时提前结束。
        """
        progress_cb = progress_cb or (lambda stage, info: None)

        spots = list(self.req.spots)
        n = len(spots)
        iters = 1 if n < MULTISTART_MIN_SPOTS else MULTISTART_ITERS
        rng = random.Random(MULTISTART_SEED)
        base_order = sorted(spots, key=lambda x: -x.score)

        # N2 地理聚类：额外给一轮「按地理位置分组」的构造顺序（详见 USE_GEO_CLUSTER 注释）
        geo_order: list[Spot] | None = None
        if USE_GEO_CLUSTER and iters > 1 and self.req.days > 1:
            geo_order = self._cluster_order(spots, self.req.days) or None

        progress_cb("构造", {"msg": (f"多起点搜索启动：{n} 个景点 / {self.req.days} 天"
                                    f"（最多 {iters} 轮，取目标最优"
                                    + ("，含地理聚类轮" if geo_order else "") + "）"
                                    if iters > 1
                                    else f"贪心构造：{n} 个景点 / {self.req.days} 天")})

        self._unplanned: list[UnplannedSpot] = []
        best_obj = float("-inf")
        best_snap = None
        t0 = time.perf_counter()
        _budget_start = None      # 预算从第 0 轮结束后起算：首轮含真实通勤预热，不应吃掉搜索预算
        _last_report = t0
        rounds_done = 0

        for k in range(iters):
            # 每轮重置：清空各天序列
            for d in self.days:
                d.spots = []
            # 第 0 轮：分数降序（保底，与旧版一致）；第 1 轮：地理聚类顺序（若启用）；
            # 之后：带抖动顺序
            if k == 0:
                order = base_order
            elif k == 1 and geo_order is not None:
                # ⚠️ 必须仍然消耗一次 rng：否则后续抖动轮的随机序列整体错位，
                # 开启聚类与关闭聚类就不可比，「聚类不劣于旧版」的不变量随之失效
                # （实测过：不消耗 rng 时出现过「通勤降了但收益也降」的不可比结果）
                self._perturbed_order(spots, rng, k)
                order = geo_order
            else:
                order = self._perturbed_order(spots, rng, k)
            self._unplanned = self._construct(order)
            self._optimize()
            obj = self._objective()
            if obj > best_obj:
                best_obj, best_snap = obj, self._snapshot()
            rounds_done = k + 1
            _now = time.perf_counter()
            if iters > 1 and ((k + 1) % 100 == 0 or _now - _last_report > 2.0):
                _last_report = _now
                progress_cb("构造", {"msg": f"多起点搜索 {k + 1}/{iters} 轮，"
                                            f"当前最优收益 {self._planned_score():.1f}"
                                            f"（通勤 {self._global_commute():.0f} 分钟）"})
            if k == 0:
                _budget_start = time.perf_counter()
            elif iters > 1 and time.perf_counter() - _budget_start > MULTISTART_TIME_BUDGET_S:
                break

        self._restore(best_snap)
        unplanned = self._unplanned
        progress_cb("构造", {"msg": (f"多起点 {rounds_done} 轮取最优：" if rounds_done > 1
                                    else "单起点贪心：")
                                    + f"已排入 {n - len(unplanned)}/{n} 个，"
                                      f"收益 {self._planned_score():.1f}，"
                                      f"通勤 {self._global_commute():.0f} 分钟，"
                                      f"耗时 {(time.perf_counter() - t0) * 1000:.0f}ms"})
        progress_cb("完成", {"msg": "求解完成"})
        self.last_elapsed_ms = (time.perf_counter() - t0) * 1000

        # 领域层日志：同步 /plan 路径没有任务进度面板，靠日志定位问题
        log.info("求解完成：%d 景点 / %d 天 → 排入 %d、收益 %.1f、通勤 %.0f 分钟、"
                 "%d 轮、耗时 %.0fms",
                 n, self.req.days, n - len(unplanned), self._planned_score(),
                 self._global_commute(), rounds_done, (time.perf_counter() - t0) * 1000)
        if unplanned:
            log.info("未排入 %d 个：%s", len(unplanned),
                     "; ".join(f"{u.name}({u.reason})" for u in unplanned))

        # 输出结构化结果
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

"""CP-SAT 精确求解器：为本项目的 OPTW 变体提供最优解参照，用于计算启发式解的 gap。

与启发式（solver.Solver）同口径：
- 多日行程，每天从起点出发（有住宿时为酒店）到终点返回
- 景点开放时间窗硬约束（到早了等待，晚于关门不可行）
- 每日时间窗（daily_start_h ~ daily_end_h）；有住宿时返程也必须落在窗口内
- 预算硬约束（门票合计）
- 目标：最大化访问景点收益总和，其次最小化总通勤（防止等分时随意绕路）

建模采用弧模型（arc-based M-TSPTW）：
  y[d,i,j] 表示第 d 天从 i 到 j 的弧；z[d,i] 表示第 d 天访问 i；
  时间变量 t / s 保证时序，且天然排除子回路（每次停留 stay_min>0，时间严格递增）。

用法：
    from solver_cpsat import CPSatSolver
    days, unplanned, cost, score = CPSatSolver(req, cm.minutes, time_limit=5).solve()
"""
from __future__ import annotations

from ortools.sat.python import cp_model

from models import DayPlan, PlanRequest, Spot, UnplannedSpot, VisitedSpot
from solver import commute_min

# 目标函数权重：收益为主，通勤为次（单位：收益 1000 分 ≈ 通勤 1 分钟的量级）
SCORE_W = 1000


class CPSatSolver:
    def __init__(self, req: PlanRequest, commute_fn=None, time_limit: float = 5.0,
                 workers: int = 8, warm_start=None, target_floor: float | None = None):
        self.req = req
        self.commute_fn = commute_fn or commute_min
        self.time_limit = time_limit
        self.workers = workers
        self.hotel = req.hotel
        self.warm_start = warm_start   # list[DayPlan]：启发式解，作为 CP-SAT hint
        # 目标下界（obj 单位同上）：设成启发式的 obj 后，返回值保证不差于启发式。
        # 与 hint 不同，这是**硬保证**——CP-SAT 只能在"不比启发式差"的解空间里找。
        self.target_floor = target_floor
        self.status_name = ""
        self.wall_time = 0.0

    # ---------- 工具 ----------
    def _start_min(self) -> int:
        return int(round(self.req.daily_start_h * 60))

    def _end_min(self) -> int:
        return int(round(self.req.daily_end_h * 60))

    def _commute(self, a, b) -> int:
        return int(round(self.commute_fn(a, b)))

    # ---------- 暖启动 ----------
    def _apply_hints(self, m, z, y, t, s, spots, S, E, n, D, day_start) -> None:
        """把启发式解映射成变量取值提示（hint）。

        实现要点：**每个变量只能 hint 一次**——先收集成 {变量: 取值} 再统一应用，
        否则重复 AddHint 会让 CP-SAT 报 MODEL_INVALID（实测踩过）。
        若启发式解与模型语义有细微出入，CP-SAT 会自行忽略提示，不影响正确性。
        """
        idx = {sp.name: i for i, sp in enumerate(spots)}
        plan_hint: dict = {}

        for d in range(D):                       # 默认全不访问，后面被子解覆盖
            for j in range(n):
                plan_hint[("z", d, j)] = 0

        for d, plan in enumerate(self.warm_start or []):
            if d >= D:
                break
            seq = [idx[v.name] for v in plan.spots if v.name in idx]
            if not seq:
                plan_hint[("y", d, S, E)] = 1
                continue
            plan_hint[("y", d, S, seq[0])] = 1
            for a, b in zip(seq, seq[1:]):
                plan_hint[("y", d, a, b)] = 1
            plan_hint[("y", d, seq[-1], E)] = 1
            for v, j in zip(plan.spots, seq):
                plan_hint[("z", d, j)] = 1
                plan_hint[("t", d, j)] = int(round(v.arrive_h * 60))
                stay = spots[j].stay_min
                plan_hint[("s", d, j)] = int(round(v.depart_h * 60)) - stay
            plan_hint[("tE", d)] = int(round(plan.spots[-1].depart_h * 60))

        for key, val in plan_hint.items():
            kind = key[0]
            if kind == "z":
                m.AddHint(z[key[1], key[2]], val)
            elif kind == "y":
                m.AddHint(y[key[1], key[2], key[3]], val)
            elif kind == "s":
                m.AddHint(s[key[1], key[2]], val)
            elif kind == "t":
                m.AddHint(t[key[1], key[2]], val)
            elif kind == "tE":
                m.AddHint(t[key[1], E], val)

    def solve(self):
        req = self.req
        spots = list(req.spots)
        n = len(spots)
        D = req.days
        S, E = n, n + 1                     # 起点 / 终点（节点索引）
        nodes = n + 2
        day_start, day_end = self._start_min(), self._end_min()

        m = cp_model.CpModel()

        # 时间上界：给时间变量一个保守的 horizon
        horizon = day_end + 1

        z = {}   # (d, i) -> bool
        y = {}   # (d, i, j) -> bool
        t = {}   # (d, node) -> IntVar 到达时间
        s = {}   # (d, i) -> IntVar 开始游览时刻

        # 通勤矩阵（含首尾；无住宿时首尾通勤为 0，模型与启发式一致）
        def c(a_node, b_node) -> int:
            if a_node == S or b_node == E or a_node == E or b_node == S:
                if self.hotel is not None:
                    # 有住宿：起点/终点就是酒店，需计算真实通勤
                    if a_node == S and b_node < n:
                        return self._commute(self.hotel, spots[b_node])
                    if a_node < n and b_node == E:
                        return self._commute(spots[a_node], self.hotel)
                    if a_node == S and b_node == E:
                        return 0
                return 0
            return self._commute(spots[a_node], spots[b_node])

        for d in range(D):
            t[d, S] = m.NewConstant(day_start)
            t[d, E] = m.NewIntVar(day_start, horizon, f"t_{d}_E")
            for j in range(n):
                t[d, j] = m.NewIntVar(day_start, horizon, f"t_{d}_{j}")
                s[d, j] = m.NewIntVar(day_start, horizon, f"s_{d}_{j}")
                z[d, j] = m.NewBoolVar(f"z_{d}_{j}")
            for i in list(range(n)) + [S]:
                for j in list(range(n)) + [E]:
                    if i == j:
                        continue
                    y[d, i, j] = m.NewBoolVar(f"y_{d}_{i}_{j}")

            # 起点出度 = 终点入度（空天允许 S->E 直连）
            m.Add(sum(y[d, S, j] for j in list(range(n)) + [E]) == 1)
            m.Add(sum(y[d, i, E] for i in list(range(n)) + [S]) == 1)

            for j in range(n):
                m.Add(sum(y[d, i, j] for i in list(range(n)) + [S] if i != j) == z[d, j])
                m.Add(sum(y[d, j, k] for k in list(range(n)) + [E] if k != j) == z[d, j])

                # 时间窗
                m.Add(t[d, j] <= s[d, j])
                m.Add(s[d, j] >= int(round(spots[j].open_h * 60)))
                m.Add(s[d, j] + spots[j].stay_min <= int(round(spots[j].close_h * 60)))
                m.Add(s[d, j] + spots[j].stay_min <= day_end)
                # 未访问则时间变量无意义，给个界避免溢出
                m.Add(s[d, j] <= horizon).OnlyEnforceIf(z[d, j])
                m.Add(t[d, j] == day_start).OnlyEnforceIf(z[d, j].Not())

            # 弧上的时序约束
            M = horizon * 4
            for i in list(range(n)) + [S]:
                for j in list(range(n)) + [E]:
                    if i == j:
                        continue
                    travel = c(i, j)
                    if j == E:
                        # 到终点：从 i 出发 + 通勤
                        depart_i = day_start if i == S else None
                        if i == S:
                            m.Add(t[d, E] >= day_start + travel).OnlyEnforceIf(y[d, S, E])
                        else:
                            m.Add(t[d, E] >= s[d, i] + spots[i].stay_min + travel) \
                                .OnlyEnforceIf(y[d, i, E])
                    else:
                        if i == S:
                            m.Add(t[d, j] >= day_start + travel).OnlyEnforceIf(y[d, S, j])
                        else:
                            m.Add(t[d, j] >= s[d, i] + spots[i].stay_min + travel) \
                                .OnlyEnforceIf(y[d, i, j])

            # 返程必须在时间窗内（仅有住宿时约束；无住宿与启发式一致，不管返程）
            if self.hotel is not None:
                m.Add(t[d, E] <= day_end)

        # 每个景点最多访问一次
        for j in range(n):
            m.Add(sum(z[d, j] for d in range(D)) <= 1)

        # 预算硬约束
        if req.budget is not None:
            m.Add(sum(int(round(spots[j].ticket)) * z[d, j]
                      for d in range(D) for j in range(n)) <= int(req.budget))

        # 目标：收益优先，通勤为次
        obj = []
        for d in range(D):
            for j in range(n):
                obj.append(SCORE_W * int(round(spots[j].score)) * z[d, j])
            for i in list(range(n)) + [S]:
                for j in list(range(n)) + [E]:
                    if i == j:
                        continue
                    obj.append(-c(i, j) * y[d, i, j])
        total_obj = sum(obj)
        m.Maximize(total_obj)
        if self.target_floor is not None:
            m.Add(total_obj >= int(self.target_floor))

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = self.time_limit
        solver.parameters.num_search_workers = self.workers

        # 暖启动：把启发式解作为 hint 交给 CP-SAT。
        # 收益有两层：① 快速找到高质量可行解 ② 大实例上保证"不比启发式差"
        if self.warm_start:
            self._apply_hints(m, z, y, t, s, spots, S, E, n, D, day_start)

        status = solver.Solve(m)
        self.status_name = solver.StatusName(status)
        self.wall_time = solver.WallTime()

        day_plans: list[DayPlan] = []
        unplanned: list[UnplannedSpot] = []
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            # 拿不到解时（超时/UNKNOWN，或下界约束太紧没搜到）：
            # 有暖启动解就直接回落到它——这样"不差于启发式"的承诺才是硬的。
            if self.warm_start:
                by_name = {sp.name: sp for sp in spots}
                covered = {v.name for d in self.warm_start for v in d.spots}
                for sp in spots:
                    if sp.name not in covered:
                        reason = ("预算不足"
                                  if req.budget is not None and sp.ticket > req.budget
                                  else "时间窗装不下")
                        unplanned.append(UnplannedSpot(name=sp.name, reason=reason))
                score = sum(by_name[v.name].score for d in self.warm_start
                            for v in d.spots if v.name in by_name)
                cost = sum(v.ticket for d in self.warm_start for v in d.spots)
                return list(self.warm_start), unplanned, round(cost, 1), round(score, 1)
            for sp in spots:
                unplanned.append(UnplannedSpot(name=sp.name, reason="时间窗装不下"))
            return day_plans, unplanned, 0.0, 0.0

        visited: set[int] = set()
        total_cost = total_score = 0.0
        for d in range(D):
            seq_idx: list[int] = []
            cur = S
            guard = 0
            while guard <= n + 1:
                guard += 1
                nxt = None
                for j in list(range(n)) + [E]:
                    if j != cur and (d, cur, j) in y and solver.Value(y[d, cur, j]) == 1:
                        nxt = j
                        break
                if nxt is None or nxt == E:
                    break
                seq_idx.append(nxt)
                cur = nxt

            vspots: list[VisitedSpot] = []
            comm = 0.0
            cost = active = 0.0
            prev_node = S
            for j in seq_idx:
                arr = solver.Value(t[d, j]) / 60.0
                dep = (solver.Value(s[d, j]) + spots[j].stay_min) / 60.0
                start_v = solver.Value(s[d, j]) / 60.0
                vspots.append(VisitedSpot(
                    name=spots[j].name, arrive_h=round(arr, 2),
                    depart_h=round(dep, 2), ticket=spots[j].ticket,
                    desc=spots[j].desc, image=spots[j].image, intro=spots[j].intro))
                comm += c(prev_node, j)
                cost += spots[j].ticket
                active += spots[j].stay_min
                visited.add(j)
                prev_node = j
            if seq_idx:
                if self.hotel is not None:
                    comm += c(prev_node, E)
                total_cost += cost
                total_score += sum(spots[j].score for j in seq_idx)
            day_plans.append(DayPlan(
                day=d + 1, spots=vspots, commute_min=round(comm, 1),
                cost=round(cost, 1), active_min=round(active, 0)))

        for j, sp in enumerate(spots):
            if j not in visited:
                reason = "时间窗装不下"
                if req.budget is not None and sp.ticket > req.budget:
                    reason = "预算不足"
                unplanned.append(UnplannedSpot(name=sp.name, reason=reason))

        return day_plans, unplanned, round(total_cost, 1), round(total_score, 1)

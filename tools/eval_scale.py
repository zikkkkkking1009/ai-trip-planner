"""规模对照实验：启发式 vs CP-SAT 在 N 景点规模下的耗时与质量（2026-09-30）。

动机（面试官质询）：「提速 8 倍」是拿启发式比 CP-SAT，规模上到 50 个景点还快吗？
本实验回答：不同规模下两个求解器各耗时多少、质量差多少、CP-SAT 是否还能求最优。

口径：
- 合成景点 = 真实西安 14 景坐标加确定性抖动（性能夹具，非展示数据，不入 demo 库）；
- 通勤 = solver.commute_min 的 haversine 直线估算（与线上一致，零 API、纯离线）；
- CP-SAT time_limit 随规模放宽（30/30/60/120s），status 如实记录（OPTIMAL/FEASIBLE/…）。
输出：backend/eval_scale.json
"""
from __future__ import annotations

import json
import random
import statistics
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from demo_data import XI_AN_SPOTS  # noqa: E402
from models import PlanRequest, Spot  # noqa: E402
from solver import Solver  # noqa: E402
from solver_cpsat import CPSatSolver  # noqa: E402

SIZES = [8, 14, 25, 50]
LIMITS = {8: 30, 14: 30, 25: 60, 50: 120}
DAYS, BUDGET = 3, 800


def synth_spots(n: int, seed: int = 42) -> list[Spot]:
    """真实西安坐标加确定性抖动的合成景点（性能夹具）。"""
    rng = random.Random(seed)
    base = [r for r in XI_AN_SPOTS for _ in range(4)][:n]  # 循环铺底再抖动
    out = []
    for i, b in enumerate(base, 1):
        out.append(Spot(
            source_id=i, name=f"合成景点-{i:02d}",
            lat=round(b.lat + rng.uniform(-0.18, 0.18), 6),
            lon=round(b.lon + rng.uniform(-0.18, 0.18), 6),
            stay_min=rng.choice([60, 90, 120, 150, 180]),
            score=round(rng.uniform(6.0, 9.5), 1),
            ticket=rng.choice([0, 0, 50, 80, 120]),
            open_h=8.0, close_h=18.0,
            desc="规模实验合成景点（性能夹具）",
        ))
    return out


def run_once(n: int) -> dict:
    req_days = DAYS
    spots = synth_spots(n)

    req = PlanRequest(city="西安", days=req_days, budget=BUDGET,
                      daily_start_h=9.0, daily_end_h=19.0,
                      hotel=None, spots=spots)

    heur_ms = []
    for _ in range(3):  # 启发式跑 3 次取中位（毫秒级，抖动可忽略但要稳）
        t0 = time.perf_counter()
        Solver(req, None).solve()
        heur_ms.append((time.perf_counter() - t0) * 1000)
    h_days, h_un, h_cost, h_score = Solver(req, None).solve()

    limit = LIMITS[n]
    t0 = time.perf_counter()
    cs = CPSatSolver(req, time_limit=limit)
    c_days, c_un, c_cost, c_score = cs.solve()
    c_s = time.perf_counter() - t0

    return {
        "n": n, "days": req_days,
        "heur_ms_median": round(statistics.median(heur_ms), 1),
        "heur_score": round(h_score, 1), "heur_unplaced": len(h_un),
        "cpsat_s": round(c_s, 2), "cpsat_status": cs.status_name,
        "cpsat_score": round(c_score, 1), "cpsat_unplaced": len(c_un),
        "time_limit_s": limit,
    }


def main() -> None:
    rows = [run_once(n) for n in SIZES]
    summary = {
        "purpose": "规模对照：启发式 vs CP-SAT（面试质询「50 个景点还快吗」的实测回答）",
        "note": "合成坐标=真实西安景点确定性抖动（性能夹具）；通勤=haversine 直线估算（与线上一致）",
        "rows": rows,
        "speedup_at_14": round(next(r["cpsat_s"] for r in rows if r["n"] == 14) * 1000
                                / next(r["heur_ms_median"] for r in rows if r["n"] == 14), 1),
    }
    out = BACKEND / "eval_scale.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in rows:
        print(f"N={r['n']:>3}  启发式 {r['heur_ms_median']:>8}ms (分{r['heur_score']}/未排{r['heur_unplaced']})"
              f"  CP-SAT {r['cpsat_s']:>7}s [{r['cpsat_status']}] (分{r['cpsat_score']}/未排{r['cpsat_unplaced']})")
    print(f"14 景规模加速比 = {summary['speedup_at_14']}x")
    print(f"已写入 {out}")


if __name__ == "__main__":
    main()

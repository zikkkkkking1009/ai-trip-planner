"""启发式 vs CP-SAT 最优解对照实验：算 gap（批次 2 / 路线图 A1）。

口径说明（与 evaluation.py 完全一致，保证可比）：
- 场景来自同一个生成器 `evaluation.gen_scenarios(n, seed)`，默认 seed=42
- 通勤矩阵均使用默认估算函数（不调高德 API），两侧输入完全相同
- 目标函数：`obj = 1000 · Σ收益 − Σ通勤(分钟)`（收益优先、通勤为次，与 CP-SAT 目标一致）
- gap = (obj_opt − obj_heur) / obj_opt，越小说明启发式越接近最优
- CP-SAT 设时间上限，未证最优的实例标 status=FEASIBLE，gap 仍为有效上界方向

用法：
    python eval_gap.py                 # 50 场景，每个 CP-SAT 最多 8 秒
    python eval_gap.py --n 20 --limit 5
"""
from __future__ import annotations

import argparse
import json
import statistics
import time

from evaluation import gen_scenarios
from solver import Solver
from solver_cpsat import CPSatSolver, SCORE_W

DEFAULT_N = 50
DEFAULT_LIMIT = 8.0


def _commute_of(days) -> float:
    return round(sum(d.commute_min for d in days), 1)


def _obj(score: float, commute: float) -> float:
    return SCORE_W * score - commute


def run(n: int = DEFAULT_N, seed: int = 42, limit: float = DEFAULT_LIMIT,
        warm: bool = False) -> dict:
    reqs = gen_scenarios(n, seed)
    rows = []
    for k, req in enumerate(reqs):
        t0 = time.time()
        days_h, un_h, cost_h, score_h = Solver(req, None).solve()
        t_h = time.time() - t0

        t0 = time.time()
        cs = CPSatSolver(req, time_limit=limit,
                         warm_start=days_h if warm else None)
        days_c, un_c, cost_c, score_c = cs.solve()
        t_c = time.time() - t0

        comm_h, comm_c = _commute_of(days_h), _commute_of(days_c)
        obj_h, obj_c = _obj(score_h, comm_h), _obj(score_c, comm_c)
        gap = (obj_c - obj_h) / obj_c * 100 if obj_c > 0 else 0.0
        rows.append({
            "i": k, "spots": len(req.spots), "days": req.days,
            "budget": req.budget,
            "score_heur": score_h, "score_opt": score_c,
            "commute_heur": comm_h, "commute_opt": comm_c,
            "gap_pct": round(gap, 2),
            "status": cs.status_name,
            "time_heur_s": round(t_h, 3), "time_cpsat_s": round(t_c, 2),
        })
        print(f"[{k+1}/{n}] 景点{len(req.spots)} 天{req.days} "
              f"收益 {score_h}→{score_c} 通勤 {comm_h}→{comm_c} "
              f"gap {gap:.2f}% {cs.status_name} ({t_c:.1f}s)", flush=True)

    gaps = [r["gap_pct"] for r in rows]
    summary = {
        "n_scenarios": n,
        "warm_start": warm,
        "seed": seed,
        "cpsat_time_limit_s": limit,
        "objective": f"{SCORE_W}·收益 − 通勤(分钟)",
        "gap_pct": {
            "mean": round(statistics.mean(gaps), 2),
            "median": round(statistics.median(gaps), 2),
            "max": round(max(gaps), 2),
            "min": round(min(gaps), 2),
            "pct_le_3": round(sum(1 for g in gaps if g <= 3) / len(gaps) * 100, 1),
        },
        "solved_optimal": round(sum(1 for r in rows if r["status"] == "OPTIMAL") / len(rows) * 100, 1),
        "time": {
            "heur_mean_ms": round(statistics.mean(r["time_heur_s"] for r in rows) * 1000, 1),
            "cpsat_mean_s": round(statistics.mean(r["time_cpsat_s"] for r in rows), 2),
            "cpsat_max_s": round(max(r["time_cpsat_s"] for r in rows), 2),
        },
        "score": {
            "heur_mean": round(statistics.mean(r["score_heur"] for r in rows), 1),
            "opt_mean": round(statistics.mean(r["score_opt"] for r in rows), 1),
        },
        "commute": {
            "heur_mean": round(statistics.mean(r["commute_heur"] for r in rows), 1),
            "opt_mean": round(statistics.mean(r["commute_opt"] for r in rows), 1),
        },
        "rows": rows,
    }
    fname = "eval_gap_warm.json" if warm else "eval_gap.json"
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=DEFAULT_N)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--limit", type=float, default=DEFAULT_LIMIT,
                    help="每个场景 CP-SAT 时间上限（秒）")
    ap.add_argument("--warm", action="store_true",
                    help="用启发式解做暖启动（hint）")
    args = ap.parse_args()
    s = run(args.n, args.seed, args.limit, warm=args.warm)
    print("\n=== 汇总 ===")
    print(json.dumps({k: v for k, v in s.items() if k != "rows"}, ensure_ascii=False, indent=2))

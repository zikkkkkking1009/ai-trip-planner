"""N2 地理聚类改进的改前/改后对照实验。

口径（与项目既有实验一致）：
- 同一批程序生成场景（`evaluation.gen_scenarios`，固定种子 → 可复现）
- 每个场景在「关闭地理聚类」与「开启地理聚类」下各跑一次
- 通勤走本地估算（不调高德），保证两次对照口径完全一致、且不吃 API 配额

看什么：
- **通勤占比** = 通勤 / (通勤 + 游玩)，越低越好（N2 的目标指标）
- 总收益、排入景点数（确认没有为了省通勤而少排景点）
- 逐场景胜负（聚类版更好/更差/持平各多少）
- 耗时（确认新增的聚类轮没有拖慢）

用法：
    cd backend && python eval_cluster.py --n 50 --seed 42
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import solver as solver_mod
from evaluation import gen_scenarios
from solver import Solver

OUT_FILE = Path(__file__).parent / "eval_cluster.json"


def run_once(req, use_cluster: bool, pref: str = "balanced") -> dict:
    """跑一次求解；use_cluster 控制是否启用地理聚类候选轮，pref 指定偏好。

    ⚠️ 偏好要写到 req 上（Solver 从请求里读），只改 PREFERENCES 表是无效的。
    """
    solver_mod.USE_GEO_CLUSTER = use_cluster
    req.preference = pref
    solver = Solver(req)
    days, unplanned, cost, score = solver.solve()
    commute = sum(d.commute_min for d in days)
    active = sum(d.active_min for d in days)
    total = commute + active
    return {
        "ratio": round(commute / total, 4) if total else 0.0,
        "commute_min": round(commute, 1),
        "active_min": round(active, 1),
        "score": round(score, 2),
        "cost": round(cost, 1),
        "planned": sum(len(d.spots) for d in days),
        "unplanned": len(unplanned),
        "elapsed_ms": round(solver.last_elapsed_ms, 1),
    }


def mean(rows: list[dict], key: str) -> float:
    return round(statistics.mean(r[key] for r in rows), 4)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--pref", default="balanced",
                    help="偏好（复测 N2：在 less_walk 下看聚类是否有用）")
    args = ap.parse_args()

    scenarios = gen_scenarios(args.n, args.seed)
    off_rows, on_rows, detail = [], [], []
    for i, req in enumerate(scenarios):
        off = run_once(req, False, args.pref)
        on = run_once(req, True, args.pref)
        off_rows.append(off)
        on_rows.append(on)
        detail.append({
            "scenario": i, "days": req.days, "spots": len(req.spots),
            "budget": req.budget,
            "off": off, "on": on,
            "delta_ratio": round(on["ratio"] - off["ratio"], 4),
            "delta_score": round(on["score"] - off["score"], 2),
        })
        print(f"  场景 {i + 1:>2}/{len(scenarios)}："
              f"通勤占比 {off['ratio']:.3f} → {on['ratio']:.3f}"
              f"（{on['ratio'] - off['ratio']:+.3f}），"
              f"收益 {off['score']:.1f} → {on['score']:.1f}")

    better = sum(1 for d in detail if d["delta_ratio"] < -1e-6)
    worse = sum(1 for d in detail if d["delta_ratio"] > 1e-6)
    tie = len(detail) - better - worse

    print("\n=== 汇总（n={} / seed={} / 偏好={}）===".format(len(scenarios), args.seed, args.pref))
    print(f"{'指标':<16}{'关闭聚类':>12}{'开启聚类':>12}{'变化':>10}")
    for key, label, better_smaller in [
        ("ratio", "通勤占比", True), ("commute_min", "通勤分钟", True),
        ("score", "总收益", False), ("planned", "排入景点数", False),
        ("elapsed_ms", "耗时(ms)", True),
    ]:
        a, b = mean(off_rows, key), mean(on_rows, key)
        delta = b - a
        mark = ""
        if abs(delta) > 1e-9:
            good = (delta < 0) if better_smaller else (delta > 0)
            mark = "✅" if good else "⚠️"
        print(f"{label:<16}{a:>12}{b:>12}{delta:>+10.4f} {mark}")

    print(f"\n逐场景通勤占比：改善 {better} / 变差 {worse} / 持平 {tie}")
    scorediff = [d for d in detail if abs(d["delta_score"]) > 1e-9]
    print(f"逐场景收益：有变化 {len(scorediff)} 个"
          f"（其中提升 {sum(1 for d in scorediff if d['delta_score'] > 0)} 个）")

    if worse:
        print("\n注意：以下场景通勤占比变差（应分析原因）：")
        for d in sorted(detail, key=lambda x: -x["delta_ratio"])[:5]:
            if d["delta_ratio"] > 1e-6:
                print(f"  场景 {d['scenario']}（{d['days']}天/{d['spots']}景点）："
                      f"{d['off']['ratio']:.3f} → {d['on']['ratio']:.3f}")

    OUT_FILE.write_text(json.dumps({
        "n": len(scenarios), "seed": args.seed,
        "off_mean": {k: mean(off_rows, k) for k in
                     ("ratio", "commute_min", "score", "planned", "elapsed_ms")},
        "on_mean": {k: mean(on_rows, k) for k in
                    ("ratio", "commute_min", "score", "planned", "elapsed_ms")},
        "better": better, "worse": worse, "tie": tie,
        "detail": detail,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n结果已写入 {OUT_FILE.name}")


if __name__ == "__main__":
    main()

"""A2 偏好权重实验：标定 + 对照。

两个目的：
1. **标定**：扫描通勤权重，看通勤占比如何随之变化，选出「明显改善但不牺牲太多景点」的值。
   权重太小 → 偏好只是摆设（点了"少走路"却没反应，等于欺骗用户）。
2. **对照**：四种偏好在同一批场景上的指标对比，证明偏好确实改变了结果。

口径与既有实验一致：同一批程序生成场景（固定种子）、通勤走本地估算（不吃 API 配额）。

用法：
    cd backend
    python eval_preference.py --sweep          # 通勤权重扫描（标定用）
    python eval_preference.py --n 50           # 四偏好对照
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import solver as solver_mod
from evaluation import gen_scenarios
from solver import PREFERENCES, Solver

OUT_FILE = Path(__file__).parent / "eval_preference.json"
# 扫描范围要覆盖"肯为省通勤而放弃景点"的量级：
# 放弃一个景点损失 ≈ 1000×9=9000，省下通勤约 45 分钟 → 权重 ≈ 200 才是临界。
SWEEP_WEIGHTS = (1.0, 25.0, 60.0, 120.0, 200.0, 320.0, 500.0, 800.0)
# 门票同理：一张票几十元，要让"放弃这个景点"划算，权重也在百量级。
SWEEP_COST_WEIGHTS = (1.0, 20.0, 60.0, 120.0, 200.0, 350.0, 600.0)


def run_once(req, pref: str, **weights) -> dict:
    """跑一次求解；weights 用于扫描模式（临时覆盖某偏好的权重）。

    ⚠️ 必须同时设置 req.preference：Solver 从请求里读偏好（不是从参数传入），
    只改 PREFERENCES 表而不改请求，实测会得出「权重完全无效」的错误结论
    （踩过：第一版扫描就是这样，误以为偏好没用）。
    """
    if weights:
        solver_mod.PREFERENCES[pref] = dict(PREFERENCES[pref], **weights)
    req.preference = pref
    solver = Solver(req)
    days, unplanned, cost, score = solver.solve()
    commute = sum(d.commute_min for d in days)
    active = sum(d.active_min for d in days)
    total = commute + active
    return {
        "ratio": round(commute / total, 4) if total else 0.0,
        "commute_min": round(commute, 1),
        "cost": round(cost, 1),
        "score": round(score, 2),
        "planned": sum(len(d.spots) for d in days),
        "unplanned": len(unplanned),
        "elapsed_ms": round(solver.last_elapsed_ms, 1),
    }


def mean(rows: list[dict], key: str) -> float:
    return round(statistics.mean(r[key] for r in rows), 4)


def do_sweep(scenarios, which: str = "commute") -> None:
    if which == "cost":
        weights, kw, label, pref = SWEEP_COST_WEIGHTS, "cost", "门票权重", "save_money"
        head = f"{'门票权重':>8}{'总门票':>10}{'排入景点':>10}{'总收益':>10}{'通勤占比':>10}"
    else:
        weights, kw, label, pref = SWEEP_WEIGHTS, "commute", "通勤权重", "less_walk"
        head = f"{'通勤权重':>8}{'通勤占比':>10}{'通勤分钟':>10}{'排入景点':>10}{'总收益':>10}"

    print(f"=== {label}扫描（{len(scenarios)} 场景）===")
    print(head)
    out = []
    for w in weights:
        rows = [run_once(r, pref, **{kw: w}) for r in scenarios]
        rec = {"weight": w, "ratio": mean(rows, "ratio"),
               "commute_min": mean(rows, "commute_min"),
               "cost": mean(rows, "cost"),
               "planned": mean(rows, "planned"), "score": mean(rows, "score")}
        out.append(rec)
        if which == "cost":
            print(f"{w:>8}{rec['cost']:>10.1f}{rec['planned']:>10.2f}"
                  f"{rec['score']:>10.2f}{rec['ratio']:>10.3f}")
        else:
            print(f"{w:>8}{rec['ratio']:>10.3f}{rec['commute_min']:>10.1f}"
                  f"{rec['planned']:>10.2f}{rec['score']:>10.2f}")

    base = out[0]
    print("\n（权重 1.0 = 现行 balanced 口径，作为基线）")
    for r in out[1:]:
        dp = r["planned"] - base["planned"]
        if which == "cost":
            dc = r["cost"] - base["cost"]
            gain = f"门票 {dc:+.1f} 元，收益 {r['score'] - base['score']:+.2f}"
            eff = (-dc) / max(1e-9, -dp) if dp < -1e-9 else float("inf")
            unit = "每少 1 个景点省 %.0f 元" % eff if dp < -1e-9 else "景点数未下降"
        else:
            dr = r["ratio"] - base["ratio"]
            gain = f"通勤占比 {dr:+.3f}（降 {-dr*100:.1f} 个百分点）"
            eff = (-dr * 100) / max(1e-9, -dp) if dp < -1e-9 else float("inf")
            unit = ("每少 1 个景点换来 %.1f 个百分点" % eff) if dp < -1e-9 else "景点数未下降（或反而更多）"
        print(f"  权重 {r['weight']:>6}：{gain}，景点 {dp:+.2f}，{unit}")
    Path(OUT_FILE).write_text(json.dumps({f"sweep_{which}": out}, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    print(f"\n扫描结果已写入 {OUT_FILE.name}")


def do_compare(scenarios) -> None:
    print(f"=== 四种偏好对照（{len(scenarios)} 场景）===")
    keys = [("ratio", "通勤占比", True), ("commute_min", "通勤分钟", True),
            ("cost", "总门票", True), ("planned", "排入景点", False),
            ("score", "总收益", False), ("elapsed_ms", "耗时ms", True)]
    print(f"{'指标':<10}" + "".join(f"{p:>12}" for p in PREFERENCES))
    detail: dict[str, list[dict]] = {}
    for pref in PREFERENCES:
        detail[pref] = [run_once(r, pref) for r in scenarios]
    for key, label, smaller_better in keys:
        row = "".join(f"{mean(detail[p], key):>12}" for p in PREFERENCES)
        print(f"{label:<10}{row}")
    print()
    base = detail["balanced"]
    for pref in PREFERENCES:
        if pref == "balanced":
            continue
        dr = mean(detail[pref], "ratio") - mean(base, "ratio")
        dc = mean(detail[pref], "cost") - mean(base, "cost")
        dp = mean(detail[pref], "planned") - mean(base, "planned")
        print(f"  {pref:<11} 相对 balanced：通勤占比 {dr:+.3f}，门票 {dc:+.1f}，景点 {dp:+.2f}")
    Path(OUT_FILE).write_text(json.dumps(
        {p: {k: mean(detail[p], k) for k in ("ratio", "commute_min", "cost", "planned", "score")}
         for p in PREFERENCES}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n对照结果已写入 {OUT_FILE.name}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--sweep", nargs="?", const="commute", default=None,
                    choices=["commute", "cost"], help="跑权重扫描（标定）")
    args = ap.parse_args()

    scenarios = gen_scenarios(args.n, args.seed)
    if args.sweep:
        do_sweep(scenarios, args.sweep)
    else:
        do_compare(scenarios)


if __name__ == "__main__":
    main()

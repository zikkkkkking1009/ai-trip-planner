# 最小可跑 demo：不依赖 fastapi / openai，纯标准库即可验证求解器
# 用法：python run_demo.py

from demo_data import XI_AN_SPOTS
from models import PlanRequest
from solver import Solver
from constraint_check import check_plan


def main() -> None:
    req = PlanRequest(city="西安", days=3, budget=500, spots=XI_AN_SPOTS)
    import time
    t0 = time.perf_counter()
    days, unplanned, total_cost, total_score = Solver(req).solve()
    elapsed = (time.perf_counter() - t0) * 1000

    for d in days:
        print(f"\n===== Day {d.day}  门票 ¥{d.cost}  通勤 {d.commute_min} 分钟 =====")
        for v in d.spots:
            print(f"  {v.arrive_h:5.2f}h - {v.depart_h:5.2f}h  {v.name}  (¥{v.ticket})")

    print(f"\n总门票 ¥{total_cost} / 预算 ¥{req.budget}，总收益 {total_score}")
    if unplanned:
        print("没排进去：")
        for u in unplanned:
            print(f"  {u.name}（{u.reason}）")

    report = check_plan(req, days, total_cost)
    print("\n约束校验：", "✅ 通过" if report["passed"] else "❌ 违规")
    for v in report["violations"]:
        print("  [违规]", v)
    for w in report["warnings"]:
        print("  [提示]", w)
    print(f"\n求解耗时 {elapsed:.0f} ms（14 景点 / 3 天）")


if __name__ == "__main__":
    main()

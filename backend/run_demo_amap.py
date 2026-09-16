# 一键 demo（高德版）：有 Key 走真实通勤数据，没 Key 自动降级估算
# 用法：python run_demo_amap.py

from commute import CommuteMatrix
from demo_data import XI_AN_SPOTS
from models import PlanRequest
from solver import Solver
from constraint_check import check_plan


def main() -> None:
    cm = CommuteMatrix()
    mode = f"高德真实数据 (key={cm.key[:6]}...)" if cm.key else "估算模式（未配置 AMAP_KEY）"
    print(f"通勤数据源：{mode}\n")

    req = PlanRequest(city="西安", days=2, budget=300, spots=XI_AN_SPOTS)
    import time
    t0 = time.perf_counter()
    days, unplanned, total_cost, total_score = Solver(req, cm.minutes).solve()
    elapsed = (time.perf_counter() - t0) * 1000

    for d in days:
        print(f"===== Day {d.day}  门票 ¥{d.cost}  通勤 {d.commute_min} 分钟 =====")
        for v in d.spots:
            print(f"  {v.arrive_h:5.2f}h - {v.depart_h:5.2f}h  {v.name}  (¥{v.ticket})")
        print()

    print(f"总门票 ¥{total_cost} / 预算 ¥{req.budget}，总收益 {total_score}")
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

    print(f"\n通勤API统计：{cm.stats}，缓存命中率 {cm.hit_rate():.0%}")
    print(f"求解耗时 {elapsed:.0f} ms（{len(req.spots)} 景点 / {req.days} 天）")


if __name__ == "__main__":
    main()

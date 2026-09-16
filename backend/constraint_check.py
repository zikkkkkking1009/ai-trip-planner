"""约束校验器：用纯代码检查 LLM/求解器的产出是否违反现实约束。

来源：OSU TravelPlanner (ICML'24) 的核心思想——
LLM 生成的行程普遍违反预算/时间约束，必须用确定性代码兜底。
校验不过 → 把违规项喂回上游重排（本项目当前是直接报告）。
"""
from __future__ import annotations

from models import PlanRequest


def check_plan(req: PlanRequest, days, total_cost: float) -> dict:
    """对一份求解结果做体检，返回结构化报告。

    days: List[DayPlan]（见 models.py）
    """
    violations: list[str] = []
    warnings: list[str] = []

    # 1) 预算约束（硬约束）
    if req.budget is not None and total_cost > req.budget:
        violations.append(
            f"预算超支：总花费 {total_cost:.0f} 元 > 预算 {req.budget:.0f} 元")

    # 2) 每日时间负载（软约束：负载过重给警告）
    for d in days:
        if not d.spots:
            continue
        window = (req.daily_end_h - req.daily_start_h) * 60
        used = d.active_min + d.commute_min
        if used > window * 0.95:
            warnings.append(
                f"第 {d.day} 天负载 {used:.0f} 分钟，接近时间窗上限 {window:.0f} 分钟")

    # 3) 通勤合理性（软约束：通勤占比过高说明排得绕）
    for d in days:
        if d.active_min > 0:
            ratio = d.commute_min / (d.active_min + d.commute_min)
            if ratio > 0.4:
                warnings.append(
                    f"第 {d.day} 天通勤占比 {ratio:.0%}，路线可能绕路")

    # 4) 空天警告
    for d in days:
        if not d.spots:
            warnings.append(f"第 {d.day} 天没有安排任何景点")

    return {
        "passed": len(violations) == 0,
        "violations": violations,   # 违反 → 必须回炉重排
        "warnings": warnings,       # 提示 → 可人工决定是否接受
        "stats": {
            "total_cost": total_cost,
            "total_commute_min": round(sum(d.commute_min for d in days), 1),
            "spots_planned": sum(len(d.spots) for d in days),
        },
    }

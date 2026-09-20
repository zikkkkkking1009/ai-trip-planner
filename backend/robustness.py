"""N3b 稳健排程：把波动"排进去"，而不是排完再告诉用户"你只有 3% 概率走得完"。

**背景（N3 实测）**：时间窗压到 15:30 时，求解器"纸面可行"地排了 7~8 个景点，
但蒙特卡洛显示按时概率只有 3% —— 纸面可行 ≠ 现实可行。

**核心机制（这里踩过一个概念错误，记下来）**：
排程用**内缩的**时间窗（`daily_end_h - buffer`）给波动留缓冲，
但**判定始终用用户给的原始时间窗**。

为什么必须这样分：求解器的目标是尽量多排景点，**它总会把可用的时间窗填到 ~93%**。
所以如果"排程"和"判定"都用收窄后的窗口，只是把窗口整体变小、填充率不变——
下一轮模拟的按时概率几乎不动（实测：15.5→14.0 时，概率 3%→7%，仍然远达不到 70%）。
正确做法是"按 13.5 时收工排，但按 15.5 时判定"——这样缓冲才真的存在。

**为什么用内缩时间窗而不是调目标函数权重**：A2 已证明"只调权重"对结果毫无影响
（构造判据固定、优化算子本就在最小化通勤）。时间窗是硬约束，内缩它才会真的少排景点——
"用确定性换数量"，也符合用户直觉（"我要稳妥，那就少安排点"）。

**为什么缓冲量用 P90 超时估**：固定每轮收 30 分钟太慢（实测 3 轮仍不达标）；
用本轮模拟的 90 分位超时量作为下一轮缓冲，一步就能接近目标。

**为什么到下限就如实报告**：到 `min_hours` 仍不达标（景点太多或通勤太长），
就明确说"未达标 + 建议减景点"，而不是假装达标。
"""
from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)

ROBUST_STEP_H = 0.5        # 缓冲的最小步长（30 分钟）
ROBUST_MAX_ROUNDS = 3      # 最多调整轮数
ROBUST_MIN_HOURS = 5.0     # 内缩后时间窗的下限：再缩就没法玩了，宁可承认达不到目标
ROBUST_RUNS = 600          # 稳健化过程中的抽样次数（比展示用的 1000 少，省时间）


def robustify(req, day_plans, unplanned, cost, score, sim_fn: Callable,
              solve_fn: Callable,
              on_progress: Callable[[str], None] | None = None,
              target: float | None = None,
              step_h: float = ROBUST_STEP_H,
              max_rounds: int = ROBUST_MAX_ROUNDS,
              min_hours: float = ROBUST_MIN_HOURS) -> tuple[list, list, float, float, dict]:
    """迭代增大缓冲（内缩排程窗口）直到按时概率达标或到上限。

    sim_fn(day_plans, req) -> 有 .on_time_prob / .per_day 的对象（**判定用原始 req**）
    solve_fn(req) -> (day_plans, unplanned, cost, score)
    返回 (day_plans, unplanned, cost, score, info)。

    ⚠️ unplanned/cost/score 必须传进来：否则"首轮就达标"这条最常见路径会返回空值覆盖真实结果
    （踩过：写成函数内局部变量 → 首轮达标时 UnboundLocalError）。
    """
    say = on_progress or (lambda _m: None)
    target = float(req.robustness if target is None else target)
    if target <= 0:
        return day_plans, unplanned, cost, score, {}

    base_end = req.daily_end_h
    floor_end = req.daily_start_h + min_hours      # 内缩窗口的下限
    cur_un, cur_c, cur_s = unplanned, cost, score
    buffer, prob, rounds, reached = 0.0, 0.0, 0, False

    for attempt in range(max_rounds):
        sim = sim_fn(day_plans, req)               # ← 判定用**原始** req（用户真正的截止时间）
        prob, rounds = sim.on_time_prob, attempt + 1
        if prob >= target - 1e-9:
            reached = True
            break
        p90 = max((getattr(d, "return_p90", base_end) for d in getattr(sim, "per_day", [])),
                  default=base_end)
        need = max(step_h, max(0.0, p90 - base_end) + buffer)   # 需要多少总缓冲
        max_room = max(0.0, base_end - floor_end)               # 最多能缩多少
        new_buffer = min(need, max_room)
        if new_buffer <= buffer + 1e-9:
            say(f"缓冲已到上限（{buffer * 60:.0f} 分钟，时间窗下限 {min_hours:.0f} 小时）："
                f"按时概率 {prob:.0%} 仍低于目标 {target:.0%}")
            break
        say(f"第 {attempt + 1} 轮：按时概率 {prob:.0%} 未达目标 {target:.0%}，"
            f"给波动留 {new_buffer * 60:.0f} 分钟缓冲后重排"
            f"（按 {base_end - new_buffer:.1f} 时收工排，仍按 {base_end:.1f} 时判定）")
        buffer = new_buffer
        day_plans, cur_un, cur_c, cur_s = solve_fn(
            req.model_copy(update={"daily_end_h": base_end - buffer}))

    info = {"target": round(target, 2), "achieved": round(prob, 4),
            "rounds": rounds, "reached": reached,
            "buffer_h": round(buffer, 2),
            "solve_end_h": round(base_end - buffer, 2),
            "note": ("已达到目标稳妥度" if reached else
                     "已留到最大缓冲仍未达标（景点太多或通勤太长）——建议减景点或放宽时间窗")}
    log.info("稳健化：目标 %.0f%%，实测 %.1f%%，缓冲 %.0f 分钟，%d 轮，%s",
             target * 100, prob * 100, buffer * 60, rounds,
             "达标" if reached else "未达标")
    say(f"完成：按时概率 {prob:.0%}（目标 {target:.0%}）"
        + ("✅" if reached else "⚠️ 未达标"))
    return day_plans, cur_un, cur_c, cur_s, info

"""评估实验：对比「朴素装填基线 / 真 LLM 排期 / OPTW 求解器」的行程质量。

定义行程质量函数并跑批量对比实验，产出量化对比数字。

基线说明（诚实性）：
- naive_baseline：按收益降序依次装填，只做最粗的容量估计，不优化通勤、
  不精细检查开放时间窗——这是「LLM 直接排期」的行为代理。LLM 在没有
  工具/求解器辅助时正是这个模式：看起来合理，但违反开放时间、路线绕路。
  真正的 LLM 排期基线（调 API 版）见 llm_baseline_plan()，配置 Key 后
  `python evaluation.py --with-llm` 自动纳入对比。
- 我们的求解器：solver.Solver（贪心+2-opt+跨日搬运，时间窗硬约束）。

质量函数：
quality = α·(1-通勤占比) + β·(1-时间冲突率) + γ·(1-负载不均衡度)
另计硬指标：时间冲突数、预算违规数。

用法：python evaluation.py          # 50 组场景
      python evaluation.py --with-llm  # 额外跑真 LLM 基线（需 Key）
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
from dataclasses import dataclass

from demo_data import XI_AN_SPOTS
from models import DayPlan, PlanRequest, Spot, VisitedSpot
from solver import Solver, commute_min

# ---- 质量函数权重 ----
ALPHA = 0.4   # 通勤占比
BETA = 0.4    # 时间冲突
GAMMA = 0.2   # 负载均衡


@dataclass
class PlanMetrics:
    conflicts: int          # 时间冲突数（depart 超过 close_h 或 daily_end）
    budget_violated: bool
    commute_ratio: float    # 通勤 / (游玩+通勤)
    load_imbalance: float   # 各日游玩时长的变异系数 (std/mean)
    quality: float          # 加权总分 0~1


def _day_metrics(d: DayPlan, spots_by_name: dict[str, Spot],
                 req: PlanRequest) -> int:
    """数一天里的时间冲突：结束时间超过景点 close_h 或日窗。"""
    n = 0
    for v in d.spots:
        s = spots_by_name.get(v.name)
        if s and (v.depart_h > s.close_h + 1e-6
                  or v.depart_h > req.daily_end_h + 1e-6):
            n += 1
    return n


def evaluate(req: PlanRequest, days: list[DayPlan],
             total_cost: float) -> PlanMetrics:
    spots_by_name = {s.name: s for s in req.spots}
    conflicts = sum(_day_metrics(d, spots_by_name, req) for d in days)

    total_active = sum(d.active_min for d in days)
    total_commute = sum(d.commute_min for d in days)
    commute_ratio = (total_commute / (total_active + total_commute)
                     if total_active + total_commute else 0.0)

    loads = [d.active_min for d in days if d.spots]
    if len(loads) >= 2 and statistics.mean(loads) > 0:
        imbalance = min(1.0, statistics.pstdev(loads) / statistics.mean(loads))
    else:
        imbalance = 0.0

    conflict_rate = conflicts / max(1, sum(len(d.spots) for d in days))
    quality = (ALPHA * (1 - commute_ratio)
               + BETA * (1 - conflict_rate)
               + GAMMA * (1 - imbalance))

    return PlanMetrics(
        conflicts=conflicts,
        budget_violated=bool(req.budget is not None and total_cost > req.budget),
        commute_ratio=round(commute_ratio, 4),
        load_imbalance=round(imbalance, 4),
        quality=round(quality, 4),
    )


# ---- 基线一：朴素装填（LLM 无优化排期的行为代理）----
def naive_baseline(req: PlanRequest) -> tuple[list[DayPlan], float]:
    """按收益降序逐天装填：不优化顺序、不检查开放窗冲突。

    只做最粗的容量控制（累计游玩时长不超日窗），冲突照样产生——
    这正是无约束 LLM 排期的典型缺陷，用确定性代码复现其行为。
    """
    spots_by_name = {s.name: s for s in req.spots}
    days: list[DayPlan] = []
    pool = sorted(req.spots, key=lambda x: -x.score)
    capacity_min = (req.daily_end_h - req.daily_start_h) * 60
    spent = 0.0
    total_cost = 0.0
    idx = 0
    for di in range(req.days):
        vspots: list[VisitedSpot] = []
        load = 0.0
        t = req.daily_start_h
        while idx < len(pool) and load < capacity_min * 0.85:
            s = pool[idx]
            idx += 1
            depart = t + s.stay_min / 60
            vspots.append(VisitedSpot(
                name=s.name, arrive_h=round(t, 2), depart_h=round(depart, 2),
                ticket=s.ticket))
            load += s.stay_min
            t = depart          # 不加通勤——LLM 排期常忽略通勤
            spent += s.ticket
            total_cost += s.ticket
        # 事后按真实坐标补算通勤（衡量它绕了多少路）
        comm, prev = 0.0, None
        for v in vspots:
            s = spots_by_name[v.name]
            if prev is not None:
                comm += commute_min(prev, s)
            prev = s
        days.append(DayPlan(day=di + 1, spots=vspots, commute_min=round(comm, 1),
                            cost=round(sum(v.ticket for v in vspots), 1),
                            active_min=round(load, 0)))
    return days, round(total_cost, 1)


# ---- 基线二：真 LLM 排期（需 Key，结构化输出让它返回逐日名单）----
def llm_baseline_plan(req: PlanRequest) -> tuple[list[DayPlan], float]:
    """让 LLM 直接把景点分到 N 天并排顺序，不做任何算法辅助。

    输出格式与求解器对齐，方便统一评估。没有 Key 时抛 RuntimeError。
    """
    from extractor import _tolerant_json_parse
    import os
    from openai import OpenAI

    api_key = os.environ.get("LLM_API_KEY")
    base_url = os.environ.get("LLM_BASE_URL")
    if not api_key or not base_url:
        raise RuntimeError("未配置 LLM_API_KEY / LLM_BASE_URL")

    client = OpenAI(api_key=api_key, base_url=base_url)
    spot_list = [{"name": s.name, "stay_min": s.stay_min, "ticket": s.ticket,
                  "open_h": s.open_h, "close_h": s.close_h} for s in req.spots]
    prompt = (
        f"你是旅行规划师。把下面的景点安排进 {req.days} 天，每天 "
        f"{req.daily_start_h} 点到 {req.daily_end_h} 点，总门票预算 "
        f"{req.budget} 元。只输出 JSON："
        f'{{"days": [["景点名", "景点名", ...], ...]}}，共 {req.days} 个数组，'
        f"数组内是当天的游览顺序。\n景点列表：{json.dumps(spot_list, ensure_ascii=False)}"
    )
    resp = client.chat.completions.create(
        model=os.environ.get("LLM_MODEL_ID", "deepseek-chat"),
        messages=[{"role": "user", "content": prompt}], temperature=0.2)

    data = _tolerant_json_parse(resp.choices[0].message.content or "")
    spots_by_name = {s.name: s for s in req.spots}

    days: list[DayPlan] = []
    total_cost = 0.0
    for di, names in enumerate(data.get("days", [])[: req.days]):
        vspots: list[VisitedSpot] = []
        t, load, comm = req.daily_start_h, 0.0, 0.0
        prev = None
        for name in names:
            s = spots_by_name.get(name)
            if not s:
                continue
            if prev is not None:
                comm += commute_min(prev, s)
            start = max(t, s.open_h)
            depart = start + s.stay_min / 60
            vspots.append(VisitedSpot(name=s.name, arrive_h=round(t, 2),
                                      depart_h=round(depart, 2), ticket=s.ticket))
            t, load = depart, load + s.stay_min
            total_cost += s.ticket
            prev = s
        days.append(DayPlan(day=di + 1, spots=vspots, commute_min=round(comm, 1),
                            cost=round(sum(v.ticket for v in vspots), 1),
                            active_min=round(load, 0)))
    return days, round(total_cost, 1)


# ---- 场景生成（固定种子，可复现）----
def gen_scenarios(n: int, seed: int = 42) -> list[PlanRequest]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        k = rng.randint(8, len(XI_AN_SPOTS))
        spots = rng.sample(XI_AN_SPOTS, k)
        out.append(PlanRequest(
            city="西安", days=rng.randint(2, 4),
            daily_start_h=9.0, daily_end_h=rng.choice([18.0, 19.0]),
            budget=rng.choice([None, 300, 500, 800]),
            spots=spots))
    return out


def run(n: int = 50, with_llm: bool = False) -> dict:
    # .env → os.environ（extractor / llm_baseline 都从环境变量读 Key）
    import os
    from commute import load_env_file
    for k, v in load_env_file().items():
        os.environ.setdefault(k, v)

    scenarios = gen_scenarios(n)
    acc = {
        "naive": {"conflicts": [], "budget": 0, "quality": [], "commute": []},
        "solver": {"conflicts": [], "budget": 0, "quality": [], "commute": []},
    }
    llm_ok, llm_err = 0, []
    if with_llm:
        acc["llm"] = {"conflicts": [], "budget": 0, "quality": [], "commute": []}

    for req in scenarios:
        # naive
        days, cost = naive_baseline(req)
        m = evaluate(req, days, cost)
        acc["naive"]["conflicts"].append(m.conflicts)
        acc["naive"]["quality"].append(m.quality)
        acc["naive"]["commute"].append(m.commute_ratio)
        acc["naive"]["budget"] += m.budget_violated

        # solver
        days, unplanned, cost, _ = Solver(req).solve()
        m = evaluate(req, days, cost)
        acc["solver"]["conflicts"].append(m.conflicts)
        acc["solver"]["quality"].append(m.quality)
        acc["solver"]["commute"].append(m.commute_ratio)
        acc["solver"]["budget"] += m.budget_violated

        # llm（可选）
        if with_llm:
            try:
                days, cost = llm_baseline_plan(req)
                m = evaluate(req, days, cost)
                acc["llm"]["conflicts"].append(m.conflicts)
                acc["llm"]["quality"].append(m.quality)
                acc["llm"]["commute"].append(m.commute_ratio)
                acc["llm"]["budget"] += m.budget_violated
                llm_ok += 1
            except Exception as e:
                llm_err.append(f"{type(e).__name__}: {e}")  # 不静默吞错

    def summarize(a: dict) -> dict:
        c = a["conflicts"]
        return {
            "avg_conflicts_per_plan": round(statistics.mean(c), 2) if c else 0,
            "conflict_free_rate": round(sum(1 for x in c if x == 0) / len(c), 3) if c else 0,
            "avg_quality": round(statistics.mean(a["quality"]), 3) if a["quality"] else 0,
            "avg_commute_ratio": round(statistics.mean(a["commute"]), 3) if a["commute"] else 0,
            "budget_violations": a["budget"],
        }

    result = {"n_scenarios": n, "naive_baseline": summarize(acc["naive"]),
              "solver": summarize(acc["solver"])}
    if with_llm:
        result["llm_baseline"] = summarize(acc.get("llm", {"conflicts": [], "quality": [], "commute": [], "budget": 0}))
        result["llm_completed"] = llm_ok
        if llm_err:
            result["llm_errors_sample"] = llm_err[:3]
            result["llm_error_count"] = len(llm_err)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--with-llm", action="store_true")
    args = parser.parse_args()

    r = run(args.n, with_llm=args.with_llm)
    print(json.dumps(r, ensure_ascii=False, indent=2))

    if not args.with_llm:
        print("\n[提示] 未跑真 LLM 基线。配置 LLM_API_KEY 后执行 "
              "python evaluation.py --with-llm 可获得三方对比。")

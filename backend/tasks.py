"""异步任务系统：task_id + 后台求解 + 进度推送。

解决的真实工程问题（参考 TripStar 的设计）：
排期涉及高德 API 预计算（首次可达数十秒，受 QPS 节流），
同步接口会被网关 504。改为：
  POST /plan/async → 立即返回 task_id
  GET  /task/{id}  → 轮询状态（降级方案）
  WS   /ws/{id}    → WebSocket 实时推送进度与最终结果

任务目前存内存（重启即失效）；持久化到磁盘/Redis 在路线图上。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from commute import CommuteMatrix
from constraint_check import check_plan
from models import PlanRequest
from solver import Solver


@dataclass
class Task:
    id: str
    status: str = "pending"          # pending / running / completed / failed
    progress: list[dict] = field(default_factory=list)   # [{stage, msg, ts}]
    version: int = 0                 # 每次更新 +1，WebSocket 靠它判断要不要发
    result: dict | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    # 编辑流水线需要：原始请求参数与景点列表（dict 形式，便于序列化）
    req_params: dict = field(default_factory=dict)
    request_spots: list[dict] = field(default_factory=list)

    def snapshot(self) -> dict:
        return {"task_id": self.id, "status": self.status,
                "progress": self.progress, "version": self.version,
                "result": self.result, "error": self.error}


class TaskManager:
    def __init__(self):
        self._tasks: dict[str, Task] = {}

    def create(self) -> Task:
        task = Task(id=uuid.uuid4().hex[:12])
        self._tasks[task.id] = task
        return task

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def say(self, task: Task, stage: str, msg: str) -> None:
        task.progress.append({"stage": stage, "msg": msg,
                              "ts": round(time.time(), 1)})
        task.version += 1


MANAGER = TaskManager()


async def run_plan_task(task_id: str, req: PlanRequest) -> None:
    """后台执行完整排期流水线，阶段进度写入任务状态。"""
    task = MANAGER.get(task_id)
    if task is None:
        return
    task.status = "running"
    task.req_params = {"city": req.city, "days": req.days,
                       "budget": req.budget,
                       "daily_start_h": req.daily_start_h,
                       "daily_end_h": req.daily_end_h}
    task.request_spots = [s.model_dump() for s in req.spots]
    MANAGER.say(task, "启动", f"收到排期请求：{req.city} {req.days} 天，"
                              f"{len(req.spots)} 个景点，预算 {req.budget or '不限'}")

    try:
        cm = CommuteMatrix()

        # 阶段 1：预计算通勤矩阵（在子线程里跑，含 QPS 节流sleep，不卡事件循环）
        def work_commute() -> int:
            return cm.precompute(req.spots, lambda done, total: MANAGER.say(
                task, "通勤矩阵", f"景点间通勤数据 {done}/{total} 对"
                                  f"（{'高德API' if cm.key else '估算降级'}）"))

        api_calls = await asyncio.to_thread(work_commute)
        MANAGER.say(task, "通勤矩阵", f"通勤矩阵就绪：本次真实调用 {api_calls} 次，"
                                      f"缓存命中率 {cm.hit_rate():.0%}")

        # 阶段 2/3：求解（贪心 + 优化），阶段回调直写任务状态
        def work_solve():
            solver = Solver(req, cm.minutes)
            return solver.solve(
                lambda stage, info: MANAGER.say(task, stage, info.get("msg", "")))

        day_plans, unplanned, total_cost, total_score = await asyncio.to_thread(work_solve)

        # 阶段 4：约束校验
        report = check_plan(req, day_plans, total_cost)
        report["stats"]["commute_api"] = cm.stats
        report["stats"]["cache_hit_rate"] = round(cm.hit_rate(), 3)
        MANAGER.say(task, "校验", "约束校验通过 ✅" if report["passed"]
                    else f"发现违规：{'; '.join(report['violations'])}")

        task.result = {
            "city": req.city, "days": [d.model_dump() for d in day_plans],
            "total_cost": total_cost, "total_score": total_score,
            "unplanned": [u.model_dump() for u in unplanned],
            "check_report": report,
        }
        task.status = "completed"
        task.version += 1
    except Exception as e:  # 后台任务不能静默死掉
        task.status = "failed"
        task.error = f"{type(e).__name__}: {e}"
        task.version += 1


async def run_edit_task(task_id: str, base_task_id: str, instruction: str) -> None:
    """对话式修改：LLM 解析意图 → 确定性执行 → 重排求解 → 校验。

    复用整套进度推送；完成后 result 与排期任务同构（多一个 reply/changes）。
    """
    import editor
    task = MANAGER.get(task_id)
    base = MANAGER.get(base_task_id)
    if task is None or base is None or base.result is None:
        if task:
            task.status = "failed"
            task.error = "基准任务不存在或未完成"
            task.version += 1
        return
    task.status = "running"
    try:
        # 阶段 1：LLM 解析意图（子线程，防阻塞事件循环）
        MANAGER.say(task, "理解", "正在解析你的指令…")
        plan_summary = "\n".join(
            f"Day{d['day']}: " + "、".join(v["name"] for v in d["spots"])
            for d in base.result["days"])
        def work_parse():
            return editor.parse_instruction(instruction, plan_summary)
        parsed = await asyncio.to_thread(work_parse)
        ops, reply = parsed.get("ops", []), parsed.get("reply", "")

        if not ops:
            MANAGER.say(task, "理解", reply or "没有识别到可执行的修改")
            task.result = {"reply": reply or "没有识别到可执行的修改",
                           "changes": []}
            task.status = "completed"
            task.version += 1
            return
        for op in ops:
            MANAGER.say(task, "理解", f"识别到操作：{op.get('op')} "
                                      f"{op.get('name') or op.get('old') or op.get('query', '')}")

        # 阶段 2：确定性执行（周边搜索锚点 = 各天几何中心）
        from models import Spot as SpotModel
        base_spots = [SpotModel(**s) for s in base.request_spots]
        day_anchors = {}
        for d in base.result["days"]:
            pts = [next((s for s in base_spots if s.name == v["name"]), None)
                   for v in d["spots"]]
            pts = [p for p in pts if p]
            if pts:
                day_anchors[d["day"]] = (sum(p.lat for p in pts) / len(pts),
                                         sum(p.lon for p in pts) / len(pts))
        def work_apply():
            return editor.apply_ops(base_spots, ops,
                                    poi_search_fn=editor.poi_search,
                                    day_anchors=day_anchors)
        new_spots, changes = await asyncio.to_thread(work_apply)
        for c in changes:
            MANAGER.say(task, "执行", c)

        # 阶段 3：重排求解 + 校验（复用排期流水线）
        params = base.req_params
        new_req = PlanRequest(city=params.get("city", "西安"),
                              days=params.get("days", 2),
                              budget=params.get("budget"),
                              daily_start_h=params.get("daily_start_h", 9.0),
                              daily_end_h=params.get("daily_end_h", 18.0),
                              spots=new_spots)
        cm = CommuteMatrix()
        def work_solve():
            return Solver(new_req, cm.minutes).solve(
                lambda stage, info: MANAGER.say(task, stage, info.get("msg", "")))
        day_plans, unplanned, total_cost, total_score = await asyncio.to_thread(work_solve)
        report = check_plan(new_req, day_plans, total_cost)
        report["stats"]["commute_api"] = cm.stats
        report["stats"]["cache_hit_rate"] = round(cm.hit_rate(), 3)
        MANAGER.say(task, "校验", "约束校验通过 ✅" if report["passed"]
                    else f"发现违规：{'; '.join(report['violations'])}")

        task.result = {
            "city": new_req.city, "days": [d.model_dump() for d in day_plans],
            "total_cost": total_cost, "total_score": total_score,
            "unplanned": [u.model_dump() for u in unplanned],
            "check_report": report,
            "reply": reply or "行程已更新",
            "changes": changes,
        }
        task.req_params = params
        task.request_spots = [s.model_dump() for s in new_spots]
        task.status = "completed"
        task.version += 1
    except Exception as e:
        task.status = "failed"
        task.error = f"{type(e).__name__}: {e}"
        task.version += 1

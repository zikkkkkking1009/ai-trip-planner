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

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
import json
import time
import uuid
from dataclasses import dataclass, field

from commute import CommuteMatrix
from constraint_check import check_plan
from models import DayPlan, PlanRequest
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
    # 对话记忆：[{q, ops, reply}]——跨指令指代（"换到西安站"）靠它
    chat_history: list[dict] = field(default_factory=list)

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
                       "daily_end_h": req.daily_end_h, "hotel": None}
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
        from main import save_plan_snapshot
        save_plan_snapshot(task)
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
        hotel_now = (base.result.get("hotel") or {}).get("name")
        if hotel_now:
            plan_summary += f"\n当前住宿：{hotel_now}"
        def work_parse():
            return editor.parse_instruction(instruction, plan_summary,
                                            history=list(base.chat_history))
        parsed = await asyncio.to_thread(work_parse)
        ops, reply = parsed.get("ops", []), parsed.get("reply", "")
        # 调试可见性：把模型原始输出暴露到进度日志（排查「不聪明」问题的第一现场）
        if parsed.get("_raw"):
            MANAGER.say(task, "调试", "模型原始输出: " + str(parsed["_raw"])[:200])
        # ops 去重：LLM 偶尔把同一操作按天数重复输出多份
        seen, dedup = set(), []
        for o in ops:
            key = (o.get("op"), o.get("name") or o.get("old"),
                   o.get("query"), o.get("day"))
            if key not in seen:
                seen.add(key)
                dedup.append(o)
        ops = dedup

        if not ops:
            MANAGER.say(task, "理解", reply or "没有识别到可执行的修改")
            task.result = {"reply": reply or "没有识别到可执行的修改",
                           "changes": []}
            task.status = "completed"
            task.version += 1
            task.chat_history = list(base.chat_history) + \
                [{"q": instruction, "ops": [], "reply": reply}]
            return
        for op in ops:
            MANAGER.say(task, "理解", f"识别到操作: {json.dumps(op, ensure_ascii=False)}")

        # 阶段 2/3：两条执行路径
        # A) 全部是 pin_add（定点插入酒店/自定义地点）→ 只改目标天，其他天保持不变
        # B) 有其他操作 → 全局重排（现有路径）；pin 插不进时间窗时也降级到 B
        import copy
        from models import Spot as SpotModel
        base_spots = [SpotModel(**s) for s in base.request_spots]
        spot_by_name = {s.name: s for s in base_spots}
        day_anchors = {}
        for d in base.result["days"]:
            pts = [next((s for s in base_spots if s.name == v["name"]), None)
                   for v in d["spots"]]
            pts = [p for p in pts if p]
            if pts:
                day_anchors[d["day"]] = (sum(p.lat for p in pts) / len(pts),
                                         sum(p.lon for p in pts) / len(pts))
        params = base.req_params

        pin_ops = [o for o in ops if o.get("op") == "pin_add"]
        hotel_ops = [o for o in ops if o.get("op") == "hotel"]
        other_ops = [o for o in ops if o.get("op") not in ("pin_add", "hotel")]
        changes: list[str] = []
        cm = CommuteMatrix()

        # 统一的 POI 搜索提供器：周边搜索 → 全城文本搜索兜底
        # （区域型查询如「未央区 酒店」周边半径内可能搜不到，必须能降级）
        city = params.get("city", "西安")
        def search_provider(query, lat, lon):
            cands = editor.poi_search(query, lat, lon)
            if not cands:
                cands = editor.text_search(query, city)
            return cands

        # ---- hotel 操作：设定住宿锚点，影响之后所有天的通勤 ----
        for hop in hotel_ops:
            query = hop.get("query") or hop.get("name", "")
            def work_hotel():
                # 区域+类型查询（如「未央区 酒店」）用全城文本搜索更稳，
                # 搜不到再退回周边搜索
                cands = editor.text_search(query, city)
                if not cands:
                    cands = editor.poi_search(query, 34.26, 108.94, radius=10000)
                return cands
            cands = await asyncio.to_thread(work_hotel)
            poi = next((c for c in cands
                        if "酒店" in c["name"] or "宾馆" in c["name"]), None) \
                  or (cands[0] if cands else None)
            if poi is None:
                changes.append(f"没有搜到酒店「{query}」")
                continue
            params["hotel"] = {"name": poi["name"], "lat": poi["lat"],
                               "lon": poi["lon"], "desc": "住宿锚点"}
            changes.append(f"住宿设为「{poi['name']}」——每天从这里出发、回到这里")

        # ---- pin_add 定点插入（仅当没有其他操作时走轻量路径）----
        other_ops = other_ops + hotel_ops  # 有 hotel 时走全局重排（酒店影响所有天）

        if pin_ops and not other_ops:
            days = copy.deepcopy(base.result["days"])
            new_spots_acc: list[SpotModel] = []
            fallback = False
            for op in pin_ops:
                day_i = op.get("day") or 1
                d = next((x for x in days if x["day"] == day_i), None)
                if d is None:
                    continue
                query = op.get("query") or op.get("name", "")
                # 锚点优先级：after 指定的景点坐标 > 该天几何中心 > 全部景点中心
                # （修过的 bug：用 Day 几何中心搜「钟楼的全季酒店」会因距离 20km 搜不到）
                after = op.get("after")
                if after and after in spot_by_name:
                    anchor = (spot_by_name[after].lat, spot_by_name[after].lon)
                else:
                    anchor = day_anchors.get(day_i)
                if anchor is None:
                    anchor = (sum(p[0] for p in day_anchors.values()) / len(day_anchors),
                              sum(p[1] for p in day_anchors.values()) / len(day_anchors)) \
                             if day_anchors else (34.26, 108.94)
                def work_search():
                    return search_provider(query, *anchor)
                cands = await asyncio.to_thread(work_search)
                poi = editor.pick_poi(cands)
                if poi is None:
                    changes.append(f"没有搜到「{query}」")
                    continue
                if any(v["name"] == poi["name"] for dd in days for v in dd["spots"]):
                    changes.append(f"「{poi['name']}」已在行程中，跳过重复添加")
                    continue
                new_spot = editor.make_spot(poi)
                day_objs = [spot_by_name[v["name"]] for v in d["spots"]
                            if v["name"] in spot_by_name]
                def work_pin():
                    # commute_fn 必须与基准求解同一口径（高德真实数据），
                    # 否则估算偏差会把可行判成不可行。
                    # pin_insert_best 扫描全部插入位置 × 停留时长(60/30)，选通勤最小可行方案
                    from models import Hotel as HotelModel
                    hotel_obj = HotelModel(**params["hotel"]) if params.get("hotel") else None
                    return editor.pin_insert_best(
                        day_objs, new_spot, op.get("after"),
                        params.get("daily_start_h", 9.0),
                        params.get("daily_end_h", 18.0),
                        commute_fn=cm.minutes, hotel=hotel_obj)
                res = await asyncio.to_thread(work_pin)
                if res is None:
                    # 该天装不下：记录并继续（部分成功优于整单回滚）
                    changes.append(f"「{poi['name']}」在 Day{day_i} 装不下，已跳过")
                    continue
                d["spots"] = [v.model_dump() for v in res["spots"]]
                d["commute_min"], d["cost"], d["active_min"] = \
                    res["commute_min"], res["cost"], res["active_min"]
                new_spots_acc.append(new_spot)
                changes.append(f"Day{day_i} 新增「{poi['name']}」（停留 {res['stay_min']} 分钟"
                               + (f"，{op['after']} 之后" if op.get("after") else "，选通勤最小位置") + "）")

            if new_spots_acc:  # 部分成功也算成功，只有全部失败才降级全局重排
                total_cost = round(sum(d["cost"] for d in days), 1)
                total_score = round(sum(
                    spot_by_name[v["name"]].score
                    for d in days for v in d["spots"] if v["name"] in spot_by_name)
                    + sum(s.score for s in new_spots_acc), 1)
                req_for_check = PlanRequest(
                    city=params.get("city", "西安"), days=len(days),
                    budget=params.get("budget"),
                    daily_start_h=params.get("daily_start_h", 9.0),
                    daily_end_h=params.get("daily_end_h", 18.0),
                    spots=base_spots + new_spots_acc)
                report = check_plan(req_for_check, [
                    DayPlan(**d) for d in days], total_cost)
                MANAGER.say(task, "校验", "约束校验通过 ✅" if report["passed"]
                            else f"发现违规：{'; '.join(report['violations'])}")
                task.result = {
                    "city": params.get("city", "西安"), "days": days,
                    "total_cost": total_cost, "total_score": total_score,
                    "unplanned": base.result.get("unplanned", []),
                    "check_report": report,
                    "reply": reply or "行程已更新",
                    "changes": changes,
                    "hotel": params.get("hotel"),
                }
                task.req_params = params
                task.request_spots = [s.model_dump() for s in base_spots] + \
                                     [s.model_dump() for s in new_spots_acc]
                task.status = "completed"
                task.version += 1
                return
            # 降级：任一 pin 插不进时间窗 → 走全局重排（pin_add 视为普通 add）

        # 阶段 2'：全局重排路径（pin_add 在此视为普通 add）
        all_ops = [{**o, "op": "add"} if o.get("op") == "pin_add" else o
                   for o in ops]
        def work_apply():
            return editor.apply_ops(base_spots, all_ops,
                                    poi_search_fn=search_provider,
                                    day_anchors=day_anchors)
        new_spots, changes = await asyncio.to_thread(work_apply)
        # 重複去重：LLM 重复输出同一操作会导致同名景点被加多次
        seen_names, uniq = set(), []
        for s in new_spots:
            if s.name not in seen_names:
                seen_names.add(s.name)
                uniq.append(s)
        new_spots = uniq
        for c in changes:
            MANAGER.say(task, "执行", c)

        # 阶段 3：重排求解 + 校验（复用排期流水线）
        params = base.req_params
        from models import Hotel
        hotel_obj = Hotel(**params["hotel"]) if params.get("hotel") else None
        new_req = PlanRequest(city=params.get("city", "西安"),
                              days=params.get("days", 2),
                              budget=params.get("budget"),
                              daily_start_h=params.get("daily_start_h", 9.0),
                              daily_end_h=params.get("daily_end_h", 18.0),
                              spots=new_spots,
                              hotel=hotel_obj)
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

        # 新增/替换的点最终落在哪天，明确告知用户（全局重排可能挪动原计划）
        for np in new_spots:
            for d in day_plans:
                if any(v.name == np.name for v in d.spots):
                    changes.append(f"「{np.name}」已安排在 Day{d.day}")
                    break
            else:
                changes.append(f"「{np.name}」时间窗装不下，未排入")

        task.result = {
            "city": new_req.city, "days": [d.model_dump() for d in day_plans],
            "total_cost": total_cost, "total_score": total_score,
            "unplanned": [u.model_dump() for u in unplanned],
            "check_report": report,
            "reply": reply or "行程已更新",
            "changes": changes,
            "hotel": params.get("hotel"),
        }
        task.req_params = params
        task.request_spots = [s.model_dump() for s in new_spots]
        task.status = "completed"
        task.version += 1
        task.chat_history = list(base.chat_history) + \
            [{"q": instruction, "ops": ops, "reply": reply}]
        from main import save_plan_snapshot
        save_plan_snapshot(task)
    except Exception as e:
        task.status = "failed"
        task.error = f"{type(e).__name__}: {e}"
        task.version += 1


async def run_set_hotel(task_id: str, base_task_id: str, hotel: dict) -> None:
    """界面选择酒店（不走 LLM）：设住宿锚点 → 全局重排 → 校验。"""
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
        params = dict(base.req_params)
        params["hotel"] = {**hotel, "desc": "住宿锚点"}
        MANAGER.say(task, "执行", f"住宿设为「{hotel['name']}」，重新规划…")
        from models import Spot as SpotModel, Hotel
        base_spots = [SpotModel(**s) for s in base.request_spots]
        cm = CommuteMatrix()
        new_req = PlanRequest(city=params.get("city", "西安"),
                              days=params.get("days", 2),
                              budget=params.get("budget"),
                              daily_start_h=params.get("daily_start_h", 9.0),
                              daily_end_h=params.get("daily_end_h", 18.0),
                              spots=base_spots, hotel=Hotel(**params["hotel"]))
        def work():
            return Solver(new_req, cm.minutes).solve(
                lambda st, info: MANAGER.say(task, st, info.get("msg", "")))
        day_plans, unplanned, total_cost, total_score = await asyncio.to_thread(work)
        report = check_plan(new_req, day_plans, total_cost)
        report["stats"]["commute_api"] = cm.stats
        report["stats"]["cache_hit_rate"] = round(cm.hit_rate(), 3)
        MANAGER.say(task, "校验", "约束校验通过 ✅" if report["passed"]
                    else f"发现违规：{'; '.join(report['violations'])}")
        changes = [f"住宿设为「{hotel['name']}」"]
        task.result = {
            "city": new_req.city, "days": [d.model_dump() for d in day_plans],
            "total_cost": total_cost, "total_score": total_score,
            "unplanned": [u.model_dump() for u in unplanned],
            "check_report": report,
            "reply": f"住宿已设为「{hotel['name']}」",
            "changes": changes, "hotel": params["hotel"],
        }
        task.req_params = params
        task.request_spots = [s.model_dump() for s in base_spots]
        task.chat_history = list(base.chat_history) + \
            [{"q": f"（界面选择住宿：{hotel['name']}）", "ops": [],
              "reply": f"住宿已设为「{hotel['name']}」"}]
        task.status = "completed"
        task.version += 1
        from main import save_plan_snapshot
        save_plan_snapshot(task)
    except Exception as e:
        task.status = "failed"
        task.error = f"{type(e).__name__}: {e}"
        task.version += 1

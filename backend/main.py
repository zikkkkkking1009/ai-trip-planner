"""FastAPI 服务入口。

启动：uvicorn main:app --reload --port 8000
文档：http://localhost:8000/docs

接口：
- GET  /health          健康检查
- GET  /                演示页（浏览器看实时进度与行程）
- GET  /demo/spots      内置西安演示景点（零 Key 可跑）
- POST /plan            排期主接口（同步，简单场景/CI 用）
- POST /plan/async      异步排期：立即返回 task_id，后台求解
- GET  /task/{id}       任务状态轮询（降级方案）
- WS   /ws/{id}         WebSocket 实时推送进度与最终结果
- POST /extract         攻略文本 → LLM 抽取 → 实体对齐（需配 LLM Key）
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from commute import CommuteMatrix, load_env_file
from constraint_check import check_plan
from demo_data import XI_AN_SPOTS
from extractor import extract_spots
from models import PlanRequest, PlanResult
from solver import Solver

# .env → os.environ（extractor 等模块从环境变量读 Key）
for _k, _v in load_env_file().items():
    os.environ.setdefault(_k, _v)

app = FastAPI(title="AI 行程规划 API", version="0.5.0")

STATIC_DIR = Path(__file__).parent.parent / "static"


@app.get("/health")
def health() -> dict:
    cm = CommuteMatrix()
    return {"status": "ok", "solver": "greedy+2opt",
            "amap": "enabled" if cm.key else "fallback(estimate)"}


@app.post("/plan", response_model=PlanResult)
def make_plan(req: PlanRequest) -> PlanResult:
    """排期主接口。有 AMAP_KEY 时通勤走高德真实数据（带缓存），否则估算降级。"""
    if not req.spots:
        raise HTTPException(400, "景点列表为空")

    cm = CommuteMatrix()
    day_plans, unplanned, total_cost, total_score = Solver(req, cm.minutes).solve()
    report = check_plan(req, day_plans, total_cost)
    report["stats"]["commute_api"] = cm.stats
    report["stats"]["cache_hit_rate"] = round(cm.hit_rate(), 3)

    return PlanResult(
        city=req.city, days=day_plans, total_cost=total_cost,
        total_score=total_score, unplanned=unplanned, check_report=report,
    )


@app.post("/extract")
def extract(text: str, city: str = "西安") -> dict:
    """攻略文本 → LLM 抽取 → 实体对齐到高德 POI（坐标校正为真实值）。

    链路：LLM 抽取（占位坐标）→ POIAligner（标准名 + 真实坐标）
    低置信度的条目标记 needs_review，坐标保持占位值，由前端让用户点选。
    """
    try:
        spots = extract_spots(text)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except (ValueError, KeyError) as e:
        raise HTTPException(422, f"抽取失败：{e}")

    from aligner import POIAligner, align_spot
    aligner = POIAligner()
    aligned, review = [], []
    for s in spots:
        s, r = align_spot(s, aligner, city)
        item = s.model_dump() | {"confidence": r.confidence,
                                 "needs_review": r.needs_review}
        (review if r.needs_review else aligned).append(item)
    return {"count": len(spots), "spots": aligned + review,
            "needs_review_count": len(review),
            "poi_api_stats": aligner.stats}


# ---------- 异步任务化（v0.5） ----------

@app.get("/")
def index():
    """演示页：浏览器里看实时进度推送与逐日行程。"""
    index_html = STATIC_DIR / "index.html"
    if index_html.exists():
        from fastapi.responses import HTMLResponse
        return HTMLResponse(index_html.read_text(encoding="utf-8"))
    raise HTTPException(404, "static/index.html 不存在")


@app.get("/demo/spots")
def demo_spots() -> dict:
    return {"city": "西安", "spots": [s.model_dump() for s in XI_AN_SPOTS]}


@app.post("/plan/async")
async def create_async_plan(req: PlanRequest) -> dict:
    """异步排期：立即返回 task_id，后台跑完整流水线（不再被网关 504）。"""
    if not req.spots:
        raise HTTPException(400, "景点列表为空")
    from tasks import MANAGER, run_plan_task
    task = MANAGER.create()
    asyncio.create_task(run_plan_task(task.id, req))
    return {"task_id": task.id, "ws_url": f"/ws/{task.id}",
            "poll_url": f"/task/{task.id}"}


@app.get("/task/{task_id}")
def get_task(task_id: str) -> dict:
    """轮询接口（WebSocket 不可用时的降级方案）。"""
    from tasks import MANAGER
    task = MANAGER.get(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task.snapshot()


@app.websocket("/ws/{task_id}")
async def ws_task(websocket: WebSocket, task_id: str) -> None:
    """WebSocket 实时推送：进度更新时发增量，任务结束时发终态。"""
    from tasks import MANAGER
    await websocket.accept()
    last_version = -1
    try:
        while True:
            task = MANAGER.get(task_id)
            if task is None:
                await websocket.send_json({"status": "not_found"})
                break
            if task.version != last_version:
                await websocket.send_json(task.snapshot())
                last_version = task.version
            if task.status in ("completed", "failed"):
                break
            await asyncio.sleep(0.3)
    except WebSocketDisconnect:
        pass  # 客户端提前关页面是正常行为

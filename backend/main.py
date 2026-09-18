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
import json
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

app = FastAPI(title="AI 行程规划 API", version="0.5.1")

STATIC_DIR = Path(__file__).parent.parent / "static"
from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

DATA_DIR = Path(__file__).parent.parent / "data"
PLANS_DIR = DATA_DIR / "plans"
FAV_FILE = DATA_DIR / "favorites.json"
MEDIA_FILE = Path(__file__).parent / "spot_media.json"


def _load_favorites() -> list[dict]:
    if FAV_FILE.exists():
        return json.loads(FAV_FILE.read_text(encoding="utf-8"))
    return []


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
    """演示景点，富化高德实景图与介绍（spot_media.json，预抓取零 Key 可用）。"""
    media = json.loads(MEDIA_FILE.read_text(encoding="utf-8")) if MEDIA_FILE.exists() else {}
    spots = []
    for s in XI_AN_SPOTS:
        d = s.model_dump()
        m = media.get(s.name) or {}
        d["image"] = m.get("image", "")
        d["intro"] = m.get("intro", "")
        spots.append(d)
    return {"city": "西安", "spots": spots}


# ---------- 用户页：历史规划 + 收藏 ----------

@app.get("/plans")
def list_plans() -> dict:
    """历史规划列表（落盘持久化，重启不丢）。"""
    plans = []
    if PLANS_DIR.exists():
        for f in sorted(PLANS_DIR.glob("*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                r = d.get("result") or {}
                plans.append({
                    "task_id": d.get("task_id"),
                    "created_at": d.get("created_at"),
                    "city": r.get("city"),
                    "total_cost": r.get("total_cost"),
                    "spots_planned": (r.get("check_report") or {})
                        .get("stats", {}).get("spots_planned"),
                    "hotel": (r.get("hotel") or {}).get("name"),
                })
            except Exception:
                continue
    return {"plans": plans}


def save_plan_snapshot(task) -> None:
    """规划完成后落盘，供「我的-历史规划」随时回看。"""
    try:
        PLANS_DIR.mkdir(parents=True, exist_ok=True)
        (PLANS_DIR / f"{task.id}.json").write_text(
            json.dumps({"task_id": task.id, "created_at": task.created_at,
                        "params": task.req_params, "result": task.result},
                       ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass  # 持久化失败不影响主流程


@app.get("/poi/detail")
def poi_detail(name: str) -> dict:
    """景点媒体详情：图片组/介绍/营业时间/地址。

    预抓取（spot_media.json）没收录的名字（如对话中新增的酒店）现场走高德
    搜索+详情，并缓存进媒体文件，下次零开销。
    """
    media = json.loads(MEDIA_FILE.read_text(encoding="utf-8")) if MEDIA_FILE.exists() else {}
    m = media.get(name)
    if not m:
        from fetch_spot_details import fetch
        m = fetch(name)
        if not (m.get("image") or m.get("address")):
            raise HTTPException(404, "未找到该地点的高德信息")
        media[name] = m
        MEDIA_FILE.write_text(json.dumps(media, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    return m


@app.post("/hotel/search")
def hotel_search(body: dict) -> dict:
    """酒店搜索（携程式选择器用）：名称/区域均可。"""
    from editor import poi_search, text_search
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(400, "需要 query")
    city = body.get("city", "西安")
    cands = text_search(query, city) or poi_search(query, 34.26, 108.94, radius=10000)
    return {"results": [{"name": c["name"], "lat": c["lat"], "lon": c["lon"],
                         "intro": c.get("type_str", "").split(";")[0]}
                        for c in cands[:8]]}


@app.post("/hotel/set")
async def hotel_set(body: dict) -> dict:
    """界面选择酒店 → 设住宿锚点 → 异步重排（返回新 task_id）。"""
    from tasks import MANAGER, run_set_hotel
    base_task_id = body.get("task_id", "")
    hotel = {"name": body.get("name"), "lat": body.get("lat"), "lon": body.get("lon")}
    if not base_task_id or not hotel["name"]:
        raise HTTPException(400, "需要 task_id 和酒店信息")
    if MANAGER.get(base_task_id) is None or MANAGER.get(base_task_id).status != "completed":
        raise HTTPException(404, "基准任务不存在或未完成")
    task = MANAGER.create()
    asyncio.create_task(run_set_hotel(task.id, base_task_id, hotel))
    return {"task_id": task.id, "poll_url": f"/task/{task.id}"}


@app.delete("/plans/{task_id}")
def delete_plan(task_id: str) -> dict:
    f = PLANS_DIR / f"{task_id}.json"
    if not f.exists():
        raise HTTPException(404, "该规划不存在")
    f.unlink()
    from tasks import MANAGER
    MANAGER._tasks.pop(task_id, None)
    return {"deleted": task_id}


@app.get("/favorites")
def get_favorites() -> dict:
    return {"favorites": _load_favorites()}


@app.post("/favorites")
def mod_favorites(body: dict) -> dict:
    action = body.get("action")
    spot = body.get("spot") or {}
    if not spot.get("name"):
        raise HTTPException(400, "需要 spot.name")
    favs = _load_favorites()
    if action == "add":
        if not any(f["name"] == spot["name"] for f in favs):
            favs.insert(0, {"name": spot["name"], "lat": spot.get("lat"),
                            "lon": spot.get("lon"), "image": spot.get("image", ""),
                            "desc": spot.get("desc", ""), "intro": spot.get("intro", ""),
                            "city": spot.get("city", "")})
    elif action == "remove":
        favs = [f for f in favs if f["name"] != spot["name"]]
    else:
        raise HTTPException(400, "action 需要 add 或 remove")
    FAV_FILE.parent.mkdir(parents=True, exist_ok=True)
    FAV_FILE.write_text(json.dumps(favs, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    return {"favorites": favs}


@app.get("/demo/config")
def demo_config() -> dict:
    """前端地图底图配置。TILE_PROVIDER=osm 切换 OSM（需前端做 GCJ→WGS84 转换）。

    默认高德栅格瓦片仅建议本地开发/演示使用；合规生产用法是官方 JS API（Web端 Key）。
    """
    env = load_env_file()
    provider = env.get("TILE_PROVIDER", os.environ.get("TILE_PROVIDER", "amap"))
    if provider == "osm":
        return {"tile_url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
                "subdomains": "abc", "attribution": "© OpenStreetMap",
                "gcj": False}
    return {"tile_url": "https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}",
            "subdomains": "1234", "attribution": "© 高德地图", "gcj": True}


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
    """轮询接口；历史任务（内存已清）从磁盘快照兜底。"""
    from tasks import MANAGER
    task = MANAGER.get(task_id)
    if task is not None:
        return task.snapshot()
    f = PLANS_DIR / f"{task_id}.json"
    if f.exists():
        d = json.loads(f.read_text(encoding="utf-8"))
        return {"task_id": task_id, "status": "completed",
                "progress": [], "result": d.get("result"),
                "error": None}
    raise HTTPException(404, "任务不存在")


@app.post("/plan/edit")
async def edit_plan(body: dict) -> dict:
    """对话式修改：{"task_id": 基准任务, "instruction": "明天下午加个咖啡馆"}。

    返回新任务 id，进度与结果走同样的 WS /task 通道。
    需要 LLM Key 解析指令（503 = 未配置）。
    """
    import os
    base_task_id = body.get("task_id", "")
    instruction = (body.get("instruction") or "").strip()
    if not base_task_id or not instruction:
        raise HTTPException(400, "需要 task_id 和 instruction")
    from commute import load_env_file
    env = load_env_file()
    if not (env.get("LLM_API_KEY") or os.environ.get("LLM_API_KEY")):
        raise HTTPException(503, "未配置 LLM_API_KEY，无法解析编辑指令")
    from tasks import MANAGER, run_edit_task
    if MANAGER.get(base_task_id) is None or MANAGER.get(base_task_id).status != "completed":
        raise HTTPException(404, "基准任务不存在或未完成")
    task = MANAGER.create()
    asyncio.create_task(run_edit_task(task.id, base_task_id, instruction))
    return {"task_id": task.id, "ws_url": f"/ws/{task.id}",
            "poll_url": f"/task/{task.id}"}


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

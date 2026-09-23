"""FastAPI 服务入口。

启动：uvicorn main:app --reload --port 8000
文档：http://localhost:8000/docs

接口：
- GET  /health          健康检查
- GET  /meta            首页用：接口清单 + 服务状态（均取自运行时，非手写）
- GET  /                演示页（浏览器看实时进度与行程）
- GET  /cities          可演示城市列表（前端城市选择器数据源）
- GET  /demo/spots      按城市返回演示景点（零 Key 可跑）
- POST /plan            排期主接口（同步，简单场景/CI 用）
- POST /plan/async      异步排期：立即返回 task_id，后台求解
- GET  /task/{id}       任务状态轮询（降级方案）
- WS   /ws/{id}         WebSocket 实时推送进度与最终结果
- POST /extract         攻略文本 → LLM 抽取（含城市识别）→ 实体对齐（需配 LLM Key）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.routing import APIRoute

from commute import CommuteMatrix, load_env_file

# .env → os.environ（extractor 等模块从环境变量读 Key）
for _k, _v in load_env_file().items():
    os.environ.setdefault(_k, _v)

# 日志必须在上面的 env 加载之后初始化，这样 LOG_LEVEL / LOG_FILE 才生效
from logging_setup import new_request_id, request_id_var, setup_logging

setup_logging()
log = logging.getLogger(__name__)

from cities import CITY_CENTERS, DEFAULT_CITY, city_center, normalize_city
from constraint_check import check_plan
from demo_data import demo_cities, demo_spots
from extractor import extract_guide
from media_cache import (get_entry, load_media, media_key, save_media,
                         suspected_wrong_city)
from models import PlanRequest, PlanResult
from solver import Solver
from weather import daily_weather

# Key 可用性自检。selftest 顶层**只**依赖标准库（项目内 import 全部写在函数里），
# 所以这里 import 它不会形成环 —— 把探测逻辑内联进 main 反而会破坏
# 「/meta 只读内存快照」的纪律。
import selftest


@asynccontextmanager
async def lifespan(_: FastAPI):
    """启动时探测一次各 Key 的真实可用性（结果缓存，见 selftest.PROBE_TTL_SEC）。

    **只投递、不等待**：应用必须立刻可监听端口 —— /health 是 Docker healthcheck，
    不能等一次网络超时之后才就绪。探测在后台线程里跑，跑完前 /meta 返回
    `probe.state = "probing"`，前端据此显示「检测中」而不是谎称可用或不可用。
    """
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, selftest.schedule_probe)
    yield


app = FastAPI(title="AI 行程规划 API", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """为每个请求注入 request_id，并记录耗时——日志可按 ID 串起完整链路。"""
    rid = new_request_id()
    token = request_id_var.set(rid)
    t0 = time.time()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("请求异常 %s %s", request.method, request.url.path)
        raise
    finally:
        request_id_var.reset(token)
    cost_ms = (time.time() - t0) * 1000
    if not request.url.path.startswith("/static"):
        log.info("%s %s → %d（%.0fms）", request.method, request.url.path,
                 response.status_code, cost_ms)
    response.headers["X-Request-Id"] = rid
    return response

STATIC_DIR = Path(__file__).parent.parent / "static"
from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

DATA_DIR = Path(__file__).parent.parent / "data"
PLANS_DIR = DATA_DIR / "plans"
FAV_FILE = DATA_DIR / "favorites.json"
# 媒体缓存（spot_media.json）的读写统一走 media_cache 模块（key 规则 = 「城市|景点名」）


def _load_favorites() -> list[dict]:
    if FAV_FILE.exists():
        return json.loads(FAV_FILE.read_text(encoding="utf-8"))
    return []


def _service_status() -> dict:
    """服务状态的真实取数：/health 与 /meta 共用同一份，避免两处口径漂移。

    刻意的口径分工（不要"统一"它们）：
      · /health 的 `amap` 表示「Key 是否存在」—— 它是 Docker healthcheck，
        判的是"进程能不能服务"，探测失败（网络不通）不能让它变 unhealthy；
      · /meta 的 `amap_key` / `llm` 表示「Key 探测后是否真的可用」——
        那是给人看的状态，必须诚实。
    两者共用 `CommuteMatrix().key` 判存在性，但可用性只由 selftest 探测给出。
    """
    from tasks import MANAGER
    cm = CommuteMatrix()
    return {"status": "ok", "solver": "greedy+2opt",
            "amap": "enabled" if cm.key else "fallback(estimate)",
            "tasks": MANAGER.stats()}


@app.get("/health")
def health() -> dict:
    """健康检查：求解器、通勤数据源、内存任务队列的实时状态（首页「服务状态」与它同源）。"""
    return _service_status()


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
def extract(text: str, city: str = "") -> dict:
    """攻略文本 → LLM 抽取（含城市识别）→ 实体对齐到高德 POI（坐标校正为真实值）。

    - `city`：前端已选城市，作为 LLM 未识别时的兜底
    - 返回 `detected_city`（LLM 识别结果）与 `needs_city`（是否未能确定城市）——
      `needs_city=true` 时前端必须提示用户选择，**不要静默按默认城市排行程**
    - 对齐必须用**确定的城市**：高德搜索带 citylimit，用错城市会搜不到或搜到同名异地 POI
    - 低置信度的条目标记 needs_review，坐标保持占位值，由前端让用户点选
    """
    try:
        spots, detected_city = extract_guide(text, city_hint=city)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except (ValueError, KeyError) as e:
        raise HTTPException(422, f"抽取失败：{e}")

    resolved = detected_city or normalize_city(city)
    needs_city = not resolved
    align_city = resolved or DEFAULT_CITY
    if needs_city:
        log.warning("抽取未能确定城市，暂用 %s 对齐并向用户索取城市选择", align_city)

    from aligner import POIAligner, align_spot
    aligner = POIAligner()
    aligned, review = [], []
    for s in spots:
        s, r = align_spot(s, aligner, align_city)
        item = s.model_dump() | {"confidence": r.confidence,
                                 "needs_review": r.needs_review}
        (review if r.needs_review else aligned).append(item)
    return {"count": len(spots), "spots": aligned + review,
            "needs_review_count": len(review),
            "city": align_city, "detected_city": detected_city,
            "needs_city": needs_city,
            "poi_api_stats": aligner.stats}


# ---------- 异步任务化（v0.5） ----------

@app.get("/")
def home():
    """首页（Landing）：项目介绍与入口——与规划器分开，见 `static/home.html`。"""
    return _serve_html("home.html")


@app.get("/app")
def planner_page():
    """规划器本体：表单 / 进度 / 逐日行程 / 地图 / 我的（`static/index.html`）。"""
    return _serve_html("index.html")


def _serve_html(filename: str):
    """直接把 static/ 下的单文件页面返回。

    这个项目的前端是「单文件、零构建、零 CDN」，所以不需要模板引擎——
    读文件原样返回即可，也便于把首页与规划器做成两个独立入口。
    """
    f = STATIC_DIR / filename
    if f.exists():
        from fastapi.responses import HTMLResponse
        return HTMLResponse(f.read_text(encoding="utf-8"))
    raise HTTPException(404, f"static/{filename} 不存在")


@app.get("/demo/spots")
def demo_spot_list(city: str = DEFAULT_CITY) -> dict:
    """按城市返回演示景点，富化高德实景图与介绍（spot_media.json，预抓取零 Key 可用）。

    未知城市返回空列表 + supported 提示，**不会静默换成别的城市的数据**。
    """
    city = normalize_city(city) or DEFAULT_CITY
    media = load_media()
    spots = []
    for s in demo_spots(city):
        d = s.model_dump()
        m = get_entry(media, city, s.name) or {}
        d["image"] = m.get("image", "")
        d["intro"] = m.get("intro", "")
        spots.append(d)
    return {"city": city, "spots": spots,
            "available": bool(spots), "supported_cities": demo_cities(),
            "center": city_center(city)}


@app.get("/cities")
def list_cities() -> dict:
    """可演示城市列表（前端城市选择器数据源）。"""
    return {"cities": demo_cities(), "default": DEFAULT_CITY,
            "centers": {c: city_center(c) for c in demo_cities()}}


@app.get("/weather")
def get_weather(city: str, start: str, end: str) -> dict:
    """行程期间的按天天气（可选增强）。

    为什么独立成接口、不并进 /plan 的返回：天气**不参与求解**（不该影响排期结果），
    拆开可以让它自己失败而不拖累主流程，前端也能在行程渲染完之后再异步取。
    拿不到时返回 available=false + reason，由前端明说原因 —— 不用假数据填充。
    """
    try:
        d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise HTTPException(400, "start / end 需为 YYYY-MM-DD 格式")
    if d1 < d0:
        raise HTTPException(400, "end 不能早于 start")
    if (d1 - d0).days > 31:
        raise HTTPException(400, "日期范围过大（最多 32 天）")
    return daily_weather(normalize_city(city) or DEFAULT_CITY, d0, d1)


def _api_catalog() -> dict:
    """接口清单：从**运行时路由表**现算，不手写 —— 手写的第二份列表迟早与代码漂移。

    框架自带的路由（/docs、/openapi.json 等）与无 HTTP 方法的路由（/static 挂载、
    /ws WebSocket）一律跳过，但都记进 `skipped` 一并返回，让首页能如实说明
    「这些去哪了」，而不是让人以为接口悄悄少了几个。
    """
    endpoints, skipped = [], []
    for r in app.routes:
        path = getattr(r, "path", "")
        methods = sorted(getattr(r, "methods", None) or [])
        if not methods:
            skipped.append({"path": path, "reason": "无 HTTP 方法（静态挂载或 WebSocket）"})
        elif not isinstance(r, APIRoute):
            skipped.append({"path": path, "reason": "框架自带路由（非本项目接口）"})
        else:
            doc = (r.endpoint.__doc__ or "").strip().splitlines()
            # 摘要优先取 docstring 首行；没写 docstring 就退回函数名，至少有个标识
            endpoints.append({"methods": methods, "path": path,
                              "summary": doc[0].strip() if doc else r.name})
    endpoints.sort(key=lambda e: e["path"])
    return {"endpoints": endpoints, "skipped": skipped}


@app.get("/meta")
def meta() -> dict:
    """首页「接口清单 / 服务状态」的唯一数据源：全部取自运行时，不手写。

    为什么单独开这个接口：首页 footer 的「接口文档 / 服务状态」原先分别指向 /docs
    （Swagger UI 的静态资源从 CDN 加载，违反本站零 CDN 的纪律）与 /health
    （137 字节裸 JSON，撑不起一个区块）。把真实路由表与真实配置状态汇总成一份数据，
    首页即可在**站内**渲染这两块，且数据永远与代码一致。

    Key 的可用性由 selftest 在后台探测并缓存 —— 这里只读内存快照，**不发任何外部
    请求**，所以状态接口依然没有超时/重试风险（这是当初的设计底线，不能破）。
    """
    status = _service_status()
    city_list = demo_cities()
    probe = selftest.status()
    status.update({
        # 四态（missing / ok / invalid / unreachable；快通道另有 inherited）。
        # 只回状态不回 Key 值 —— 响应会直接渲染到首页，test_meta 有测试锁死这一点
        "llm": probe["llm"],
        "llm_fast": probe["llm_fast"],
        "amap_key": probe["amap_key"],
        # 「配置了但还没探到」需要单独告诉前端：这时只能显示灰色「检测中」，
        # 绝不能显示成绿色可用或橙色未配置 —— 两个都是谎
        "configured": selftest.configured(),
        "probe": {"state": probe["state"], "age_s": selftest.probe_age_s(),
                  "stale": bool(probe["stale"]), "reasons": probe["reasons"]},
        "cities": len(CITY_CENTERS),
        "spots": {"total": sum(len(demo_spots(c)) for c in city_list),
                  "by_city": {c: len(demo_spots(c)) for c in city_list}},
        "python": sys.version.split()[0],
        "version": app.version,
    })
    return {**_api_catalog(), "status": status}


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
                if d.get("deleted"):
                    continue
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
            except Exception as e:
                # 单条历史损坏 → 跳过该条，但记日志（否则用户看不到某条历史却无从解释）
                log.warning("历史规划快照解析失败，已跳过: %s: %s", type(e).__name__, e)
                continue
    return {"plans": plans}


def save_plan_snapshot(task) -> None:
    """规划完成后落盘，供「我的-历史规划」随时回看。"""
    try:
        PLANS_DIR.mkdir(parents=True, exist_ok=True)
        (PLANS_DIR / f"{task.id}.json").write_text(
            json.dumps({"task_id": task.id, "created_at": task.created_at,
                        "params": task.req_params, "result": task.result,
                        # 景点明细也存：稳健性模拟（N3）需要每个景点的坐标与计划停留时长，
                        # 只靠 result 里的名字没法重算通勤时间线
                        "request_spots": task.request_spots},
                       ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        # 持久化失败不影响主流程，但必须留痕——静默吞异常是踩过的坑（HANDOFF 坑表）
        log.warning("规划快照落盘失败 task_id=%s: %s: %s",
                    task.id, type(e).__name__, e)


@app.get("/poi/detail")
def poi_detail(name: str, city: str = "") -> dict:
    """快接口：图片组/介绍/营业时间/地址，毫秒级返回。

    评价摘要不走这里（首次生成需 ~3s），前端拿到基础数据立即渲染后
    再调 /poi/reviews 异步补上——避免点开详情要等好几秒。

    **city 必须传**：高德搜索带 citylimit，用错城市搜不到（返回 404）。
    未命中缓存时按 city 抓取，抓到后写入 spot_media.json 供后续零成本命中。
    """
    city = normalize_city(city) or DEFAULT_CITY
    media = load_media()
    m = get_entry(media, city, name)
    if not m:
        from fetch_spot_details import fetch
        m = fetch(name, city)
        if not (m.get("image") or m.get("address")):
            raise HTTPException(404, f"未找到「{name}」在 {city} 的高德信息")
        # 写入前校验载荷真的属于该城市：高德 citylimit 不严格，抓回来的可能是别的城市的
        # 同名/近似 POI，一旦写进缓存就被永久固化（历史实例：四川博物院 存了西安的数据）
        wrong = suspected_wrong_city(city, m)
        if wrong:
            log.warning("高德返回的是「%s」的数据，与请求城市 %s 不符，拒绝写入缓存 "
                        "name=%s address=%s", wrong, city, name, m.get("address", ""))
            raise HTTPException(404, f"「{name}」在 {city} 未找到（高德返回了 {wrong} 的结果）")
        media[media_key(city, name)] = m
        save_media(media)
    out = {k: m.get(k, "") for k in
           ("image", "intro", "photos", "opentime", "address", "lat", "lon")}
    out["reviews"] = m.get("reviews")
    out["reviews_ai"] = m.get("reviews_ai", False)
    return out


@app.get("/poi/reviews")
def poi_reviews(name: str, city: str = "") -> dict:
    """评价摘要（AI 生成，首次约 3s，之后走缓存）。前端二次拉取用。

    city 用于定位缓存条目：缓存 key 是「城市|景点名」，同名景点跨城市不能混用。
    """
    city = normalize_city(city) or DEFAULT_CITY
    media = load_media()
    m = get_entry(media, city, name)
    if m is None:
        raise HTTPException(404, "该地点未收录")
    if "reviews" not in m:
        try:
            from editor import generate_reviews
            m["reviews"] = generate_reviews(
                name, (m.get("intro", "") or "") + " " + (m.get("address", "") or ""))
            m["reviews_ai"] = m["reviews"] is not None
        except Exception as e:
            # 降级为「无评价」，但要留痕（静默 fallback 会让线上问题无法定位）
            log.warning("评价生成失败 name=%s: %s: %s", name, type(e).__name__, e)
            m["reviews"] = None
            m["reviews_ai"] = False
        media[media_key(city, name)] = m
        save_media(media)
    return {"reviews": m.get("reviews"), "reviews_ai": m.get("reviews_ai", False)}


@app.post("/hotel/search")
def hotel_search(body: dict) -> dict:
    """酒店搜索（携程式选择器用）：名称/区域均可。"""
    from editor import poi_search, text_search
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(400, "需要 query")
    city = normalize_city(body.get("city")) or DEFAULT_CITY
    # 降级路径用**该城市中心**做周边搜索，不能写死某个城市的坐标
    center = city_center(city) or city_center(DEFAULT_CITY)
    if center is None:
        raise HTTPException(500, f"城市表缺少 {DEFAULT_CITY}，请检查 backend/cities.py")
    cands = text_search(query, city) or poi_search(query, center[0], center[1], radius=10000)
    return {"results": [{"name": c["name"], "lat": c["lat"], "lon": c["lon"],
                         "intro": c.get("type_str", "").split(";")[0],
                         "image": c.get("image", "")}
                        for c in cands[:8]],
            "city": city}


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


def _soft_delete_plan(f) -> bool:
    """软删除：标记 deleted 字段（不物理删文件——误删可恢复，也避开
    沙箱的批量物理删除安全守卫，避免大批量删除时被拦成 500）。"""
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("软删除失败（文件不可解析）%s: %s: %s", f.name, type(e).__name__, e)
        return False
    d["deleted"] = True
    f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return True


@app.delete("/plans/{task_id}")
def delete_plan(task_id: str) -> dict:
    """删除单个历史规划（软删除，标记而非抹掉，可恢复）。"""
    f = PLANS_DIR / f"{task_id}.json"
    if not f.exists():
        raise HTTPException(404, "该规划不存在")
    _soft_delete_plan(f)
    from tasks import MANAGER
    MANAGER._tasks.pop(task_id, None)
    return {"deleted": task_id}


@app.post("/plans/delete")
def delete_plans_batch(body: dict) -> dict:
    """批量删除（我的页管理模式）。"""
    ids = body.get("task_ids") or []
    if not ids:
        raise HTTPException(400, "task_ids 为空")
    from tasks import MANAGER
    deleted = []
    for tid in ids:
        f = PLANS_DIR / f"{tid}.json"
        if f.exists() and _soft_delete_plan(f):
            deleted.append(tid)
        MANAGER._tasks.pop(tid, None)
    return {"deleted": deleted, "count": len(deleted)}


@app.get("/favorites")
def get_favorites() -> dict:
    """收藏的景点列表（按收藏时间倒序）。"""
    return {"favorites": _load_favorites()}


@app.post("/favorites")
def mod_favorites(body: dict) -> dict:
    """新增或取消收藏（body.action = add / remove）。"""
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
        if not d.get("deleted"):
            return {"task_id": task_id, "status": "completed",
                    "progress": [], "result": d.get("result"),
                    "error": None}
    raise HTTPException(404, "任务不存在")


def _load_task_payload(task_id: str) -> tuple[dict | None, dict, dict]:
    """取任务结果：内存优先、磁盘快照兜底。返回 (result, params, spot_by_name)。

    抽成公共函数是因为「稳健性模拟」和「AI 总评」都要用同一套取用逻辑——
    两处各写一份的话，将来只改一处就会出现口径不一致（这类重复已经踩过）。
    """
    from models import Spot
    from tasks import MANAGER

    task = MANAGER.get(task_id)
    if task is not None and task.status == "completed" and task.result:
        return (task.result, dict(task.req_params or {}),
                {s["name"]: Spot(**s) for s in (task.request_spots or [])})
    f = PLANS_DIR / f"{task_id}.json"
    if not f.exists():
        raise HTTPException(404, "任务不存在或尚未完成")
    snap = json.loads(f.read_text(encoding="utf-8"))
    if snap.get("deleted"):
        raise HTTPException(404, "任务已删除")
    # 快照里的键名是 params（不是 req_params）
    spot_by_name = {s["name"]: Spot(**s) for s in (snap.get("request_spots") or [])}
    if not spot_by_name:
        log.warning("快照缺少 request_spots（旧版本数据），将退化为无通勤口径 task_id=%s", task_id)
    return snap.get("result"), (snap.get("params") or {}), spot_by_name


@app.post("/plan/simulate")
def simulate_task(body: dict) -> dict:
    """N3 稳健性模拟：给停留与通勤加噪声，返回「按时完成概率 + 风险点」。

    为什么放服务端算：模拟要按真实通勤重算时间线（前端只有分钟数），
    且 1000 次抽样叠加"逐个景点去掉再评"（共同随机数）在浏览器里会卡。
    通勤走 CommuteMatrix 的磁盘缓存 → 不额外消耗高德配额。
    """
    from commute import CommuteMatrix
    from models import DayPlan, Hotel, Spot
    from simulation import simulate as run_sim

    result, params, spot_by_name = _load_task_payload(body.get("task_id", ""))
    if not result or not result.get("days"):
        raise HTTPException(400, "该任务不是行程结果，无法模拟")

    runs = max(200, min(int(body.get("runs") or 1000), 5000))
    plan_days = [DayPlan(**d) for d in result["days"]]
    hotel = Hotel(**params["hotel"]) if params.get("hotel") else None
    city = params.get("city", DEFAULT_CITY)
    # 兜底坐标取**当前城市**中心（老快照缺 request_spots 时才会用到），不写死字面坐标
    _fallback = city_center(city) or city_center(DEFAULT_CITY)
    # 稳妥度（N3b）：稳健化**判定用的就是用户原始时间窗**（排程才内缩留缓冲），
    # 所以这里也用原始时间窗 → 与稳健化的 achieved 口径一致
    eff_end_h = params.get("daily_end_h", 18.0)
    req_obj = PlanRequest(
        city=city,
        days=max(1, len(plan_days)),
        daily_start_h=params.get("daily_start_h", 9.0),
        daily_end_h=eff_end_h,
        spots=list(spot_by_name.values()) or [Spot(
            source_id=0, name=v.name,
            lat=hotel.lat if hotel else _fallback[0],
            lon=hotel.lon if hotel else _fallback[1], stay_min=60, score=8.0)
            for v in plan_days[0].spots],
        hotel=hotel,
    )
    cm = CommuteMatrix()
    sim = run_sim(plan_days, spot_by_name, req_obj, cm.minutes, runs=runs)
    return {"runs": sim.runs, "on_time_prob": sim.on_time_prob,
            "per_day": [vars(d) for d in sim.per_day],
            "risks": [vars(r) for r in sim.risks],
            "noise": sim.noise, "seed": sim.seed}


@app.post("/plan/review")
def review_task(body: dict) -> dict:
    """AI 总评：对整个行程给一次综合评价（分数 / 总评 / 亮点 / 提醒）。

    和"对话式问答"的区别：这是**主动**给出的整体判断，不需要用户提问。
    同步调用 LLM（约 0.7~3s）——FastAPI 会把同步 def 放进线程池，不阻塞事件循环。
    """
    import os
    from commute import load_env_file

    env = load_env_file()
    if not (env.get("LLM_API_KEY") or os.environ.get("LLM_API_KEY")):
        raise HTTPException(503, "未配置 LLM_API_KEY，无法生成总评")

    result, _params, _spots = _load_task_payload(body.get("task_id", ""))
    if not result or not result.get("days"):
        raise HTTPException(400, "该任务不是行程结果，无法评价")

    from editor import build_review_summary, review_plan

    memory = [str(m).strip() for m in (body.get("memory") or []) if str(m).strip()][:10]
    digest = build_review_summary(result)
    try:
        out = review_plan(digest, memory=memory)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    out["digest"] = digest        # 便于前端展示与排查"它是基于什么评的"
    return out


@app.post("/plan/edit")
async def edit_plan(body: dict) -> dict:
    """对话式修改：{"task_id": 基准任务, "instruction": "明天下午加个咖啡馆"}。

    返回新任务 id，进度与结果走同样的 WS /task 通道。
    需要 LLM Key 解析指令（503 = 未配置）。
    """
    import os
    base_task_id = body.get("task_id", "")
    instruction = (body.get("instruction") or "").strip()
    # 跨会话长期偏好：由前端持久化（localStorage）后随每次对话带来，
    # 只作为上下文注入 LLM，不改变操作语义
    memory = [str(m).strip() for m in (body.get("memory") or []) if str(m).strip()][:20]
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
    asyncio.create_task(run_edit_task(task.id, base_task_id, instruction, memory=memory))
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

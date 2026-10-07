"""FastAPI 服务入口。

启动：uvicorn main:app --reload --port 8000
文档：http://localhost:8000/docs

接口：
- GET  /health          健康检查
- GET  /meta            首页用：接口清单 + 服务状态（均取自运行时，非手写）
- GET  /                演示页（浏览器看实时进度与行程）
- GET  /cities          可演示城市列表（前端城市选择器数据源）
- GET  /demo/spots      按城市返回演示景点（零 Key 可跑）
- GET  /ask             内置语料检索问答（BM25 基线零 Key；with_answer=1 叠加 LLM 生成，默认关）
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
import re
import secrets
import sys
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import (Depends, FastAPI, HTTPException, Request, WebSocket,
                     WebSocketDisconnect)
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
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
from rag import ask as rag_ask
from extractor import extract_guide
from media_cache import (get_media, suspected_wrong_city,
                         update_media)
from models import PlanRequest, PlanResult
from solver import Solver, capacity_estimate
from weather import daily_weather
import leads
import llm_ledger
import support
from wechat_mp import router as wechat_router

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


# 响应压缩（gzip）—— 映射到公网后**带宽是第一瓶颈**，这是性价比最高的一处改动。
# 实测（2026-09-28）：
#   · 页面未压缩：`/` 87 KB、`/app` **223 KB**（单文件 SPA，JS/CSS 全内联）
#   · gzip 实测压缩比：/app 223 KB → 74 KB（3.08×）、/ 87 KB → 28 KB（3.15×）
#   · 穿隧道实测速率 139~185 KB/s ⇒ 打开规划页 1.64s → 压缩后约 0.53s
#   · 1 GB 月流量下可服务的完整会话数 约 3000 → **约 9700**
#
# ⚠️ 必须加在**内层**（也就是要写在下面 `@app.middleware("http")` 之前）—— 这点反直觉，实测踩过：
#   Starlette 里「最后添加的在最外层」，若加在装饰器之后它就变成最外层，于是它看到的是经过
#   BaseHTTPMiddleware 包装后的响应，而后者**会去掉 content-length** ⇒ gzip 无从判断大小，
#   `minimum_size` 静默失效（实测 127 字节的 /health 也被压）。放到内层后它直接面对路由响应，
#   能读到 content-length，阈值才真正生效（tests/test_api_endpoints.py 里有一条断言锁着这个行为）。
#
# minimum_size=1024：小于 1 KB 的响应压缩收益抵不上 CPU 开销（一次 /plan 的 JSON 只有 2.3 KB，
#   压不压差别很小）；真正的大头是上面那两张 HTML。
# ⚠️ 只对带 `Accept-Encoding: gzip` 的请求生效 —— 浏览器自动带，用 curl 验证要加 `--compressed`。
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """为每个请求注入 request_id、限流，并记录耗时——日志可按 ID 串起完整链路。"""
    rid = new_request_id()
    token = request_id_var.set(rid)
    t0 = time.time()

    path = request.url.path
    if is_costly_path(path):
        ip = client_ip(request)
        if rate_limit_hit(ip, t0):
            request_id_var.reset(token)
            log.warning("限流命中 ip=%s path=%s（>%d 次/分钟）转发头=%s",
                        ip, path, RATE_LIMIT_PER_MIN, forwarding_headers(request))
            return JSONResponse(
                {"detail": f"请求过于频繁：每分钟最多 {RATE_LIMIT_PER_MIN} 次，请稍后再试"},
                status_code=429, headers={"X-Request-Id": rid})

    if path == "/ask" and request.query_params.get("with_answer") in ("1", "true", "True"):
        if ask_gen_limit_hit(t0):
            request_id_var.reset(token)
            log.warning("答案生成配额命中 path=%s（>%d 次/分钟）", path, ASK_GEN_RATE_LIMIT_PER_MIN)
            return JSONResponse(
                {"detail": f"答案生成额度已满：每分钟最多 {ASK_GEN_RATE_LIMIT_PER_MIN} 次，"
                           "取消勾选「生成答案」可继续免费检索"},
                status_code=429, headers={"X-Request-Id": rid})

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

_TASK_ID_RE = re.compile(r"^[0-9a-f]{12}$")


# ---------- 访问令牌（公开部署闸门） ----------
#
# 为什么需要：全站原先是"演示定位 = 无鉴权"，但一旦映射到公网，收藏/历史是**全局共享**的，
# 而且任何人拿到链接都能触发 LLM 与高德调用 —— 等于把Key 交给陌生人烧。
#
# 配置方式：`backend/.env` 里写 `APP_TOKEN=<随机串>`（.env.example 有一键生成命令）。
# **未配置时放行**（本地开发 / CI 不受影响），但启动时会打醒目告警，/meta 也会回 `token_required=false`
# 让部署者一眼看出"还没上锁"。
APP_TOKEN: str = os.environ.get("APP_TOKEN", "").strip()

if not APP_TOKEN:
    log.warning("APP_TOKEN 未配置：接口**无鉴权**。本地开发无妨；"
                "映射到公网前请务必在 backend/.env 里设置（见 .env.example）")
else:
    log.info("APP_TOKEN 已启用：受保护接口需要 X-App-Token 头")


def verify_token(request: Request) -> None:
    """FastAPI 依赖：校验 `X-App-Token` 头（也接受 `Authorization: Bearer <token>`）。

    - 用 `secrets.compare_digest` 而不是 `==`：避免按字符短路带来的时序侧信道。
    - 未配置 APP_TOKEN 时直接放行（见上面说明）。
    """
    if not APP_TOKEN:
        return
    got = request.headers.get("X-App-Token", "").strip()
    if not got:
        auth = request.headers.get("Authorization", "")
        if auth[:7].lower() == "bearer ":
            got = auth[7:].strip()
    if not secrets.compare_digest(got, APP_TOKEN):
        raise HTTPException(401, "需要访问令牌：请在页面右上角填入，或带 X-App-Token 头")


def ws_token_ok(websocket: WebSocket) -> bool:
    """WebSocket 没法自定义 header（浏览器 WebSocket API 不支持），所以令牌走查询参数。

    失败时调用方要 `close(code=4401)` —— 前端据此提示重新输入令牌（见 index.html）。
    """
    if not APP_TOKEN:
        return True
    return secrets.compare_digest(websocket.query_params.get("token", ""), APP_TOKEN)


# ---------- 每 IP 限流（不引入依赖，内存计数） ----------
#
# 为什么要有：令牌挡得住陌生人，挡不住"拿到令牌的人手抖/脚本"把 Key 烧光。
# 只对**会触发 LLM / 高德调用**的路径计数，静态页面与 /health 不占额度。
def _int_env(name: str, default: int) -> int:
    """读整型环境变量；坏值退回默认（不让一个笔误把服务起不来）。"""
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        log.warning("%s 不是整数，使用默认值 %d", name, default)
        return default


# 可用环境变量覆盖（见 .env.example）。
#
# ⚠️ 默认值为什么是 120 而不是 30：实测（2026-09-28，花生壳免费 HTTPS 映射）
# **上游一个转发头都不发**（XFF / X-Real-IP / Forwarded 全无），所以 `client_ip()`
# 拿到的恒为 127.0.0.1 ⇒ 这个限流**不是"每 IP"，而是"全场共享一个桶"**。
# 30 次/分钟 全场共享的话，两三个朋友同时规划就会互相打到 429（而且提示很莫名）。
# 120 仍然能挡住"脚本跑飞烧 Key"（配合高德侧 3 QPS 节流），又不会误伤朋友。
# 换成会透传真实 IP 的隧道/反代后，`client_ip()` 会自动开始按人计数，届时可调回小值。
RATE_LIMIT_PER_MIN = _int_env("RATE_LIMIT_PER_MIN", 120)

# 只对**会花钱/触发外部调用**的路径计数。/ask 整体计入：with_answer=1 会烧 LLM 配额，
# 而限流按路径前缀无法区分查询参数，纯检索的少量误伤可接受（2026-10-04 workbuddy 风险登记后补入）。
# /support 同口径：/support/message 是公开写接口（落线索/会话），/support/wechat 是
# 公网回调（匿名写）——都是检索零成本但**必须挡刷**；429 对微信侧无害（5s 无回复它会重发）。
RATE_LIMIT_PREFIXES = ("/plan", "/extract", "/hotel/", "/poi/", "/weather", "/food", "/ask",
                       "/support")

# /ask 的 with_answer=1 烧 LLM 配额，且隧道不透传 IP（全场共享一桶）——按 IP 配额无意义，
# 所以生成走**独立的全局桶**（默认 10 次/分钟）。换隧道后 client_ip 按人生效，此桶仍作总闸。
ASK_GEN_RATE_LIMIT_PER_MIN = _int_env("ASK_GEN_RATE_LIMIT_PER_MIN", 10)
_ask_gen_hits: deque[float] = deque()

# 是否信任上游的 XFF / X-Real-IP（限流按真实访客分桶）。
# 默认 0：2026-10-04 实测花生壳是直通代理、伪造头原样到达，信任它 = 限流可被无限绕过。
# 换成可信反代（追加/覆盖式写入真实来源）时在 .env 设 TRUST_PROXY_HEADERS=1。
TRUST_PROXY_HEADERS = _int_env("TRUST_PROXY_HEADERS", 0)


def is_costly_path(path: str) -> bool:
    """是否是"花钱路径"。

    用 `path == p or path.startswith(p + "/")` 而不是裸 `startswith(p)`：
    裸前缀会把 **`/plans`（只读的历史列表）**也算成 `/plan`，白白吃掉额度。
    做减法很重要 —— 额度是全场共享的，省下的额度就是朋友能用的额度。
    """
    for p in RATE_LIMIT_PREFIXES:
        if p.endswith("/"):
            if path.startswith(p):
                return True
        elif path == p or path.startswith(p + "/"):
            return True
    return False
_rate_hits: dict[str, deque[float]] = defaultdict(deque)
_rate_lock = threading.Lock()


def client_ip(request: Request) -> str:
    """取限流身份。

    ⚠️ **2026-10-04 复测修正（推翻 09-28 的部分结论）**：真正的「信任 XFF」发生在
    **uvicorn 自带的 ProxyHeadersMiddleware**——它默认信任来自 127.0.0.1 的请求，把
    `request.client` 改写成 `X-Forwarded-For` 的值，发生在本函数之前。花生壳是直通
    代理（客户端伪造头原样到达，实测伪造 203.0.113.77 穿隧道后 ip=203.0.113.77），
    于是伪造头等于**无限换桶绕过限流**。09-28 旧结论「花生壳一个头都不发」只对
    「客户端没带头」的请求成立，当时没测伪造头。
    修复分两层：
    1. **uvicorn 层（真闸门）**：start-backend.bat 加 `--no-proxy-headers`，
       request.client 恢复为真实 TCP 对端（隧道下恒为 127.0.0.1，全场共享一桶）；
    2. **应用层（显式开关，纵深防御）**：`TRUST_PROXY_HEADERS=1`（.env）才允许读
       转发头。两层必须同时打开才按「真实访客」限流——只该在上游是可信反代
       （追加式 nginx / 覆盖式网关）时一起打开。
    打开后的取值规则（保留原有两道防线）：

    1. 只在"直连方是回环"时才读转发头。局域网机器伪造 XFF 没用，按自己的真实 IP 限流。
    2. **取最后一个值，不是第一个。** 标准反代是**追加**：`XFF = <客户端自带的> + ", "
       + <真实 peer>`，第一个元素恰是攻击者塞的；取最后对追加式/覆盖式代理都更安全。
    """
    peer = request.client.host if request.client else "-"
    if TRUST_PROXY_HEADERS and peer in ("127.0.0.1", "::1"):
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            last = xff.split(",")[-1].strip()
            if last:
                return last
        real = request.headers.get("x-real-ip", "").strip()
        if real:
            return real
    return peer


def forwarding_headers(request: Request) -> str:
    """把与"真实来源"相关的头拼成一行，供限流日志用。

    为什么值得单独记：限流一旦按错的身份计数（例如隧道不透传 IP，所有人都被算成一个），
    表现是"大家莫名其妙一起被限流"，而只看 `ip=127.0.0.1` 根本判断不出原因。
    把原始头记下来，一眼就能看出是"上游没发"还是"我们读错了"。
    """
    keys = ("x-forwarded-for", "x-real-ip", "forwarded", "x-forwarded-host")
    got = {k: request.headers[k] for k in keys if k in request.headers}
    return str(got) if got else "（上游未发任何转发头）"


def rate_limit_hit(ip: str, now: float) -> bool:
    """记一次命中；超过阈值返回 True（调用方回 429）。滑动窗口 60 秒。"""
    with _rate_lock:
        dq = _rate_hits[ip]
        while dq and now - dq[0] > 60.0:
            dq.popleft()
        if len(dq) >= RATE_LIMIT_PER_MIN:
            return True
        dq.append(now)
        # 顺手清理长期空闲的键，避免内存随 IP 数无限增长
        if len(_rate_hits) > 4096:
            for k in [k for k, v in _rate_hits.items() if not v][:1024]:
                _rate_hits.pop(k, None)
    return False


def ask_gen_limit_hit(now: float) -> bool:
    """答案生成的独立全局配额（滑动窗口 60 秒）；超限返回 True。

    为什么是全局桶而不是按 IP：花生壳不透传转发头（2026-09-28/10-04 两次实测），
    所有公网请求解析出来都是 127.0.0.1，按 IP 分桶等于没有分桶；生成答案烧 LLM 配额，
    必须有一个不管来源的总闸。换隧道后 client_ip 按人生效，此桶继续作全局总闸。
    """
    with _rate_lock:
        while _ask_gen_hits and now - _ask_gen_hits[0] > 60.0:
            _ask_gen_hits.popleft()
        if len(_ask_gen_hits) >= ASK_GEN_RATE_LIMIT_PER_MIN:
            return True
        _ask_gen_hits.append(now)
    return False


def _plan_snapshot_file(task_id: object) -> Path | None:
    """task_id → 规划快照文件路径；格式非法返回 None。

    task_id 由 MANAGER.create() 的 uuid4().hex[:12] 生成，格式是封闭集合。
    但它来自**无鉴权的请求参数**，直接拼路径的话 `../x` 就能指到 PLANS_DIR
    之外 —— 读走任意 .json 的 result（/task），删除接口还会往里写 deleted
    标记（Windows 下路径参数里 %5C 反斜杠同样能穿越）。所以所有拿 task_id
    摸磁盘的地方必须从这里取路径，不许自己拼。
    """
    if isinstance(task_id, str) and _TASK_ID_RE.fullmatch(task_id):
        return PLANS_DIR / f"{task_id}.json"
    return None


_favorites_lock = threading.Lock()


def _load_favorites() -> list[dict]:
    if FAV_FILE.exists():
        try:
            _loaded: list[dict] = json.loads(FAV_FILE.read_text(encoding="utf-8"))
            return _loaded
        except (json.JSONDecodeError, OSError) as e:
            # 损坏时按空处理并留痕：直接 500 会让「我的」页整体不可用。
            # 文件原样保留到下一次 add/remove 才被重建（丢数据但服务能恢复）
            log.warning("收藏文件解析失败，按空处理: %s: %s", type(e).__name__, e)
            return []
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


@app.post("/plan/capacity", dependencies=[Depends(verify_token)])
def plan_capacity(req: PlanRequest) -> dict:
    """规划前容量预估（2026-10-04，workbuddy 登记的「勾 14 只排 12 无预警」）：毫秒级粗估。

    不跑求解器、零网络零配额——纯算术 + NN 贪心装箱（solver.capacity_estimate）。
    估算有误差（真实通勤走高德、求解有全局优化），前端措辞用「可能排不下」。
    """
    if not req.spots:
        raise HTTPException(400, "景点列表为空")
    try:
        return capacity_estimate(req)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/plan", dependencies=[Depends(verify_token)], response_model=PlanResult)
def make_plan(req: PlanRequest) -> PlanResult:
    """排期主接口。有 AMAP_KEY 时通勤走高德真实数据（带缓存），否则估算降级。"""
    if not req.spots:
        raise HTTPException(400, "景点列表为空")

    cm = CommuteMatrix()
    day_plans, unplanned, total_cost, total_score = Solver(req, cm.minutes).solve()
    report = check_plan(req, day_plans, total_cost)
    report["stats"]["commute_api"] = cm.stats
    report["stats"]["cache_hit_rate"] = round(cm.hit_rate(), 3)

    # 只要还有一个已排景点的票价未知，总价就只是"已知部分之和" —— 必须如实告知
    cost_known = all(v.ticket_known for d in day_plans for v in d.spots)
    if not cost_known:
        log.warning("票价未知：%d 个景点的票价没拿到，total_cost 仅是已知部分之和",
                    sum(1 for d in day_plans for v in d.spots if not v.ticket_known))

    return PlanResult(
        city=req.city, days=day_plans, total_cost=total_cost,
        total_score=total_score, unplanned=unplanned, check_report=report,
        cost_known=cost_known,
    )


# 攻略文本上限：超长输入多半是误贴整篇文章（抽取质量会崩），也是无鉴权接口
# 防 LLM token 被烧的硬止损。文本现走 JSON body —— 旧版用 query 传，
# encodeURIComponent 后中文 ×9 字节，长攻略还没到这里的校验就先撞上网关请求行上限
EXTRACT_MAX_CHARS = 12000
EDIT_INSTRUCTION_MAX_CHARS = 2000


@app.post("/extract", dependencies=[Depends(verify_token)])
def extract(body: dict) -> dict:
    """攻略文本 → LLM 抽取（含城市识别）→ 实体对齐到高德 POI（坐标校正为真实值）。

    - body: `{"text": "攻略文本", "city": "前端已选城市（LLM 未识别时的兜底）"}`
    - 返回 `detected_city`（LLM 识别结果）与 `needs_city`（是否未能确定城市）——
      `needs_city=true` 时前端必须提示用户选择，**不要静默按默认城市排行程**
    - 对齐必须用**确定的城市**：高德搜索带 citylimit，用错城市会搜不到或搜到同名异地 POI
    - 低置信度的条目标记 needs_review，坐标保持占位值，由前端让用户点选
    """
    text = str(body.get("text") or "").strip()
    city = str(body.get("city") or "")
    if not text:
        raise HTTPException(400, "攻略文本为空")
    if len(text) > EXTRACT_MAX_CHARS:
        raise HTTPException(
            413, f"攻略文本过长（{len(text)} 字，上限 {EXTRACT_MAX_CHARS}），"
                 "请只贴与本次行程相关的段落")
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
    aligned: list[dict] = []
    review: list[dict] = []
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
    no-cache：页面会持续迭代（如 hero 动画），但 HTMLResponse 默认允许
    启发式缓存，用户强刷前可能一直看旧版 —— 明确要求每次回源校验。
    """
    f = STATIC_DIR / filename
    if f.exists():
        from fastapi.responses import HTMLResponse
        return HTMLResponse(f.read_text(encoding="utf-8"),
                            headers={"Cache-Control": "no-cache"})
    raise HTTPException(404, f"static/{filename} 不存在")


@app.get("/demo/spots")
def demo_spot_list(city: str = DEFAULT_CITY) -> dict:
    """按城市返回演示景点，富化高德实景图与介绍（spot_media.json，预抓取零 Key 可用）。

    未知城市返回空列表 + supported 提示，**不会静默换成别的城市的数据**。
    """
    city = normalize_city(city) or DEFAULT_CITY
    spots = []
    for s in demo_spots(city):
        d = s.model_dump()
        m = get_media(city, s.name) or {}
        d["image"] = m.get("image", "")
        d["intro"] = m.get("intro", "")
        spots.append(d)
    return {"city": city, "spots": spots,
            "available": bool(spots), "supported_cities": demo_cities(),
            "center": city_center(city)}


@app.get("/ask")
def ask_corpus(q: str, city: str | None = None, k: int = 5,
               with_answer: bool = False, tag: str | None = None) -> dict:
    """R1 检索基线 + R4 grounded 生成层 + R6 标签过滤（ROADMAP 2026-09-30 立项）。

    BM25 + 字符 bigram 检索（零 Key），每条结果带 source 引用 + R2 引用核查
    + R6 结构化字段（tags/ticket/stay_min）；tag= 允许标签过滤（免费/亲子/室内等）；
    with_answer=1 时基于检索片段生成 ≤100 字答案并做实体回链核查（默认关——
    公开白名单接口不自动烧 LLM 配额），LLM 失败自动降级为纯检索结果。
    """
    return rag_ask(q=q, city=city, k=k, with_answer=with_answer, tag=tag)


# ---------- 客服会话（国内化一期：RAG 客服 + 转人工 + 线索） ----------
# 与 /ask 的关系：/ask 是检索问答页（白名单、只读），/support/message 是客服
# 动作（公开但**有写**：线索与会话落盘）。三档决策在 support.py，这里只做
# 接口层校验与生成配额闸门。
SUPPORT_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# 站内客服的生成层总开关（渠道侧公众号永远不生成——被动回复 5s 时限）。
# 默认 0 = 零 LLM 成本；置 1 且请求显式带 generate=true 才可能生成。
SUPPORT_GENERATE = _int_env("SUPPORT_GENERATE", 0)


@app.get("/support")
def support_page():
    """客服会话页（国内化一期前端件，workbuddy）。"""
    return _serve_html("support.html")


@app.post("/support/message")
def support_message(body: dict) -> dict:
    """客服一问一答：检索 → 三档决策（命中/部分答案/转人工）→ 线索联动。

    body: `{"session_id": 前端生成, "text": 用户消息, "generate": 可选 true}`
    generate=true 时尝试 grounded 生成（复用 /ask 生成配额总闸，超限自动
    降级为纯检索组句——客服不该把 429 抛到用户脸上）。默认零 LLM 成本。
    """
    sid = str(body.get("session_id") or "").strip()
    if not SUPPORT_SESSION_RE.fullmatch(sid):
        raise HTTPException(400, "session_id 格式非法（字母数字下划线短横线，≤64 字）")
    allow_gen = False
    if body.get("generate") and SUPPORT_GENERATE:
        if ask_gen_limit_hit(time.time()):
            log.info("客服生成额度已满，本条降级为纯检索组句")
        else:
            allow_gen = True
    try:
        return support.answer(sid, str(body.get("text") or ""),
                              channel="web", allow_generate=allow_gen)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/support/leads", dependencies=[Depends(verify_token)])
def support_leads(status: str | None = None, limit: int = 100) -> dict:
    """线索清单（管理端）：按更新时间倒序；handoff 态是运营唯一必看的待跟进。"""
    try:
        rows = leads.list_leads(status=status, limit=limit)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"leads": rows,
            "handoff_count": sum(1 for r in rows if r.get("status") == "handoff")}


@app.post("/support/leads/status", dependencies=[Depends(verify_token)])
def support_lead_status(body: dict) -> dict:
    """人工跟进闭环：改线索状态（open / handoff / closed）。"""
    try:
        lead = leads.set_status(str(body.get("lead_id") or ""),
                                str(body.get("status") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if lead is None:
        raise HTTPException(404, "线索不存在")
    return {"lead": lead}


@app.get("/support/conversation/{session_id}", dependencies=[Depends(verify_token)])
def support_conversation(session_id: str) -> dict:
    """会话留痕查询（管理端）：每轮的决策档位都在——审计「当时为什么这么答」。"""
    try:
        msgs = support.load_conversation(session_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"session_id": session_id, "messages": msgs}


@app.get("/admin/usage", dependencies=[Depends(verify_token)])
def admin_usage(days: int = 7) -> dict:
    """LLM 成本台账：按「用途 × 模型」聚合最近 N 天的调用量与 token 数。"""
    return llm_ledger.summary(days=days)


app.include_router(wechat_router)


@app.get("/cities")
def list_cities() -> dict:
    """可演示城市列表（前端城市选择器数据源）。"""
    return {"cities": demo_cities(), "default": DEFAULT_CITY,
            "centers": {c: city_center(c) for c in demo_cities()}}


@app.get("/weather", dependencies=[Depends(verify_token)])
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

    ⚠️ FastAPI 0.141 起 `include_router` 的路由在 app.routes 里被包成
    `_IncludedRouter`（自身无 methods、path=None）——不展开它，/meta 与首页清单
    就会悄悄漏掉 router 里的接口（实测漏过 GET/POST /support/wechat 两个）。
    真实路由在其 `original_router.routes` 里，这里递归展开（2026-10-07 修复）。
    """
    endpoints, skipped = [], []

    def _collect(route, *, via_router: bool = False):
        path = getattr(route, "path", "")
        methods = sorted(getattr(route, "methods", None) or [])
        if not methods:
            nested = getattr(route, "original_router", None)
            if nested is not None and not via_router:      # 只展开一层，防意外环
                for sub in getattr(nested, "routes", []) or []:
                    _collect(sub, via_router=True)
                skipped.append({"path": path or "（router 容器）",
                                "reason": "router 容器（子路由已展开计入）"})
            else:
                skipped.append({"path": path, "reason": "无 HTTP 方法（静态挂载或 WebSocket）"})
        elif not isinstance(route, APIRoute):
            skipped.append({"path": path, "reason": "框架自带路由（非本项目接口）"})
        else:
            doc = (route.endpoint.__doc__ or "").strip().splitlines()
            # 摘要优先取 docstring 首行；没写 docstring 就退回函数名，至少有个标识
            endpoints.append({"methods": methods, "path": path,
                              "summary": doc[0].strip() if doc else route.name})

    for r in app.routes:
        _collect(r)
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
        # 部署闸门状态：一眼看出"还没上锁"。只回布尔，不回令牌本身。
        "token_required": bool(APP_TOKEN),
        "rate_limit_per_min": RATE_LIMIT_PER_MIN,
        "cities": len(CITY_CENTERS),
        "spots": {"total": sum(len(demo_spots(c)) for c in city_list),
                  "by_city": {c: len(demo_spots(c)) for c in city_list}},
        "python": sys.version.split()[0],
        "version": app.version,
    })
    return {**_api_catalog(), "status": status}


# ---------- 用户页：历史规划 + 收藏 ----------

@app.get("/plans", dependencies=[Depends(verify_token)])
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
                    # 票价未知 ≠ 免费：历史列表同样要带上口径，前端 moneyTxt
                    # 才能把「票价待查」如实显示出来（缺这字段会被当成已知）
                    "cost_known": r.get("cost_known"),
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


@app.get("/poi/detail", dependencies=[Depends(verify_token)])
def poi_detail(name: str, city: str = "") -> dict:
    """快接口：图片组/介绍/营业时间/地址，毫秒级返回。

    评价摘要不走这里（首次生成需 ~3s），前端拿到基础数据立即渲染后
    再调 /poi/reviews 异步补上——避免点开详情要等好几秒。

    **city 必须传**：高德搜索带 citylimit，用错城市搜不到（返回 404）。
    未命中缓存时按 city 抓取，抓到后写入 spot_media.json 供后续零成本命中。
    """
    city = normalize_city(city) or DEFAULT_CITY
    m = get_media(city, name)
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
        # 统一走 update_media（锁 + 共享字典 + 合并写）：自己 load→改→save 会与
        # 预取线程互相整份覆盖（预取刚写的评价会被这里抹掉）
        update_media(city, name, m)
    out = {k: m.get(k, "") for k in
           ("image", "intro", "photos", "opentime", "address", "lat", "lon")}
    out["reviews"] = m.get("reviews")
    out["reviews_ai"] = m.get("reviews_ai", False)
    return out


@app.get("/poi/reviews", dependencies=[Depends(verify_token)])
def poi_reviews(name: str, city: str = "") -> dict:
    """评价摘要（AI 生成，首次约 3s，之后走缓存）。前端二次拉取用。

    city 用于定位缓存条目：缓存 key 是「城市|景点名」，同名景点跨城市不能混用。
    """
    city = normalize_city(city) or DEFAULT_CITY
    m = get_media(city, name)
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
        update_media(city, name, m)
    return {"reviews": m.get("reviews"), "reviews_ai": m.get("reviews_ai", False)}


def _lodging_intro(type_str: str) -> str:
    """卡片副标题取"住宿相关"的那段类别。

    高德同一个 POI 会给多段类别，用 `;` 与 `|` 分隔，例如
    `餐饮服务;中餐厅;中餐厅|住宿服务;宾馆酒店;宾馆酒店`。
    直接 `split(";")[0]` 会把一家酒店描述成「餐饮服务」（用户会以为搜错了）。
    """
    for seg in re.split(r"[;|]", type_str or ""):
        if any(k in seg for k in ("住宿", "宾馆", "酒店")):
            return seg
    return (type_str or "").split(";")[0]


def _hotel_card(c: dict) -> dict:
    """酒店卡片数据：基础信息 + 能拿到的富信息（携程式卡片用）。

    ⚠️ price 大多为空：高德免费接口没有房价（biz_ext.lowest_price/cost 基本是空的），
    我们也没有 OTA 数据源 —— 拿不到就留空，由前端决定不显示，**绝不编造价格**。
    """
    return {
        "name": c.get("name", ""), "lat": c.get("lat"), "lon": c.get("lon"),
        "intro": _lodging_intro(c.get("type_str", "")),
        "image": c.get("image", ""),
        "rating": c.get("rating", ""),        # 高德评分（extensions=all 才有）
        "price": c.get("price", ""),          # 房价：拿不到就是空
        "keytag": c.get("keytag", ""),        # 经济型/舒适型/高档型/豪华型 ≈ 携程钻级
        "grade": c.get("grade", ""),          # 归一化档位（经济型/舒适型/高档型/豪华型/民宿）
        "adname": c.get("adname", ""),        # 所在区（碑林区）≈ 携程"外滩核心区"
        "address": c.get("address", ""),
        "tel": c.get("tel", ""),
        "photos": c.get("photos", []),        # 多图画廊要用（高德最多给 3 张）
    }


@app.post("/hotel/search", dependencies=[Depends(verify_token)])
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
    # 酒店必须是「住宿服务」类别：不加过滤时搜「钟楼」会返回风景名胜 / 地铁站，
    # 界面上看着像酒店，用户一点就把景点当成住宿选走了。
    HOTEL_TYPES = "100000"          # 高德 POI 分类：住宿服务
    # extensions=all 才能拿到 biz_ext.rating（评分）与多张图
    # 分页取满 25 条/页（高德单页上限）：西安这类城市实测有 600+ 家，
    # 一次只给 12 家会让用户以为"就这么多"（用户原话）。
    try:                                   # 客户端可能传来非数字 page（曾把事件对象发上来）
        page = max(1, int(body.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    cands = text_search(query, city, types=HOTEL_TYPES, extensions="all",
                        offset=25, page=page)
    if not cands:
        # 关键词多半是地标/景点：先定位它，再搜它附近的住宿（携程的做法）
        for m in (text_search(query, city, extensions="all",
                              offset=25, page=page) or [])[:1]:
            if m.get("lat") is not None:
                cands = poi_search("", m["lat"], m["lon"], radius=3000,
                                   types=HOTEL_TYPES, extensions="all",
                                   offset=25, page=page) or []
    if not cands:                   # 最后才退回原来的不限制类别，保证有结果
        cands = (text_search(query, city, extensions="all", offset=25, page=page)
                 or poi_search(query, center[0], center[1], radius=10000,
                               extensions="all", offset=25, page=page))
    return {"results": [_hotel_card(c) for c in cands], "city": city, "page": page}


@app.post("/hotel/recommend", dependencies=[Depends(verify_token)])
def hotel_recommend(body: dict) -> dict:
    """打开酒店选择器时的**默认推荐**：不需要关键词，按给定中心返回附近住宿。

    携程的模式是"进来就有一屏推荐，再让用户筛"，而不是"逼用户先搜"。
    lat/lon 由前端给（有行程时是行程景点质心，否则不传、后端退回城市中心）。
    """
    from editor import poi_search
    city = normalize_city(body.get("city")) or DEFAULT_CITY
    lat, lon = body.get("lat"), body.get("lon")
    center = ((float(lat), float(lon))
              if (lat is not None and lon is not None)
              else (city_center(city) or city_center(DEFAULT_CITY)))
    if center is None:
        raise HTTPException(500, f"城市表缺少 {DEFAULT_CITY}，请检查 backend/cities.py")
    HOTEL_TYPES = "100000"          # 住宿服务
    # 只给 types、不给 keywords = "附近这类 POI"，正是推荐想要的效果；
    # extensions=all 拿到评分与多图。
    try:                                   # 客户端可能传来非数字 page（曾把事件对象发上来）
        page = max(1, int(body.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    cands = poi_search("", center[0], center[1], radius=5000, types=HOTEL_TYPES,
                       extensions="all", offset=25, page=page) or []
    if not cands:                   # 兜底：附近实在没有住宿大类
        cands = poi_search("酒店", center[0], center[1], radius=5000,
                           types=HOTEL_TYPES, extensions="all",
                           offset=25, page=page) or []
    return {"results": [_hotel_card(c) for c in cands], "city": city, "page": page}


@app.post("/hotel/set", dependencies=[Depends(verify_token)])
async def hotel_set(body: dict) -> dict:
    """界面选择酒店 → 设住宿锚点 → 异步重排（返回新 task_id）。"""
    from tasks import MANAGER, run_set_hotel
    base_task_id = body.get("task_id", "")
    hotel = {"name": body.get("name"), "lat": body.get("lat"), "lon": body.get("lon")}
    if not base_task_id or not hotel["name"]:
        raise HTTPException(400, "需要 task_id 和酒店信息")
    base = MANAGER.get(base_task_id)
    if base is None or base.status != "completed":
        raise HTTPException(404, "基准任务不存在或未完成")
    task = MANAGER.create()
    MANAGER.spawn(task.id, run_set_hotel(task.id, base_task_id, hotel))
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


@app.delete("/plans/{task_id}", dependencies=[Depends(verify_token)])
def delete_plan(task_id: str) -> dict:
    """删除单个历史规划（软删除，标记而非抹掉，可恢复）。"""
    f = _plan_snapshot_file(task_id)
    if f is None or not f.exists():
        raise HTTPException(404, "该规划不存在")
    _soft_delete_plan(f)
    from tasks import MANAGER
    MANAGER._tasks.pop(task_id, None)
    return {"deleted": task_id}


@app.post("/plans/delete", dependencies=[Depends(verify_token)])
def delete_plans_batch(body: dict) -> dict:
    """批量删除（我的页管理模式）。"""
    ids = body.get("task_ids") or []
    if not ids:
        raise HTTPException(400, "task_ids 为空")
    from tasks import MANAGER
    deleted = []
    for tid in ids:
        f = _plan_snapshot_file(tid)
        if f is not None and f.exists() and _soft_delete_plan(f):
            deleted.append(tid)
        MANAGER._tasks.pop(str(tid), None)
    return {"deleted": deleted, "count": len(deleted)}


@app.get("/favorites", dependencies=[Depends(verify_token)])
def get_favorites() -> dict:
    """收藏的景点列表（按收藏时间倒序）。"""
    return {"favorites": _load_favorites()}


@app.post("/favorites", dependencies=[Depends(verify_token)])
def mod_favorites(body: dict) -> dict:
    """新增或取消收藏（body.action = add / remove）。"""
    action = body.get("action")
    spot = body.get("spot") or {}
    if not spot.get("name"):
        raise HTTPException(400, "需要 spot.name")
    # 读-改-写全程持锁：并发 add/remove 各持一份快照会互相覆盖（丢收藏）
    with _favorites_lock:
        favs = _load_favorites()
        if action == "add":
            # get 而非 [] 取 name：历史脏数据缺 name 字段时不能 KeyError → 500
            if not any(f.get("name") == spot["name"] for f in favs):
                favs.insert(0, {"name": spot["name"], "lat": spot.get("lat"),
                                "lon": spot.get("lon"), "image": spot.get("image", ""),
                                "desc": spot.get("desc", ""), "intro": spot.get("intro", ""),
                                "city": spot.get("city", "")})
        elif action == "remove":
            favs = [f for f in favs if f.get("name") != spot["name"]]
        else:
            raise HTTPException(400, "action 需要 add 或 remove")
        FAV_FILE.parent.mkdir(parents=True, exist_ok=True)
        # 原子写（临时文件 + replace）：读方（GET /favorites）不会读到半截 JSON
        tmp = FAV_FILE.with_name(FAV_FILE.name + ".tmp")
        tmp.write_text(json.dumps(favs, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, FAV_FILE)
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


@app.post("/plan/async", dependencies=[Depends(verify_token)])
async def create_async_plan(req: PlanRequest) -> dict:
    """异步排期：立即返回 task_id，后台跑完整流水线（不再被网关 504）。"""
    if not req.spots:
        raise HTTPException(400, "景点列表为空")
    from tasks import MANAGER, run_plan_task
    task = MANAGER.create()
    MANAGER.spawn(task.id, run_plan_task(task.id, req))
    return {"task_id": task.id, "ws_url": f"/ws/{task.id}",
            "poll_url": f"/task/{task.id}"}


@app.get("/task/{task_id}", dependencies=[Depends(verify_token)])
def get_task(task_id: str) -> dict:
    """轮询接口；历史任务（内存已清）从磁盘快照兜底。"""
    from tasks import MANAGER
    task = MANAGER.get(task_id)
    if task is not None:
        return task.snapshot()
    f = _plan_snapshot_file(task_id)
    if f is not None and f.exists():
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
    f = _plan_snapshot_file(task_id)
    if f is None or not f.exists():
        raise HTTPException(404, "任务不存在或尚未完成")
    snap = json.loads(f.read_text(encoding="utf-8"))
    if snap.get("deleted"):
        raise HTTPException(404, "任务已删除")
    # 快照里的键名是 params（不是 req_params）
    spot_by_name = {s["name"]: Spot(**s) for s in (snap.get("request_spots") or [])}
    if not spot_by_name:
        log.warning("快照缺少 request_spots（旧版本数据），将退化为无通勤口径 task_id=%s", task_id)
    return snap.get("result"), (snap.get("params") or {}), spot_by_name


@app.post("/plan/simulate", dependencies=[Depends(verify_token)])
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
    if _fallback is None:
        raise HTTPException(500, f"城市表缺少 {DEFAULT_CITY}，请检查 backend/cities.py")
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


@app.post("/plan/review", dependencies=[Depends(verify_token)])
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


@app.post("/plan/recheck", dependencies=[Depends(verify_token)])
def recheck_plan(body: dict) -> dict:
    """结果侧确定性编辑：前端提交「每天放哪些名字（可覆盖 stay_min）」，
    通勤/时间线/成本/校验**全部服务端重算**（见 tasks.recheck_task 的信任边界）。

    语义：装不下时间窗的景点剔除进 unplanned（与主排期一致），不 500；
    任务不在内存/未完成 → 404；成员非法 → 400（带出错名字）。
    """
    task_id = str(body.get("task_id") or "")
    days = body.get("days")
    if not task_id or not isinstance(days, list) or not days:
        raise HTTPException(400, "需要 task_id 与 days 数组")
    from tasks import recheck_task
    try:
        snap = recheck_task(task_id, days)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if snap is None:
        raise HTTPException(404, "任务不存在或未完成（编辑要求任务在内存中；"
                                "可从「我的」载入历史后重新规划）")
    return snap


# ---------- 美食情报卡（按城市持续追加的数据资产） ----------
FOOD_DIR = DATA_DIR / "food"
_food_lock = threading.Lock()
# 城市名做文件名：字符白名单（normalize_city 不设防，这里必须自己挡住路径穿越）
_FOOD_CITY_RE = re.compile(r"^[一-龥A-Za-z0-9· ]{1,24}$")
_FOOD_MAX = 200          # 单城市条数上限（防一个文件无限膨胀）
_FOOD_NAME_MAX = 40      # 店名
_FOOD_NOTE_MAX = 140     # 一句话备注


def _food_file(city: object) -> Path | None:
    c = str(city or "").strip()
    if not _FOOD_CITY_RE.fullmatch(c):
        return None
    return FOOD_DIR / f"{c}.json"


def _food_read(f: Path) -> list[dict]:
    """读 entries 列表（兼容旧 list 文件与新 {entries, recommended} 结构）。"""
    entries = _food_doc(f).get("entries")
    return entries if isinstance(entries, list) else []


def _food_doc(f: Path) -> dict:
    """读整份美食文件；旧版是裸 list（只有手动记录），包成 dict 结构。"""
    if not f.exists():
        return {"entries": [], "recommended": None}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {"entries": data, "recommended": None}
        if isinstance(data, dict):
            data.setdefault("entries", [])
            data.setdefault("recommended", None)
            return data
        return {"entries": [], "recommended": None}
    except (json.JSONDecodeError, OSError) as e:
        log.warning("美食卡读取失败 %s: %s: %s", f.name, type(e).__name__, e)
        return {"entries": [], "recommended": None}


def _food_write(f: Path, doc: dict) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, f)


@app.get("/food", dependencies=[Depends(verify_token)])
def food_list(city: str) -> dict:
    """按城市列出美食情报卡（用户数据，与 /favorites 同口径需令牌）。
    未知/非法城市返回空列表，不是 500。"""
    f = _food_file(city)
    return {"city": city, "entries": [] if f is None else _food_read(f)}


@app.post("/food", dependencies=[Depends(verify_token)])
def food_add(body: dict) -> dict:
    """记一条美食（按城市持续追加）。同名去重更新（note 以新盖旧），上限 200 条。"""
    name = str(body.get("name") or "").strip()
    note = str(body.get("note") or "").strip()
    if not name or len(name) > _FOOD_NAME_MAX:
        raise HTTPException(400, f"name 必填且不超过 {_FOOD_NAME_MAX} 字")
    if len(note) > _FOOD_NOTE_MAX:
        raise HTTPException(400, f"note 不超过 {_FOOD_NOTE_MAX} 字")
    f = _food_file(body.get("city"))
    if f is None:
        raise HTTPException(400, "city 不合法")
    with _food_lock:
        doc = _food_doc(f)
        rows = [e for e in doc["entries"] if str(e.get("name")) != name]
        rows.append({"name": name, "note": note, "ts": int(time.time())})
        if len(rows) > _FOOD_MAX:
            rows = rows[-_FOOD_MAX:]
        doc["entries"] = rows
        _food_write(f, doc)
    return {"entries": rows}


@app.post("/food/remove", dependencies=[Depends(verify_token)])
def food_remove(body: dict) -> dict:
    """删一条美食（同风格于 /plans/delete：POST + body）。"""
    name = str(body.get("name") or "")
    f = _food_file(body.get("city"))
    if f is None:
        raise HTTPException(400, "city 不合法")
    with _food_lock:
        doc = _food_doc(f)
        doc["entries"] = [e for e in doc["entries"] if str(e.get("name")) != name]
        if f.exists():
            _food_write(f, doc)
    return {"entries": doc["entries"]}


def _is_food_poi(type_str: object) -> bool:
    """高德 POI 是否真是吃的：主判据 = 标准大类「餐饮服务」；
    type 缺失时用菜系关键词兜底。硬闸剔除书店/停车场/购物/交通
    （用户实测混入过非餐饮——推荐卡片只该有吃的）。"""
    t = str(type_str or "")
    if "餐饮服务" in t:
        return True
    if not t:
        return False
    bad = ("购物", "交通", "停车", "书店", "超市", "银行", "药店", "邮局",
           "风景名胜", "住宿", "医疗", "汽车")
    if any(b in t for b in bad):
        return False
    good = ("小吃", "美食", "餐厅", "面馆", "粥", "糕", "火锅", "烧烤",
            "快餐", "咖啡", "茶馆", "饮品", "甜品")
    return any(g in t for g in good)


@app.get("/food/recommend", dependencies=[Depends(verify_token)])
def food_recommend(city: str) -> dict:
    """按城市推荐美食（用户定案：**只出美食本体，不出饭店**）。

    展示主体 = 内置特色种子（food_seeds.py，25 城公开常识菜名+一句介绍）；
    高德只当**图源**：按菜名搜代表店的实景图（严格餐饮过滤，搜不到用首字占位，
    绝不编图、绝不把搜到的饭店本身放进列表）。
    结果缓存进该城市美食文件 recommended（TTL 7 天），重复调用零配额。
    """
    from food_seeds import seed_food

    f = _food_file(city)
    if f is None:
        raise HTTPException(400, "city 不合法")
    with _food_lock:
        doc = _food_doc(f)
    rec = doc.get("recommended")
    ttl = 7 * 86400
    if rec and isinstance(rec.get("ts"), (int, float))             and time.time() - rec["ts"] < ttl and isinstance(rec.get("items"), list)             and rec["items"]:
        return {"city": city, "items": rec["items"], "cached": True}

    from editor import text_search
    c = normalize_city(city) or DEFAULT_CITY
    reason = ""
    seeds = seed_food(c)
    if not seeds:
        return {"city": city, "items": [], "cached": False,
                "reason": "该城市还没有内置特色数据"}
    items = []
    for sd in seeds:
        entry = {"name": sd["name"], "intro": sd["intro"],
                 "source": "内置特色", "image": "", "rating": "",
                 "address": "", "price": ""}
        try:
            # 给这道菜配一张代表店实景图（同名店搜图，非饭店推荐）
            hits = text_search(sd["name"], c, extensions="all", offset=3, page=1)
            h = next((x for x in hits
                      if x.get("name") and _is_food_poi(x.get("type_str"))
                      and not any(b in (x.get("name") or "")
                                  for b in ("停车场", "书店", "超市", "银行",
                                            "加油站", "服务区", "地铁"))), None)
            if h:
                photos = h.get("photos") or []
                entry["image"] = h.get("image") or (photos[0] if photos else "")
                entry["ref"] = h.get("name", "")   # 图源参考店（仅详情展示用）
        except Exception:
            pass    # 图源降级：搜失败就用首字占位，介绍仍在（不影响菜品展示）
        items.append(entry)

    with _food_lock:
        doc = _food_doc(f)
        doc["recommended"] = {"ts": int(time.time()), "items": items}
        try:
            _food_write(f, doc)
        except OSError as e:
            log.warning("美食推荐缓存写失败 %s: %s", f.name, e)
    return {"city": city, "items": items, "cached": False, "reason": reason}


@app.get("/poi/search", dependencies=[Depends(verify_token)])
def poi_search(q: str, city: str = "", limit: int = 8) -> dict:
    """景点关键词搜索（Step2 挑选用）：复用高德文本搜索，返回可直接入列的最小字段。

    会打高德配额 ⇒ 需令牌 + /poi/ 前缀限流；空 query 400，limit 收进 1..20。
    """
    q = (q or "").strip()
    if not q:
        raise HTTPException(400, "q 为空")
    n = max(1, min(20, int(limit) if str(limit).lstrip("-").isdigit() else 8))
    from editor import text_search
    rows = text_search(q, normalize_city(city) or DEFAULT_CITY,
                       offset=n, page=1)
    return {"results": [
        {k: r.get(k) for k in ("name", "lat", "lon", "type_str", "rating")}
        for r in rows[:n]]}


@app.post("/plan/edit", dependencies=[Depends(verify_token)])
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
    if len(instruction) > EDIT_INSTRUCTION_MAX_CHARS:
        # 同 /extract：无鉴权接口的输入必须有上限，防 LLM token 被烧
        raise HTTPException(400, f"指令过长（上限 {EDIT_INSTRUCTION_MAX_CHARS} 字）")
    from commute import load_env_file
    env = load_env_file()
    if not (env.get("LLM_API_KEY") or os.environ.get("LLM_API_KEY")):
        raise HTTPException(503, "未配置 LLM_API_KEY，无法解析编辑指令")
    from tasks import MANAGER, run_edit_task
    base = MANAGER.get(base_task_id)
    if base is None or base.status != "completed":
        raise HTTPException(404, "基准任务不存在或未完成")
    task = MANAGER.create()
    MANAGER.spawn(task.id, run_edit_task(task.id, base_task_id, instruction, memory=memory))
    return {"task_id": task.id, "ws_url": f"/ws/{task.id}",
            "poll_url": f"/task/{task.id}"}


@app.websocket("/ws/{task_id}")
async def ws_task(websocket: WebSocket, task_id: str) -> None:
    """WebSocket 实时推送：进度更新时发增量，任务结束时发终态。"""
    from tasks import MANAGER
    if not ws_token_ok(websocket):
        # ⚠️ 必须先 accept 再 close：在 accept 之前 close，Starlette 会直接以
        # HTTP 403 拒绝握手，浏览器拿不到 4401 这个业务码，前端就没法区分
        # "令牌不对" 和 "网络断了"（两者提示完全不同）。
        await websocket.accept()
        await websocket.close(code=4401)
        log.warning("WS 拒绝：令牌缺失或不匹配 task=%s", task_id)
        return
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

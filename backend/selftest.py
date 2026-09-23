"""Key 可用性自检：让「服务状态」说真话，而不是只看环境变量非空。

为什么必须单独一个模块：editor.py 已经 `from commute import ...`，若把自检塞进
commute.py、又要复用 editor._llm() 拿客户端，就形成 commute ↔ editor 循环依赖，
import 期直接炸。所以依赖方向严格单向：main.py → selftest.py → {commute, cities, editor}。

为什么探测口径必须复用 editor._llm()：那是「逐变量回落」的唯一实现
（LLM_FAST_* 缺哪项就回落主配置哪项）。若自检自己重新拼一遍 base_url / key，
探的口径和用的口径迟早漂移 —— 探绿了、用的时候照样挂。

四态（与旧版「configured / missing」两态的差异）：
- missing      纯本地判定，零外呼
- ok           探测通过
- invalid      上游明确拒绝（401/403、业务错误码、429 额度、快通道配置矛盾）
- unreachable  超时 / 网络不通 —— **必须与 invalid 分开**：两者的修复动作完全相反
               （前者查网络与代理，后者换 Key）。把网络不通显示成「Key 无效」，
               会让人白白重置一个没坏的 Key。

探测在服务启动后的后台线程里跑一次，之后按 PROBE_TTL_SEC 懒刷新；
/health 与 /meta 一律只读内存快照，绝不在请求路径上发外部请求 ——
所以状态接口依然没有超时/重试风险（main.py 的注释与这条纪律绑定）。

零副作用：不复用 CommuteMatrix / POIAligner，因为它们失败会累计降级计数、
成功会写业务缓存文件；自检只读、只改自己这份内存快照。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

log = logging.getLogger(__name__)

# 缓存 6 小时：改 .env 反正要重启进程（main.py 只在 import 期灌一次环境），
# 进程内缓存自然清零，所以 TTL 只需对付「长跑进程里 Key 被上游停用」这一种情况。
# 不用定时线程 —— 要管生命周期、多 worker 下 N 倍调用、无 Key 时空转；
# 懒刷新的触发面天然被「真的有人看首页」限制住。
PROBE_TTL_SEC = 6 * 3600
AMAP_PROBE_TIMEOUT_S = 5.0
LLM_PROBE_TIMEOUT_S = 10.0
SKIP_ENV = "SELFTEST_SKIP"

# 四态。inherited 只给快通道用：未单独配置、回落主通道 —— 那是**可用**，不是 missing。
OK, INVALID, UNREACHABLE, MISSING = "ok", "invalid", "unreachable", "missing"
INHERITED = "inherited"

# 探测结果。state: idle(没配任何 Key，根本没探) / probing / ready
_snapshot: dict[str, Any] = {
    "state": "idle", "at": 0.0, "stale": False,
    "llm": MISSING, "llm_fast": MISSING, "amap_key": MISSING,
    "reasons": {"llm": "", "llm_fast": "", "amap_key": ""},
}
_state_lock = threading.Lock()
_refreshing = False

# 测试接缝：不要直接 patch selftest.time.time —— 那是全局 time 模块，会污染整个进程的时钟。


def _now() -> float:
    return time.time()


def _spawn(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


def _env(name: str) -> str:
    """读一个配置项。.env 与进程环境都看 —— 与原 _key_state 的口径一致。"""
    from commute import load_env_file
    return (load_env_file().get(name) or os.environ.get(name) or "").strip()


# ---------- 高德 ----------

def _amap_probe_once(key: str) -> tuple[str, str]:
    """打一次 /v3/distance。返回 (state, reason)。

    选这个接口而不是 place/text 或地理编码：commute.py 运行时真正依赖的就是它 ——
    一次调用同时验证「Key 有效」+「距离测量接口有权限」，探的口径就是用的口径。
    起终点取城市中心表里**两个不同的城市**（不写死城市名与坐标，成本维度会拦）。
    """
    from cities import CITY_CENTERS
    centers = list(CITY_CENTERS.values())
    a, b = centers[0], centers[1]
    params = urllib.parse.urlencode({
        # 高德参数顺序是 经度,纬度（与 commute.py 同一写法）
        "origins": f"{a[1]},{a[0]}",
        "destination": f"{b[1]},{b[0]}",
        "type": 1,
        "key": key,
    })
    url = f"https://restapi.amap.com/v3/distance?{params}"
    try:
        with urllib.request.urlopen(url, timeout=AMAP_PROBE_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return INVALID, "unauthorized"
        log.warning("高德自检收到 HTTP %s", e.code)
        return INVALID, "unknown"
    except (urllib.error.URLError, TimeoutError, OSError):
        # 网络不通是**预期**内的探测结论之一（unreachable），不是异常路径：
        # 它和「Key 无效」必须分开上报，所以这里不打 warning 也不重试
        return UNREACHABLE, "network"

    if str(payload.get("status")) == "1":
        return OK, ""
    # 业务性拒绝：请求到了高德、被它拒了。infocode 的语义随版本变，不在这里硬编码
    # 映射表（写错会把「Key 类型不对」误判成「额度用完」），原始码只进日志，
    # 响应里给固定的机器可读枚举，让人去看日志定位。
    log.warning("高德自检被拒绝: infocode=%s info=%s",
                payload.get("infocode"), payload.get("info"))
    return INVALID, "config_error"


# ---------- LLM ----------

def _classify_llm_error(exc: Exception) -> tuple[str, str]:
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return INVALID, "unauthorized"
    if status == 429:
        return INVALID, "quota"
    if status == 404:
        return INVALID, "model_not_found"
    name = type(exc).__name__
    if name in ("APITimeoutError", "APIConnectionError"):
        return UNREACHABLE, "network"
    if name == "ImportError":
        return INVALID, "dependency_missing"
    log.warning("LLM 自检遇到未归类异常: %s: %s", type(exc).__name__, exc)
    return INVALID, "config_error"


def _probe_llm_channel(fast: bool) -> tuple[str, str]:
    """探一条 LLM 通道。client 一律来自 editor._llm —— 探的口径 = 用的口径。"""
    import editor
    try:
        client, model = editor._llm(fast=fast)
    except RuntimeError:
        # _llm 的「未配置」提示。走到这里说明 _env() 认为已配置、os.environ 却是空的 ——
        # 正常只发生在「既不经 main.py 也没调 _ensure_env_loaded()」的调用方，
        # 报 missing 是如实的；打条日志帮人定位到这一层差异
        log.warning("_llm 报告未配置，但 _env 认为已配置 —— 调用方可能没把 .env 灌进进程环境")
        return MISSING, ""
    except ImportError:
        return INVALID, "dependency_missing"
    # 自检不值得等主链路那么久；SDK 支持就收短，不支持就沿用（都在后台线程里）
    if hasattr(client, "with_options"):
        client = client.with_options(timeout=LLM_PROBE_TIMEOUT_S)
    try:
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,           # 只验鉴权与模型名，最省钱
            temperature=0,
        )
        return OK, ""
    except Exception as e:
        # 这里是**预期**的收敛点：任何探测期异常都要被归类成四态之一，
        # 不允许把异常抛给后台线程外 —— 否则首页状态就永远停在「检测中」
        return _classify_llm_error(e)


def _probe_llm() -> tuple[str, str, str]:
    """返回 (主通道态, 快通道态, reason)。"""
    if not (_env("LLM_API_KEY") and _env("LLM_BASE_URL")):
        return MISSING, MISSING, ""

    fast_url, fast_key = _env("LLM_FAST_BASE_URL"), _env("LLM_FAST_API_KEY")
    fast_separate = bool(fast_url or fast_key or _env("LLM_FAST_MODEL"))
    # 配置矛盾：快通道指向**别家供应商**却没给对应的 Key —— 必然拿主 Key 去打别家地址，
    # 一路 401。静态就能断言，所以不发请求：真发一次只会把错误显示成「主 Key 无效」，
    # 把人引到完全错误的方向（editor.py 的逐变量回落会让 fast 拿到主 Key）。
    if fast_separate and fast_url and fast_url != _env("LLM_BASE_URL") and not fast_key:
        return INVALID, INVALID, "fast_key_missing"

    main_state, main_reason = _probe_llm_channel(fast=False)
    if not fast_separate:
        # 快通道回落主通道：主通道探过就等于快通道也探过，不多花一次请求
        return main_state, INHERITED, main_reason
    fast_state, fast_reason = _probe_llm_channel(fast=True)
    return main_state, fast_state, main_reason or fast_reason


# ---------- 编排 ----------

def probe_all() -> None:
    """跑一次全量探测并整体替换快照。绝不让异常逃出去（后台线程死了没人知道）。"""
    if _env(SKIP_ENV) in ("1", "true", "True"):
        with _state_lock:
            _snapshot.update({"state": "idle", "stale": False})
        return

    amap_key = _env("AMAP_KEY")
    llm_cfg = bool(_env("LLM_API_KEY") and _env("LLM_BASE_URL"))
    fast_cfg = bool(_env("LLM_FAST_BASE_URL") or _env("LLM_FAST_API_KEY")
                    or _env("LLM_FAST_MODEL"))

    llm_state, fast_state, llm_reason = MISSING, MISSING, ""
    if llm_cfg or fast_cfg:
        llm_state, fast_state, llm_reason = _probe_llm()

    amap_state, amap_reason = MISSING, ""
    if amap_key:
        amap_state, amap_reason = _amap_probe_once(amap_key)

    probed = bool(amap_key or llm_cfg or fast_cfg)
    with _state_lock:
        _snapshot.update({
            "state": "ready" if probed else "idle",
            "at": _now(),
            "stale": False,
            "llm": llm_state,
            "llm_fast": fast_state,
            "amap_key": amap_state,
            "reasons": {"llm": llm_reason, "llm_fast": "", "amap_key": amap_reason},
        })


def _schedule_refresh() -> None:
    """投递一次后台刷新；已有一次在飞就不再投（防雪崩）。"""
    global _refreshing
    with _state_lock:
        if _refreshing:
            return
        _refreshing = True

    def run() -> None:
        global _refreshing
        try:
            probe_all()
        except Exception as e:
            # 后台线程里的失败必须留痕，否则「状态为什么一直灰着」无从查起
            log.warning("Key 自检失败: %s: %s", type(e).__name__, e)
        finally:
            with _state_lock:
                _refreshing = False

    _spawn(run)


def schedule_probe() -> None:
    """服务启动时调用一次：只投递、不等待 —— 应用必须立刻可服务，
    不能等 3 次网络超时之后再监听端口。"""
    _schedule_refresh()


def configured() -> dict[str, bool]:
    """三个通道「是否配置了」的本地判定 —— 零外呼、零探测。

    为什么单独给这个：探测是异步的，刚启动那一两秒里快照还是「没探到」。
    前端需要区分「配置了但还没探到」（灰色检测中）和「根本没配」（橙色未启用），
    缺了这份基线就只能二选一说谎。
    """
    main_cfg = bool(_env("LLM_API_KEY") and _env("LLM_BASE_URL"))
    fast_separate = bool(_env("LLM_FAST_BASE_URL") or _env("LLM_FAST_API_KEY")
                         or _env("LLM_FAST_MODEL"))
    return {"llm": main_cfg,
            # 快通道只有两条路成立：自己配全了，或回落到已配置的主通道
            "llm_fast": (bool(_env("LLM_FAST_API_KEY") and _env("LLM_FAST_BASE_URL"))
                         or main_cfg),
            "amap_key": bool(_env("AMAP_KEY"))}


def probe_age_s() -> int | None:
    """距上次探测完成多少秒；从未探测过返回 None（前端别拿它算「X 分钟前」）。"""
    if _snapshot["state"] != "ready":
        return None
    return max(0, int(_now() - float(_snapshot["at"])))


def status() -> dict[str, Any]:
    """给 /meta 用的只读快照。过期就立刻返回旧值（标 stale）并投递后台刷新，
    绝不在请求路径上同步刷新。"""
    snap = dict(_snapshot)
    snap["reasons"] = dict(_snapshot.get("reasons") or {})
    if snap["state"] == "ready" and _now() - float(snap["at"]) > PROBE_TTL_SEC:
        snap["stale"] = True
        _schedule_refresh()
    return snap


def _ensure_env_loaded() -> None:
    """把 .env 灌进进程环境 —— 与 main.py 启动时同一动作。

    为什么必须有：editor._llm() 读的是 **os.environ**，而 .env 只有 main.py 在
    import 期灌进去。命令行自检不经 main.py，若不自己灌，就会出现
    「配置明明写在 .env 里、_llm 却看到空环境」→ 探测把已配置误报成 missing
    （实测踩过：高德 ok、LLM missing，因为高德探测拿的是 _env() 的值）。
    setdefault 与 main.py 同语义：真实环境变量优先于 .env。
    """
    from commute import load_env_file
    for k, v in load_env_file().items():
        os.environ.setdefault(k, v)


def main() -> int:
    """命令行自检：拿到 Key 后先验一次再启服务，省得「改了 .env 重启了才发现 Key 错了」。

    退出码：0 = 所有已配置通道都可用（或未配置/回落），1 = 有通道不可用。
    """
    _ensure_env_loaded()
    probe_all()
    snap = status()
    reasons = snap.get("reasons") or {}
    rows = [
        ("LLM 主通道", snap["llm"], reasons.get("llm", ""),
         "抽取 / AI 总评 / 对话改行程"),
        ("LLM 快通道", snap["llm_fast"], reasons.get("llm_fast", ""),
         "评价生成（inherited = 回落主模型，正常）"),
        ("高德 Key", snap["amap_key"], reasons.get("amap_key", ""),
         "真实驾车通勤 / POI 检索"),
    ]
    bad = 0
    print("Key 自检结果：")
    for label, st, reason, note in rows:
        if st in (INVALID, UNREACHABLE):
            bad += 1
        suffix = f"（{reason}）" if reason else ""
        print(f"  {label:<10} {st}{suffix}   —— {note}")
    if snap.get("stale"):
        print("  ⚠ 显示的是上一次的探测结果（已过期，后台正在刷新）")
    print(f"\n结论：{'有通道不可用，请先修配置' if bad else '所有已配置通道可用'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

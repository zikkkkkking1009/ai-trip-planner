"""天气：行程期间按天的天气预报（open-meteo，无需密钥）。

**为什么可以破一次「不做天气」的既定决策**

ROADMAP / HANDOFF / docs/feature_ideas 三处都把「天气接入」列为明确不做，给了两条理由：
① 数据源不稳定；② 与「LLM + 组合优化」主线无关，加了反而分散叙事。

实测 ① **已经不成立**：open-meteo 无需注册、无需 key、免费，本地实测 ~0.9s 返回，
并且客户端零新增依赖（用标准库 urllib，与 commute.py 同一范式）。
而 ② 仍然成立 —— 所以这里刻意收紧定位：**天气是行程的注脚，不是新的叙事线**：
- 只给「本次行程那几天」的按天天气；不做逐小时、不做历史气候、不做独立页面/版块
- 拿不到就明说拿不到（未知城市 / 超出预报范围 / 网络失败），**绝不用假数据填充**
  这与本项目一贯立场一致：宁可显示「未收录」，也不显示错的

数据源：https://open-meteo.com（WMO weather code）
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta

from cities import city_center
from reliability import retry_call

log = logging.getLogger(__name__)

# ---- 成本保护常量（超时 / 缓存 / 范围上限）----
WEATHER_TIMEOUT_S = 6.0        # 外部调用必须有超时；天气是锦上添花，宁可快速失败
CACHE_TTL_SEC = 1800           # 30 分钟：预报本身一天只更新几次，缓存够用又不会永久过期
FORECAST_MAX_DAYS = 16         # open-meteo 预报上限；超出必须明说，不能假装有
ARCHIVE_MAX_PAST_DAYS = 5      # 归档数据有滞后，过近的「历史」拿不到

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
# 注意域名不同：归档是独立的 archive-api 子域，写成 api.open-meteo.com/v1/archive 会 404
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# WMO weather code → (中文, emoji)。只保留「要不要带伞 / 穿多少」这个粒度，
# 不做 28 种细分 —— 出行决策用不到那么多，细分反而增加维护面。
WMO_MAP: dict[int, tuple[str, str]] = {
    0: ("晴", "☀️"), 1: ("大致晴朗", "🌤️"), 2: ("多云", "⛅"), 3: ("阴", "☁️"),
    45: ("雾", "🌫️"), 48: ("冻雾", "🌫️"),
    51: ("毛毛雨", "🌦️"), 53: ("小雨", "🌦️"), 55: ("中雨", "🌧️"),
    56: ("冻毛毛雨", "🌧️"), 57: ("冻雨", "🌧️"),
    61: ("小雨", "🌧️"), 63: ("中雨", "🌧️"), 65: ("大雨", "🌧️"),
    66: ("冻雨", "🌧️"), 67: ("强冻雨", "🌧️"),
    71: ("小雪", "🌨️"), 73: ("中雪", "🌨️"), 75: ("大雪", "❄️"), 77: ("雪粒", "🌨️"),
    80: ("阵雨", "🌦️"), 81: ("中阵雨", "🌧️"), 82: ("强阵雨", "⛈️"),
    85: ("阵雪", "🌨️"), 86: ("强阵雪", "❄️"),
    95: ("雷阵雨", "⛈️"), 96: ("雷阵雨伴冰雹", "⛈️"), 99: ("强雷暴伴冰雹", "⛈️"),
}

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 进程内缓存：(城市, 起, 止, 数据源) → (写入时刻, 结果)。
# 只读-写单次赋值，dict 操作本身是原子的；这里够用，不需要上锁。
_cache: dict[tuple[str, str, str, str], tuple[float, dict]] = {}


def describe_code(code: int | None) -> tuple[str, str]:
    """WMO code → (中文, emoji)。未知 code 返回「未知」，不猜一个像样的天气出来。"""
    if code is None:
        return "未知", "❓"
    return WMO_MAP.get(int(code), ("未知", "❓"))


def pick_source(start: date, end: date, today: date) -> tuple[str | None, str]:
    """决定数据源。返回 (source, 原因)；source 为 None 表示拿不到，第二个值是原因说明。

    为什么要把「拿不到」显式表达出来：天气预报只能覆盖未来十几天、归档有滞后。
    行程日期完全可能是任意日期，这时唯一诚实的做法是告诉用户拿不到，
    而不是拿最近的天气凑上去（那就是本项目最忌讳的静默错误）。
    """
    if end < start:
        return None, "行程日期范围不合法"
    if end > today + timedelta(days=FORECAST_MAX_DAYS):
        return None, f"超出预报范围（最多提供未来 {FORECAST_MAX_DAYS} 天）"
    if end < today:
        if start < today - timedelta(days=ARCHIVE_MAX_PAST_DAYS):
            return None, "日期太久远，超出可查范围"
        return "archive", ""
    return "forecast", ""


def _daily_params(source: str) -> str:
    """按数据源给 daily 变量。

    归档接口没有降水概率（历史实况只有降水量），所以两边的参数不能共用 ——
    这里显式分开，避免请求一个归档不认识的字段导致整体失败。
    """
    base = "weather_code,temperature_2m_max,temperature_2m_min,wind_speed_10m_max"
    if source == "archive":
        return base + ",precipitation_sum"
    return base + ",precipitation_sum,precipitation_probability_max"


def _build_url(source: str, lat: float, lon: float,
               start: date, end: date) -> str:
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": _daily_params(source),
        "timezone": "Asia/Shanghai",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    host = ARCHIVE_URL if source == "archive" else FORECAST_URL
    return f"{host}?{urllib.parse.urlencode(params)}"


def _fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=WEATHER_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _num(seq: list, i: int) -> float | None:
    """安全取数：open-meteo 对缺测值会给 null，直接 float() 会炸。"""
    if i >= len(seq) or seq[i] is None:
        return None
    try:
        return round(float(seq[i]), 1)
    except (TypeError, ValueError):
        return None


def _parse(data: dict, start: date, end: date, source: str) -> list[dict]:
    """把 open-meteo 的按列（列式）响应转成按天的列表，并裁到行程范围内。"""
    daily = data.get("daily") or {}
    times = daily.get("time") or []
    days: list[dict] = []
    for i, iso in enumerate(times):
        try:
            d = date.fromisoformat(iso)
        except (TypeError, ValueError):
            continue
        if not (start <= d <= end):
            continue
        text, emoji = describe_code(_num(daily.get("weather_code") or [], i))
        prob = _num(daily.get("precipitation_probability_max") or [], i)
        days.append({
            "date": iso,
            "weekday": WEEKDAYS[d.weekday()],
            "code": int(_num(daily.get("weather_code") or [], i) or 0),
            "text": text,
            "emoji": emoji,
            "t_max": _num(daily.get("temperature_2m_max") or [], i),
            "t_min": _num(daily.get("temperature_2m_min") or [], i),
            "precip_mm": _num(daily.get("precipitation_sum") or [], i),
            # 归档数据没有概率，显式给 None，前端据此不显示这一项（而不是显示 0%）
            "precip_prob": None if prob is None else int(prob),
            "wind_max": _num(daily.get("wind_speed_10m_max") or [], i),
        })
    return days


def daily_weather(city: str | None, start: date, end: date,
                  today: date | None = None) -> dict:
    """取行程期间按天的天气。

    返回：{"available": True, "source": ..., "days": [...]}
      或  {"available": False, "reason": "..."}
    拿不到时**不用假数据填充**，前端据 available 决定显示还是说明原因。
    """
    center = city_center(city)
    if not center:
        return {"available": False, "reason": "未知城市，无法定位取天气"}

    today = today or date.today()
    source, why = pick_source(start, end, today)
    if not source:
        return {"available": False, "reason": why}

    key = (str(city), start.isoformat(), end.isoformat(), source)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_SEC:
        return hit[1]

    lat, lon = center
    url = _build_url(source, lat, lon, start, end)
    try:
        data = retry_call(lambda: _fetch(url),
                          what=f"天气({city} {start}~{end})")
    except Exception as exc:  # noqa: BLE001 —— 天气是可选增强，任何失败都只降级不抛出
        log.warning("天气获取失败 city=%s %s~%s: %s: %s",
                    city, start, end, type(exc).__name__, exc)
        return {"available": False, "reason": f"天气服务暂时不可用（{type(exc).__name__}）"}

    days = _parse(data, start, end, source)
    if not days:
        log.warning("天气响应里没有 %s~%s 的记录 city=%s", start, end, city)
        return {"available": False, "reason": "天气服务没有返回这几天的记录"}

    result = {
        "available": True,
        "source": "open-meteo 归档" if source == "archive" else "open-meteo 预报",
        "days": days,
    }
    _cache[key] = (time.time(), result)
    return result

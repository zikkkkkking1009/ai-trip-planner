"""媒体缓存的 key 规则与读写（spot_media.json）。

**为什么 key 必须带城市**：缓存原本只用景点名做 key，而不同城市存在同名景点
（「人民公园」成都/上海都有），谁先被查询谁的数据就被写进去，另一个城市会**读到错的坐标与地址**。
更糟的是高德 `citylimit=true` 并不严格（实测用西安搜「四川博物院」照样返回成都地址），
所以这类错配是**静默错误**——不报错、结果却是错的。因此缓存 key 统一为 `城市|景点名`。

兼容策略：读取时先查带城市的 key，未命中再回退裸名（迁移前的既有数据仍可命中）；
写入只写带城市的 key。迁移用 `tools/migrate_media_keys.py`。

**写入前的城市一致性校验**（`suspected_wrong_city()`）：key 带城市还不足够——高德
`citylimit=true` 并不严格，**抓回来的数据本身可能属于别的城市**，一旦写进缓存就会被
永久固化成错数据（历史实例：裸名 `四川博物院` 里存的是西安博物院的数据）。
所以写入前要用载荷坐标反查一次"它到底属于哪个城市"。

并发安全：写入走「临时文件 + os.replace」原子替换，避免多个请求同时写坏 JSON。
但原子写**只保证不写坏，不保证不丢更新**——两个写方各持一份 load_media() 快照
时，后写的整份覆盖先写的。所以「读-改-写」必须串行化：所有写方统一走
`update_media()`（模块级线程锁 + 进程内共享字典 + 按键合并）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from cities import CITY_CENTERS, normalize_city
from solver import haversine_km

log = logging.getLogger(__name__)

MEDIA_FILE = Path(__file__).parent / "spot_media.json"

# RLock：update_media 持锁期间还要调 shared_media()（同样加锁），可重入才不死锁。
# 用线程锁而不是 asyncio.Lock：写方既有线程池里的同步 endpoint，也有 to_thread
# 里的预取协程，asyncio.Lock 只罩得住后者。
_io_lock = threading.RLock()
_shared: dict | None = None

# ---- 城市一致性判据（见 suspected_wrong_city 的说明，为什么必须用"相对距离"）----
_MISMATCH_NEAR_KM = 60.0    # 载荷离某个**别的**城市中心多近，才算"明显属于它"
_MISMATCH_FAR_KM = 100.0    # 同时离**请求的**城市多远，才算"明显不是本城"


def media_key(city: str | None, name: str) -> str:
    """缓存键：`城市|景点名`。城市为空时退化为 `|景点名`（仍比裸名更好区分）。"""
    return f"{(city or '').strip()}|{name}"


def load_media() -> dict:
    """读整个媒体缓存；文件缺失或损坏时返回空字典（缓存可重建，不阻断主流程）。"""
    if not MEDIA_FILE.exists():
        return {}
    try:
        _loaded: dict = json.loads(MEDIA_FILE.read_text(encoding="utf-8"))
        return _loaded
    except (json.JSONDecodeError, OSError) as e:
        log.warning("媒体缓存读取失败，按空处理: %s: %s", type(e).__name__, e)
        return {}


def save_media(media: dict) -> None:
    """原子写：先写临时文件再替换，避免并发写导致 JSON 损坏。"""
    tmp = MEDIA_FILE.with_name(MEDIA_FILE.name + ".tmp")
    try:
        tmp.write_text(json.dumps(media, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, MEDIA_FILE)
    except OSError as e:
        # 写失败不影响主流程（详情卡下次再抓），但要留痕
        log.warning("媒体缓存写入失败: %s: %s", type(e).__name__, e)


def get_entry(media: dict, city: str | None, name: str) -> dict | None:
    """从已加载的 media 里取条目：带城市 key 优先，回退裸名（兼容旧数据）。"""
    return media.get(media_key(city, name)) or media.get(name)


def suspected_wrong_city(city: str | None, entry: dict | None) -> str | None:
    """载荷是否**明显属于别的城市**？是则返回那个城市名，否则 None。

    为什么需要它：`citylimit=true` 并不严格。实测用西安搜「四川博物院」，返回的是
    西安本地的「西安博物院」（地址「友谊西路72号」，坐标落在西安市区）——
    而抓取代码信任 `pois[0]`，于是这份**别的城市的数据**被写进缓存并长期固化。
    之后任何城市查该景点都会拿到西安的图片与地址，且不报错。key 带城市只能防
    「同名景点互相覆盖」，防不住「抓回来的就是错的」。

    判据为什么必须是**相对**的：真实景点可以离市中心很远——重庆武隆天生三桥离
    重庆市中心 122km（行政上确属重庆）、都江堰离成都 65km、八达岭离北京 60km。
    所以只按"离请求城市多远"判会大量误伤。正确判据是：载荷离**另一个**城市中心
    很近（≤60km）、同时离请求的城市很远（≥100km）——这才叫"明显属于别处"。

    城市表里查不到请求城市、或载荷没有坐标时返回 None（无法判定，交给调用方）。
    """
    if not entry:
        return None
    lat, lon = entry.get("lat"), entry.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    # 注意：bool 是 int 的子类，但坐标不会是布尔值，不必特判
    req = normalize_city(city)
    if req not in CITY_CENTERS:
        return None
    rc = CITY_CENTERS[req]
    if haversine_km(rc[0], rc[1], lat, lon) < _MISMATCH_FAR_KM:
        return None
    nearest = min(CITY_CENTERS.items(),
                  key=lambda kv: haversine_km(kv[1][0], kv[1][1], lat, lon))
    if nearest[0] == req:
        return None
    if haversine_km(nearest[1][0], nearest[1][1], lat, lon) > _MISMATCH_NEAR_KM:
        return None
    return nearest[0]


def get_media(city: str | None, name: str) -> dict | None:
    return get_entry(load_media(), city, name)


def shared_media() -> dict:
    """进程内共享的一份媒体缓存（懒加载）：所有写方写的是同一个字典。"""
    global _shared
    with _io_lock:
        if _shared is None:
            _shared = load_media()
        return _shared


def update_media(city: str | None, name: str, entry: dict) -> None:
    """把一个条目合并进共享缓存并落盘（线程安全，所有写方的唯一入口）。

    合并语义：entry 的键优先，已有条目里 entry 没有的键保留 ——
    典型场景：详情接口刚写的评价不该被预取线程的整份覆盖抹掉
    （2026-09-28 修：main.py 两个 endpoint 曾各自 load→改→save，
    与 tasks.prefetch_media 的锁+共享字典并存，互相丢更新）。
    """
    with _io_lock:
        media = shared_media()
        existing = media.get(media_key(city, name))
        media[media_key(city, name)] = {**(existing or {}), **entry}
        save_media(media)

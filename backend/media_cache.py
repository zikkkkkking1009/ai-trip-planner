"""媒体缓存的 key 规则与读写（spot_media.json）。

**为什么 key 必须带城市**：缓存原本只用景点名做 key，而不同城市存在同名景点
（「人民公园」成都/上海都有），谁先被查询谁的数据就被写进去，另一个城市会**读到错的坐标与地址**。
更糟的是高德 `citylimit=true` 并不严格（实测用西安搜「四川博物院」照样返回成都地址），
所以这类错配是**静默错误**——不报错、结果却是错的。因此缓存 key 统一为 `城市|景点名`。

兼容策略：读取时先查带城市的 key，未命中再回退裸名（迁移前的既有数据仍可命中）；
写入只写带城市的 key。迁移用 `tools/migrate_media_keys.py`。

并发安全：写入走「临时文件 + os.replace」原子替换，避免多个请求同时写坏 JSON。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

MEDIA_FILE = Path(__file__).parent / "spot_media.json"


def media_key(city: str | None, name: str) -> str:
    """缓存键：`城市|景点名`。城市为空时退化为 `|景点名`（仍比裸名更好区分）。"""
    return f"{(city or '').strip()}|{name}"


def load_media() -> dict:
    """读整个媒体缓存；文件缺失或损坏时返回空字典（缓存可重建，不阻断主流程）。"""
    if not MEDIA_FILE.exists():
        return {}
    try:
        return json.loads(MEDIA_FILE.read_text(encoding="utf-8"))
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


def get_media(city: str | None, name: str) -> dict | None:
    return get_entry(load_media(), city, name)


def put_media(city: str | None, name: str, entry: dict) -> None:
    media = load_media()
    media[media_key(city, name)] = entry
    save_media(media)

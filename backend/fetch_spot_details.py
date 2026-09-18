"""一次性工具：为演示景点抓取高德实景图与介绍，写入 spot_media.json。

用法：python fetch_spot_details.py   （需要 .env 里的 AMAP_KEY）
产出：backend/spot_media.json  {景点名: {"image": url, "intro": "..."}}
该文件提交进仓库——高德图片 CDN 是公开 URL，之后运行/CI 零 Key 可用。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from commute import load_env_file
from demo_data import XI_AN_SPOTS

HERE = Path(__file__).parent
_last = 0.0


def amap_get(path: str, **params) -> dict:
    global _last
    key = load_env_file().get("AMAP_KEY")
    params["key"] = key
    url = f"https://restapi.amap.com/{path}?{urllib.parse.urlencode(params)}"
    wait = 0.35 - (time.time() - _last)
    if wait > 0:
        time.sleep(wait)
    _last = time.time()
    with urllib.request.urlopen(url, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch(name: str, city: str = "西安") -> dict:
    out = {"image": "", "intro": "", "photos": [], "opentime": "", "address": "",
           "lat": 0.0, "lon": 0.0}
    try:
        text = amap_get("v3/place/text", keywords=name, city=city,
                        citylimit="true", offset=1, page=1)
        pois = text.get("pois") or []
        if not pois:
            return out
        poi = pois[0]
        try:  # GCJ-02 坐标，导航 URI 直接可用
            lon, lat = map(float, poi["location"].split(","))
            out["lat"], out["lon"] = lat, lon
        except (KeyError, ValueError):
            pass
        photos = [ph["url"] for ph in (poi.get("photos") or []) if ph.get("url")]
        # 深度信息（评分/人均/类型/营业时间/地址）
        detail = amap_get("v3/place/detail", id=poi["id"])
        dp = (detail.get("pois") or [{}])[0]
        photos += [ph["url"] for ph in (dp.get("photos") or []) if ph.get("url")]
        photos = list(dict.fromkeys(photos))[:4]
        out["photos"] = photos
        if photos:
            out["image"] = photos[0]
        out["opentime"] = str(dp.get("opentime") or dp.get("opentime2") or "")
        out["address"] = str(dp.get("address") or "")
        bits = []
        if dp.get("type"):
            bits.append(dp["type"].split(";")[0])
        biz = dp.get("biz_ext") or {}
        if biz.get("rating"):
            bits.append(f"评分{biz['rating']}")
        if biz.get("cost"):
            bits.append(f"人均¥{biz['cost']}")
        if dp.get("business_area"):
            bits.append(dp["business_area"])
        out["intro"] = " · ".join(bits)
    except Exception as e:
        print(f"  [warn] {name}: {e}")
    return out


if __name__ == "__main__":
    media = {}
    for s in XI_AN_SPOTS:
        print(f"抓取 {s.name} ...")
        media[s.name] = fetch(s.name)
    (HERE / "spot_media.json").write_text(
        json.dumps(media, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for v in media.values() if v["image"])
    print(f"完成：{ok}/{len(media)} 个景点拿到图片 → spot_media.json")

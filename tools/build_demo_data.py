"""一次性数据生成脚本：抓取多城市 demo 景点（真实高德 POI 坐标）。

为什么需要脚本：demo 数据的**坐标必须真实**，手工编造的坐标会让地图与行程全错，
而这种错误在演示时最致命。所以坐标一律走高德 place/text 搜索，
门票/停留时长/开放时间属演示用近似参数（文件头会标注），评分优先用高德真实值。

用法：
    cd backend && python ../tools/build_demo_data.py

输出：
    backend/demo_spots.json          —— 4 个新增城市的景点数据
    控制台打印城市中心坐标            —— 供 backend/cities.py 使用
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from aligner import type_flag  # noqa: E402  复用既有干扰类型过滤规则
from commute import load_env_file  # noqa: E402

OUT_FILE = BACKEND / "demo_spots.json"
QPS_INTERVAL = 0.35  # 个人 Key 限频

# ---- 要抓取的城市（西安已有 14 条精心手写数据，不重复抓）----
CITY_SPOT_QUERIES: dict[str, list[str]] = {
    "成都": ["宽窄巷子", "锦里", "成都大熊猫繁育研究基地", "都江堰景区", "青城山",
             "武侯祠", "杜甫草堂", "金沙遗址博物馆", "人民公园", "春熙路"],
    "北京": ["故宫博物院", "天安门广场", "颐和园", "八达岭长城", "天坛公园",
             "圆明园", "北海公园", "南锣鼓巷", "鸟巢", "什刹海"],
    "杭州": ["西湖风景名胜区", "灵隐寺", "雷峰塔", "西溪国家湿地公园", "宋城",
             "河坊街", "六和塔", "九溪烟树", "浙江省博物馆", "中国茶叶博物馆"],
    "重庆": ["洪崖洞", "解放碑步行街", "磁器口古镇", "长江索道", "武隆天生三桥",
             "南山一棵树观景台", "白公馆", "渣滓洞", "朝天门广场", "鹅岭二厂文创公园"],
}

# ---- 门票：演示用近似值（非实时票价），只列常见收费景点，其余默认免费 ----
TICKET = {
    "成都大熊猫繁育研究基地": 55, "都江堰景区": 80, "青城山": 80, "武侯祠": 50,
    "杜甫草堂": 50, "金沙遗址博物馆": 70,
    "故宫博物院": 60, "颐和园": 30, "八达岭长城": 40, "天坛公园": 15,
    "圆明园": 10, "北海公园": 10, "鸟巢": 50,
    "灵隐寺": 75, "雷峰塔": 40, "西溪国家湿地公园": 80, "宋城": 320, "六和塔": 20,
    "长江索道": 20, "武隆天生三桥": 125, "南山一棵树观景台": 20,
}

# ---- 停留时长推断：三层判断（演示用参数，非真实游览数据）----
# 为什么要分三层：单看类型会被泛化规则带偏——「人民公园」是国家级景点但只需 1.5 小时，
# 「西溪国家湿地公园」同样含「公园」却要逛 3 小时；「浙江省博物馆(孤山馆区)」名字里有
# 「山」字但它是博物馆。所以顺序是：类型硬规则 → 名字决定性词 → 类型兜底。
STAY_MUSEUM_TYPE = ("博物馆", "展览馆")
STAY_LONG_NAME = ("湿地", "山", "长城", "峡谷", "湖", "遗址公园", "古镇", "风景区")
STAY_SHORT_NAME = ("公园", "广场", "塔")
STAY_STREET_NAME = ("街", "巷", "商圈")
STAY_TEMPLE_NAME = ("寺", "祠", "堂", "观")

# 名称里的状态后缀：不属于标准名，应剥离并记录为备注
STATUS_SUFFIX = ("暂停开放", "已关闭", "停业", "维修中")


def cleanup_name(name: str) -> tuple[str, str]:
    """剥离「(暂停开放)」这类状态后缀 → (标准名, 备注)。馆区名等区分性括号保留。"""
    for status in STATUS_SUFFIX:
        for left, right in (("(", ")"), ("（", "）")):
            token = f"{left}{status}{right}"
            if token in name:
                return name.replace(token, "").strip(), f"当前{status}（数据源标注）"
    return name, ""


def guess_stay(name: str, type_str: str) -> int:
    if any(k in type_str for k in STAY_MUSEUM_TYPE):
        return 150
    if any(k in name for k in STAY_LONG_NAME):
        return 180
    if any(k in name for k in STAY_SHORT_NAME):
        return 90
    if any(k in name for k in STAY_STREET_NAME):
        return 120
    if any(k in name for k in STAY_TEMPLE_NAME):
        return 120
    if "国家级景点" in type_str or "世界遗产" in type_str:
        return 180
    return 90


def guess_hours(type_str: str) -> tuple[float, float]:
    if any(k in type_str for k in ("商业街", "步行街", "特色商业街", "街", "巷", "古镇")):
        return 10.0, 22.0
    if any(k in type_str for k in ("山", "长城", "湿地")):
        return 8.0, 17.5
    return 8.0, 18.0


_last_call = 0.0


def amap_get(url: str) -> dict:
    """带限频的高德请求。"""
    global _last_call
    wait = QPS_INTERVAL - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()
    with urllib.request.urlopen(url, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


class Amap:
    def __init__(self) -> None:
        self.key = load_env_file().get("AMAP_KEY", "")
        if not self.key:
            raise SystemExit("缺少 AMAP_KEY，无法抓取真实 POI 数据")

    def city_center(self, city: str) -> dict:
        """地理编码：城市 → 中心坐标 + adcode。"""
        params = urllib.parse.urlencode({"address": city, "city": city, "key": self.key})
        data = amap_get(f"https://restapi.amap.com/v3/geocode/geo?{params}")
        geos = data.get("geocodes") or []
        if not geos:
            return {}
        g = geos[0]
        lon, lat = map(float, g["location"].split(","))
        return {"city": city, "lat": round(lat, 6), "lon": round(lon, 6),
                "adcode": g.get("adcode", ""), "level": g.get("level", "")}

    def search(self, keywords: str, city: str, limit: int = 5) -> list[dict]:
        """文本搜索：返回候选 POI（含评分/人均，供筛选真实值）。"""
        params = urllib.parse.urlencode({
            "keywords": keywords, "city": city, "citylimit": "true",
            "offset": limit, "page": 1, "extensions": "all", "key": self.key,
        })
        data = amap_get(f"https://restapi.amap.com/v3/place/text?{params}")
        if data.get("status") != "1":
            raise RuntimeError(f"高德搜索异常：{data.get('info')}")
        out = []
        for p in data.get("pois", []):
            loc = p.get("location") or ""
            if "," not in loc:
                continue
            lon, lat = map(float, loc.split(","))
            biz = p.get("biz_ext") or {}
            rating = biz.get("rating")
            cost = biz.get("cost")
            out.append({
                "name": p.get("name", ""), "lat": lat, "lon": lon,
                "type_str": p.get("type", ""), "address": p.get("address", ""),
                "rating": float(rating) if rating not in (None, "", []) else None,
                "avg_cost": float(cost) if cost not in (None, "", []) else None,
            })
        return out


def pick_clean(cands: list[dict], city: str) -> dict | None:
    """选第一个干净类型的候选（过滤公交站/公司/生活服务等干扰）。"""
    clean = [c for c in cands if type_flag(c["type_str"]) > 0]
    return (clean or cands or [None])[0]


def main() -> None:
    amap = Amap()
    result: dict[str, list[dict]] = {}

    print("=== 城市中心（地理编码真实值，供 backend/cities.py）===")
    centers = {}
    for city in ["西安", *CITY_SPOT_QUERIES]:
        info = amap.city_center(city)
        centers[city] = info
        print(f'    "{city}": ({info.get("lat")}, {info.get("lon")}),  '
              f'# adcode={info.get("adcode")} level={info.get("level")}')

    for city, names in CITY_SPOT_QUERIES.items():
        print(f"\n=== {city}（{len(names)} 个景点）===")
        rows = []
        for idx, name in enumerate(names, 1):
            try:
                cand = pick_clean(amap.search(name, city), city)
            except Exception as e:
                print(f"  {idx:>2}. {name:<22} 抓取失败：{e}")
                continue
            if not cand:
                print(f"  {idx:>2}. {name:<22} 无候选，跳过")
                continue
            type_str = cand["type_str"]
            open_h, close_h = guess_hours(type_str)
            std_name, note = cleanup_name(cand["name"])
            row = {
                "source_id": idx, "name": std_name,
                "lat": round(cand["lat"], 6), "lon": round(cand["lon"], 6),
                "stay_min": guess_stay(std_name, type_str),
                "score": round(cand["rating"], 1) if cand["rating"] else 7.5,
                "ticket": TICKET.get(std_name, TICKET.get(name, 0)),
                "open_h": open_h, "close_h": close_h,
                "desc": note,
                "type_str": type_str, "address": cand["address"],
                "avg_cost": cand["avg_cost"],
            }
            rows.append(row)
            print(f'  {idx:>2}. {std_name:<22} ({cand["lat"]:.4f},{cand["lon"]:.4f}) '
                  f'评分={row["score"]} 票={row["ticket"]} 停留={row["stay_min"]}'
                  f' | {type_str[:34]}')
        result[city] = rows

    OUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    total = sum(len(v) for v in result.values())
    print(f"\n已写入 {OUT_FILE}：{len(result)} 城 / {total} 个景点")
    print("（坐标=高德真实值；门票/停留/开放时间为演示用近似值，评分优先取高德真实评分）")


if __name__ == "__main__":
    main()

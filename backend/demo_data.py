"""演示数据：多城市景点（零 Key、零成本可跑通）。

数据来源与可信度（重要）：
- **西安 14 条**：初版手写数据，坐标为 GCJ-02 近似值，desc 是行程卡片上的一句话介绍。
- **成都/北京/杭州/重庆 40 条**（`demo_spots.json`）：由 `tools/build_demo_data.py`
  从高德 place/text 搜索抓取，**坐标是高德真实值**；门票/停留时长/开放时间为演示用
  近似参数（非实时票价），评分优先取高德真实评分。要扩充城市请跑那个脚本，不要手写坐标。

兼容性：`XI_AN_SPOTS` 这个名称被 8 处引用（evaluation / run_demo* / fetch_spot_details /
main / 测试），因此保留为别名，不要重命名。
"""
from __future__ import annotations

import json
from pathlib import Path

from cities import DEMO_CITIES, normalize_city
from models import Spot

BACKEND_DIR = Path(__file__).parent
EXTRA_SPOTS_FILE = BACKEND_DIR / "demo_spots.json"

# 只取 Spot 模型认得的字段（JSON 里还带 type_str/address/avg_cost，属抓取元信息）
_SPOT_FIELDS = ("source_id", "name", "lat", "lon", "stay_min", "score",
                "ticket", "open_h", "close_h", "desc")

XI_AN_SPOTS = [
    Spot(source_id=1, name="秦始皇兵马俑博物馆", lat=34.3847, lon=109.2785,
         stay_min=180, score=9.5, ticket=120, open_h=8.5, close_h=17.0,
         desc="世界第八大奇迹，一号坑的军阵气势最足，建议请讲解或租导览器"),
    Spot(source_id=2, name="陕西历史博物馆", lat=34.2225, lon=108.9530,
         stay_min=150, score=9.2, ticket=0, open_h=9.0, close_h=17.5,
         desc="「给我一天，还你万年」，何家村窖藏与大唐壁画是镇馆之宝，需提前预约"),
    Spot(source_id=3, name="西安城墙", lat=34.2760, lon=108.9470,
         stay_min=120, score=8.5, ticket=54, open_h=8.0, close_h=22.0,
         desc="中国现存最完整的古代城垣，推荐傍晚骑一圈自行车，看夕阳落城楼"),
    Spot(source_id=4, name="大雁塔", lat=34.2185, lon=108.9640,
         stay_min=90, score=8.3, ticket=50, open_h=8.0, close_h=18.0,
         desc="玄奘译经之地，北广场音乐喷泉傍晚最热闹"),
    Spot(source_id=5, name="回民街", lat=34.2650, lon=108.9350,
         stay_min=120, score=8.0, ticket=0, open_h=10.0, close_h=22.0,
         desc="泡馍、肉夹馍、甑糕一条街，人多但烟火气足，认准现做的"),
    Spot(source_id=6, name="华清宫", lat=34.3620, lon=109.2130,
         stay_min=120, score=7.8, ticket=120, open_h=7.5, close_h=18.0,
         desc="杨贵妃沐浴的骊山温泉行宫，和兵马俑顺路，可安排同一天"),
    Spot(source_id=7, name="钟楼", lat=34.2610, lon=108.9420,
         stay_min=45, score=7.5, ticket=30, open_h=8.5, close_h=21.0,
         desc="西安的城市原点，夜景比白天好看，登楼看四条大街"),
    Spot(source_id=8, name="大唐不夜城", lat=34.2150, lon=108.9670,
         stay_min=120, score=8.2, ticket=0, open_h=10.0, close_h=23.0,
         desc="夜游天花板，灯火+演出+不倒翁小姐姐，天黑后去才对味"),
    Spot(source_id=9, name="小雁塔", lat=34.2360, lon=108.9400,
         stay_min=75, score=7.2, ticket=0, open_h=9.0, close_h=17.5,
         desc="比大雁塔清静的唐代佛塔，「雁塔晨钟」关中八景之一"),
    Spot(source_id=10, name="碑林博物馆", lat=34.2520, lon=108.9470,
         stay_min=90, score=7.4, ticket=50, open_h=8.0, close_h=18.0,
         desc="书法爱好者的圣地，颜真卿柳公权真迹石刻都在这里"),
    Spot(source_id=11, name="大明宫国家遗址公园", lat=34.2860, lon=108.9600,
         stay_min=105, score=7.0, ticket=60, open_h=8.5, close_h=19.0,
         desc="盛唐皇宫遗址，含元殿的夯土台基足够想象当年规模"),
    Spot(source_id=12, name="永兴坊", lat=34.2690, lon=108.9570,
         stay_min=90, score=7.3, ticket=0, open_h=10.0, close_h=22.0,
         desc="陕西非遗美食集合地，摔碗酒就在这里"),
    Spot(source_id=13, name="西安博物院", lat=34.2300, lon=108.9320,
         stay_min=90, score=7.1, ticket=0, open_h=9.0, close_h=17.0,
         desc="小雁塔就在院里，人少展精，适合慢逛"),
    Spot(source_id=14, name="青龙寺", lat=34.2380, lon=108.9930,
         stay_min=60, score=6.8, ticket=0, open_h=8.5, close_h=17.0,
         desc="日本密宗祖庭，春季樱花是西安一绝"),
]


def _load_extra_cities() -> dict[str, list[Spot]]:
    """加载脚本生成的多城市数据；文件缺失时优雅降级为「只有西安」。"""
    if not EXTRA_SPOTS_FILE.exists():
        return {}
    try:
        raw = json.loads(EXTRA_SPOTS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, list[Spot]] = {}
    for city, rows in raw.items():
        try:
            out[normalize_city(city)] = [
                Spot(**{k: r[k] for k in _SPOT_FIELDS if k in r}) for r in rows
            ]
        except (TypeError, KeyError, ValueError):
            continue  # 单城数据损坏不影响其他城市
    return out


DEMO_SPOTS: dict[str, list[Spot]] = {"西安": XI_AN_SPOTS, **_load_extra_cities()}


def demo_spots(city: str | None) -> list[Spot]:
    """取某城市的演示景点列表；未知城市返回空列表（调用方负责提示用户）。"""
    return list(DEMO_SPOTS.get(normalize_city(city), []))


def demo_cities() -> list[str]:
    """实际可演示的城市（按 DEMO_CITIES 顺序，缺数据的城市不出现）。"""
    return [c for c in DEMO_CITIES if DEMO_SPOTS.get(c)]

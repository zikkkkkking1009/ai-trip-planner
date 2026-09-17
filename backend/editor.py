"""对话式修改行程：用户自然语言 → 结构化编辑操作 → 确定性执行。

职责边界（与整个项目的架构一致）：
- LLM 只做一件事：把「明天下午加个附近的咖啡馆」翻译成结构化操作
  {"op": "add", "query": "咖啡馆", "day": 2}
- 之后的一切（POI 搜索、选点、重排、校验）都是确定性代码——
  LLL 永远不直接改行程数据

支持的操作：
- remove  {"op": "remove", "name": "回民街"}
- add     {"op": "add", "query": "咖啡馆", "day": 2, "name": "可选指定名"}
- replace {"op": "replace", "old": "回民街", "query": "本地人去的美食街", "day": 3}

add/replace 需要网络（高德周边搜索 POI）；remove 纯本地。
没有 LLM Key 时 parse_instruction 抛 RuntimeError，由调用方降级。
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request

from aligner import type_flag
from commute import load_env_file
from models import Spot


# ---- LLM 意图解析 ----
_SYSTEM = """你是行程编辑助手。根据当前行程和用户指令，输出 JSON（不要解释）：
{"ops": [...], "reply": "一句话回复用户"}
ops 支持三种操作：
- remove: {"op":"remove","name":"行程中准确的景点名"}
- add:    {"op":"add","query":"POI搜索关键词","day":天数(从1开始,可null),"name":"可省略"}
- replace:{"op":"replace","old":"要移除的景点名","query":"搜索关键词","day":天数或null}
规则：
- remove 的 name 必须逐字取自当前行程，不要改写
- add/replace 必须给 query（用于地图搜索），如"咖啡馆""美食街""博物馆"
- 可以一次给多个 ops
- 指令与行程无关或无法理解时输出 {"ops":[],"reply":"没听懂，试试：把XX换成XX / 加个咖啡馆"}"""


def parse_instruction(instruction: str, plan_summary: str) -> dict:
    """调 LLM 把自然语言指令解析成结构化操作。需要 LLM_API_KEY。"""
    from extractor import _tolerant_json_parse
    from openai import OpenAI

    api_key = os.environ.get("LLM_API_KEY")
    base_url = os.environ.get("LLM_BASE_URL")
    if not api_key or not base_url:
        raise RuntimeError("未配置 LLM_API_KEY，无法解析编辑指令")
    client = OpenAI(api_key=api_key, base_url=base_url)
    resp = client.chat.completions.create(
        model=os.environ.get("LLM_MODEL_ID", "deepseek-chat"),
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content":
                f"当前行程：\n{plan_summary}\n\n用户指令：{instruction}"},
        ],
        temperature=0.1,
    )
    return _tolerant_json_parse(resp.choices[0].message.content or "")


# ---- 高德周边搜索（加点用）----
def poi_search(query: str, lat: float, lon: float,
               radius: int = 3000) -> list[dict]:
    """高德周边搜索 POI，返回 [{name, lat, lon, type_str}]。失败返回空列表。"""
    env = load_env_file()
    key = env.get("AMAP_KEY") or os.environ.get("AMAP_KEY")
    if not key:
        return []
    params = urllib.parse.urlencode({
        "location": f"{lon},{lat}", "keywords": query,
        "radius": radius, "offset": 5, "page": 1, "key": key,
        "sortrule": "distance",
    })
    url = f"https://restapi.amap.com/v3/place/around?{params}"
    wait = 0.35 - (time.time() - getattr(poi_search, "_last", 0))
    if wait > 0:
        time.sleep(wait)
    poi_search._last = time.time()
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    if data.get("status") != "1":
        return []
    out = []
    for p in data.get("pois", []):
        try:
            lon, lat2 = map(float, p["location"].split(","))
        except (KeyError, ValueError):
            continue
        out.append({"name": p.get("name", ""), "lat": lat2, "lon": lon,
                    "type_str": p.get("type", "")})
    return out


def pick_poi(candidates: list[dict]) -> dict | None:
    """从周边搜索结果里选第一个干净类型的 POI（过滤公交站等干扰）。"""
    for c in candidates:
        if type_flag(c.get("type_str", "")) >= 0:
            return c
    return candidates[0] if candidates else None


def make_spot(poi: dict) -> Spot:
    """POI → 求解器可用的 Spot（餐饮类默认参数：1小时、免费、营业到22点）。"""
    return Spot(source_id=0, name=poi["name"], lat=poi["lat"], lon=poi["lon"],
                stay_min=60, score=6.5, ticket=0, open_h=9.0, close_h=22.0,
                desc=f"新增：{poi.get('type_str', '').split(';')[0]}")


# ---- 确定性执行：把 ops 应用到景点列表（纯函数，可单测）----
def apply_ops(base_spots: list[Spot], ops: list[dict],
              poi_search_fn=None,
              day_anchors: dict[int, tuple[float, float]] | None = None
              ) -> tuple[list[Spot], list[str]]:
    """返回 (修改后的景点列表, 变更说明列表)。

    poi_search_fn(query, lat, lon) -> list[dict]：由调用方注入真实搜索；
    测试时注入桩函数即可，不碰网络。
    day_anchors: day(1-based) → (lat, lon)，由调用方从原行程算好传入，
    作为「附近」搜索的锚点；缺省用全部景点的几何中心。
    """
    spots = [s.model_copy() for s in base_spots]
    anchors = day_anchors or {}
    changes: list[str] = []
    for op in ops:
        kind = op.get("op")
        if kind == "remove":
            name = op.get("name", "")
            before = len(spots)
            spots = [s for s in spots if s.name != name]
            if len(spots) < before:
                changes.append(f"移除「{name}」")
            else:
                changes.append(f"未找到「{name}」，跳过移除")

        elif kind == "add":
            query = op.get("query", "")
            day = op.get("day")
            anchor = anchors.get(day) or _overall_centroid(base_spots)
            cands = (poi_search_fn or (lambda q, a, b: []))(query, *anchor)
            poi = pick_poi(cands)
            if poi is None:
                changes.append(f"没有搜到「{query}」，跳过新增")
                continue
            spots.append(make_spot(poi))
            changes.append(f"新增「{poi['name']}」")

        elif kind == "replace":
            old = op.get("old", "")
            spots = [s for s in spots if s.name != old]
            changes.append(f"移除「{old}」")
            query = op.get("query", "")
            day = op.get("day")
            anchor = anchors.get(day) or _overall_centroid(base_spots)
            cands = (poi_search_fn or (lambda q, a, b: []))(query, *anchor)
            poi = pick_poi(cands)
            if poi is not None:
                spots.append(make_spot(poi))
                changes.append(f"替换为「{poi['name']}」")
            else:
                changes.append(f"没有搜到「{query}」，未完成替换")
    return spots, changes


def _overall_centroid(spots: list[Spot]) -> tuple[float, float]:
    """全部景点的几何中心，作为无 day 信息时的搜索锚点。"""
    if not spots:
        return (34.26, 108.94)
    return (sum(s.lat for s in spots) / len(spots),
            sum(s.lon for s in spots) / len(spots))

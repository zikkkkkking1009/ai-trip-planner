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
ops 支持五种操作：
- remove:   {"op":"remove","name":"行程中准确的景点名"}
- add:      {"op":"add","query":"POI搜索关键词","day":天数或null,"name":"可省略"}
- replace:  {"op":"replace","old":"要移除的景点名","query":"搜索关键词","day":天数或null}
- pin_add:  {"op":"pin_add","name":"用户想加的地点名","query":"搜索词","day":天数,"after":"插到该景点之后或null表示路线中间"}
- hotel:    {"op":"hotel","name":"酒店名","query":"搜索词"}
规则：
- remove 的 name 必须逐字取自当前行程，不要改写
- add/replace 必须给 query（用于地图搜索），如"咖啡馆""美食街""博物馆"
- 只要用户指令里出现"酒店/民宿/旅馆/住"等住宿意图（如"加个酒店""我想住未央区"
  "换个酒店"），一律用 hotel 操作——酒店是每天的起点和终点，影响所有天的通勤，
  不是某一天的行程条目；query 支持"区域 + 类型"（如"未央区 酒店"）
- 用户要往某天游览路线里加自定义地点（如歇脚的咖啡馆）并指定位置时用 pin_add：
  name 取地点名，query 用于地图搜索，after 逐字取自该天行程里的景点名，
  用户说"中间"传 null
- 用户没说清楚具体地点时，ops 给空数组，在 reply 里反问地点名
- 可以一次给多个 ops；无法理解时输出 {"ops":[],"reply":"没听懂，试试：把XX换成XX / 住未央区的酒店"}"""


def parse_instruction(instruction: str, plan_summary: str,
                      history: list[dict] | None = None) -> dict:
    """调 LLM 把自然语言指令解析成结构化操作。需要 LLM_API_KEY。

    history：之前几轮对话 [{q, ops, reply}]——支撑「换到西安站」这类
    指代之前内容的多轮指令。
    返回 {"ops": [...], "reply": "...", "_raw": 模型原始输出(截断)}。
    """
    from extractor import _tolerant_json_parse
    from openai import OpenAI

    api_key = os.environ.get("LLM_API_KEY")
    base_url = os.environ.get("LLM_BASE_URL")
    if not api_key or not base_url:
        raise RuntimeError("未配置 LLM_API_KEY，无法解析编辑指令")
    hist_txt = ""
    for h in (history or [])[-6:]:
        hist_txt += (f"用户：{h.get('q', '')}\n"
                     f"助手：{h.get('reply', '')}"
                     f"（操作：{json.dumps(h.get('ops', []), ensure_ascii=False)}）\n")
    user_content = (f"当前行程：\n{plan_summary}\n\n"
                    + (f"之前的对话记录：\n{hist_txt}\n" if hist_txt else "")
                    + f"用户指令：{instruction}")
    client = OpenAI(api_key=api_key, base_url=base_url)
    resp = client.chat.completions.create(
        model=os.environ.get("LLM_MODEL_ID", "deepseek-chat"),
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_content},
        ],
        temperature=0.1,
    )
    raw = resp.choices[0].message.content or ""
    try:
        parsed = _tolerant_json_parse(raw)
    except Exception:
        parsed = {"ops": [], "reply": "没听懂，换个说法试试"}
    parsed["_raw"] = raw[:300]
    return parsed


# ---- 高德周边搜索（加点用）----
def poi_search(query: str, lat: float, lon: float,
               radius: int = 5000) -> list[dict]:
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
        photos = p.get("photos") or []
        out.append({"name": p.get("name", ""), "lat": lat2, "lon": lon,
                    "type_str": p.get("type", ""),
                    "image": (photos[0].get("url", "") if photos else "")})
    return out


def text_search(query: str, city: str = "西安") -> list[dict]:
    """高德全城文本搜索（周边搜不到时的降级），返回结构与 poi_search 一致。"""
    env = load_env_file()
    key = env.get("AMAP_KEY") or os.environ.get("AMAP_KEY")
    if not key:
        return []
    params = urllib.parse.urlencode({
        "keywords": query, "city": city, "citylimit": "true",
        "offset": 5, "page": 1, "key": key,
    })
    url = f"https://restapi.amap.com/v3/place/text?{params}"
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
        photos = p.get("photos") or []
        out.append({"name": p.get("name", ""), "lat": lat2, "lon": lon,
                    "type_str": p.get("type", ""),
                    "image": (photos[0].get("url", "") if photos else "")})
    return out


def pick_poi(candidates: list[dict]) -> dict | None:
    """从周边搜索结果里选第一个干净类型的 POI（过滤公交站等干扰）。"""
    for c in candidates:
        if type_flag(c.get("type_str", "")) >= 0:
            return c
    return candidates[0] if candidates else None


def make_spot(poi: dict, stay_min: int = 60) -> Spot:
    """POI → 求解器可用的 Spot。

    score 给到最高档：用户点名要加的地点是「必去项」，
    求解器必须优先安置（否则会被当低分景点第一个放弃）。
    """
    return Spot(source_id=0, name=poi["name"], lat=poi["lat"], lon=poi["lon"],
                stay_min=stay_min, score=9.9, ticket=0, open_h=9.0, close_h=22.0,
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


# ---- 定点修改：在某天的序列里插入/删除，重算该天时间线 ----
def pin_modify_day(day_spots: list[Spot],
                   inserts: list[Spot],
                   remove_names: list[str],
                   after: str | None,
                   daily_start_h: float,
                   daily_end_h: float,
                   commute_fn=None) -> dict | None:
    """在一天内的序列里删/插景点后重算时间线（不全局重排，保住其他天的安排）。

    after=None 时插在路线中间（「路线中间加酒店」）。
    commute_fn 必须传基准求解所用的同一函数——口径不一致会把可行判成不可行。
    时间窗不可行时返回 None，由调用方决定是否降级全局重排。
    """
    from models import VisitedSpot, PlanRequest
    from solver import _Seq

    seq = [s for s in day_spots if s.name not in remove_names]
    for ns in inserts:
        idx = len(seq)
        if after:
            for i, s in enumerate(seq):
                if s.name == after:
                    idx = i + 1
                    break
        else:
            idx = len(seq) // 2  # 中点：3个景点 → 插在第2位
        seq.insert(idx, ns)

    req = PlanRequest(city="pin", days=1, spots=seq,
                      daily_start_h=daily_start_h, daily_end_h=daily_end_h)
    s = _Seq(spots=seq, commute_fn=commute_fn) if commute_fn else _Seq(spots=seq)
    tl = s.timeline(req)
    if tl is None:
        return None  # 插入后违反时间窗 → 让调用方降级

    vspots = [VisitedSpot(name=sp.name, arrive_h=round(a, 2),
                          depart_h=round(d, 2), ticket=sp.ticket,
                          desc=sp.desc)
              for sp, a, d in tl]
    return {
        "spots": vspots,
        "commute_min": round(s.commute_total(req), 1),
        "cost": round(sum(sp.ticket for sp in seq), 1),
        "active_min": round(sum(sp.stay_min for sp in seq), 0),
    }


def pin_insert_best(day_spots: list[Spot],
                    new_spot: Spot,
                    after: str | None,
                    daily_start_h: float,
                    daily_end_h: float,
                    commute_fn=None,
                    stay_options: tuple[int, ...] = (60, 30),
                    hotel=None) -> dict | None:
    """定点插入的智能版：扫描所有可插入位置 × 停留时长，选通勤最小的可行方案。

    after 给定时只考虑其后位置；否则全位置扫描（「路线中间加酒店」）。
    停留时长按 stay_options 依次尝试（60 装不下就 30）。
    全部不可行返回 None。
    """
    from models import VisitedSpot, PlanRequest
    from solver import _Seq

    def evaluate(seq: list[Spot]):
        req = PlanRequest(city="pin", days=1, spots=seq,
                          daily_start_h=daily_start_h,
                          daily_end_h=daily_end_h, hotel=hotel)
        s = _Seq(spots=seq, commute_fn=commute_fn) if commute_fn \
            else _Seq(spots=seq)
        tl = s.timeline(req, hotel)
        if tl is None:
            return None
        return s, tl, req

    base_idx = len(day_spots)
    if after:
        for i, s in enumerate(day_spots):
            if s.name == after:
                base_idx = i + 1
                break
        positions = [base_idx]
    else:
        positions = list(range(len(day_spots) + 1))

    best = None
    for stay in stay_options:
        ns = new_spot.model_copy(update={"stay_min": stay})
        for pos in positions:
            seq = day_spots[:pos] + [ns] + day_spots[pos:]
            ev = evaluate(seq)
            if ev is None:
                continue
            s, tl, req = ev
            comm = s.commute_total(req)
            if best is None or comm < best[0]:
                best = (comm, seq, tl, stay)
        if best is not None:
            break  # 当前停留时长已有可行解，不再压缩

    if best is None:
        return None

    _, seq, tl, stay = best
    vspots = [VisitedSpot(name=sp.name, arrive_h=round(a, 2),
                          depart_h=round(d, 2), ticket=sp.ticket,
                          desc=sp.desc)
              for sp, a, d in tl]
    return {
        "spots": vspots,
        "commute_min": round(sum(
            (commute_fn(seq[i], seq[i + 1]) if commute_fn
             else _overall_est(seq[i], seq[i + 1]))
            for i in range(len(seq) - 1)), 1),
        "cost": round(sum(sp.ticket for sp in seq), 1),
        "active_min": round(sum(sp.stay_min for sp in seq), 0),
        "stay_min": stay,
    }


def _overall_est(a: Spot, b: Spot) -> float:
    from solver import commute_min
    return commute_min(a, b)


def generate_reviews(name: str, intro: str = "") -> dict | None:
    """生成景点评价摘要（3 好评 + 3 避雷）+ 长介绍。LLM 生成，调用方缓存。

    高德不开放评论 API，页面上的「真实评价」由 LLM 生成（前端标注 AI 生成，
    与圆周旅迹同类功能做法一致）。无 LLM Key 时返回 None，前端隐藏该区块。
    """
    from extractor import _tolerant_json_parse
    from openai import OpenAI

    api_key = os.environ.get("LLM_API_KEY")
    base_url = os.environ.get("LLM_BASE_URL")
    if not api_key or not base_url:
        return None
    client = OpenAI(api_key=api_key, base_url=base_url)
    resp = client.chat.completions.create(
        model=os.environ.get("LLM_MODEL_ID", "deepseek-chat"),
        messages=[
            {"role": "system", "content": (
                "你是旅游点评生成器。根据景点信息，模仿真实游客的口吻输出 JSON：\n"
                '{"good": ["标题: 一句话", "标题: 一句话", "标题: 一句话"],\n'
                ' "bad": ["标题: 一句话", "标题: 一句话", "标题: 一句话"],\n'
                ' "intro_long": "150字左右的景点介绍段落"}\n'
                "good 是最值得称赞的方面（景色/体验/文化），bad 是避雷提示"
                "（人多/暴晒/禁令/交通），标题 2-4 个字，内容具体不空泛。")},
            {"role": "user", "content": f"景点：{name}\n已知信息：{intro or '无'}"},
        ],
        temperature=0.8,
    )
    try:
        parsed = _tolerant_json_parse(resp.choices[0].message.content or "")
        if "good" in parsed or "bad" in parsed:
            return parsed
    except Exception:
        pass
    return None

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
import logging
import os
import re
import time
import urllib.parse
import urllib.request

from aligner import type_flag
from cities import DEFAULT_CITY
from commute import AMAP_TIMEOUT_S, load_env_file
from models import Spot
from reliability import retry_call

log = logging.getLogger(__name__)

LLM_TIMEOUT_S = 30.0   # 单次 LLM 调用超时（秒）


# ---- LLM 意图解析 ----
_SYSTEM = """你是行程编辑助手。根据当前行程和用户指令，输出 JSON（不要解释）：
{"ops": [...], "reply": "一句话回复用户"}

用户的话有两类意图，先判断是哪一类：
① **修改行程** → 按下面的操作给出 ops
② **提问 / 咨询**（问合不合理、赶不赶、为什么这么排、哪个值得去、有没有问题…）→ ops 给空数组，
   在 reply 里**基于当前行程给出有依据的回答**，不要装傻说"没听懂"

ops 支持五种操作：
- remove:   {"op":"remove","name":"行程中准确的景点名"}
- add:      {"op":"add","query":"POI搜索关键词","day":天数或null,"name":"可省略"}
- replace:  {"op":"replace","old":"要移除的景点名","query":"搜索关键词","day":天数或null}
- pin_add:  {"op":"pin_add","name":"用户想加的地点名","query":"搜索词","day":天数,"after":"插到该景点之后或null表示路线中间"}
- hotel:    {"op":"hotel","name":"酒店名","query":"搜索词"}
- pref:     {"op":"pref","value":"less_walk|save_money|more_spots|balanced"}
  偏好：用户表达"想怎么玩"而不是"改哪个景点"时用。映射：
  · 少走路/不想走太多/轻松点/别太赶/腿要断了 → less_walk（通勤权重提高，会为省通勤放弃远处景点）
  · 省钱/预算紧/便宜点/门票太贵 → save_money（门票计入目标，会少去收费景点）
  · 多玩/安排满一点/难得来一次/想多去几个 → more_spots（每天多玩 1.5 小时）
  · 都行/随便/均衡/默认 → balanced
  一次只给一个 pref；改偏好后行程会**整体重排**，并在 reply 里说清代价（少走路会少去几个、省钱会跳过收费景点）
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
- **回答问题的要求**（这是"有脑子"的关键）：
  · **行程内**的问题（赶不赶、合不合理、为什么这么排）必须引用当前行程里的**具体事实**：
    景点名、第几天、通勤分钟、门票/预算、当天负载；判断"赶不赶"看每天
    「游玩 + 通勤」是否接近时间窗上限（≈540 分钟）
  · **行程外的一般旅游问题也要答**（"兵马俑怎么去""西安必吃啥""带什么衣服"）：
    用你的常识给实用建议，像一个去过当地的朋友那样说话，别推回给用户说"我只管改行程"
  · **但绝不编造具体数字**：票价、营业时间、班次时刻这类容易过期的信息，
    不确定就直说"以官方为准 / 建议现场确认"，**给一个看起来精确的错数字比说不知道更糟**
  · 能给出**可执行的下一步**（例如"如果你想轻松点，我可以把第 2 天的 XX 去掉"）
- 可以一次给多个 ops；确实无法理解时才输出 {"ops":[],"reply":"没听懂，试试：把XX换成XX / 住未央区的酒店"}"""


def parse_instruction(instruction: str, plan_summary: str,
                      history: list[dict] | None = None,
                      memory: list[str] | None = None) -> dict:
    """调 LLM 把自然语言指令解析成结构化操作。需要 LLM_API_KEY。

    history：之前几轮对话 [{q, ops, reply}]——支撑「换到西安站」这类
    指代之前内容的多轮指令。
    memory：**跨会话的长期偏好**（用户说过「记住…」的那些）——由前端持久化后随请求带来。
    它只作为上下文注入，不改变操作语义（比如"不爱爬山"会影响选点建议，但不会凭空删景点）。
    返回 {"ops": [...], "reply": "...", "_raw": 模型原始输出(截断)}。
    """
    from extractor import _tolerant_json_parse

    # 意图解析在交互路径上（用户要等），实测主模型 0.73s 快于免费模型 3.7s，故用主模型
    client, model = _llm(fast=False)
    hist_txt = ""
    for h in (history or [])[-6:]:
        hist_txt += (f"用户：{h.get('q', '')}\n"
                     f"助手：{h.get('reply', '')}"
                     f"（操作：{json.dumps(h.get('ops', []), ensure_ascii=False)}）\n")
    mem_txt = ""
    if memory:
        mem_txt = ("用户的长期偏好（他之前让我记住的，安排时应当考虑；"
                   "但不要为了它擅自删改景点，需要时在 reply 里提一句）：\n"
                   + "\n".join(f"- {m}" for m in memory[:10]) + "\n\n")
    user_content = (f"当前行程：\n{plan_summary}\n\n" + mem_txt
                    + (f"之前的对话记录：\n{hist_txt}\n" if hist_txt else "")
                    + f"用户指令：{instruction}")
    _t0 = time.time()
    resp = retry_call(lambda: client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_content},
        ],
        temperature=0.1,
    ), what="意图解析 LLM 调用")
    log.info("意图解析完成：%.2fs（模型 %s，历史 %d 轮）",
             time.time() - _t0, model, len(history or []))
    raw = resp.choices[0].message.content or ""
    try:
        parsed = _tolerant_json_parse(raw)
    except Exception as e:
        # 用户可见的失败（会回「没听懂」），必须留痕便于排查提示词/模型问题
        log.warning("意图解析失败，回退为「没听懂」: %s: %s | raw=%r",
                    type(e).__name__, e, raw[:120])
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

    def once() -> dict:
        wait = 0.35 - (time.time() - getattr(poi_search, "_last", 0))
        if wait > 0:
            time.sleep(wait)
        poi_search._last = time.time()
        with urllib.request.urlopen(url, timeout=AMAP_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        # 与通勤/LLM 保持一致：先重试（网络抖动），最终失败才降级——且降级要留痕
        data = retry_call(once, what=f"高德周边搜索({query})")
    except Exception as e:
        log.warning("周边搜索失败 query=%s: %s: %s", query, type(e).__name__, e)
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


def text_search(query: str, city: str = DEFAULT_CITY) -> list[dict]:
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

    def once() -> dict:
        wait = 0.35 - (time.time() - getattr(poi_search, "_last", 0))
        if wait > 0:
            time.sleep(wait)
        poi_search._last = time.time()
        with urllib.request.urlopen(url, timeout=AMAP_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        data = retry_call(once, what=f"高德文本搜索({query}@{city})")
    except Exception as e:
        log.warning("文本搜索失败 query=%s city=%s: %s: %s",
                    query, city, type(e).__name__, e)
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
        if kind == "pref":
            # 偏好不改景点列表，只记一条说明——真正的切换由上层（run_edit_task）
            # 写回请求参数后重排。放在这里处理是为了让 changes 里能看到它。
            changes.append(f"偏好切换为「{op.get('value', '')}」，行程已按新偏好重排")
            continue
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

    try:
        client, model = _llm(fast=True)   # 轻任务走快模型通道
    except RuntimeError:
        # 未配置 LLM Key → 跳过评价生成（预期降级，前端有「暂无评价」占位）
        return None
    _t0 = time.time()
    resp = retry_call(lambda: client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": (
                "你是旅游点评生成器。根据景点信息，模仿真实游客口吻输出**紧凑**的 JSON：\n"
                '{"good": ["标题: 一句话", "标题: 一句话", "标题: 一句话", "标题: 一句话"],\n'
                ' "bad": ["标题: 一句话", "标题: 一句话", "标题: 一句话", "标题: 一句话"],\n'
                ' "intro_long": "100字左右的介绍"}\n'
                "标题 2-4 字，每条一句话（不超过 30 字），good 是亮点，bad 是避雷。")},
            {"role": "user", "content": f"景点：{name}\n已知信息：{intro or '无'}"},
        ],
        temperature=0.8,
    ), what=f"评价生成 LLM 调用（{name}）")
    log.info("评价生成完成：%.2fs（模型 %s，%s）", time.time() - _t0, model, name)
    try:
        parsed = _tolerant_json_parse(resp.choices[0].message.content or "")
        if "good" in parsed or "bad" in parsed:
            # 清洗：部分模型会照抄提示词里的「标题:」前缀
            for k in ("good", "bad"):
                parsed[k] = [re.sub(r"^\s*标题\s*[:：]\s*", "", str(t))
                             for t in (parsed.get(k) or [])]
            return parsed
    except Exception as e:
        log.warning("评价解析失败，降级为无评价 name=%s: %s: %s",
                    name, type(e).__name__, e)
    return None

def _llm(fast: bool = False):
    """返回 (OpenAI client, model)。

    fast=True 的轻任务（评价生成/意图解析）优先用 LLM_FAST_* 配置——
    可以指向更快的模型或另一家供应商（如 GLM-4-Flash / qwen-turbo），
    没配就回落主配置。重任务（攻略抽取）始终用主模型。
    """
    from openai import OpenAI
    if fast:
        api_key = os.environ.get("LLM_FAST_API_KEY") or os.environ.get("LLM_API_KEY")
        base_url = os.environ.get("LLM_FAST_BASE_URL") or os.environ.get("LLM_BASE_URL")
        model = os.environ.get("LLM_FAST_MODEL") or os.environ.get("LLM_MODEL_ID", "deepseek-chat")
    else:
        api_key = os.environ.get("LLM_API_KEY")
        base_url = os.environ.get("LLM_BASE_URL")
        model = os.environ.get("LLM_MODEL_ID", "deepseek-chat")
    if not api_key or not base_url:
        raise RuntimeError("未配置 LLM_API_KEY")
    return OpenAI(api_key=api_key, base_url=base_url, timeout=LLM_TIMEOUT_S), model

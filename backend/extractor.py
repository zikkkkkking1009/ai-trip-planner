"""LLM 抽取模块：自然语言攻略 → 结构化景点列表。

设计（对应调研报告的「LLM 只做理解，排期交给求解器」）：
1. 调 OpenAI 兼容接口（DeepSeek/Qwen/豆包均可，走 .env 配置）
2. 强制 JSON 输出（response_format），带容错解析
3. 没配 Key 或调用失败 → 抛出明确异常，由调用方降级（如让用户手工填表）
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

from cities import DEFAULT_CITY, city_center, normalize_city
from models import Spot
from reliability import retry_call
import llm_ledger

LLM_TIMEOUT_S = 30.0   # 单次 LLM 调用超时（秒），失败由 reliability 重试

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是旅游信息抽取器。从用户给的攻略文本中抽取城市与所有景点，
只输出 JSON，不要解释。格式：
{"city": "攻略所属城市名（如「西安」「成都」；无法判断时空字符串）",
 "spots": [{"name": "景点名", "stay_min": 建议停留分钟数, "rating": 评分0-10,
            "ticket": 门票元, "note": "一句话亮点"}]}
规则：
- city 只填城市名本身，不要带「市」以外的后缀，也不要填省份
- 只抽确定的景点，不要编造
- stay_min 按攻略描述估计，没有描述给默认 90
- rating 按文中语气估计，没提给 7.0
"""

_EXAMPLE = {"city": "西安",
            "spots": [{"name": "兵马俑", "stay_min": 180, "rating": 9.5,
                       "ticket": 120, "note": "世界第八大奇迹"}]}


def _tolerant_json_parse(text: str) -> dict:
    """LLM JSON 输出的容错解析（参考 TripStar 的做法）。

    顺序：剥 markdown 围栏 → 提取最外层花括号 → 修尾逗号 → 最后才报错。
    失败一律抛 ValueError（JSONDecodeError 是其子类），由调用方各自降级。
    """
    text = re.sub(r"```(json)?|```", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"LLM 输出中找不到 JSON：{text[:120]!r}")
    raw = text[start:end + 1]
    try:
        _loaded: dict = json.loads(raw)
        return _loaded
    except json.JSONDecodeError:
        cleaned = re.sub(r",\s*([}\]])", r"\1", raw)  # 去尾逗号
        _loaded = json.loads(cleaned)
        return _loaded


def _num(v: object, default: float) -> float:
    """LLM 数值字段容错：null / 非数字不该让整份攻略报废（缺了用默认值）。"""
    if v is None or isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except ValueError:
        return default


def extract_guide(text: str, city_hint: str = "") -> tuple[list[Spot], str]:
    """抽取景点 + 识别城市。返回 (spots, city)。

    城市来源优先级：LLM 识别 > city_hint（前端已选的城市）。
    **city 为空字符串表示未能识别**——调用方必须提示用户选择，
    不要静默假设某个城市（旧版固定用西安坐标，导致「粘成都攻略得到西安坐标」的静默错误）。

    兜底坐标取「该城市中心」（`cities.city_center`），若城市仍未知则退到默认城市中心，
    但此时返回的 city 为空，上层据此提示用户。
    """
    from openai import OpenAI  # 延迟导入：不装 openai 也不影响求解器 demo

    api_key = os.environ.get("LLM_API_KEY")
    base_url = os.environ.get("LLM_BASE_URL")
    if not api_key or not base_url:
        raise RuntimeError(
            "未配置 LLM_API_KEY / LLM_BASE_URL，无法做文本抽取。"
            "请先在 .env 中配置，或直接使用手工输入的景点列表。")

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=LLM_TIMEOUT_S)
    model_id = os.environ.get("LLM_MODEL_ID", "deepseek-chat")
    t0 = time.time()
    resp = retry_call(lambda: client.chat.completions.create(
        model=model_id,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"攻略文本：\n{text}\n\n参考格式：{json.dumps(_EXAMPLE, ensure_ascii=False)}"},
        ],
        temperature=0.1,
    ), what="攻略抽取 LLM 调用")
    llm_ledger.record("extract", model=model_id, resp=resp, t0=t0)
    log.info("攻略抽取完成：%.2fs，输入 %d 字，输出 %d 字", time.time() - t0, len(text),
             len(resp.choices[0].message.content or ""))
    data = _tolerant_json_parse(resp.choices[0].message.content or "")

    city = normalize_city(data.get("city")) or normalize_city(city_hint)
    # 兜底坐标：城市中心（而不是写死的西安坐标，更不能硬编码数字）
    center = city_center(city) or city_center(DEFAULT_CITY)
    if center is None:
        # 启动期配置错误：默认城市必须在城市表里，否则兜底坐标无处可取
        raise RuntimeError(f"城市表缺少默认城市 {DEFAULT_CITY}，请检查 backend/cities.py")
    if not city:
        log.warning("抽取未能识别城市（city_hint=%r），兜底坐标取 %s；上层应提示用户选择城市",
                    city_hint, DEFAULT_CITY)

    raw_spots = data.get("spots", [])
    if not isinstance(raw_spots, list):
        # spots 给成字符串/对象时逐字符迭代毫无意义，按空处理走「没有抽到」降级
        log.warning("攻略抽取：spots 字段不是数组（%s），按空处理",
                    type(raw_spots).__name__)
        raw_spots = []
    spots: list[Spot] = []
    for i, s in enumerate(raw_spots):
        if not isinstance(s, dict) or not s.get("name"):
            # 单条坏数据跳过并留痕，不该让一个字段报废整份攻略
            log.warning("攻略抽取：第 %d 条景点格式异常，已跳过: %r", i + 1, s)
            continue
        ticket = _num(s.get("ticket"), 0)
        spots.append(Spot(
            source_id=len(spots) + 1,
            name=str(s["name"]),
            lat=_num(s.get("lat"), center[0]),   # 占位：由 aligner 对齐后替换为真实 POI 坐标
            lon=_num(s.get("lon"), center[1]),
            # stay_min 卡进 Spot 的边界（ge=30, le=480）：LLM 给 10 分钟不该让
            # 整份攻略 422，收到边界值即可
            stay_min=int(min(480, max(30, _num(s.get("stay_min"), 90)))),
            score=_num(s.get("rating"), 7.0),
            ticket=ticket,
            # 攻略里没写票价时 LLM 一律填 0，这跟"该景点免费"是两回事。
            # 所以 0 先记成**未知**，后面由 demo_data / 对齐结果回填；
            # 回填不到就保持未知 —— 前端据此显示"票价待查"而不是谎称 ¥0。
            ticket_known=ticket > 0,
            desc=str(s.get("note", "")),
        ))
    if not spots:
        raise ValueError("LLM 没有抽到任何景点")
    return spots, city


def extract_spots(text: str, city_hint: str = "") -> list[Spot]:
    """兼容旧签名的薄封装：只要景点列表（bench_models / 单元测试在用）。"""
    spots, _ = extract_guide(text, city_hint)
    return spots

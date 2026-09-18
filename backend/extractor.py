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

from models import Spot
from reliability import retry_call

LLM_TIMEOUT_S = 30.0   # 单次 LLM 调用超时（秒），失败由 reliability 重试

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是旅游信息抽取器。从用户给的攻略文本中抽取所有景点，
只输出 JSON，不要解释。格式：
{"spots": [{"name": "景点名", "stay_min": 建议停留分钟数, "rating": 评分0-10,
            "ticket": 门票元, "note": "一句话亮点"}]}
规则：
- stay_min 按攻略描述估计，没有描述给默认 90
- rating 按文中语气估计，没提给 7.0
- 只抽确定的景点，不要编造
"""

_EXAMPLE = {"spots": [{"name": "兵马俑", "stay_min": 180, "rating": 9.5,
                       "ticket": 120, "note": "世界第八大奇迹"}]}


def _tolerant_json_parse(text: str) -> dict:
    """LLM JSON 输出的容错解析（参考 TripStar 的做法）。

    顺序：剥 markdown 围栏 → 提取最外层花括号 → 修尾逗号 → 最后才报错。
    """
    text = re.sub(r"```(json)?|```", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"LLM 输出中找不到 JSON：{text[:120]!r}")
    raw = text[start:end + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        cleaned = re.sub(r",\s*([}\]])", r"\1", raw)  # 去尾逗号
        return json.loads(cleaned)


def extract_spots(text: str) -> list[Spot]:
    """调 LLM 抽取景点。需要环境变量：LLM_API_KEY / LLM_BASE_URL / LLM_MODEL_ID。"""
    from openai import OpenAI  # 延迟导入：不装 openai 也不影响求解器 demo

    api_key = os.environ.get("LLM_API_KEY")
    base_url = os.environ.get("LLM_BASE_URL")
    if not api_key or not base_url:
        raise RuntimeError(
            "未配置 LLM_API_KEY / LLM_BASE_URL，无法做文本抽取。"
            "请先在 .env 中配置，或直接使用手工输入的景点列表。")

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=LLM_TIMEOUT_S)
    t0 = time.time()
    resp = retry_call(lambda: client.chat.completions.create(
        model=os.environ.get("LLM_MODEL_ID", "deepseek-chat"),
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"攻略文本：\n{text}\n\n参考格式：{json.dumps(_EXAMPLE, ensure_ascii=False)}"},
        ],
        temperature=0.1,
    ), what="攻略抽取 LLM 调用")
    log.info("攻略抽取完成：%.2fs，输入 %d 字，输出 %d 字", time.time() - t0, len(text),
             len(resp.choices[0].message.content or ""))
    data = _tolerant_json_parse(resp.choices[0].message.content or "")

    spots: list[Spot] = []
    for i, s in enumerate(data.get("spots", [])):
        spots.append(Spot(
            source_id=i + 1,
            name=str(s["name"]),
            lat=float(s.get("lat", 34.26)),   # TODO: 高德地理编码 API 校正坐标
            lon=float(s.get("lon", 108.94)),
            stay_min=int(s.get("stay_min", 90)),
            score=float(s.get("rating", 7.0)),
            ticket=float(s.get("ticket", 0)),
            desc=str(s.get("note", "")),
        ))
    if not spots:
        raise ValueError("LLM 没有抽到任何景点")
    return spots

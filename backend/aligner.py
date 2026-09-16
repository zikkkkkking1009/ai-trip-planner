"""地理实体对齐管线：攻略别名 → 高德标准 POI。

对应《含金量提升方案》硬菜二。解决的问题是：
攻略里写「紫禁城」「兵马俑」「陕历博」，高德 POI 库里叫
「故宫博物院」「秦始皇兵马俑博物馆」「陕西历史博物馆」——
名字对不上，坐标就是错的，排期全错。

三步走（参考实体链接 Entity Linking 的标准流程）：
1. 候选召回：高德文本搜索 Top-K（带文件缓存 + 限频，复用通勤模块的教训）
2. 打分融合：containment（包含关系）+ 文本相似度（difflib + 字符 bigram Jaccard）
   + 类型匹配。地理邻近保留接口（LLM 抽取阶段还拿不到可信坐标）
3. 阈值分流：置信度 >= AUTO_THRESHOLD 自动采纳；否则标记 needs_review，
   前端让用户点选——这正是「用户修正返回链路」的兜底设计

评估：eval_aligner.py + eval_set.json，输出 Precision / Recall / F1。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from commute import load_env_file

BACKEND_DIR = Path(__file__).parent
POI_CACHE_FILE = BACKEND_DIR / ".poi_cache.json"
POI_CACHE_TTL_SEC = 30 * 24 * 3600  # POI 数据变化慢，缓存 30 天

AUTO_THRESHOLD = 0.72   # >= 此置信度自动采纳，否则转人工确认
TOP_K = 5               # 召回候选数

# 打分权重（可以在标注集上调参——这本身就是简历素材）
W_CONTAIN = 0.55   # 包含关系：别名是候选名的子串（或反过来），最强信号
W_TEXT = 0.30      # 字符相似度
W_TYPE = 0.15      # 类型先验：景点类 +1，交通/行政区/生活服务类 -1
W_EXT = 0.15       # 景点后缀扩展：「碑林博物馆」>「碑林湖」
GEO_WEIGHT_RESERVED = 0.20  # 地理邻近（预留：拿到粗坐标后启用并重新归一化）

# 类型先验词表（来自高德 type 字段的行业分类）
GOOD_TYPE_KEYWORDS = ["风景名胜", "旅游景点", "博物馆", "纪念馆", "文物",
                      "公园", "寺庙", "教堂", "科教文化", "名人故居", "遗产"]
BAD_TYPE_KEYWORDS = ["公交站", "地铁站", "车站", "停车场", "行政区划",
                     "地名地址", "足道", "按摩", "美发", "餐饮", "购物",
                     "酒店", "生活服务", "公司", "小区", "住宅", "厂"]


# 景点扩展后缀：「碑林」→「碑林博物馆」比「碑林湖」更像用户想去的地方
# 注意：不要放「广场」——会抬升奥莱/商场类 POI（真实踩过的坑）
EXT_KEYWORDS = ["博物馆", "纪念馆", "景区", "公园", "遗址", "陵",
                "寺", "塔", "故居", "城墙"]


def extension_bonus(alias: str, candidate: str) -> float:
    """候选名 = 别名 + 景点类后缀时加分（alias 紧邻位置向后看）。"""
    a, b = _normalize(alias), _normalize(candidate)
    if a and a != b and a in b:
        rest = b[b.index(a) + len(a):]
        if any(kw in rest for kw in EXT_KEYWORDS):
            return W_EXT
    return 0.0


def type_flag(type_str: str) -> int:
    """+1 景点类 / -1 干扰类（公交站、行政区、足疗店…）/ 0 中性。"""
    for kw in GOOD_TYPE_KEYWORDS:
        if kw in type_str:
            return 1
    for kw in BAD_TYPE_KEYWORDS:
        if kw in type_str:
            return -1
    return 0


def _normalize(name: str) -> str:
    """去空格/标点/括号内容，统一小写。"""
    name = re.sub(r"[（(【\[].*?[)）\]】]", "", name)
    return re.sub(r"[\s·、，,。.\-—()（）]+", "", name).lower()


def _char_bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def text_similarity(alias: str, candidate: str) -> float:
    """0~1 的字符相似度：difflib 序率 与 bigram Jaccard 取平均。"""
    a, b = _normalize(alias), _normalize(candidate)
    if not a or not b:
        return 0.0
    difflib_sim = SequenceMatcher(None, a, b).ratio()
    ba, bb = _char_bigrams(a), _char_bigrams(b)
    jaccard = len(ba & bb) / len(ba | bb) if ba | bb else 0.0
    return (difflib_sim + jaccard) / 2


def containment(alias: str, candidate: str) -> float:
    """包含关系打分：完全匹配 1.0，单向包含 0.85（略降以区分完全相等）。"""
    a, b = _normalize(alias), _normalize(candidate)
    if a == b:
        return 1.0
    if a and (a in b or b in a):
        return 0.85
    return 0.0


@dataclass
class PoiCandidate:
    name: str
    lat: float
    lon: float
    type_str: str = ""
    address: str = ""
    score: float = 0.0


@dataclass
class AlignResult:
    alias: str
    best: PoiCandidate | None
    candidates: list[PoiCandidate] = field(default_factory=list)
    confidence: float = 0.0
    needs_review: bool = False
    reason: str = ""          # "no_candidate" / "low_confidence" / "ok"


class POIAligner:
    """别名 → 高德 POI 对齐器。"""

    def __init__(self, amap_key: str | None = None):
        env = load_env_file()
        self.key = amap_key or env.get("AMAP_KEY") or os.environ.get("AMAP_KEY")
        self.cache: dict[str, dict] = {}
        self._last_call = 0.0
        self.stats = {"api_calls": 0, "cache_hits": 0, "fallbacks": 0}
        if POI_CACHE_FILE.exists():
            try:
                self.cache = json.loads(POI_CACHE_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.cache = {}

    # ---- 高德文本搜索（缓存 + 限频，和通勤模块同一套纪律）----
    def _search_pois(self, alias: str, city: str) -> list[PoiCandidate]:
        ck = f"{city}|{alias}"
        hit = self.cache.get(ck)
        if hit and time.time() - hit["ts"] < POI_CACHE_TTL_SEC:
            self.stats["cache_hits"] += 1
            return [PoiCandidate(**p) for p in hit["pois"]]

        params = urllib.parse.urlencode({
            "keywords": alias, "city": city, "citylimit": "true",
            "offset": TOP_K, "page": 1, "key": self.key,
        })
        url = f"https://restapi.amap.com/v3/place/text?{params}"
        wait = 0.35 - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self.stats["api_calls"] += 1
        if data.get("status") != "1":
            self.stats["fallbacks"] += 1
            raise RuntimeError(f"高德 POI 搜索异常: {data.get('info')}")

        pois: list[PoiCandidate] = []
        for p in data.get("pois", []):
            try:
                lon, lat = map(float, p["location"].split(","))
            except (KeyError, ValueError):
                continue
            pois.append(PoiCandidate(
                name=p.get("name", ""), lat=lat, lon=lon,
                type_str=p.get("type", ""), address=p.get("address", "")))
        self.cache[ck] = {
            "ts": time.time(),
            "pois": [vars(p) | {} for p in pois],
        }
        POI_CACHE_FILE.write_text(
            json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
        return pois

    # ---- 打分融合 ----
    @staticmethod
    def _score(alias: str, cand: PoiCandidate, hint_type: str | None) -> float:
        """score = 0.55*包含 + 0.30*文本相似 + 0.15*类型先验 + 0.10*后缀扩展。"""
        if hint_type:
            tf = 1 if (hint_type in cand.type_str
                       or cand.type_str in hint_type) else type_flag(cand.type_str)
        else:
            tf = type_flag(cand.type_str)
        return W_CONTAIN * containment(alias, cand.name) \
            + W_TEXT * text_similarity(alias, cand.name) \
            + W_TYPE * tf \
            + extension_bonus(alias, cand.name)

    # ---- 对外主入口 ----
    def align(self, alias: str, city: str,
              hint_type: str | None = None) -> AlignResult:
        """把一个别名对齐到标准 POI。低置信度返回 needs_review=True。"""
        alias = alias.strip()
        if not alias:
            return AlignResult(alias=alias, best=None, reason="no_candidate",
                               needs_review=True)
        try:
            pois = self._search_pois(alias, city)
        except Exception:
            pois = []
        if not pois:
            return AlignResult(alias=alias, best=None, reason="no_candidate",
                               needs_review=True)

        for p in pois:
            p.score = round(self._score(alias, p, hint_type), 4)

        # 干扰类型硬过滤：有干净候选时，公交站/行政区/足疗店直接不参选
        clean = [p for p in pois if type_flag(p.type_str) >= 0]
        pool = clean if clean else pois
        pool.sort(key=lambda p: -p.score)
        best = pool[0]
        review = best.score < AUTO_THRESHOLD
        return AlignResult(
            alias=alias, best=best, candidates=pool,
            confidence=best.score,
            needs_review=review,
            reason="ok" if not review else "low_confidence",
        )


def align_spot(spot, aligner: POIAligner, city: str):
    """把 extractor 产出的 Spot 的占位坐标替换成高德真实坐标。

    返回 (spot, align_result)；needs_review 时不覆盖坐标，交给上游处理。
    """
    r = aligner.align(spot.name, city)
    if r.best and not r.needs_review:
        spot.lat, spot.lon = r.best.lat, r.best.lon
        spot.name = r.best.name  # 统一成标准名，方便地图渲染与通勤查询
    return spot, r

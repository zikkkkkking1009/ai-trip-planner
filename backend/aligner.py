"""地理实体对齐管线：攻略别名 → 高德标准 POI。

解决的问题是：攻略里写「紫禁城」「兵马俑」「陕历博」，高德 POI 库里叫
「故宫博物院」「秦始皇兵马俑博物馆」「陕西历史博物馆」——
名字对不上，坐标就是错的，排期全错。

三步走（参考实体链接 Entity Linking 的标准流程）：
1. 候选召回：高德文本搜索 Top-K（带文件缓存 + 限频）
2. 打分融合：containment（包含关系）+ 文本相似度（difflib + 字符 bigram Jaccard）
   + 类型先验。地理邻近保留接口（LLM 抽取阶段还拿不到可信坐标）
3. 阈值分流：置信度 >= AUTO_THRESHOLD 自动采纳；否则两道兜底——
   a) LLM 仲裁：让 LLM 在已召回候选中选优（答案必须 ∈ 候选集，防幻觉）；
   b) 仍不确定才标记 needs_review，由用户点选修正

另有两道结构性规则（A7，F1 90%→100% 的关键）：
- 尾部子景点惩罚：「北京国际雕塑公园-远望紫禁城」= 长前缀 + 别名在尾部，
  本体是前缀那个 POI，与别名所指实体无关 → containment/text 相似度归零
- 主名权威性先验：搜「华清池」时老名 POI 仍是精确匹配，但「华清宫-杨妃池」
  等子点的地址里引用了「华清池」——证明该景区已更名 → 用主名「华清宫」重查

评估：eval_aligner.py + eval_set.json，输出 Precision / Recall / F1。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from commute import load_env_file
from reliability import retry_call

log = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).parent
POI_CACHE_FILE = BACKEND_DIR / ".poi_cache.json"
POI_CACHE_TTL_SEC = 30 * 24 * 3600  # POI 数据变化慢，缓存 30 天
ARB_CACHE_FILE = BACKEND_DIR / ".align_arb_cache.json"  # LLM 仲裁结果缓存

POI_TIMEOUT_S = 5.0     # 单次高德 POI 搜索超时（秒），失败由 reliability 重试
AUTO_THRESHOLD = 0.72   # >= 此置信度自动采纳，否则先 LLM 仲裁、再转人工
TOP_K = 5               # 召回候选数

SUFFIX_PREFIX_MIN = 3   # 别名在候选名尾部时，前缀长度 >= 此值判为「子景点引用」
APPENDED_SUFFIX_MAX = 5  # 候选名挂在别名之后的短后缀上限（A8；超过就不像子景点）
RENAMED_ROOT_MIN = 2    # 主名先验：同一根名的「主名-子点」至少出现次数

# 打分权重（在标注集上调参确定，见 eval_aligner.py）
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


def _is_tailed_subvenue(a: str, b: str) -> bool:
    """别名 a 是否为候选名 b 的「长前缀 + 尾部」结构。

    例：「北京国际雕塑公园-远望紫禁城」的辨识主体是前缀（雕塑公园），
    「紫禁城」只是它里面一个观景点的名字——与用户想去的紫禁城无关。
    """
    return len(a) < len(b) and b.endswith(a) and len(b) - len(a) >= SUFFIX_PREFIX_MIN


def _is_appended_subvenue(a: str, b: str) -> bool:
    """候选 b 是否为「查询名 a + 分隔符 + 短后缀」的子景点形式（A8）。

    与 `_is_tailed_subvenue` 对称——那是「别名在尾部」，这是「别名在前、后面挂短后缀」。
    例：搜「成都大熊猫繁育研究基地」→ 库里给「成都大熊猫繁育研究基地-熊猫塔」；
        搜「杜甫草堂」→「杜甫草堂-杜陵村」。
    这类候选几乎是"景区里的一个小点"，用户要的是主景区本身。

    **为什么必须要求分隔符 + 限制后缀长度**（否则会误伤真正的上级 POI）：
    「故宫」→「故宫博物院」、「大雁塔」→「大雁塔文化休闲景区」都是**合法扩展名**
    （无分隔符、或后缀很长），不该被当成子景点。只有「主名-短后缀」才是子景点。
    实测把这两条约束去掉会让「故宫/大雁塔」这类正常对齐被误判为子点。
    """
    if not a or not b or b == a or not b.startswith(a):
        return False
    rest = b[len(a):]
    for sep in ("-", "－", "—", "(", "（", "·"):
        if rest.startswith(sep):
            suffix = rest[len(sep):].strip(" )）(（")
            return 0 < len(suffix) <= APPENDED_SUFFIX_MAX
    return False


def text_similarity(alias: str, candidate: str) -> float:
    """0~1 的字符相似度：difflib 序率 与 bigram Jaccard 取平均。

    两种子景点结构都直接记 0（字符重叠是假信号）：
    - 尾部子景点（`_is_tailed_subvenue`）：候选的辨识部分是前缀
    - 挂在后面的子景点（`_is_appended_subvenue`）：候选的辨识部分是那个短后缀
    """
    a, b = _normalize(alias), _normalize(candidate)
    # 子景点结构判定必须用**原始名**：归一化会把分隔符（- / （ / ·）抹掉，
    # 而"挂后缀"的判据恰恰依赖分隔符（踩过：传归一化名 → 判定永远为 False，
    # 子景点又拿到 0.85 的高分）
    if not a or not b or _is_tailed_subvenue(alias, candidate) \
            or _is_appended_subvenue(alias, candidate):
        return 0.0
    difflib_sim = SequenceMatcher(None, a, b).ratio()
    ba, bb = _char_bigrams(a), _char_bigrams(b)
    jaccard = len(ba & bb) / len(ba | bb) if ba | bb else 0.0
    return (difflib_sim + jaccard) / 2


def containment(alias: str, candidate: str) -> float:
    """包含关系打分：完全匹配 1.0，单向包含 0.85（略降以区分完全相等）。

    例外：两种子景点结构都不视为包含——
    「……远望紫禁城」是别的 POI 在引用别名（尾部结构）；
    「……-熊猫塔」是别名在主景区后面挂了子点（前部结构）。
    这两种情况下 `a in b` 成立但语义相反，给 0.85 会让子景点压过主景区。
    """
    a, b = _normalize(alias), _normalize(candidate)
    if a == b:
        return 1.0
    # 同样用原始名判定子景点结构（归一化会抹掉分隔符）
    if a and (_is_tailed_subvenue(alias, candidate)
              or _is_appended_subvenue(alias, candidate)):
        return 0.0
    if a and (a in b or b in a):
        return 0.85
    return 0.0


def detect_renamed_root(alias: str, pois: list[PoiCandidate]) -> str | None:
    """景区更名检测（主名权威性先验）。

    证据链：搜老名「华清池」时，库里同时有老名 POI 和多个「华清宫-子点」，
    且子点地址引用了老名（「……华清池景区内」）——说明华清池景区的现名
    是华清宫，主名候选应优先于老名精确匹配。
    返回主名原文（如「华清宫」），无证据返回 None。
    """
    a = _normalize(alias)
    roots: dict[str, tuple[str, list[PoiCandidate]]] = {}
    for p in pois:
        if type_flag(p.type_str) < 0:
            continue
        head = re.split(r"[-·—]", p.name)[0].strip()
        r = _normalize(head)
        if not r or r == a or len(r) < 2 or a in r or r in a:
            continue  # 全称包含别名（如「西湖风景名胜区」）是正常匹配，不是更名
        roots.setdefault(r, (head, []))[1].append(p)
    for r, (head, ps) in roots.items():
        if len(ps) >= RENAMED_ROOT_MIN and any(
                a in _normalize(p.address) for p in ps):
            return head
    return None


def _confidence(raw_score: float) -> float:
    """把加权分归一为 0~1 的对外置信度。

    权重合计 W_CONTAIN+W_TEXT+W_TYPE+W_EXT = 1.15，理论上限 1.15，
    直接对外输出会出现「置信度 1.02」这种不合理值。内部阈值判断仍用加权分
    （AUTO_THRESHOLD=0.72 是在该尺度上调参的），只在对外输出时截断到 [0,1]。
    """
    return round(min(max(raw_score, 0.0), 1.0), 4)


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
    reason: str = ""          # "no_candidate" / "low_confidence" / "ok" / "renamed_main" / "llm_arbitrated"


class POIAligner:
    """别名 → 高德 POI 对齐器。"""

    def __init__(self, amap_key: str | None = None):
        env = load_env_file()
        self.key = amap_key or env.get("AMAP_KEY") or os.environ.get("AMAP_KEY")
        self.cache: dict[str, dict] = {}
        self._last_call = 0.0
        self.stats = {"api_calls": 0, "cache_hits": 0, "fallbacks": 0,
                      "renamed_requery": 0, "llm_arbitrations": 0}
        if POI_CACHE_FILE.exists():
            try:
                self.cache = json.loads(POI_CACHE_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.cache = {}
        if ARB_CACHE_FILE.exists():
            try:
                self.arb_cache = json.loads(
                    ARB_CACHE_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.arb_cache = {}
        else:
            self.arb_cache = {}

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

        def once() -> dict:
            # 限频放在重试内部：每次尝试都遵守 QPS 约束
            wait = 0.35 - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()
            with urllib.request.urlopen(url, timeout=POI_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))

        # 与通勤/LLM 一致：网络抖动先重试，最终失败才降级为 no_candidate
        try:
            data = retry_call(once, what=f"高德 POI 搜索({alias}@{city})")
        except Exception as e:
            self.stats["fallbacks"] += 1
            log.warning("POI 搜索失败 alias=%s city=%s: %s: %s",
                        alias, city, type(e).__name__, e)
            raise RuntimeError(f"高德 POI 搜索异常: {type(e).__name__}: {e}") from e
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
        """把一个别名对齐到标准 POI。

        低置信度先走 LLM 仲裁（答案必须 ∈ 候选集）；仲裁失败才 needs_review，
        交给用户点选兜底。
        """
        alias = alias.strip()
        if not alias:
            return AlignResult(alias=alias, best=None, reason="no_candidate",
                               needs_review=True)
        try:
            pois = self._search_pois(alias, city)
        except Exception as e:
            pois = []
            log.warning("POI 搜索异常，按无候选处理 %r@%s: %s: %s",
                        alias, city, type(e).__name__, e)
        if not pois:
            log.info("对齐无候选：%r@%s（转人工）", alias, city)
            return AlignResult(alias=alias, best=None, reason="no_candidate",
                               needs_review=True)

        # 主名权威性先验：检测到「老名 POI + 主名子点引用老名」的更名证据时，
        # 用主名重查一次，采纳主名本体（老名精确匹配是过时名称，不能直接信）
        renamed_root = detect_renamed_root(alias, pois)
        if renamed_root:
            try:
                main_pois = self._search_pois(renamed_root, city)
                self.stats["renamed_requery"] += 1
            except Exception as e:
                main_pois = []
                log.warning("更名重查失败，退回常规打分 root=%r@%s: %s: %s",
                            renamed_root, city, type(e).__name__, e)
            exact = [p for p in main_pois
                     if _normalize(p.name) == _normalize(renamed_root)]
            if exact:
                best = exact[0]
                best.score = round(self._score(renamed_root, best, hint_type), 4)
                log.info("检测到景区更名：%r → 主名 %r@%s（重查命中 %r）",
                         alias, renamed_root, city, best.name)
                return AlignResult(
                    alias=alias, best=best, candidates=[best],
                    confidence=_confidence(best.score), needs_review=False,
                    reason="renamed_main",
                )
            # 主名重查无精确命中 → 证据不足，落回常规打分

        for p in pois:
            p.score = round(self._score(alias, p, hint_type), 4)

        # 干扰类型硬过滤：有干净候选时，公交站/行政区/足疗店直接不参选
        clean = [p for p in pois if type_flag(p.type_str) >= 0]
        pool = clean if clean else pois
        pool.sort(key=lambda p: -p.score)
        best = pool[0]
        review = best.score < AUTO_THRESHOLD

        # LLM 仲裁兜底：文本打分拿不准时，让 LLM 在已召回候选里选优。
        # 答案必须精确等于某个候选名（防幻觉），否则忽略并保持转人工。
        arbitrated = False
        if review:
            picked = self._arbitrate(alias, city, pool)
            if picked is not None:
                if picked.name != best.name:
                    log.info("LLM 仲裁改判：%r@%s → %r（文本分 top1 为 %r %.2f）",
                             alias, city, picked.name, best.name, best.score)
                best = picked
                review = False
                arbitrated = True

        if review:
            log.info("低置信转人工：%r@%s 置信 %.2f（top1=%r），候选 %d 个",
                     alias, city, best.score, best.name, len(pool))

        return AlignResult(
            alias=alias, best=best, candidates=pool,
            confidence=_confidence(best.score),
            needs_review=review,
            reason=("llm_arbitrated" if arbitrated
                    else "ok" if not review else "low_confidence"),
        )

    # ---- LLM 仲裁（低置信兜底，答案锁定在候选集内）----
    def _arbitrate(self, alias: str, city: str,
                   pool: list[PoiCandidate]) -> PoiCandidate | None:
        """返回 LLM 选中的候选；LLM 不可用 / 不在候选集内 / 无把握 → None。"""
        if len(pool) < 2:
            return None  # 只有一个候选时仲裁无意义
        ck = f"{city}|{alias}|{hashlib.md5('|'.join(p.name for p in pool).encode('utf-8')).hexdigest()[:8]}"
        pick_name = self.arb_cache.get(ck)
        if pick_name is None:
            pick_name = self._call_llm_arbiter(alias, city, pool) or ""
            self.arb_cache[ck] = pick_name
            try:
                ARB_CACHE_FILE.write_text(
                    json.dumps(self.arb_cache, ensure_ascii=False),
                    encoding="utf-8")
            except OSError:
                pass
        if not pick_name:
            return None
        for p in pool:
            if p.name == pick_name:
                self.stats["llm_arbitrations"] += 1
                return p
        return None

    @staticmethod
    def _call_llm_arbiter(alias: str, city: str,
                          pool: list[PoiCandidate]) -> str | None:
        """调 LLM 在候选中仲裁；任何失败都静默降级（保持转人工）。"""
        api_key = os.environ.get("LLM_API_KEY")
        base_url = os.environ.get("LLM_BASE_URL")
        if not api_key or not base_url:
            env = load_env_file()
            api_key = api_key or env.get("LLM_API_KEY")
            base_url = base_url or env.get("LLM_BASE_URL")
        if not api_key or not base_url:
            return None
        try:
            from openai import OpenAI  # 局部导入：纯函数测试环境不强依赖
        except ImportError:
            return None
        lines = "\n".join(
            f"{i}. {p.name}（{p.type_str}）地址: {p.address}"
            for i, p in enumerate(pool))
        prompt = (
            f"用户在{city}想去「{alias}」。高德地图召回的候选地点：\n{lines}\n"
            "请判断哪个候选最可能是用户真正想去的地方本体"
            "（注意排除商店、公交站、景区内子景点、同名商户）。\n"
            '只输出 JSON，不要输出其他内容：{"pick": "<候选名称原文，若都不合适则为空字符串>"}'
        )
        try:
            client = OpenAI(api_key=api_key, base_url=base_url, timeout=30)
            resp = client.chat.completions.create(
                model=os.environ.get("LLM_MODEL_ID", "deepseek-chat"),
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.choices[0].message.content or ""
            m = re.search(r"\{.*\}", text, re.S)
            data = json.loads(m.group(0)) if m else {}
            pick = (data.get("pick") or "").strip()
        except Exception as e:
            # 仲裁是可选增强：失败即降级为转人工，不算错误但要知道它失败了
            log.debug("LLM 仲裁调用失败，降级为转人工: %s: %s", type(e).__name__, e)
            return None
        return pick if any(p.name == pick for p in pool) else None


def align_spot(spot, aligner: POIAligner, city: str):
    """把 extractor 产出的 Spot 的占位坐标替换成高德真实坐标。

    返回 (spot, align_result)；needs_review 时不覆盖坐标，交给上游处理。
    """
    r = aligner.align(spot.name, city)
    if r.best and not r.needs_review:
        spot.lat, spot.lon = r.best.lat, r.best.lon
        spot.name = r.best.name  # 统一成标准名，方便地图渲染与通勤查询
    return spot, r

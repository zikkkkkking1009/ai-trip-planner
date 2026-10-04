"""R1 检索基线：BM25 + 字符 bigram（零新依赖、零 Key、离线可测）——RAG 骨架。

ROADMAP R1（2026-09-30 立项）：中厂评审指出「工程扎实但 AI 偏浅」，语料现成
（38 城预置景点 + 美食种子 ≈ 600 篇），先上**零新依赖**的检索基线，把「了解 RAG」
升级成「实践 RAG」；向量重排对照（R3）与引用核查（R2，复用 aligner）后续增量。

为什么 bigram：中文无空格分词，jieba 是新增依赖；字符 bigram 对专名检索
（兵马俑 / 乳扇 / 鼓浪屿）召回稳定，600 篇语料下 BM25 毫秒级。
"""
from __future__ import annotations

import logging
import math
import re
from functools import lru_cache

from corpus_fields import food_tags, spot_fields
from demo_data import DEMO_SPOTS
from editor import _llm
from models import Spot
from food_seeds import FOOD_SEEDS
from rag_intent import parse_intent
from reliability import retry_call
from spot_desc import SPOT_DESCS

_K1, _B = 1.5, 0.75          # Okapi BM25 标准参数
_PUNCT = re.compile(r"[\s·、，,。.\-—()（）【】\[\]!！?？:：;；\"'`~～]+")
_K_LIMIT = 10

# ---- 让检索「听懂真实问法」（2026-10-04，tools/rag_realbench.py 实测驱动）----
# 背景：自造 golden 30 条上 hit@1 90.0 / MRR 0.940，但换成真实问法 12 条只有 3 条全对。
# 翻车集中在两点，**都不是算法不够好，是没听懂问题**：
#   ① 用户说了城市，后端不认 —— city 得调用方显式传，而前端 R5 前从来不传
#      （「西安有什么好吃的」→ 青岛·流亭猪蹄，因为「吃的」这个稀有 bigram 压过「西安」）
#   ② 「好吃 / 玩多久 / 几点关门」这些意图词在语料里一次都不出现，
#      BM25 只能靠稀有 bigram 撞（「拉萨必去的景点」→ 威海·猫头山，「必去」IDF 极高）
# 所以补的是意图理解，不是换检索算法。
_FOOD_HINT = ("好吃", "美食", "吃什么", "小吃", "特色菜", "餐厅", "味道",
              "夜宵", "早点", "吃")
_SPOT_HINT = ("景点", "景区", "好玩", "值得去", "必去", "游览", "参观",
              "门票", "票价", "开放", "几点", "多久", "玩")
# ×1.6：实测够翻转「黄山要爬多久」→ 美食·黄山烧饼 这类错位，又压不下
# 「西安回民街小吃」里正确命中的景点（那条景点 22.9 分、同城美食 0 分，差着量级）
_KIND_BOOST = 1.6
_ANCHOR_MARGIN = 1.5          # top1 领先其它城市多少倍才敢把城市定死（见 ask 里的说明）

# ---- 能力边界：说破「库里没有」，比硬塞噪音诚实（2026-10-04 真机复核加）----
# 实测：「哈尔滨冬天穿什么」返回哈尔滨·冰雪大世界、「故宫需要预约吗」返回故宫简介——
# 前者我们在拿景点冒充气象答案，后者等于没回答。跨城市污染是「答案错了」，
# 这个是「装作能答」，更坏。R2 引用核查只管「引用的东西在不在库里」，
# 管不了「库里压根没有这类信息」，所以这里补一层能力边界。
# 硬拒答：这几类数据我们一个都没有，给条目=给噪音。
_UNSUPPORTED = (
    ("气象穿搭", ("天气", "气温", "下雨", "冷不冷", "冷吗", "热吗", "会下雪",
                   "穿什么", "穿多", "带伞", "紫外线")),
    ("季节花期", ("几月去", "什么时候去", "花期", "淡季", "旺季", "几月份")),
    ("人流排队", ("人多不多", "人挤", "排队", "要等多久")),
    ("预约规则", ("要预约", "需不需要预约", "预约吗")),
)
# 软提示：交通类**不**拒答——景点身份给得了，给不了的只是路线，别因噎废食。
_SOFT_UNSUPPORTED = (("交通路线", ("怎么去", "怎么走", "怎么坐", "地铁", "公交",
                                     "打车", "有多远", "怎么过去")),)
_MONTH_RE = re.compile(r"([一二三四五六七八九十]|\d{1,2})\s*月")
# 属性类：问了就该有答案，没有必须说没有（覆盖率在 _fact_coverage 里动态算）
_ATTR_HINT = (
    ("门票", ("门票", "票价", "多少钱")),
    ("开放时间", ("几点开门", "几点关门", "开放时间", "开门时间", "关门时间", "营业时间")),
    ("游玩时长", ("玩多久", "要多久", "多久", "多长时间", "逛多久")),
)
# 语料里字段的形态，与 spot_facts() 的拼法一一对应。
# tools/rag_realbench.py 直接 import 这张表——**单一事实源**，两边各写一份必然漂移。
FACT_PATTERNS: dict[str, re.Pattern[str]] = {
    "门票": re.compile(r"门票\s*\d+(?:\.\d+)?\s*元"),
    "开放时间": re.compile(r"开放时间\s*\d{1,2}[:：]\d{2}"),
    "游玩时长": re.compile(r"建议游玩\s*\d+\s*分钟"),
}

log = logging.getLogger(__name__)


def tokenize(text: str) -> list[str]:
    """清洗后切字符 bigram（单字输入降级为单字 token）。"""
    s = _PUNCT.sub("", text)
    if len(s) < 2:
        return [s] if s else []
    return [s[i:i + 2] for i in range(len(s) - 1)]


def detect_city(q: str) -> str | None:
    """从问题里认城市：最长匹配（「西双版纳」不能被短名截走）。认不出返回 None。"""
    hit = ""
    for c in DEMO_SPOTS:
        if c in q and len(c) > len(hit):
            hit = c
    return hit or None


def detect_kind(q: str) -> str | None:
    """认用户要的是景点还是美食；两边都像 / 都不像就返回 None（不做无根据的偏向）。"""
    food = any(w in q for w in _FOOD_HINT)
    spot = any(w in q for w in _SPOT_HINT)
    if food and not spot:
        return "美食"
    if spot and not food:
        return "景点"
    return None


def detect_unsupported(q: str) -> str | None:
    """认库里压根没有的数据类型（气象/季节/人流/预约）→ 该硬拒答。"""
    for topic, words in _UNSUPPORTED:
        if any(w in q for w in words):
            return topic
    # 「九月去」「11 月份」这类说法词表盖不住（月份是数字/汉字，不是固定词），用正则兜
    if _MONTH_RE.search(q):
        return "季节花期"
    return None


def detect_soft_unsupported(q: str) -> str | None:
    """认「能给部分答案」的缺口（交通）：给条目，但必须说清哪部分没有。"""
    for topic, words in _SOFT_UNSUPPORTED:
        if any(w in q for w in words):
            return topic
    return None


def detect_attr(q: str) -> str | None:
    """认用户问的是哪个属性（门票/开放时间/游玩时长）。"""
    for attr, words in _ATTR_HINT:
        if any(w in q for w in words):
            return attr
    return None


@lru_cache(maxsize=1)
def _fact_coverage() -> dict[str, str]:
    """字段覆盖率（如 门票 62/375）——拒答文案要报实数，不能拍脑袋写个「大部分」。"""
    docs = [d for d in _index_cached().docs if d["type"] == "景点"]
    n = len(docs)
    return {a: f"{sum(1 for d in docs if p.search(str(d['text'])))}/{n}"
            for a, p in FACT_PATTERNS.items()}


def _hhmm(h: float) -> str:
    hh = int(h)
    return f"{hh:02d}:{int(round((h - hh) * 60)):02d}"


# models.Spot 的默认值：等于它俩说明「没有这个数据」，不能当事实写进语料
_DEF_OPEN_H = float(Spot.model_fields["open_h"].default)
_DEF_CLOSE_H = float(Spot.model_fields["close_h"].default)


def spot_facts(s: Spot) -> str:
    """拼进语料的字段 —— **只拼库里真有的**，缺的宁可不写。

    硬约束（2026-10-04 查源数据得出的，不是估计）：
    375 个景点里 313 个 ticket=0（无票价来源，高德免费档 `biz_ext.cost` 实测空）、
    358 个开放时间是模型默认值 8:00–18:00、spot_media.json 103 条 opentime 全空。
    把「免费」或「8:00–18:00」写进语料就是编造 —— 检索会照着编的回答用户。
    """
    parts = [f"建议游玩{s.stay_min}分钟"]      # 求解器自己的参数，写「建议」不是声明事实
    if s.ticket > 0:
        parts.append(f"门票{s.ticket:g}元")
    if not (s.open_h == _DEF_OPEN_H and s.close_h == _DEF_CLOSE_H):
        parts.append(f"开放时间{_hhmm(s.open_h)}-{_hhmm(s.close_h)}")
    return " ".join(parts)


def build_corpus() -> list[dict[str, object]]:
    """语料文档：{type, city, name, text, source} + R6 结构化字段（每次新建，测试可用）。

    R6 起携带 tags/ticket/ticket_known/stay_min/open_h/close_h，供 /ask 的 tag
    过滤与前端卡片使用；文本层字段拼串见 spot_facts（workbuddy）。
    """
    docs: list[dict[str, object]] = []
    for city, spots in DEMO_SPOTS.items():
        for s in spots:
            # desc 为空的脚本抓取景点用一句话描述兜底（语料增强，R3 定论的检索天花板）
            desc = s.desc if s.desc and len(s.desc.strip()) >= 10 else SPOT_DESCS.get(s.name, "")
            docs.append({
                "type": "景点", "city": city, "name": s.name,
                "text": " ".join(x for x in (s.name, desc, city, spot_facts(s)) if x),
                "source": "预置景点库",
                **spot_fields(s),
            })
    for city, foods in FOOD_SEEDS.items():
        for name, intro in foods:
            docs.append({
                "type": "美食", "city": city, "name": name,
                "text": " ".join(x for x in (name, intro, city) if x),
                "source": "美食种子库",
                "tags": food_tags(), "ticket": None, "ticket_known": False,
                "stay_min": None, "open_h": None, "close_h": None,
            })
    return docs


class _Index:
    """BM25 索引：名字权重 ×2（查询意图主要落在专名上）。"""

    def __init__(self, docs: list[dict[str, object]]):
        self.docs = docs
        self.tfs: list[dict[str, int]] = []
        self.dls: list[int] = []
        df: dict[str, int] = {}
        for d in docs:
            tf: dict[str, int] = {}
            for tok in tokenize(str(d["name"])) * 2 + tokenize(str(d["text"])):
                tf[tok] = tf.get(tok, 0) + 1
            self.tfs.append(tf)
            self.dls.append(sum(tf.values()))
            for tok in tf:
                df[tok] = df.get(tok, 0) + 1
        self.n = len(docs)
        self.avgdl = (sum(self.dls) / self.n) if self.n else 0.0
        self.idf = {t: math.log((self.n - d + 0.5) / (d + 0.5) + 1.0)
                    for t, d in df.items()}

    def score(self, q_toks: list[str]) -> list[float]:
        scores = [0.0] * self.n
        for tok in set(q_toks):
            idf = self.idf.get(tok)
            if idf is None:
                continue
            qtf = q_toks.count(tok)
            for i, tf in enumerate(self.tfs):
                f = tf.get(tok)
                if not f:
                    continue
                denom = f + _K1 * (1 - _B + _B * self.dls[i] / self.avgdl)
                scores[i] += idf * qtf * f * (_K1 + 1) / denom
        return scores


@lru_cache(maxsize=1)
def _index_cached() -> _Index:
    return _Index(build_corpus())


# ---- R2 引用核查：每条引用必须落到库内可对齐实体（反幻觉闸门）----
# 语料当前 100% 来自库内，"必过"是机制旁证；闸门真正兜底的是未来
# 语料混入外部文本（攻略识别 / 网页抓取）的场景——对不上一律 verified=False。

def _norm(name: str) -> str:
    """归一化：剥括号内容（馆区/分馆后缀）→ 去标点空白 → 小写（对齐器同思路）。"""
    s = re.sub(r"[（(【\[].*?[)）\]】]", "", name)
    return _PUNCT.sub("", s).lower()


@lru_cache(maxsize=1)
def _entity_index() -> tuple[dict[tuple[str, str], Spot],
                             dict[tuple[str, str], None]]:
    """{(city, 归一名): Spot} 与 {(city, 归一名): None}（美食无坐标语义）。"""
    spots: dict[tuple[str, str], Spot] = {}
    for city, spot_list in DEMO_SPOTS.items():
        for s in spot_list:
            spots.setdefault((city, _norm(s.name)), s)
    foods: dict[tuple[str, str], None] = {}
    for city, food_list in FOOD_SEEDS.items():
        for name, _intro in food_list:
            foods.setdefault((city, _norm(name)), None)
    return spots, foods


def verify_citation(doc: dict[str, object]) -> dict[str, object]:
    """单条引用核查：景点 → 库内对齐出真实坐标；美食 → 库内对齐即可。

    对不上库内实体一律 verified=False（宁可标失败，不假装可核查）。
    """
    city = str(doc.get("city", ""))
    name = str(doc.get("name", ""))
    spots, foods = _entity_index()
    key = (city, _norm(name))
    if doc.get("type") == "美食":
        ok = key in foods
        return {"verified": ok, "lat": None, "lon": None}
    spot = spots.get(key)
    if spot is None:
        return {"verified": False, "lat": None, "lon": None}
    return {"verified": True, "lat": float(spot.lat), "lon": float(spot.lon)}


# ---- R4 生成层：grounded 生成——答案只准出自检索片段，实体回链核查（反幻觉）----

def check_grounding(answer: str, cited_names: set[str],
                    all_names: set[str]) -> dict[str, object]:
    """生成答案的实体回链核查（纯函数，离线可测）。

    答案里出现的库内实体必须来自本次引用片段；未引用实体若只是被引用实体
    的子串（「古城」⊂「大同古城」）视为同一提及，不算违规——宁可漏报不误报。
    """
    ans = _norm(answer)
    cited = {n for n in (_norm(x) for x in cited_names) if n}

    def spans(text: str, needle: str) -> list[tuple[int, int]]:
        found: list[tuple[int, int]] = []
        i = 0
        while (j := text.find(needle, i)) >= 0:
            found.append((j, j + len(needle)))
            i = j + 1
        return found

    cited_spans = [sp for n in cited for sp in spans(ans, n)]
    outside: list[str] = []
    for name in all_names:
        n = _norm(name)
        if not n or n in cited:
            continue
        uncovered = [sp for sp in spans(ans, n)
                     if not any(cs[0] <= sp[0] and sp[1] <= cs[1]
                                for cs in cited_spans)]
        if uncovered:
            outside.append(name)
    outside.sort()
    return {"grounded": not outside, "outside": outside}


def generate_answer(q: str, results: list[dict[str, object]]) -> dict[str, object]:
    """R4 生成层：只准依据检索片段作答（≤100 字），答案实体回链核查。

    LLM 失败/超时一律降级为 text=None——宁可无生成，不给未经核查的答案。
    """
    try:
        client, model = _llm(fast=True)
        snippets = "\n".join(
            f"[{i}] {r['type']}｜{r['city']}｜{r['name']}｜{r['text']}"
            for i, r in enumerate(results, 1))
        resp = retry_call(lambda: client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content":
                 "你是旅游问答助手。只使用资料中的信息作答，禁止编造资料里没有的"
                 "景点、美食或事实；答案不超过 100 字，直接给结论。"},
                {"role": "user", "content": f"问题：{q}\n\n资料：\n{snippets}"},
            ],
            temperature=0.1,
        ), what="RAG 答案生成 LLM 调用")
        text = (resp.choices[0].message.content or "").strip()
    except Exception as e:  # 降级：生成不可用不影响检索结果本身，宁可缺答案不编答案
        log.warning("RAG 生成降级：%s: %s", type(e).__name__, e)
        return {"text": None, "grounded": None, "outside": [],
                "note": f"生成不可用：{e}"}
    if not text:
        return {"text": None, "grounded": None, "outside": [], "note": "生成返回为空"}
    grounding = check_grounding(text, {str(r["name"]) for r in results},
                                {str(d["name"]) for d in _index_cached().docs})
    return {"text": text, "model": model, **grounding}


def ask(q: str, city: str | None = None, k: int = 5,
        with_answer: bool = False, tag: str | None = None) -> dict:
    """检索问答：带来源引用；with_answer=True 叠加 grounded 生成层（失败自动降级）。

    tag（R6，ZCode）：标签过滤（免费/亲子/室内…，来自 rag_intent.parse_intent
    或前端显式传入），与 city 同为检索前过滤。
    """
    from cities import normalize_city
    q_toks = tokenize(q)
    index = _index_cached()
    # 调用方没传城市就从问题里认：用户说话是带城市的，以前白扔了
    if city:
        city_n = normalize_city(city)
        auto_city = False
    else:
        got = detect_city(q)
        city_n = normalize_city(got) if got else ""
        auto_city = bool(got)
    picked = list(range(index.n))
    if city_n:
        picked = [i for i in picked if index.docs[i]["city"] == city_n]
    if tag:                                 # R6：标签过滤（结构性意图，过滤比加权准）。
        picked = [i for i in picked         # 刻意只支持显式传参：auto-tag 实验被 realbench
                  if isinstance(doc_tags := index.docs[i].get("tags"), list)  # 否决——「西安回民街小吃」
                  and tag in doc_tags]      # 这类专名+意图混合 query 会被单标签误伤（见 COLLAB_LOG）
    kind = detect_kind(q)
    # 问的是美食就不查门票（「西安有什么好吃的多少钱」问的是菜价不是门票）
    attr = detect_attr(q) if kind != "美食" else None
    unsupported = detect_unsupported(q)      # 库里没有的：硬拒答
    if unsupported and "室内" in parse_intent(q):
        # 豁免：「下雨天能去哪」问的是室内选项，不是问天气——tag 过滤能答，不拒
        unsupported = None
    scores = index.score(q_toks) if not unsupported else [0.0] * index.n
    if kind:                            # 只加权不过滤：过滤会连正确的专名命中一起丢掉
        scores = [s * _KIND_BOOST if index.docs[i]["type"] == kind else s
                  for i, s in enumerate(scores)]
    scored: list[tuple[dict[str, object], float]] = [(index.docs[i], scores[i])
                                                     for i in picked if scores[i] > 0]
    scored.sort(key=lambda x: -x[1])
    # 城市锚定：用户没说城市时，以 top1 所在城市为准，其它城市一律丢弃。
    # 一个回答里混着好几个城市的条目对用户就是噪音——实测「兵马俑门票多少钱」会捎上
    # 拉萨·色拉寺 / 敦煌·敦煌古城，只因为它们语料里也写了「门票」两个字。
    # 宁可少给几条，不给一锅跨城市的大杂烩。
    anchor = ""
    if not city_n and scored:
        top_city = scored[0][0]["city"]
        best_other = next((s for d, s in scored if d["city"] != top_city), 0.0)
        # ×1.5 领先才敢替用户定城市：同名实体跨城重复（「土笋冻」厦门/泉州语料里都有，
        # 分数几乎持平）时锚下去就是把正确答案删掉——那种时候宁可两个城市都给。
        if scored[0][1] >= _ANCHOR_MARGIN * best_other:
            anchor = str(top_city)
            scored = [x for x in scored if x[0]["city"] == anchor]
    kk = max(1, min(k, _K_LIMIT))
    # 兜底：听得懂意图（美食/景点）且命中不足时，按「同城 + 同类型」补齐并打
    # fallback 标记——宁可给一份标了「非精确匹配」的同城清单，也不跨城市塞噪音。
    # 认不出意图时不补：那说明我们也不知道用户想要什么，补什么都可能是噪音。
    filled: list[tuple[dict[str, object], float, bool]] = [(d, s, False)
                                                           for d, s in scored[:kk]]
    if kind and city_n and len(filled) < kk and not unsupported:
        have = {str(d["name"]) for d, _, _ in filled}
        for d in index.docs:
            if len(filled) >= kk:
                break
            if d["city"] != city_n or d["type"] != kind or d["name"] in have:
                continue
            if tag:                          # tag 场景下补齐也必须带标签
                doc_tags = d.get("tags")
                if not isinstance(doc_tags, list) or tag not in doc_tags:
                    continue
            filled.append((d, 0.0, True))
    results = [{
        "type": d["type"], "city": d["city"], "name": d["name"],
        "text": d["text"], "source": d["source"], "score": round(s, 3),
        "tags": d.get("tags", []), "ticket": d.get("ticket"),
        "ticket_known": d.get("ticket_known", False), "stay_min": d.get("stay_min"),
        **({"fallback": True} if fb else {}),
    } for d, s, fb in filled]
    for r in results:                       # R2：引用核查（verified + 坐标落地）
        r.update(verify_citation(r))
    out: dict[str, object] = {
        "q": q, "city": city_n or None, "k": kk, "results": results,
        # 城市从哪来的，接口自己交代清楚——「跨城市污染」这类问题全靠这个字段定位
        "city_source": ("参数" if city else "问题识别" if auto_city
                        else "top1 锚定" if anchor else None),
        "kind": kind,                    # 识别出的意图（None = 没听懂，前端可据此不强凑）
        "tag": tag,                      # R6：生效的标签过滤（None = 未过滤；auto-tag 实验已否决）
        "attr": attr,                    # 识别出的属性诉求（None = 没在问字段）
        "fallback_count": sum(1 for d, _s, fb in filled if fb),
        "verified_count": sum(1 for r in results if r["verified"]),
        "coverage": _fact_coverage(),   # 字段覆盖率，拒答时用来告诉用户「那能问什么」
        "index": {"docs": index.n, "cities": len(DEMO_SPOTS)},
    }
    gap = _capability_gap(q, results, attr, unsupported)
    out["gap"] = gap
    out["abstain"] = bool(gap and gap["kind"] == "unsupported")
    if with_answer:                         # R4：默认关——公开接口不自动烧 LLM 配额
        out["answer"] = generate_answer(q, results)
    return out


def _capability_gap(q: str, results: list[dict], attr: str | None,
                    unsupported: str | None) -> dict[str, object] | None:
    """这条问题我们答不全 / 答不了，差在哪——说清楚，比装作答了强。

    三档（刻意区分，别合并）：
      unsupported  库里没有这类数据 → 一条都不给（「哈尔滨冬天穿什么」）
      attr         条目在，但缺被问的那个字段（「大理古城门票」——库里 62/375 有票价）
      soft         能给一部分（「鼓浪屿怎么去」能给景点身份，给不了路线）
    """
    if unsupported:
        return {"kind": "unsupported", "topic": unsupported,
                "note": f"库里只有景点和美食的名称与简介，没有{unsupported}数据——"
                        f"这条答不了，不拿景点凑。"}
    if attr and results and not FACT_PATTERNS[attr].search(str(results[0]["text"])):
        cov = _fact_coverage().get(attr, "0/0")
        return {"kind": "attr", "attr": attr,
                "note": f"库里只有 {cov} 个景点有「{attr}」数据（其余为空值，不是 0），"
                        f"排在第一的「{results[0]['name']}」也没有——不猜。"}
    soft = detect_soft_unsupported(q)
    if soft:
        return {"kind": "soft", "topic": soft,
                "note": f"库里没有{soft}数据；能告诉你这是哪个景点，怎么去得自己看地图。"}
    return None

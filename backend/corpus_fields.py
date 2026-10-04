"""R6 语料结构化字段模块：给 RAG 语料补结构化字段与主题标签。

背景：R1~R5 的语料（rag.build_corpus）只拼接了 name+desc+city 文本，Spot 模型里
现成的 ticket / ticket_known / stay_min / open_h / close_h / intro 字段全被丢掉，
「免费 / 亲子 / 室内 / 夜景」这类用户真实问法（「带娃去哪」「下雨天去哪」）在
语料里没有可检索的落点。本模块供 rag.build_corpus 给语料补字段与标签：
spot_fields 输出结构化字段，spot_tags / food_tags 输出主题标签。

设计约束（刻意的，别"优化"掉）：
- **确定性关键词规则**：子串匹配，不引入分词 / 模糊匹配库——同一输入永远同一
  输出，标签错了能定位到具体词表条目修掉，评测可复现；
- **零 I/O 零网络**：纯函数，不读文件不发请求，离线可测、全量重算毫秒级。

「免费」的口径与 models.Spot.ticket_known 对齐：ticket==0 且 ticket_known 才算
免费；ticket_known=False 的 0 是「不知道票价」，当免费写进语料就是编造
（与 rag.spot_facts 只写已知字段是同一条硬约束）。
"""
from __future__ import annotations

from models import Spot

# 固定顺序 = spot_tags 的返回顺序，也是前端展示顺序；只准追加到末尾，不要重排。
SPOT_TAGS: list[str] = [
    "免费", "亲子", "夜景", "室内", "自然风光", "历史文化", "登山徒步",
    "古镇街区", "博物馆", "寺庙宗教", "海滨", "演出", "美食",
]

# 各标签的关键词表：命中任一子串即打标（中文无分词，专名关键词子串匹配够用）。
# 口径为 2026-10-04 对 demo_spots.json 全量 361 景点 + 西安 14 条实测校准：
#   - 海滨不收单词「海」：「上海」「威海」这类城市字会大面积误伤，只收复合词；
#   - 博物馆/室内补「博物院」：否则故宫博物院这个最大名头反而漏标；
#   - 历史文化在基线外补「俑」：兵马俑的 name/desc/intro 对基线词表一个都命不中。
_KW_MUSEUM = ("博物馆", "博物院", "纪念馆", "美术馆", "科技馆", "展览馆")
_KW_INDOOR = ("博物馆", "博物院", "纪念馆", "美术馆", "科技馆", "展览馆",
              "海洋馆", "极地馆", "教堂")
_KW_FAMILY = ("动物园", "乐园", "海洋馆", "极地", "水族", "游乐园", "主题乐园")
_KW_NIGHT = ("夜景", "夜市", "夜色", "灯光")
_KW_SHOW = ("演出", "千古情", "印象", "实景", "演艺")
_KW_ANCIENT = ("古镇", "古城", "古街", "老街", "街区", "巷", "坊")
_KW_SEASIDE = ("海滨", "海湾", "海岛", "沙滩", "浴场", "赶海", "海边")
_KW_NATURE = ("湖", "江", "河", "泉", "瀑", "森林", "湿地", "地质",
              "山", "岛", "湾", "滩", "花海", "草原")
_KW_HISTORY = ("遗址", "石窟", "陵", "故居", "书院", "历史", "文化", "祠",
               "府", "城墙", "碑", "故里", "古", "俑")
_KW_HIKE = ("山", "峰", "岭", "峡", "溪", "谷", "步道", "索道")
# 登山徒步的著名反例（demo 数据实测命中的全部名单见 tests/test_corpus_fields.py）：
# 「中山」是人名前缀（中山公园/中山路/中山陵/中山纪念堂）不是地形，「塔」是建筑，
# 寺/庙/观 结尾的（寒山寺）「山」是地名——这类名字不是可爬的山。
_HIKE_EXCLUDE = ("纪念堂", "塔", "中山")


def _has(text: str, words: tuple[str, ...]) -> bool:
    """text 里出现任一关键词（子串匹配）。"""
    return any(w in text for w in words)


def spot_tags(s: Spot) -> list[str]:
    """按确定性关键词规则推导景点主题标签（多标签，顺序同 SPOT_TAGS）。

    只查 name/desc/intro 三个文本字段，city/address 不参与——「上海」这类
    城市字天然进不了判定。美食标签不在此函数，由调用方按条目类型用
    food_tags() 补。
    """
    name, desc, intro = s.name, s.desc, s.intro
    body = f"{name}{desc}{intro}"        # 三字段拼起来判子串即可，无需分词
    hit: list[str] = []

    if s.ticket_known and s.ticket == 0:
        hit.append("免费")                # 已知 0 元才是免费；未知票价的 0 不是
    if _has(body, _KW_FAMILY):
        hit.append("亲子")
    if _has(body, _KW_NIGHT):
        hit.append("夜景")
    if _has(body, _KW_INDOOR):
        hit.append("室内")
    if _has(body, _KW_NATURE):
        hit.append("自然风光")
    if _has(body, _KW_HISTORY):
        hit.append("历史文化")
    # 登山徒步只查 name/intro（基线口径）：desc 提一句「山下有餐馆」不该把
    # 城区商场打成爬山；著名反例见 _HIKE_EXCLUDE 注释。
    if (_has(name, _KW_HIKE) or _has(intro, _KW_HIKE)) \
            and not _has(name, _HIKE_EXCLUDE) \
            and not name.endswith(("寺", "庙", "观")):
        hit.append("登山徒步")
    if _has(body, _KW_ANCIENT):
        hit.append("古镇街区")
    if _has(body, _KW_MUSEUM):
        hit.append("博物馆")
    if name.endswith(("寺", "庙", "观")) or _has(name, ("教堂", "清真")) \
            or _has(intro, ("寺院", "佛教", "道教")):
        hit.append("寺庙宗教")
    if _has(body, _KW_SEASIDE):
        hit.append("海滨")
    if _has(body, _KW_SHOW):
        hit.append("演出")

    # 输出顺序固定为 SPOT_TAGS 顺序（「美食」不在 hit 里，天然被过滤掉）
    return [t for t in SPOT_TAGS if t in hit]


def food_tags() -> list[str]:
    """美食条目的标签：恒为 [「美食」]——美食语料只有这一个主题维度。"""
    return ["美食"]


def spot_fields(s: Spot) -> dict[str, object]:
    """Spot 的语料结构化字段：模型字段原样透出 + 主题标签。

    ticket/open_h/close_h **不代填、不猜默认值**——「是不是模型默认值」的判断
    是 rag.spot_facts 的职责（默认值写进语料等于编造），这里只忠实搬运。
    """
    return {
        "ticket": s.ticket,
        "ticket_known": s.ticket_known,
        "stay_min": s.stay_min,
        "open_h": s.open_h,
        "close_h": s.close_h,
        "tags": spot_tags(s),
    }

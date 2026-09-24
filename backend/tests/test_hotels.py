"""酒店相关测试。

分两类，**不要混在同一个 skip 规则里**：
· **纯函数**（`_grade` 档位归一 / `_lodging_intro` 类别取值）—— 不依赖网络，永远跑；
· **接口**（`/hotel/search`、`/hotel/recommend`）—— 依赖高德 POI，未配置 `AMAP_KEY` 时跳过
  （没 KEY 时接口本来就会降级返回空，断言没意义，不该让环境差异变成红灯）。
"""
import os

import pytest
from fastapi.testclient import TestClient

from editor import _grade          # 档位归一定义在 editor（main 的 _hotel_card 会调用它）
from main import _lodging_intro, app


def _has_amap_key() -> bool:
    env = {}
    try:
        from editor import load_env_file
        env = load_env_file() or {}
    except Exception:
        env = {}
    return bool(env.get("AMAP_KEY") or os.environ.get("AMAP_KEY"))


needs_amap = pytest.mark.skipif(not _has_amap_key(),
                                reason="未配置 AMAP_KEY，跳过依赖高德的用例")


# --------------------------------------------------------------------------
# 纯函数：不依赖网络，永远跑
# --------------------------------------------------------------------------

@pytest.mark.parametrize("keytag,expected", [
    ("豪华型", "豪华型"),
    ("奢华五星", "豪华型"),
    ("高档型", "高档型"),
    ("精品酒店", "高档型"),
    ("舒适型", "舒适型"),
    ("商务酒店", "舒适型"),
    ("经济型", "经济型"),
    ("快捷酒店", "经济型"),
    ("民宿", "民宿"),
    ("青年旅舍", "民宿"),
    ("公寓", "民宿"),
    ("中餐", ""),        # 高德多段类别里混进来的餐饮段 —— 必须归不出档位，不能瞎猜
    ("", ""),
])
def test_grade_normalizes_keytag(keytag, expected):
    """keytag 有十几种说法，必须收敛到 5 档，否则筛选 chip 又杂又长。

    回归点：未归一前筛选用原始 keytag，会出现「中餐」「住宿服务」这类非档位值当选项。
    """
    assert _grade(keytag) == expected


@pytest.mark.parametrize("type_str,expected", [
    # 高德同一个 POI 会给多段类别（`;` 与 `|` 分隔）—— 必须取「住宿」那一段
    ("餐饮服务;中餐厅;中餐厅|住宿服务;宾馆酒店;宾馆酒店", "住宿服务"),
    ("住宿服务;宾馆酒店", "住宿服务"),
    ("风景名胜", "风景名胜"),          # 没有住宿段时退回第一段（不返回空）
    ("", ""),
])
def test_lodging_intro_picks_lodging_segment(type_str, expected):
    """卡片副标题必须取住宿段，否则会把酒店描述成「餐饮服务」。

    回归点：原先直接 `split(";")[0]` —— 于是「东亚饭店(钟楼店)」在卡片上显示成「餐饮服务」，
    用户会以为搜错了。
    """
    assert _lodging_intro(type_str) == expected


# --------------------------------------------------------------------------
# 接口：依赖高德
# --------------------------------------------------------------------------

@needs_amap
def test_hotel_recommend_needs_no_keyword():
    """打开酒店选择器不该逼用户先搜：无关键词也要给一屏推荐（携程模式）。

    回归点：原先只有 /hotel/search 且强制要求 query，界面打开是一片空白。
    """
    client = TestClient(app)
    r = client.post("/hotel/recommend", json={"city": "西安"})
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert results, "推荐接口返回空 —— 用户打开酒店页会看到一片空白"


@needs_amap
def test_hotel_recommend_returns_lodging_only():
    """推荐出来的必须是住宿，不能是景点 / 地铁站。

    回归点：不加类别过滤时搜「钟楼」返回风景名胜，用户会把它当住宿选走。
    """
    client = TestClient(app)
    r = client.post("/hotel/recommend",
                    json={"city": "西安", "lat": 34.2594, "lon": 108.9470})
    assert r.status_code == 200, r.text
    for h in r.json()["results"]:
        assert "住宿" in (h.get("intro") or ""), f"非住宿结果混进来了：{h.get('name')}"


@needs_amap
def test_hotel_card_forwards_photos():
    """卡片数据必须带上多图，否则前端画廊永远是空的。

    回归点：`_hotel_card` 一开始漏了转发 photos —— 接口看起来正常（有名字有评分），
    但画廊一张图都没有，只有直接调 poi_search 才看得到 photos 其实是有的。
    """
    client = TestClient(app)
    r = client.post("/hotel/recommend", json={"city": "西安"})
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert any(len(h.get("photos") or []) > 1 for h in results), \
        "没有任何酒店带多图 —— photos 没有从 _poi_row 透传到卡片"


@needs_amap
def test_hotel_search_landmark_returns_lodging():
    """搜地标（钟楼）应当返回住宿，而不是地标本身。"""
    client = TestClient(app)
    r = client.post("/hotel/search", json={"query": "钟楼", "city": "西安"})
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert results, "搜「钟楼」没有结果"
    for h in results:
        assert "住宿" in (h.get("intro") or ""), f"搜地标返回了非住宿：{h.get('name')}"


@needs_amap
def test_hotel_search_supports_paging():
    """分页：第 2 页应与第 1 页不同（否则「加载更多」是假的）。"""
    client = TestClient(app)
    p1 = client.post("/hotel/search", json={"query": "酒店", "city": "西安", "page": 1})
    p2 = client.post("/hotel/search", json={"query": "酒店", "city": "西安", "page": 2})
    assert p1.status_code == 200 and p2.status_code == 200
    n1 = [h["name"] for h in p1.json()["results"]]
    n2 = [h["name"] for h in p2.json()["results"]]
    assert n1 and n2, "分页某一页为空"
    assert set(n1) != set(n2), "第 1 页与第 2 页完全相同 —— page 参数没生效"


def test_hotel_page_bad_param_does_not_500(monkeypatch):
    """坏 page 参数不能把接口打成 500。

    回归点：前端曾把 click 事件对象当 page 传上来（JSON 成 `{}`），后端 `int({})` 直接 500，
    搜索整个坏掉。这里锁定「坏参数一律当第 1 页」。
    """
    client = TestClient(app)
    r = client.post("/hotel/search", json={"query": "酒店", "city": "西安", "page": {}})
    assert r.status_code == 200, f"坏 page 参数应容错，实际 {r.status_code}: {r.text[:120]}"


def test_hotel_search_requires_query():
    """空 query 是客户端错误，应明确 400，而不是静默返回空列表。"""
    client = TestClient(app)
    assert client.post("/hotel/search", json={"query": "  ", "city": "西安"}).status_code == 400

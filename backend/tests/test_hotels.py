"""酒店「搜索 / 推荐」接口测试。

依赖高德 POI，未配置 AMAP_KEY 时整体跳过 —— 没有 KEY 时这些接口本来就会降级返回空，
断言没有意义，不该让环境差异变成红灯。
"""
import os

import pytest
from fastapi.testclient import TestClient

from main import app


def _has_amap_key() -> bool:
    env = {}
    try:
        from editor import load_env_file
        env = load_env_file() or {}
    except Exception:
        env = {}
    return bool(env.get("AMAP_KEY") or os.environ.get("AMAP_KEY"))


pytestmark = pytest.mark.skipif(not _has_amap_key(),
                                reason="未配置 AMAP_KEY，跳过依赖高德的用例")


def test_hotel_recommend_needs_no_keyword():
    """打开酒店选择器不应该逼用户先搜：无关键词也要给一屏推荐（携程模式）。

    回归点：原先只有 /hotel/search 且强制要求 query，界面打开是一片空白。
    """
    client = TestClient(app)
    r = client.post("/hotel/recommend", json={"city": "西安"})
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert results, "推荐接口返回空 —— 用户打开酒店页会看到一片空白"


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


def test_hotel_search_landmark_returns_lodging():
    """搜地标（钟楼）应当返回住宿，而不是地标本身。"""
    client = TestClient(app)
    r = client.post("/hotel/search", json={"query": "钟楼", "city": "西安"})
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert results, "搜「钟楼」没有结果"
    for h in results:
        assert "住宿" in (h.get("intro") or ""), f"搜地标返回了非住宿：{h.get('name')}"

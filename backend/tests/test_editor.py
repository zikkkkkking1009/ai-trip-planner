"""编辑器单元测试：apply_ops 纯函数，POI 搜索用桩函数注入。"""
from editor import apply_ops
from models import Spot


def _base() -> list[Spot]:
    return [
        Spot(source_id=1, name="钟楼", lat=34.261, lon=108.942,
             stay_min=45, score=7.5, ticket=30),
        Spot(source_id=2, name="回民街", lat=34.265, lon=108.935,
             stay_min=90, score=8.0),
        Spot(source_id=3, name="西安城墙", lat=34.276, lon=108.947,
             stay_min=90, score=8.5, ticket=54),
    ]


def _stub_poi(query, lat, lon):
    """桩：不管搜什么，都返回一个固定的咖啡馆 POI。"""
    return [{"name": f"测试{query}", "lat": lat, "lon": lon,
             "type_str": "餐饮服务;咖啡厅;咖啡厅"}]


def test_remove_existing():
    spots, changes = apply_ops(_base(), [{"op": "remove", "name": "回民街"}])
    assert [s.name for s in spots] == ["钟楼", "西安城墙"]
    assert any("移除「回民街」" in c for c in changes)


def test_remove_missing_is_reported_not_crash():
    spots, changes = apply_ops(_base(), [{"op": "remove", "name": "不存在的"}])
    assert len(spots) == 3
    assert any("未找到" in c for c in changes)


def test_add_uses_poi_stub():
    spots, changes = apply_ops(_base(), [{"op": "add", "query": "咖啡馆", "day": 2}],
                               poi_search_fn=_stub_poi)
    assert len(spots) == 4
    assert spots[-1].name == "测试咖啡馆"
    assert spots[-1].stay_min == 60  # 新增点默认参数


def test_replace_removes_old_and_adds_new():
    spots, changes = apply_ops(
        _base(), [{"op": "replace", "old": "回民街", "query": "美食街", "day": 3}],
        poi_search_fn=_stub_poi)
    names = [s.name for s in spots]
    assert "回民街" not in names
    assert "测试美食街" in names
    assert any("替换" in c for c in changes)


def test_poi_search_missing_returns_no_change():
    spots, changes = apply_ops(_base(), [{"op": "add", "query": "外星餐厅"}],
                               poi_search_fn=lambda q, a, b: [])
    assert len(spots) == 3
    assert any("没有搜到" in c for c in changes)

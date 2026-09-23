"""编辑器单元测试：apply_ops / pin_modify_day 纯函数，POI 搜索用桩函数注入。"""
from editor import apply_ops, pin_insert_best, pin_modify_day
from models import Hotel, Spot
from solver import commute_min


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


# ---- pin_modify_day：定点插入 ----

_HOTEL = Spot(source_id=99, name="汉庭酒店(钟楼店)", lat=34.262, lon=108.941,
              stay_min=30, score=0, ticket=0, open_h=0.0, close_h=24.0,
              desc="我的酒店")


def test_pin_add_middle_inserts_and_recomputes():
    res = pin_modify_day(_base(), [_HOTEL], [], None, 9.0, 18.0)
    assert res is not None
    names = [v.name for v in res["spots"]]
    assert names == ["钟楼", "汉庭酒店(钟楼店)", "回民街", "西安城墙"]
    assert res["cost"] == 84.0  # 30 + 54，酒店免费


def test_pin_add_after_specified_spot():
    res = pin_modify_day(_base(), [_HOTEL], [], "回民街", 9.0, 18.0)
    names = [v.name for v in res["spots"]]
    assert names.index("汉庭酒店(钟楼店)") == names.index("回民街") + 1


def test_pin_add_infeasible_returns_none():
    # 一个 6:00-6:30 营业的景点，9 点出发永远赶不上 → 时间线不可行
    closed = Spot(source_id=98, name="凌晨妖怪店", lat=34.26, lon=108.94,
                  stay_min=30, score=1, ticket=0, open_h=6.0, close_h=6.5)
    res = pin_modify_day(_base(), [closed], [], None, 9.0, 18.0)
    assert res is None


def test_pin_remove():
    res = pin_modify_day(_base(), [], ["回民街"], None, 9.0, 18.0)
    names = [v.name for v in res["spots"]]
    assert names == ["钟楼", "西安城墙"]


# ---- pin_insert_best：择优与返回的通勤口径（B4）----

def test_pin_insert_best_commute_includes_hotel_roundtrip():
    """回归 B4：返回的 commute_min 必须含酒店往返，与 solver 口径一致。

    修前 commute_total 漏传 hotel，返回值只含景点间通勤、系统性地偏小——
    写回 DayPlan.commute_min 后会与其它天（含酒店往返）不可比。
    """
    # 故意把酒店放到远离景点群的地方：酒店两段通勤才显著、不会退化成 0
    hotel = Hotel(name="远郊酒店", lat=34.20, lon=108.80)
    cafe = Spot(source_id=98, name="咖啡馆", lat=34.262, lon=108.941,
                stay_min=60, score=9.9, ticket=0, open_h=8.0, close_h=22.0)
    res = pin_insert_best(_base(), cafe, None, 8.0, 20.0, hotel=hotel)
    assert res is not None

    by_name = {s.name: s for s in _base() + [cafe]}
    order = [by_name[v.name] for v in res["spots"]]
    inter = sum(commute_min(order[i], order[i + 1])
                for i in range(len(order) - 1))
    with_hotel = commute_min(hotel, order[0]) + inter + commute_min(order[-1], hotel)

    # 数值等于手算的「含酒店往返」总通勤
    assert abs(res["commute_min"] - round(with_hotel, 1)) < 0.05
    # 且严格大于不含酒店的值——修前该断言必然失败
    assert res["commute_min"] > inter

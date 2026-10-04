"""R6 语料结构化字段测试：标签规则的真例 / 反例 / 顺序 / 字段透传。

**所有实体与字段值均抄自真实语料**（demo_data.XI_AN_SPOTS 与 demo_spots.json，
2026-10-04 实测），不是拍脑袋造的数据——规则对什么文本命中什么标签，
以这些真实条目为准；数据文件改了票价/描述，本文件要同步核对。
"""
from __future__ import annotations

import pytest

from corpus_fields import SPOT_TAGS, food_tags, spot_fields, spot_tags
from models import Spot


# ---- 真实语料实体（字段值 = 数据文件实测，勿凭记忆改动）----

def _terracotta() -> Spot:
    """西安·秦始皇兵马俑博物馆（demo_data.XI_AN_SPOTS source_id=1）。"""
    return Spot(
        source_id=1, name="秦始皇兵马俑博物馆", lat=34.3847, lon=109.2785,
        stay_min=180, score=9.5, ticket=120, open_h=8.5, close_h=17.0,
        desc="世界第八大奇迹，一号坑的军阵气势最足，建议请讲解或租导览器",
    )


def test_terracotta_gets_history_and_museum():
    tags = spot_tags(_terracotta())
    assert {"历史文化", "博物馆"} <= set(tags)
    # 返回顺序必须按 SPOT_TAGS：室内(3) < 历史文化(5) < 博物馆(8)
    assert tags == ["室内", "历史文化", "博物馆"]


def test_city_wall_gets_history_tag():
    """西安城墙（demo_data.XI_AN_SPOTS source_id=3，ticket=54）——「城墙」命中。"""
    s = Spot(source_id=3, name="西安城墙", lat=34.2760, lon=108.9470,
             stay_min=120, score=8.5, ticket=54, open_h=8.0, close_h=22.0,
             desc="中国现存最完整的古代城垣，推荐傍晚骑一圈自行车，看夕阳落城楼")
    assert "历史文化" in spot_tags(s)


def test_known_free_ticket_gets_free_tag():
    """成都·宽窄巷子景区（demo_spots.json 实测 ticket=0，ticket_known 默认 True）。"""
    s = Spot(source_id=1, name="宽窄巷子景区", lat=30.663869, lon=104.053307,
             stay_min=120, score=4.8, ticket=0, open_h=10.0, close_h=22.0,
             ticket_known=True)
    assert "免费" in spot_tags(s)


def test_paid_spot_is_never_free():
    """防恒真：兵马俑实测 ticket=120，绝不能被打上「免费」。"""
    assert "免费" not in spot_tags(_terracotta())


def test_unknown_ticket_zero_is_not_free():
    """ticket_known=False 的 0 元是「不知道票价」，不是免费（models.Spot 的硬口径）。

    实体取宽窄巷子景区真实字段值；ticket_known=False 对应高德免费档拿不到
    票价的真实现状（demo 数据全部 ticket_known=True，故此处显式改这一位）。
    """
    s = Spot(source_id=1, name="宽窄巷子景区", lat=30.663869, lon=104.053307,
             stay_min=120, score=4.8, ticket=0, open_h=10.0, close_h=22.0,
             ticket_known=False)
    assert "免费" not in spot_tags(s)


@pytest.mark.parametrize("sid,name,lat,lon", [
    (5, "丽江千古情景区", 26.827705, 100.218666),
    (8, "三亚千古情景区", 18.291009, 109.530925),
])
def test_qianqing_gets_show_tag(sid: int, name: str, lat: float, lon: float):
    """「千古情」命中演出——demo_spots.json 里真实存在的丽江/三亚两条。"""
    s = Spot(source_id=sid, name=name, lat=lat, lon=lon, stay_min=90, score=4.7)
    assert "演出" in spot_tags(s)


def test_zoo_and_polar_aquarium_get_family_tag():
    """天津动物园（source_id=9）与哈尔滨极地公园·极地馆（source_id=8）→ 亲子；
    极地馆同时命中「极地馆」→ 室内。"""
    zoo = Spot(source_id=9, name="天津动物园", lat=39.082482, lon=117.16531,
               stay_min=90, score=4.7)
    polar = Spot(source_id=8, name="哈尔滨极地公园·极地馆", lat=45.784741,
                 lon=126.586057, stay_min=90, score=4.7)
    assert "亲子" in spot_tags(zoo)
    assert {"亲子", "室内"} <= set(spot_tags(polar))


def test_mingsha_gets_nature_and_hiking_tag():
    """敦煌·鸣沙山月牙泉（demo_spots.json source_id=2，ticket=110）：
    「泉」「山」→ 自然风光，「山」→ 登山徒步。"""
    s = Spot(source_id=2, name="鸣沙山月牙泉", lat=40.088833, lon=94.680396,
             stay_min=180, score=4.8, ticket=110)
    tags = spot_tags(s)
    assert "自然风光" in tags
    assert "登山徒步" in tags


def test_pedestrian_street_not_museum():
    """反例：成都·春熙路步行街（demo_spots.json source_id=10）是商业街，
    不含「博物馆」也不含任何室内场馆词；ticket=0 已知免费，其余标签全不命中。"""
    s = Spot(source_id=10, name="春熙路步行街", lat=30.655544, lon=104.077774,
             stay_min=120, score=4.9, ticket=0, open_h=10.0, close_h=22.0)
    tags = spot_tags(s)
    assert "博物馆" not in tags
    assert "室内" not in tags
    assert tags == ["免费"]


def test_food_tags_is_constant():
    assert food_tags() == ["美食"]


def test_spot_fields_keys_and_values():
    """spot_fields 键齐全且与 Spot 字段一致（真实值透传，不代填不换算）。"""
    s = _terracotta()
    f = spot_fields(s)
    assert set(f) == {"ticket", "ticket_known", "stay_min",
                      "open_h", "close_h", "tags"}
    assert f["ticket"] == 120
    assert f["ticket_known"] is True
    assert f["stay_min"] == 180
    assert f["open_h"] == 8.5
    assert f["close_h"] == 17.0
    assert f["tags"] == spot_tags(s)


def test_spot_tags_catalog_fixed():
    """13 个标签、固定顺序、无重复——顺序是接口契约，重排会打乱前端展示。"""
    assert SPOT_TAGS == ["免费", "亲子", "夜景", "室内", "自然风光", "历史文化",
                         "登山徒步", "古镇街区", "博物馆", "寺庙宗教", "海滨",
                         "演出", "美食"]
    assert len(set(SPOT_TAGS)) == len(SPOT_TAGS)

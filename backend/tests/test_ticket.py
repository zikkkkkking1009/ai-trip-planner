"""票价「已知 / 未知」的语义测试。

为什么值得单独一个文件：
票价有两个含义完全不同的零 —— **已知免费 0** 和 **我们不知道 0**。
把后者当成免费显示给用户，就是"看起来有数据、实际是假的"，
属于本项目最忌讳的静默错误（历史上「行程总门票 ¥0」就是这么来的）。

数据源的硬约束（已实测，别再试）：
**高德免费接口不给票价** —— 景点的 `biz_ext.cost` 返回空数组（酒店的
`biz_ext.lowest_price` 同理为空），所以票价只能来自：
① 攻略原文（LLM 抽取）② 我们自己的演示数据。两者都没有 → 必须是"未知"。
"""
import pytest

from aligner import AlignResult, PoiCandidate, POIAligner, align_spot
from demo_data import ticket_of
from models import Spot


def _spot(name="某景点", ticket=0, ticket_known=False):
    """默认造一个「票价未知」的景点（模拟攻略里没写价格的抽取结果）。"""
    return Spot(source_id=1, name=name, lat=34.26, lon=108.95,
                stay_min=90, score=8.0, ticket=ticket, ticket_known=ticket_known)


def _patch_align(monkeypatch, std_name):
    """把对齐器打桩成「命中标准名 std_name」，避开真实高德请求。"""
    monkeypatch.setattr(
        POIAligner, "align",
        lambda self, alias, city, hint_type=None: AlignResult(
            alias=alias, best=PoiCandidate(name=std_name, lat=34.3, lon=109.2),
            reason="ok"))


# ------------------------------------------------------------ demo_data.ticket_of

def test_ticket_of_distinguishes_free_from_unknown():
    """**这条是整个语义的基石**：0 = 已知免费，None = 未知。两者绝不能混。"""
    assert ticket_of("西安", "回民街") == 0, "回民街是已知免费，应该返回 0 而不是 None"
    assert ticket_of("西安", "秦始皇兵马俑博物馆") == 120
    assert ticket_of("西安", "一个根本不存在的景点") is None, "未收录必须返回 None（未知）"


def test_ticket_of_unknown_city_is_none():
    assert ticket_of("火星", "兵马俑") is None


# ------------------------------------------------------------ align_spot 回填

def test_align_fills_unknown_ticket_from_demo_data(monkeypatch):
    """未知票价 + 演示数据有 → 回填为真票价并标记为已知。

    ⚠️ 已知局限（不是 bug，是刻意的保守）：回填按**精确名字**匹配，而高德的
    标准名和演示数据未必一致（如「秦始皇帝陵博物院」vs「秦始皇兵马俑博物馆」），
    所以覆盖率并不是 100%。**没匹配上就保持未知**，不做模糊匹配 —— 宁可少填，
    也不能把别处的票价按到这个景点头上。
    """
    _patch_align(monkeypatch, "秦始皇兵马俑博物馆")
    s, _ = align_spot(_spot("兵马俑"), POIAligner(), "西安")
    assert s.ticket == 120, "应回填演示数据里的真实票价"
    assert s.ticket_known is True, "回填后必须标记为已知"


def test_align_keeps_unknown_when_demo_has_no_entry(monkeypatch):
    """未知票价 + 演示数据没有 → **保持未知**，不能悄悄填 0 当免费。"""
    _patch_align(monkeypatch, "某个没收录的景点")
    s, _ = align_spot(_spot("某个没收录的景点"), POIAligner(), "西安")
    assert s.ticket == 0
    assert s.ticket_known is False, "没拿到票价就必须保持未知，不能冒充免费"


def test_align_does_not_overwrite_known_ticket(monkeypatch):
    """已经是「已知」的票价不能被演示数据覆盖 —— 攻略明写的价格优先。"""
    _patch_align(monkeypatch, "秦始皇兵马俑博物馆")
    s, _ = align_spot(_spot("兵马俑", ticket=100, ticket_known=True),
                      POIAligner(), "西安")
    assert s.ticket == 100, "已知票价不该被演示数据改写"
    assert s.ticket_known is True


def test_align_fills_even_when_demo_price_is_zero(monkeypatch):
    """演示数据里 0 元（免费）也要能回填：从「未知」变成「已知免费」。"""
    _patch_align(monkeypatch, "回民街")
    s, _ = align_spot(_spot("回民街"), POIAligner(), "西安")
    assert s.ticket == 0
    assert s.ticket_known is True, "免费也是「已知」，不能停在未知"


# ------------------------------------------------------------ 模型默认值

def test_spot_defaults_to_ticket_known():
    """手工构造 / 演示数据的景点默认「已知」，避免历史数据全变成待查。"""
    assert Spot(source_id=1, name="x", lat=0, lon=0, stay_min=90,
                score=8.0).ticket_known is True

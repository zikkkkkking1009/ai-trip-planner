"""地理聚类构造（N2）测试：算法正确性 + 「不劣于旧版」的不变量。

为什么在"实测无增量收益、默认关闭"之后仍然保留这套测试：
1. 它是「通勤权重可调」上线后的候选优化，代码留着就要有测试守着；
2. **"取最优 ⇒ 不可能更差"这个不变量必须被测住** —— 实验过程中真的踩过：
   聚类轮忘了消耗随机数 → 后续抖动轮 rng 序列错位 → 两条路径不可比，
   表面上"通勤降了"实际是不可比的假象（详见 docs/experiments.md 实验五）。
"""
from __future__ import annotations

from demo_data import XI_AN_SPOTS
from models import PlanRequest
from solver import Solver


def _req(days: int = 2, budget: float | None = 500, k: int | None = None) -> PlanRequest:
    spots = [s.model_copy() for s in XI_AN_SPOTS]
    if k:
        spots = spots[:k]
    return PlanRequest(city="西安", days=days, budget=budget, spots=spots)


def test_clusters_are_geographically_separated():
    """西安 14 个景点里，临潼的两个（兵马俑、华清宫，距市区约 40km）应被聚到同一簇。"""
    clusters = Solver._geo_clusters([s.model_copy() for s in XI_AN_SPOTS], 2)
    assert len(clusters) == 2
    assert sum(len(c) for c in clusters) == len(XI_AN_SPOTS)
    lantong = {"秦始皇兵马俑博物馆", "华清宫"}
    assert any(lantong <= {s.name for s in c} for c in clusters), \
        f"临潼景点未被聚到一起：{[sorted(s.name for s in c) for c in clusters]}"


def test_cluster_order_is_a_permutation():
    spots = [s.model_copy() for s in XI_AN_SPOTS]
    order = Solver._cluster_order(spots, 3)
    assert len(order) == len(spots)
    assert {s.name for s in order} == {s.name for s in spots}


def test_cluster_order_empty_when_no_gain():
    """只有 1 个簇（或景点数 <= 天数）时不额外消耗多起点的一轮。"""
    assert Solver._cluster_order([s.model_copy() for s in XI_AN_SPOTS[:2]], 1) == []


def test_clustering_is_deterministic():
    """确定性初始化：同一批输入两次聚类结果完全一致（可复现性纪律）。"""
    spots = [s.model_copy() for s in XI_AN_SPOTS]
    a = [[s.name for s in c] for c in Solver._geo_clusters(spots, 3)]
    b = [[s.name for s in c] for c in Solver._geo_clusters(spots, 3)]
    assert a == b


def test_geo_cluster_never_worse_than_disabled(monkeypatch):
    """核心不变量：开启聚类仍然取目标最优，且 rng 序列一致 ⇒ 目标值不低于关闭时。"""
    import solver as solver_mod

    req = _req(days=2, budget=500)

    monkeypatch.setattr(solver_mod, "USE_GEO_CLUSTER", False)
    s_off = Solver(req)
    days_off, _, _, score_off = s_off.solve()
    obj_off = 1000 * score_off - sum(d.commute_min for d in days_off)

    monkeypatch.setattr(solver_mod, "USE_GEO_CLUSTER", True)
    s_on = Solver(req)
    days_on, _, _, score_on = s_on.solve()
    obj_on = 1000 * score_on - sum(d.commute_min for d in days_on)

    assert obj_on >= obj_off - 1e-6, f"开启聚类后目标值下降：{obj_off} → {obj_on}"


def test_single_day_skips_clustering(monkeypatch):
    """单天行程没有"分天"可聚，不应走聚类分支（性能与语义都要对）。"""
    import solver as solver_mod

    called = {"n": 0}
    original = Solver._cluster_order.__func__

    def spy(cls, *args, **kwargs):
        called["n"] += 1
        return original(cls, *args, **kwargs)

    monkeypatch.setattr(solver_mod, "USE_GEO_CLUSTER", True)
    monkeypatch.setattr(Solver, "_cluster_order", classmethod(spy))
    Solver(_req(days=1)).solve()
    assert called["n"] == 0, "单天行程不该调用聚类"

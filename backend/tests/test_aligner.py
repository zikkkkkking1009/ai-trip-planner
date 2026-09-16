"""实体对齐打分函数单元测试（纯函数，不调 API）。"""
from aligner import (containment, extension_bonus, text_similarity,
                     type_flag)


def test_containment_exact_match():
    assert containment("兵马俑", "兵马俑") == 1.0


def test_containment_substring():
    assert containment("兵马俑", "秦始皇兵马俑博物馆") == 0.85


def test_containment_no_overlap():
    assert containment("紫禁城", "故宫博物院") == 0.0


def test_text_similarity_range_and_ordering():
    near = text_similarity("西安城墙", "西安城墙景区")
    far = text_similarity("紫禁城", "故宫博物院")
    assert 0.0 <= far <= near <= 1.0


def test_type_flag_attraction_is_positive():
    assert type_flag("风景名胜;风景名胜;国家级景点") == 1
    assert type_flag("科教文化服务;博物馆;博物馆") == 1


def test_type_flag_noise_is_negative():
    assert type_flag("交通设施服务;公交车站;公交车站相关") == -1
    assert type_flag("地名地址信息;普通地名;区县级地名") == -1
    assert type_flag("生活服务;洗浴推拿场所;洗浴推拿场所") == -1


def test_extension_bonus_prefers_museum_over_lake():
    # 「碑林」+「博物馆」应拿到扩展加分；「碑林」+「湖」不该拿
    assert extension_bonus("碑林", "西安碑林博物馆") > 0
    assert extension_bonus("碑林", "碑林湖") == 0


def test_extension_bonus_no_false_positive_on_plaza():
    # 「广场」曾导致钟楼对齐到奥莱商场——回归测试
    assert extension_bonus("钟楼", "临潼奥莱钟楼广场") == 0

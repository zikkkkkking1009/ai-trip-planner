"""实体对齐打分函数单元测试（纯函数，不调 API）。"""
from aligner import (POIAligner, PoiCandidate, containment,
                     detect_renamed_root, extension_bonus, text_similarity,
                     type_flag)


def test_containment_exact_match():
    assert containment("兵马俑", "兵马俑") == 1.0


def test_containment_substring():
    assert containment("兵马俑", "秦始皇兵马俑博物馆") == 0.85


def test_containment_no_overlap():
    assert containment("紫禁城", "故宫博物院") == 0.0


def test_containment_tailed_subvenue_is_not_containment():
    # 「北京国际雕塑公园-远望紫禁城」：长前缀 + 别名在尾部 = 别的 POI 在引用别名
    assert containment("紫禁城", "北京国际雕塑公园-远望紫禁城") == 0.0


def test_containment_short_prefix_suffix_still_counts():
    # 前缀只有 2 字（如「西安回民街」）不算子景点结构，保持普通包含分
    assert containment("回民街", "西安回民街") == 0.85


def test_text_similarity_zero_for_tailed_subvenue():
    assert text_similarity("紫禁城", "北京国际雕塑公园-远望紫禁城") == 0.0


def test_detect_renamed_root_huaching():
    pois = [
        PoiCandidate(name="华清池", lat=34.36, lon=109.21,
                     type_str="风景名胜;风景名胜;国家级景点",
                     address="骊山街道华清路38号"),
        PoiCandidate(name="华清宫-杨妃池", lat=34.36, lon=109.21,
                     type_str="风景名胜;风景名胜相关;旅游景点",
                     address="骊山街道华清路38号华清池景区内"),
        PoiCandidate(name="华清宫-香凝池", lat=34.36, lon=109.21,
                     type_str="风景名胜;风景名胜相关;旅游景点",
                     address="华清路3号华清宫(华清池地铁站C口步行370米)"),
    ]
    assert detect_renamed_root("华清池", pois) == "华清宫"


def test_detect_renamed_root_none_when_no_evidence():
    pois = [
        PoiCandidate(name="成都大熊猫繁育研究基地", lat=30.0, lon=104.0,
                     type_str="风景名胜;风景名胜;国家级景点",
                     address="熊猫大道1375号"),
        PoiCandidate(name="成都大熊猫繁育研究基地-熊猫塔", lat=30.0, lon=104.0,
                     type_str="风景名胜;风景名胜;风景名胜",
                     address="熊猫大道1375号成都大熊猫繁育研究基地内(西侧)"),
    ]
    # 只有 1 个「主名-子点」且地址不引用别名 → 证据不足
    assert detect_renamed_root("熊猫基地", pois) is None


def test_detect_renamed_root_none_for_full_name_containment():
    # 「西湖」是「西湖风景名胜区」的子串：全称包含别名是正常匹配，不是更名
    pois = [
        PoiCandidate(name="杭州西湖风景名胜区-湖滨公园", lat=30.0, lon=120.0,
                     type_str="风景名胜;风景名胜;风景名胜",
                     address="杭州西湖风景名胜区内"),
    ]
    assert detect_renamed_root("西湖", pois) is None


def test_arbitrate_accepts_cached_pick_within_pool():
    a = POIAligner.__new__(POIAligner)  # 跳过 __init__，不读磁盘缓存
    a.stats = {"llm_arbitrations": 0}
    pool = [
        PoiCandidate(name="故宫博物院", lat=39.9, lon=116.4),
        PoiCandidate(name="北京国际雕塑公园-远望紫禁城", lat=39.9, lon=116.2),
    ]
    import hashlib as _h
    ck = ("北京|紫禁城|"
          + _h.md5("|".join(p.name for p in pool).encode("utf-8")).hexdigest()[:8])
    a.arb_cache = {ck: "故宫博物院"}
    picked = POIAligner._arbitrate(a, "紫禁城", "北京", pool)
    assert picked is not None and picked.name == "故宫博物院"


def test_arbitrate_rejects_pick_outside_pool(tmp_path, monkeypatch):
    import aligner as _mod
    monkeypatch.setattr(_mod, "ARB_CACHE_FILE", tmp_path / "arb.json")
    a = POIAligner.__new__(POIAligner)
    a.arb_cache = {}
    a.stats = {"llm_arbitrations": 0}
    pool = [
        PoiCandidate(name="故宫博物院", lat=39.9, lon=116.4),
        PoiCandidate(name="北京国际雕塑公园-远望紫禁城", lat=39.9, lon=116.2),
    ]
    # LLM 返回不存在于候选集的名字（幻觉）→ 忽略，返回 None
    a._call_llm_arbiter = staticmethod(lambda *args, **kw: "不存在的POI")
    assert POIAligner._arbitrate(a, "紫禁城", "北京", pool) is None


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

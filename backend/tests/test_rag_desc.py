"""语料增强测试：SPOT_DESCS 覆盖率闸门 + 内容纪律（无营销话术、长度合理）+ 语料拼接生效。"""
from __future__ import annotations

from demo_data import DEMO_SPOTS
from rag import build_corpus
from spot_desc import SPOT_DESCS


def test_all_spots_have_usable_desc():
    """覆盖率闸门：每个库内景点要么自带 desc，要么有 SPOT_DESCS 兜底。"""
    missing = [f"{c}/{s.name}" for c, spots in DEMO_SPOTS.items() for s in spots
               if not (s.desc and len(s.desc.strip()) >= 10) and s.name not in SPOT_DESCS]
    assert not missing, f"缺描述 {len(missing)} 条，例如 {missing[:5]}"


def test_desc_keys_all_in_corpus():
    """SPOT_DESCS 键必须与库内实体名完全一致——防错别字静默失效。"""
    names = {s.name for spots in DEMO_SPOTS.values() for s in spots}
    stray = [k for k in SPOT_DESCS if k not in names]
    assert not stray, f"SPOT_DESCS 有库外键 {stray}"


def test_desc_discipline():
    """内容纪律：无营销话术、长度合理（12~60 字）。"""
    banned = ["必去", "打卡", "网红", "欢迎", "最佳", "首选", "一站式", "超值", "特价"]
    bad = [k for k, d in SPOT_DESCS.items() if any(b in d for b in banned)]
    assert not bad, f"营销话术 {bad}"
    odd = [k for k, d in SPOT_DESCS.items() if not 12 <= len(d) <= 60]
    assert not odd, f"长度异常 {odd}"


def test_build_corpus_uses_enhanced_desc():
    """语料拼接生效：抓取景点用兜底描述，手写景点不受影响。"""
    text = {d["name"]: d["text"] for d in build_corpus()}
    assert "江南古典园林" in text["拙政园"]
    assert "兵马俑" in text["秦始皇兵马俑博物馆"]

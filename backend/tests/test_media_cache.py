"""媒体缓存 key 规则测试：同名景点跨城市不能串味。

为什么值得专门测：这是**静默错误**类问题——串味不会报错，只会让「北京钟楼」
显示西安的图片与地址。所以用测试把 key 规则钉死。
"""
from __future__ import annotations

import json

import pytest

import media_cache
from media_cache import (get_entry, load_media, media_key, put_media,
                         save_media, suspected_wrong_city)


@pytest.fixture
def media_file(tmp_path, monkeypatch):
    """把缓存文件指到临时目录，隔离真实数据。"""
    f = tmp_path / "spot_media.json"
    monkeypatch.setattr(media_cache, "MEDIA_FILE", f)
    return f


def test_media_key_format():
    assert media_key("成都", "人民公园") == "成都|人民公园"
    assert media_key("", "人民公园") == "|人民公园"
    assert media_key(None, "人民公园") == "|人民公园"
    assert media_key("  北京  ", "钟楼") == "北京|钟楼"


def test_load_missing_file_returns_empty(media_file):
    assert load_media() == {}


def test_load_corrupted_file_returns_empty_not_raise(media_file):
    media_file.write_text("{ 这不是合法 JSON", encoding="utf-8")
    assert load_media() == {}   # 损坏按空处理，不抛异常（缓存可重建）


def test_put_then_get_roundtrip(media_file):
    put_media("成都", "人民公园", {"image": "cd.jpg", "address": "成都某路"})
    assert get_entry(load_media(), "成都", "人民公园")["image"] == "cd.jpg"
    # 落盘的 key 必须带城市
    raw = json.loads(media_file.read_text(encoding="utf-8"))
    assert "成都|人民公园" in raw


def test_same_name_different_city_not_confused(media_file):
    """核心断言：同名景点在不同城市取到各自的数据。"""
    put_media("成都", "人民公园", {"image": "cd.jpg", "address": "成都少城路"})
    put_media("上海", "人民公园", {"image": "sh.jpg", "address": "上海南京西路"})
    media = load_media()
    assert get_entry(media, "成都", "人民公园")["image"] == "cd.jpg"
    assert get_entry(media, "上海", "人民公园")["image"] == "sh.jpg"
    assert get_entry(media, "北京", "人民公园") is None   # 没抓过的城市不应命中别的城市


def test_falls_back_to_legacy_bare_key(media_file):
    """迁移前的旧数据（裸名 key）仍要能读到，避免升级后详情卡集体 404。"""
    media_file.write_text(json.dumps({"人民公园": {"image": "old.jpg"}},
                                     ensure_ascii=False), encoding="utf-8")
    assert get_entry(load_media(), "成都", "人民公园")["image"] == "old.jpg"


def test_city_key_takes_precedence_over_legacy(media_file):
    """同时存在新旧 key 时，优先用带城市的（新数据更准）。"""
    media_file.write_text(json.dumps({
        "人民公园": {"image": "legacy.jpg"},
        "成都|人民公园": {"image": "new.jpg"},
    }, ensure_ascii=False), encoding="utf-8")
    assert get_entry(load_media(), "成都", "人民公园")["image"] == "new.jpg"


def test_save_is_atomic_no_tmp_left(media_file):
    save_media({"成都|人民公园": {"image": "x.jpg"}})
    assert media_file.exists()
    assert not media_file.with_name(media_file.name + ".tmp").exists()
    assert json.loads(media_file.read_text(encoding="utf-8"))["成都|人民公园"]["image"] == "x.jpg"


def test_save_failure_does_not_raise(monkeypatch, tmp_path):
    """写失败（磁盘满/权限）不能中断主流程。"""
    monkeypatch.setattr(media_cache, "MEDIA_FILE", tmp_path / "nonexistent_dir" / "x.json")
    save_media({"a": 1})   # 目录不存在 → OSError，应被吞掉并记日志


def test_suspected_wrong_city_catches_real_case():
    """真实案例：裸名「四川博物院」里存的其实是**西安博物院**的数据。

    载荷地址「友谊西路72号」、坐标 34.238589/108.941673 与 `西安|西安博物院` 逐字段相同
    ——高德 citylimit 不严格，用西安搜「四川博物院」返回了西安本地的结果。
    裸名 key 又会被 `get_entry` 当作**任何城市**的回退命中，于是所有城市都查到西安数据。
    """
    payload = {"lat": 34.238589, "lon": 108.941673, "address": "友谊西路72号"}
    assert suspected_wrong_city("成都", payload) == "西安"


def test_suspected_wrong_city_does_not_flag_legit_far_suburbs():
    """判据必须是相对的：合法远郊景点不能被误伤。

    这些都是真实数据——武隆天生三桥离重庆市中心 122km（行政上确属重庆）、
    都江堰离成都 65km、八达岭离北京 60km、兵马俑离西安 32km。
    如果按"离市中心多远"一刀切，它们全会被误判成错城市。
    """
    assert suspected_wrong_city("重庆", {"lat": 29.4246, "lon": 107.7589}) is None
    assert suspected_wrong_city("成都", {"lat": 31.0026, "lon": 103.6184}) is None
    assert suspected_wrong_city("北京", {"lat": 40.3560, "lon": 116.0200}) is None
    assert suspected_wrong_city("西安", {"lat": 34.3847, "lon": 109.2785}) is None


def test_suspected_wrong_city_returns_none_when_undecidable():
    """判不了就说判不了——不能为了"有结论"而误判。"""
    assert suspected_wrong_city("成都", {"image": "x.jpg"}) is None      # 载荷无坐标
    assert suspected_wrong_city("成都", None) is None
    assert suspected_wrong_city("某某市", {"lat": 34.2386, "lon": 108.9417}) is None  # 城市不在表里


def test_suspected_wrong_city_accepts_correct_city():
    """同一份载荷，请求城市正确时不该被拦（避免把正常路径也挡掉）。"""
    payload = {"lat": 34.238589, "lon": 108.941673}
    assert suspected_wrong_city("西安", payload) is None
    assert suspected_wrong_city("西安市", payload) is None    # 归一化后同样判定为西安


def _load_migration_module():
    """`tools/` 不在 backend 的 import 路径上，按文件路径加载迁移脚本。"""
    import importlib.util
    from pathlib import Path

    p = (Path(__file__).resolve().parent.parent.parent
         / "tools" / "migrate_media_keys.py")
    spec = importlib.util.spec_from_file_location("migrate_media_keys", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_does_not_drop_data_on_key_collision():
    """迁移时的 key 撞车不能静默丢数据，且结果不能依赖字典遍历顺序。

    背景：裸名 `人民公园` 迁移后的目标 `成都|人民公园` 可能与缓存里**已经存在**的
    带城市 key 撞车。旧实现是一遍 `out[key] = val` 无条件写 —— 谁后写谁活，
    同一份数据换个插入顺序就得到不同结果，其中一种**丢掉一整条**（静默错误）。
    """
    mig = _load_migration_module()
    rich = {"image": "x.jpg", "address": "成都少城路", "intro": "介绍"}
    thin = {"image": "y.jpg"}
    n2c = {"人民公园": ["成都"]}

    # 带城市在前 / 裸名在前 —— 两种顺序必须得到同一个结果
    a = mig.plan_migration({"成都|人民公园": rich, "人民公园": thin}, n2c)
    b = mig.plan_migration({"人民公园": thin, "成都|人民公园": rich}, n2c)
    assert a["out"] == b["out"], "结果依赖了字典遍历顺序，说明还有顺序依赖"
    assert a["out"]["成都|人民公园"] == rich, "应当保留信息量更大的那份"
    assert a["conflicts"], "撞车必须被报告出来，而不是悄悄处理掉"
    # 撞车时条目数不增加（合并成一条），但绝不能凭空少数
    assert len(a["out"]) == 1

    # 裸名那份更完整时，应当采用裸名那份
    c = mig.plan_migration({"成都|人民公园": thin, "人民公园": rich}, n2c)
    assert c["out"]["成都|人民公园"] == rich


def test_migration_keeps_unrelated_entries():
    """迁移不能顺手丢掉带城市的条目或无关键。"""
    mig = _load_migration_module()
    media = {"成都|人民公园": {"image": "a"}, "上海|人民公园": {"image": "b"},
             "某不知名酒店": {"image": "c"}}
    plan = mig.plan_migration(media, {"人民公园": ["成都", "上海"]})
    assert plan["out"]["成都|人民公园"] == {"image": "a"}
    assert plan["out"]["上海|人民公园"] == {"image": "b"}
    assert plan["out"]["某不知名酒店"] == {"image": "c"}     # 查不到城市的原样保留
    assert len(plan["out"]) == 3


def test_migration_reports_ambiguous_bare_key():
    """裸名同名跨城市 → 无法判断归属，必须保留原样并**报告**（不能随便挑一个城市）。"""
    mig = _load_migration_module()
    plan = mig.plan_migration({"人民公园": {"image": "x"}},
                              {"人民公园": ["成都", "上海"]})
    assert plan["out"]["人民公园"] == {"image": "x"}
    assert plan["ambiguous"], "同名跨城市必须报告出来交给人工确认"
    assert plan["renamed"] == []


def test_committed_spot_media_has_no_bare_keys():
    """守卫：仓库里实际提交的 spot_media.json **不允许**出现裸名 key。

    为什么值得测：迁移曾漏掉 7 条（酒店/博物馆等非 demo 景点），而裸名 key 会被
    `get_entry()` 当作**任何城市**的回退命中，直接造成跨城市串味——静默错误。
    这里直接读真实数据文件钉死规则，防止回归。

    历史上唯一的例外是「四川博物院」：它的载荷地址「友谊西路72号」、坐标
    34.2386/108.9417 与「西安|西安博物院」**逐字段完全相同**（高德 citylimit 不严格，
    用西安查四川博物院返回了西安本地的结果并被缓存）。名实冲突无法迁移，且留着这条
    等于让所有城市都查到西安数据 —— 2026-09-23 已**删除该条目**（宁可回落成「未收录」
    再按正确城市重新抓取，也不要留着错数据）。因此白名单为空。
    """
    raw = json.loads(media_cache.MEDIA_FILE.read_text(encoding="utf-8"))
    bare = {k for k in raw if "|" not in k}
    assert not bare, f"出现裸名 key（会让同名景点跨城市串味）：{bare}"

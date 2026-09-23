"""媒体缓存 key 规则测试：同名景点跨城市不能串味。

为什么值得专门测：这是**静默错误**类问题——串味不会报错，只会让「北京钟楼」
显示西安的图片与地址。所以用测试把 key 规则钉死。
"""
from __future__ import annotations

import json

import pytest

import media_cache
from media_cache import (get_entry, load_media, media_key, put_media,
                         save_media)


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

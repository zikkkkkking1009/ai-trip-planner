"""`/meta` 接口测试：首页「接口清单 / 服务状态」的数据源必须真实、且不泄露 Key。

**全程不联网**：/meta 本身只读内存与常量，测试也只用 TestClient 打本地应用。

这里最关键的两条：
- 清单必须**确实包含**若干核心路由 —— 否则将来有人重命名路由，首页清单会悄悄失真，
  正是本项目最忌讳的「静默错误」。
- 响应里绝不能出现任何 Key 的值 —— 这是会直接渲染到页面的接口，泄了就是事故。
"""
from __future__ import annotations

import sys

from fastapi.testclient import TestClient

import main
from cities import CITY_CENTERS
from demo_data import demo_cities, demo_spots
from main import app

# 明显是假值即可，用来验证「配置状态会反映，但值本身不外泄」
FAKE_LLM_KEY = "sk-fake-llm-key-for-leak-test"
FAKE_AMAP_KEY = "fake-amap-key-for-leak-test"


def _meta() -> dict:
    return TestClient(app).get("/meta").json()


def test_catalog_contains_core_routes():
    """防「路由重命名但清单不变」：核心接口必须真的出现在清单里。"""
    paths = {e["path"] for e in _meta()["endpoints"]}
    assert {"/weather", "/plan/async", "/health"} <= paths


def test_catalog_size_has_headroom():
    """接口数会随开发增长，只卡下界并留余量，不写死精确值（避免一加接口就红）。"""
    assert len(_meta()["endpoints"]) > 20


def test_catalog_entries_are_wellformed():
    for e in _meta()["endpoints"]:
        assert e["methods"], f"{e['path']} 没有任何 HTTP 方法"
        for m in e["methods"]:
            assert m == m.upper(), f"{e['path']} 的方法不是大写：{m!r}"
        assert e["path"].startswith("/"), f"path 必须以 / 开头：{e['path']!r}"
        assert e["summary"].strip(), f"{e['path']} 的摘要为空"


def test_skipped_routes_are_declared():
    """框架自带路由必须**明说**被跳过，不能静默丢掉。"""
    skipped = {s["path"] for s in _meta()["skipped"]}
    assert {"/docs", "/openapi.json", "/redoc"} <= skipped


def test_status_reuses_health_logic():
    """status 里的 health 字段必须与 /health 同源 —— 防止两处各写一份而口径漂移。"""
    health = TestClient(app).get("/health").json()
    status = _meta()["status"]
    assert {k: status[k] for k in health} == health


def test_key_states_only_configured_or_missing():
    status = _meta()["status"]
    assert status["llm"] in {"configured", "missing"}
    assert status["amap_key"] in {"configured", "missing"}


def test_llm_key_value_never_leaks(monkeypatch):
    """配置了 LLM Key → 只回 configured，且响应体里找不到 Key 本身（防泄露）。"""
    monkeypatch.setattr(main, "load_env_file", lambda: {"LLM_API_KEY": FAKE_LLM_KEY})
    monkeypatch.setenv("LLM_API_KEY", FAKE_LLM_KEY)
    r = TestClient(app).get("/meta")
    assert r.json()["status"]["llm"] == "configured"
    assert FAKE_LLM_KEY not in r.text


def test_amap_key_value_never_leaks(monkeypatch):
    """AMAP 同理：只回 configured，不回值。"""
    monkeypatch.setattr(main, "load_env_file", lambda: {"AMAP_KEY": FAKE_AMAP_KEY})
    monkeypatch.setenv("AMAP_KEY", FAKE_AMAP_KEY)
    r = TestClient(app).get("/meta")
    assert r.json()["status"]["amap_key"] == "configured"
    assert FAKE_AMAP_KEY not in r.text


def test_city_and_spot_counts_match_real_sources():
    """规模数字必须与真实来源一致：直接对比 cities / demo_data，不写死。"""
    status = _meta()["status"]
    assert status["cities"] == len(CITY_CENTERS)
    cities = demo_cities()
    assert status["spots"]["total"] == sum(len(demo_spots(c)) for c in cities)
    assert status["spots"]["by_city"] == {c: len(demo_spots(c)) for c in cities}


def test_runtime_identity_fields():
    status = _meta()["status"]
    assert status["python"] == sys.version.split()[0]
    assert status["version"] == app.version

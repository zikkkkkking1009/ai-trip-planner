"""`/meta` 接口测试：首页「接口清单 / 服务状态」的数据源必须真实、且不泄露 Key。

**全程不联网**：/meta 只读内存快照，测试也只用 TestClient 打本地应用；
Key 探测请求被 conftest 的护栏（SELFTEST_SKIP=1 + _spawn 置空）整体掐掉。

这里最关键的三条：
- 清单必须**确实包含**若干核心路由 —— 否则将来有人重命名路由，首页清单会悄悄失真，
  正是本项目最忌讳的「静默错误」。
- Key 状态的取值集合要钉死 —— 防止哪天有人把「未探测」偷懒显示成「已配置」。
- 响应里绝不能出现任何 Key 的值 —— 这是会直接渲染到页面的接口，泄了就是事故。
"""
from __future__ import annotations

import sys
import time

from fastapi.testclient import TestClient

import main
import selftest
from cities import CITY_CENTERS
from demo_data import demo_cities, demo_spots
from main import app

# 明显是假值即可，用来验证「配置状态会反映，但值本身不外泄」
FAKE_LLM_KEY = "sk-fake-llm-key-for-leak-test"
FAKE_AMAP_KEY = "fake-amap-key-for-leak-test"

# Key 状态的四态集合（快通道另有 inherited，这里不涉及）
KEY_STATES = {"missing", "ok", "invalid", "unreachable"}


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


def test_key_states_use_four_state_machine():
    """四态，不是旧的 configured/missing 两态。

    为什么必须是四态：configured 只说明「变量非空」，不代表能用 ——
    无效 Key 与网络不通的修复动作完全相反，混在一起会让人白白重置一个没坏的 Key。
    """
    status = _meta()["status"]
    assert status["llm"] in KEY_STATES
    assert status["amap_key"] in KEY_STATES
    assert status["llm_fast"] in KEY_STATES | {"inherited"}


def test_configured_flag_reflects_env_without_probing(monkeypatch):
    """「是否配置了」与「是否可用」是两回事：前者零探测也要如实返回。"""
    monkeypatch.setenv("LLM_API_KEY", FAKE_LLM_KEY)
    monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
    monkeypatch.setenv("AMAP_KEY", FAKE_AMAP_KEY)
    status = _meta()["status"]
    assert status["configured"] == {"llm": True, "llm_fast": True, "amap_key": True}
    # 没探测过就只能标 missing（配了但没探到的情形由 probe.state 表达），
    # 绝不能在这时候谎称 ok —— 那是把「没验证过」说成「验证过可用」
    assert status["llm"] == "missing"
    assert status["probe"]["state"] in {"idle", "probing", "ready"}


def test_meta_never_waits_for_probe(monkeypatch):
    """/meta 必须**零外呼、不阻塞**：哪怕快照过期要刷新，也是先返回旧值。

    这条锁的是当初的设计底线（状态接口不该有超时/重试风险）——
    一旦有人把「探测」搬进请求路径，这个接口就会随外网上下起伏。
    """
    monkeypatch.setattr(selftest, "probe_all", lambda: time.sleep(5))
    selftest._snapshot.update({"state": "ready", "at": selftest._now() - 10 ** 9,
                               "stale": False})
    t0 = time.time()
    r = TestClient(app).get("/meta")
    assert time.time() - t0 < 2.0, "/meta 被探测拖慢了 —— 请求路径上混进了外呼"
    assert r.status_code == 200
    assert r.json()["status"]["probe"]["stale"] is True


def test_startup_probe_does_not_block_lifespan(monkeypatch):
    """lifespan 只投递不等待：探测再慢，应用也要立刻可服务（/health 是 healthcheck）。"""
    def slow_probe():
        time.sleep(3)
    monkeypatch.setattr(selftest, "schedule_probe", slow_probe)
    t0 = time.time()
    with TestClient(app):
        assert time.time() - t0 < 1.5, "启动被自检阻塞了 —— lifespan 里混进了同步等待"
    # （退出阶段可能要等后台线程收尾，所以只对「进入」计时）


def test_meta_no_network_when_no_key(monkeypatch):
    """无 Key 时 /meta 一次外呼都不该有 —— 谁把外呼搬进来这条就红。"""
    def forbidden(*a, **kw):
        raise AssertionError("无 Key 的 /meta 不应发起任何 Key 探测")
    monkeypatch.setattr(selftest, "_amap_probe_once", forbidden)
    monkeypatch.setattr(selftest, "_probe_llm", forbidden)
    assert TestClient(app).get("/meta").status_code == 200


def test_llm_key_value_never_leaks(monkeypatch):
    """**任何**状态下响应体都不允许出现 Key 本身 —— 会直接渲染到首页，泄了就是事故。

    用「探测成功」的快照来测：这是最容易顺手把调试信息（含 Key）带出去的状态。
    """
    monkeypatch.setenv("LLM_API_KEY", FAKE_LLM_KEY)
    monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
    selftest._snapshot.update({"state": "ready", "at": selftest._now(), "stale": False,
                               "llm": "ok", "llm_fast": "inherited", "amap_key": "missing",
                               "reasons": {"llm": "", "llm_fast": "", "amap_key": ""}})
    r = TestClient(app).get("/meta")
    assert r.json()["status"]["llm"] == "ok"
    assert FAKE_LLM_KEY not in r.text
    assert "sk-fake" not in r.text


def test_amap_key_value_never_leaks(monkeypatch):
    """高德同理：只回状态，不回值；连原因枚举里都不允许出现 Key。"""
    monkeypatch.setenv("AMAP_KEY", FAKE_AMAP_KEY)
    selftest._snapshot.update({"state": "ready", "at": selftest._now(), "stale": False,
                               "llm": "missing", "llm_fast": "inherited", "amap_key": "ok",
                               "reasons": {"llm": "", "llm_fast": "", "amap_key": ""}})
    r = TestClient(app).get("/meta")
    assert r.json()["status"]["amap_key"] == "ok"
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

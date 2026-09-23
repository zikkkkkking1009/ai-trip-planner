"""Key 自检（backend/selftest.py）测试：**全程不联网**。

被测对象的四个接缝全部 monkeypatch 掉，一个真请求都不会发：
- `selftest._env`                → 换成字典桩（连 SELFTEST_SKIP 的读取也一并受控）
- `selftest._amap_probe_once`    → 高德探测函数本体
- `editor._llm`                  → LLM 客户端来源（自检与生产共用这一个口径）
- `selftest._spawn` / `_now`     → 线程与时钟

写这些用例的动机：四态状态机里最容易写错的就是「把网络不通说成 Key 无效」
（用例 4/5 成对锁死），以及「探测结果里泄漏 Key 值」（用例 13）。
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from types import SimpleNamespace

import pytest

import editor
import selftest
from cities import CITY_CENTERS


# ---------- 桩与夹具 ----------

def _pristine() -> dict:
    """selftest 的快照与 _refreshing 是模块级全局 —— 每个测试复位，防止串味。"""
    return {"state": "idle", "at": 0.0, "stale": False,
            "llm": "missing", "llm_fast": "missing", "amap_key": "missing",
            "reasons": {"llm": "", "llm_fast": "", "amap_key": ""}}


@pytest.fixture(autouse=True)
def _fresh_state():
    selftest._snapshot.update(_pristine())
    selftest._refreshing = False
    yield
    selftest._snapshot.update(_pristine())
    selftest._refreshing = False


def _env(monkeypatch, **values):
    """把 selftest._env 换成字典桩：可完全控制「环境里有什么 Key」。

    连 SELFTEST_SKIP 的读取也被换掉了 —— 所以 conftest 里那条
    「SELFTEST_SKIP=1」的全局护栏不会干扰这些用例。
    """
    table = {k: str(v) for k, v in values.items()}

    def fake(name: str) -> str:
        return table.get(name, "")
    monkeypatch.setattr(selftest, "_env", fake)
    return table


def _amap_calls(monkeypatch, result=("ok", "")):
    """把高德探测换成桩，记录每次拿到的 Key。"""
    calls: list[str] = []
    monkeypatch.setattr(selftest, "_amap_probe_once",
                        lambda key: (calls.append(key), result)[1])
    return calls


class _FakeCompletions:
    def __init__(self, sink, exc=None):
        self._sink, self._exc = sink, exc

    def create(self, **kw):
        self._sink.append(kw)
        if self._exc is not None:
            raise self._exc
        return {"choices": []}


class _FakeClient:
    def __init__(self, sink, exc=None):
        self.chat = SimpleNamespace(completions=_FakeCompletions(sink, exc))

    def with_options(self, timeout):
        self.timeout = timeout
        return self


def _stub_llm(monkeypatch, exc=None):
    """把 editor._llm 换成桩，记录 (fast, kwargs)。"""
    sink: list[dict] = []
    calls: list[bool] = []

    def fake_llm(fast: bool = False):
        calls.append(fast)
        return _FakeClient(sink, exc), "stub-model"
    monkeypatch.setattr(editor, "_llm", fake_llm)
    return sink, calls


class _FakeStatusError(Exception):
    """带 status_code 的异常 —— 模拟 openai.APIStatusError 家族的可读面。

    不直接构造真的 openai 异常：那需要 httpx.Response，而这里要测的是
    selftest 的**分类逻辑**（读 status_code / 异常类名），不是 SDK 本身。
    """

    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class APITimeoutError(Exception):
    """类名与 openai.APITimeoutError 相同 —— selftest 的分类是按类名匹配的
    （不在 selftest 顶层 import openai：openai 缺装本身就是一种要上报的状态），
    所以桩必须同名才等价。"""


class APIConnectionError(Exception):
    pass


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------- 用例 ----------

def test_no_key_means_missing_and_zero_calls(monkeypatch):
    """一个 Key 都没配：三通道全 missing，且**一次外呼都没有**（CI 天然零外呼的守门）。"""
    _env(monkeypatch)
    amap_calls = _amap_calls(monkeypatch)
    _llm_calls = _stub_llm(monkeypatch)[1]

    selftest.probe_all()
    snap = selftest.status()
    assert (snap["llm"], snap["llm_fast"], snap["amap_key"]) == ("missing", "missing", "missing")
    assert snap["state"] == "idle"          # 没配 Key 就根本没探，不是「探了都失败」
    assert amap_calls == []
    assert _llm_calls == []


def test_skip_switch_disables_probe(monkeypatch):
    """SELFTEST_SKIP=1：就算配了 Key 也不发任何请求（离线演示开关）。"""
    calls = _amap_calls(monkeypatch)
    _env(monkeypatch, SELFTEST_SKIP="1", AMAP_KEY="fake-amap",
         LLM_API_KEY="sk", LLM_BASE_URL="https://x/v1")
    selftest.probe_all()
    snap = selftest.status()
    assert snap["state"] == "idle"
    assert snap["amap_key"] == "missing"
    assert calls == []


def test_llm_ok_on_success(monkeypatch):
    _env(monkeypatch, LLM_API_KEY="sk", LLM_BASE_URL="https://x/v1")
    sink, calls = _stub_llm(monkeypatch)
    selftest.probe_all()
    snap = selftest.status()
    assert snap["llm"] == "ok"
    assert snap["state"] == "ready"
    assert len(calls) == 1 and calls[0] is False     # 只探主通道
    assert sink[0]["max_tokens"] == 1


def test_llm_probe_uses_max_tokens_1_and_short_timeout(monkeypatch):
    """成本回归：有人把 max_tokens 改回默认（几十上百 token）就该红。"""
    _env(monkeypatch, LLM_API_KEY="sk", LLM_BASE_URL="https://x/v1")
    sink, _ = _stub_llm(monkeypatch)
    selftest.probe_all()
    assert sink[0]["max_tokens"] == 1
    assert sink[0]["messages"] == [{"role": "user", "content": "ping"}]


def test_llm_invalid_on_auth_error(monkeypatch):
    _env(monkeypatch, LLM_API_KEY="sk", LLM_BASE_URL="https://x/v1")
    _stub_llm(monkeypatch, exc=_FakeStatusError(401))
    selftest.probe_all()
    snap = selftest.status()
    assert snap["llm"] == "invalid"
    assert snap["reasons"]["llm"] == "unauthorized"


def test_llm_invalid_on_quota(monkeypatch):
    _env(monkeypatch, LLM_API_KEY="sk", LLM_BASE_URL="https://x/v1")
    _stub_llm(monkeypatch, exc=_FakeStatusError(429))
    selftest.probe_all()
    snap = selftest.status()
    assert snap["llm"] == "invalid"
    assert snap["reasons"]["llm"] == "quota"


def test_llm_unreachable_on_timeout(monkeypatch):
    """与上一条成对：**网络不通 ≠ Key 无效** —— 修复动作一个是查网络、一个是换 Key。"""
    _env(monkeypatch, LLM_API_KEY="sk", LLM_BASE_URL="https://x/v1")
    _stub_llm(monkeypatch, exc=APITimeoutError("timed out"))
    selftest.probe_all()
    snap = selftest.status()
    assert snap["llm"] == "unreachable"
    assert snap["reasons"]["llm"] == "network"
    assert snap["llm"] != "invalid"


def test_llm_probe_never_sets_invalid_when_connection_fails(monkeypatch):
    _env(monkeypatch, LLM_API_KEY="sk", LLM_BASE_URL="https://x/v1")
    _stub_llm(monkeypatch, exc=APIConnectionError("refused"))
    selftest.probe_all()
    snap = selftest.status()
    assert snap["llm"] == "unreachable"
    assert snap["reasons"]["llm"] == "network"


def test_fast_channel_not_probed_twice_when_inherited(monkeypatch):
    """快通道没单独配置 → 回落主通道：只发一次请求，状态标 inherited（可用，不是缺失）。"""
    _env(monkeypatch, LLM_API_KEY="sk", LLM_BASE_URL="https://x/v1")
    sink, calls = _stub_llm(monkeypatch)
    selftest.probe_all()
    snap = selftest.status()
    assert snap["llm_fast"] == "inherited"
    assert len(calls) == 1 and calls[0] is False
    assert len(sink) == 1


def test_fast_url_without_fast_key_is_invalid_without_request(monkeypatch):
    """那个最常踩的坑：快通道指向别家却没配对应 Key —— 必然一路 401。

    必须静态判定、**零请求**：真发一次只会把错误显示成「主 Key 无效」，
    把人引到完全错误的方向。
    """
    _env(monkeypatch, LLM_API_KEY="sk", LLM_BASE_URL="https://api.deepseek.com/v1",
         LLM_FAST_BASE_URL="https://open.bigmodel.cn/api/paas/v4")
    sink, calls = _stub_llm(monkeypatch)
    selftest.probe_all()
    snap = selftest.status()
    assert snap["llm"] == "invalid"
    assert snap["llm_fast"] == "invalid"
    assert snap["reasons"]["llm"] == "fast_key_missing"
    assert calls == [] and sink == []


def test_cli_main_loads_env_before_probing(monkeypatch):
    """回归：CLI 自检不经 main.py，必须自己把 .env 灌进 os.environ。

    实测踩过：_env() 走 load_env_file 能看到 Key，而 editor._llm() 读的是
    os.environ → 主通道被误报成 missing（症状是高德 ok、LLM missing）。
    """
    import os
    monkeypatch.delenv("SELFTEST_SKIP", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setattr("commute.load_env_file",
                        lambda: {"LLM_API_KEY": "sk-from-env",
                                 "LLM_BASE_URL": "https://from-env/v1"})
    seen: dict[str, str] = {}

    def fake_llm(fast: bool = False):
        seen["key"] = os.environ.get("LLM_API_KEY")
        seen["url"] = os.environ.get("LLM_BASE_URL")
        return _FakeClient([]), "stub-model"
    monkeypatch.setattr(editor, "_llm", fake_llm)

    try:
        assert selftest.main() == 0
        assert seen == {"key": "sk-from-env", "url": "https://from-env/v1"}, \
            "main() 没把 .env 灌进进程环境，_llm 看到的是空配置"
    finally:
        # _ensure_env_loaded 用 setdefault 写进 os.environ 的键，monkeypatch 不会回收
        os.environ.pop("LLM_API_KEY", None)
        os.environ.pop("LLM_BASE_URL", None)


def test_amap_ok_on_status_1(monkeypatch):
    _env(monkeypatch, AMAP_KEY="fake-amap")
    payload = {"status": "1", "results": [{"duration": "900"}]}

    def fake_urlopen(url, timeout=None):
        assert timeout == selftest.AMAP_PROBE_TIMEOUT_S
        return _FakeResp(payload)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    selftest.probe_all()
    snap = selftest.status()
    assert snap["amap_key"] == "ok"
    assert snap["state"] == "ready"


def test_amap_invalid_on_business_errorcode(monkeypatch):
    """status=0 表示请求到了高德、被它拒了 —— 属于 invalid，不是 unreachable。"""
    _env(monkeypatch, AMAP_KEY="fake-amap")
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout=None: _FakeResp({"status": "0", "info": "INVALID_USER_KEY"}))
    selftest.probe_all()
    snap = selftest.status()
    assert snap["amap_key"] == "invalid"
    assert snap["reasons"]["amap_key"] == "config_error"


def test_amap_unreachable_on_urlerror(monkeypatch):
    _env(monkeypatch, AMAP_KEY="fake-amap")
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout=None: (_ for _ in ()).throw(urllib.error.URLError("no dns")))
    selftest.probe_all()
    snap = selftest.status()
    assert snap["amap_key"] == "unreachable"
    assert snap["reasons"]["amap_key"] == "network"


def test_amap_probe_uses_city_centers_not_literals(monkeypatch):
    """探测坐标必须来自 cities.py 的城市中心表 —— 防止哪天改回写死坐标（成本维度会拦）。"""
    _env(monkeypatch, AMAP_KEY="fake-amap")
    seen: list[str] = []

    def fake_urlopen(url, timeout=None):
        seen.append(url)
        return _FakeResp({"status": "1", "results": []})
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    selftest.probe_all()

    assert len(seen) == 1
    q = urllib.parse.parse_qs(urllib.parse.urlparse(seen[0]).query)
    pairs = set()
    for field in ("origins", "destination"):
        lon, lat = q[field][0].split(",")
        pairs.add((float(lat), float(lon)))
    assert pairs <= set(CITY_CENTERS.values())
    # 起终点必须是**两个不同**的城市，否则探测形同虚设
    assert len(pairs) == 2


def test_probe_result_never_contains_key_value(monkeypatch):
    """响应里连 reason 都不允许出现 Key 本身 —— 这是会直接渲染到首页的接口。"""
    fake_key = "sk-selftest-leak-probe-1234567890"
    _env(monkeypatch, AMAP_KEY=fake_key, LLM_API_KEY=fake_key, LLM_BASE_URL="https://x/v1")
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout=None: _FakeResp({"status": "0", "info": "bad key"}))
    _stub_llm(monkeypatch, exc=_FakeStatusError(401))
    selftest.probe_all()
    dumped = json.dumps(selftest.status(), ensure_ascii=False)
    assert fake_key not in dumped
    assert "sk-selftest" not in dumped


def test_ttl_hit_does_not_reprobe(monkeypatch):
    _env(monkeypatch, AMAP_KEY="fake-amap")
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout=None: _FakeResp({"status": "1", "results": []}))
    selftest.probe_all()
    for _ in range(5):
        assert selftest.status()["stale"] is False
    # 快照时间仍是探测时刻 → TTL 内绝不重探
    assert selftest._snapshot["state"] == "ready"


def test_ttl_expiry_returns_stale_then_refreshes(monkeypatch):
    _env(monkeypatch, AMAP_KEY="fake-amap")
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout=None: _FakeResp({"status": "1", "results": []}))
    selftest.probe_all()
    before = selftest._snapshot["at"]

    # 时钟推过 TTL；_spawn 换成同步执行会立刻重探、拿不到「stale 快照」，
    # 所以这里只记录「投递过」，验证「先返回旧值、再后台刷新」的次序。
    monkeypatch.setattr(selftest, "_now", lambda: before + selftest.PROBE_TTL_SEC + 1)
    spawned: list = []
    monkeypatch.setattr(selftest, "_spawn", lambda fn: spawned.append(fn))
    snap = selftest.status()
    assert snap["stale"] is True
    assert snap["at"] == before            # 返回的还是旧快照
    assert len(spawned) == 1               # 且已投递一次刷新


def test_unexpected_probe_exception_is_logged(monkeypatch, caplog):
    """探测自己炸了：后台线程不许静默死掉，也不许把崩溃抛给请求路径。"""
    _env(monkeypatch, AMAP_KEY="fake-amap")
    monkeypatch.setattr(selftest, "_amap_probe_once",
                        lambda key: (_ for _ in ()).throw(RuntimeError("boom")))

    import logging
    # conftest 把 _spawn 置空了；这条用例恰恰要测后台刷新路径，所以换成同步执行
    monkeypatch.setattr(selftest, "_spawn", lambda fn: fn())
    with caplog.at_level(logging.WARNING, logger="selftest"):
        selftest._schedule_refresh()
    assert any("Key 自检失败" in r.message for r in caplog.records)
    # 快照保持原样（没有半新半旧的脏状态）
    assert selftest._snapshot["state"] == "idle"

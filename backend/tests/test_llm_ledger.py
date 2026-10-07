"""LLM 成本台账测试：记账聚合、轮转、旁路容错（写失败不拖垮业务）。"""
from __future__ import annotations

import json
import time

import llm_ledger


class _FakeUsage:
    def __init__(self, p: int, c: int):
        self.prompt_tokens = p
        self.completion_tokens = c


class _FakeResp:
    def __init__(self, p: int = 100, c: int = 20):
        self.usage = _FakeUsage(p, c)


def _redirect(monkeypatch, tmp_path):
    f = tmp_path / "llm_usage.jsonl"
    monkeypatch.setattr(llm_ledger, "LEDGER_FILE", f)
    return f


def test_record_and_summary(monkeypatch, tmp_path):
    f = _redirect(monkeypatch, tmp_path)
    t0 = time.monotonic()
    llm_ledger.record("rag_answer", model="glm-4-flash", resp=_FakeResp(), t0=t0)
    llm_ledger.record("rag_answer", model="glm-4-flash", resp=_FakeResp(50, 10), t0=t0)
    llm_ledger.record("extract", model="deepseek-chat", resp=_FakeResp(1000, 200), t0=t0)
    lines = f.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    out = llm_ledger.summary(days=1)
    assert out["total"]["calls"] == 3
    assert out["total"]["prompt_tokens"] == 1150
    assert out["total"]["completion_tokens"] == 230
    by_purpose = {g["purpose"]: g for g in out["groups"]}
    assert by_purpose["rag_answer"]["calls"] == 2
    assert by_purpose["extract"]["model"] == "deepseek-chat"
    # 耗时有记录且量级合理（<10s）
    entry = json.loads(lines[0])
    assert isinstance(entry["latency_ms"], int) and entry["latency_ms"] < 10000


def test_record_without_usage_or_timing(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    llm_ledger.record("align_arb", model="m")     # 无 resp、无 t0：都要能记
    out = llm_ledger.summary(days=1)
    assert out["total"]["calls"] == 1
    assert out["total"]["prompt_tokens"] == 0


def test_record_never_breaks_caller(monkeypatch, tmp_path):
    # 台账路径指向一个目录 → 写入必然 OSError，record 必须吞掉不抛
    monkeypatch.setattr(llm_ledger, "LEDGER_FILE", tmp_path)
    llm_ledger.record("reviews", model="m", resp=_FakeResp())     # 不抛即通过
    assert llm_ledger.summary(days=1)["total"]["calls"] == 0


def test_rotation_keeps_recent_lines(monkeypatch, tmp_path):
    f = _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(llm_ledger, "LEDGER_ROTATE_BYTES", 200)
    monkeypatch.setattr(llm_ledger, "LEDGER_KEEP_LINES", 5)
    for i in range(10):
        llm_ledger.record("rot", model=f"m{i}")
    rows = [json.loads(x) for x in f.read_text(encoding="utf-8").strip().splitlines()]
    assert len(rows) == 5
    assert rows[-1]["model"] == "m9"


def test_summary_skips_dirty_lines_and_old_entries(monkeypatch, tmp_path):
    f = _redirect(monkeypatch, tmp_path)
    now = int(time.time())
    old = {"ts": now - 90 * 86400, "purpose": "old", "model": "m",
           "prompt_tokens": 999, "completion_tokens": 0, "latency_ms": 1}
    new = {"ts": now, "purpose": "new", "model": "m",
           "prompt_tokens": 5, "completion_tokens": 1, "latency_ms": 1}
    f.write_text(json.dumps(old) + "\nnot-json\n" + json.dumps(new) + "\n",
                 encoding="utf-8")
    out = llm_ledger.summary(days=7)
    assert out["total"]["calls"] == 1
    assert out["total"]["prompt_tokens"] == 5

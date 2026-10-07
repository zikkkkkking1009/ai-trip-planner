"""线索存储测试：按 session upsert、三态流转、上限、原子写纪律。"""
from __future__ import annotations

import json

import leads


def _redirect(monkeypatch, tmp_path):
    f = tmp_path / "leads.json"
    monkeypatch.setattr(leads, "LEADS_FILE", f)
    return f


def test_upsert_creates_then_updates(monkeypatch, tmp_path):
    f = _redirect(monkeypatch, tmp_path)
    a = leads.upsert_lead("s1", channel="web", question="q1",
                          reply_kind="answer", handoff=False)
    assert a["status"] == "open"
    b = leads.upsert_lead("s1", channel="web", question="q2",
                          reply_kind="answer_caveat", handoff=False)
    assert b["id"] == a["id"]                       # 同 session+channel 复用一条
    assert b["last_question"] == "q2"
    assert len(f.read_text(encoding="utf-8").strip().splitlines()) >= 1
    rows = json.loads(f.read_text(encoding="utf-8"))
    assert len(rows) == 1


def test_channel_splits_leads(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    leads.upsert_lead("s1", channel="web", question="q1", reply_kind="answer",
                      handoff=False)
    leads.upsert_lead("s1", channel="wechat_mp", question="q2", reply_kind="answer",
                      handoff=False)
    assert len(leads.list_leads()) == 2


def test_handoff_and_reopen_lifecycle(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    leads.upsert_lead("s1", channel="web", question="q1", reply_kind="answer",
                      handoff=False)
    h = leads.upsert_lead("s1", channel="web", question="q2", reply_kind="handoff",
                          handoff=True)
    assert h["status"] == "handoff" and h["handoff_count"] == 1
    # 人工处理期间用户继续问：handoff 态保持（人工还在处理）
    keep = leads.upsert_lead("s1", channel="web", question="q3", reply_kind="answer",
                             handoff=False)
    assert keep["status"] == "handoff"
    # 人工收尾 → 用户再问 → 重开为 open
    leads.set_status(h["id"], "closed")
    again = leads.upsert_lead("s1", channel="web", question="q4", reply_kind="answer",
                              handoff=False)
    assert again["status"] == "open"


def test_set_status_validation(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    try:
        leads.set_status("whatever", "bogus")
        raise AssertionError("should raise")
    except ValueError:
        pass
    assert leads.set_status("no-such-id", "closed") is None


def test_list_filter_and_order(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    # updated_at 只到秒：把时钟做成递增的，锁定「按更新时间倒序」的排序行为
    import itertools
    clock = itertools.count(1700000000)
    monkeypatch.setattr(leads.time, "time", lambda: next(clock))
    for i in range(3):
        leads.upsert_lead(f"s{i}", channel="web", question=f"q{i}",
                          reply_kind="handoff", handoff=(i == 0))
    rows = leads.list_leads()
    assert [r["session_id"] for r in rows] == ["s2", "s1", "s0"]   # 倒序
    only_handoff = leads.list_leads(status="handoff")
    assert len(only_handoff) == 1 and only_handoff[0]["session_id"] == "s0"


def test_cap_drops_oldest(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(leads, "MAX_LEADS", 3)
    for i in range(5):
        leads.upsert_lead(f"s{i}", channel="web", question="q", reply_kind="answer",
                          handoff=False)
    rows = leads.list_leads(limit=10)
    assert len(rows) == 3
    assert {r["session_id"] for r in rows} == {"s2", "s3", "s4"}

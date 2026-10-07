"""客服三档决策测试：mock rag 控制分支 + 真实 rag 集成（能力边界直接复用）。

转人工语义是本模块的核心资产——测试同时锁三件事：
① 决策正确（unsupported/空结果 → handoff；attr/soft → caveat；正常 → answer）
② 留痕完整（会话文件 + 线索状态一致）
③ 生成降级安全（grounded 才采用；渠道侧永不生成在 wechat_mp 测试里锁）
"""
from __future__ import annotations

import json

import leads
import support


def _redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(support, "CONVERSATIONS_DIR", tmp_path / "conversations")
    monkeypatch.setattr(leads, "LEADS_FILE", tmp_path / "leads.json")


def _rag_out(results=None, gap=None):
    return {"results": results if results is not None else [], "gap": gap,
            "abstain": bool(gap and gap.get("kind") == "unsupported")}


def _hit(name="秦始皇兵马俑博物馆"):
    return [{"type": "景点", "city": "西安", "name": name, "text": f"{name} 的介绍文字",
             "verified": True}]


# ---- 输入校验 ----

def test_session_id_validation():
    for bad in ("", "a/b", "../etc", "有中文", "x" * 65, None):
        try:
            support.answer(bad, "你好")
            raise AssertionError(f"should raise: {bad!r}")
        except ValueError:
            pass


def test_message_length_limits(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(support, "rag_ask", lambda q, **kw: _rag_out(_hit()))
    try:
        support.answer("s1", "x" * 501)
        raise AssertionError("should raise")
    except ValueError as e:
        assert "过长" in str(e)
    try:
        support.answer("s1", "   ")
        raise AssertionError("should raise")
    except ValueError as e:
        assert "为空" in str(e)


def test_real_rag_gibberish_handoff(monkeypatch, tmp_path):
    """真实 rag 下无命中且无 gap → 宁可转人工不硬凑（不再 mock，锁真实行为）。"""
    _redirect(monkeypatch, tmp_path)
    out = support.answer("s1", "嗊堃巭廛")
    assert out["action"] == "handoff"


# ---- 三档决策 ----

def test_unsupported_becomes_handoff(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(support, "rag_ask",
                        lambda q, **kw: _rag_out([], {"kind": "unsupported",
                                                      "topic": "气象穿搭", "note": "没有天气数据"}))
    out = support.answer("s1", "哈尔滨冬天穿什么")
    assert out["action"] == "handoff"
    assert "转人工" in out["reply"]
    lead = leads.list_leads(status="handoff")
    assert len(lead) == 1 and lead[0]["session_id"] == "s1"


def test_no_results_becomes_handoff(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(support, "rag_ask", lambda q, **kw: _rag_out([]))
    out = support.answer("s1", "zzz")
    assert out["action"] == "handoff"


def test_attr_gap_becomes_caveat(monkeypatch, tmp_path):
    """web 端（plain=False）：缺口说明走结构化 gap 字段，reply 不再拼工程文本。"""
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(support, "rag_ask",
                        lambda q, **kw: _rag_out(_hit(), {"kind": "attr", "attr": "门票",
                                                          "note": "库里只有 62/375 个景点有票价"}))
    out = support.answer("s1", "兵马俑门票多少钱")
    assert out["action"] == "answer_caveat"
    assert "62/375" in out["gap"]["note"]          # 前端从 gap.note 渲染 ⚠️ 块
    assert "62/375" not in out["reply"]            # reply 不再拼缺口（重复事实源已废）


def test_wechat_channel_embeds_gap_note(monkeypatch, tmp_path):
    """公众号（plain=True）：XML 没有结构化渲染，缺口说明必须拼进正文。"""
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(support, "rag_ask",
                        lambda q, **kw: _rag_out(_hit(), {"kind": "attr", "attr": "门票",
                                                          "note": "库里只有 62/375 个景点有票价"}))
    out = support.answer("s1", "兵马俑门票多少钱", channel="wechat_mp")
    assert out["action"] == "answer_caveat"
    assert "62/375" in out["reply"] and "人工" in out["reply"]


def test_normal_hit_answers_with_citations(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(support, "rag_ask", lambda q, **kw: _rag_out(_hit()))
    out = support.answer("s1", "兵马俑")
    assert out["action"] == "answer"
    assert out["citations"][0]["name"] == "秦始皇兵马俑博物馆"
    assert out["citations"][0]["verified"] is True
    assert "snippet" in out["citations"][0]        # 干净一句话，前端卡片用它（不带检索扩展词）
    assert "根据知识库" in out["reply"]


def test_reply_never_leaks_intent_expansion(monkeypatch, tmp_path):
    """语料 text 尾部是给 BM25 的意图扩展词——任何口径都不许漏给用户（2026-10-07 实锅）。"""
    _redirect(monkeypatch, tmp_path)
    dirty = dict(_hit()[0])
    dirty["text"] = dirty["name"] + " 正文 数据 免费 不要钱 穷游 免票 不花钱 不要门票"
    dirty["display"] = "秦始皇兵马俑博物馆（西安）正文 数据"
    monkeypatch.setattr(support, "rag_ask", lambda q, **kw: _rag_out([dirty]))
    out = support.answer("s1", "西安有什么好吃的")
    assert "穷游" not in out["reply"]
    assert "穷游" not in out["citations"][0]["snippet"]
    assert out["citations"][0]["snippet"].startswith("秦始皇兵马俑博物馆")


def test_human_keyword_short_circuits(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    calls = []

    def _boom(q, **kw):
        calls.append(q)
        return _rag_out(_hit())

    monkeypatch.setattr(support, "rag_ask", _boom)
    out = support.answer("s1", "请转人工")
    assert out["action"] == "handoff" and not calls      # 没进检索，直接落工单
    assert out["reply"].startswith("好的，已为你转人工")  # 话术不被统一组句覆盖


def test_rag_failure_degrades_to_handoff(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)

    def _boom(q, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(support, "rag_ask", _boom)
    out = support.answer("s1", "随便问问")
    assert out["action"] == "handoff" and "转人工" in out["reply"]
    assert out["reply"].startswith("客服服务暂时不可用")  # 异常话术不被统一组句覆盖


# ---- 生成层（仅 web 登录态允许；grounded 才采用） ----

def test_generation_used_when_grounded(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(support, "rag_ask", lambda q, **kw: _rag_out(_hit()))
    monkeypatch.setattr(support, "generate_answer",
                        lambda q, rs: {"text": "生成的好答案", "grounded": True})
    out = support.answer("s1", "兵马俑", allow_generate=True)
    assert out["generated"] is True and "生成的好答案" in out["reply"]


def test_generation_skipped_when_not_grounded(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(support, "rag_ask", lambda q, **kw: _rag_out(_hit()))
    monkeypatch.setattr(support, "generate_answer",
                        lambda q, rs: {"text": "乱编的答案", "grounded": False})
    out = support.answer("s1", "兵马俑", allow_generate=True)
    assert out["generated"] is False
    assert "乱编的答案" not in out["reply"]


def test_generation_not_called_by_default(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(support, "rag_ask", lambda q, **kw: _rag_out(_hit()))
    monkeypatch.setattr(support, "generate_answer",
                        lambda q, rs: (_ for _ in ()).throw(AssertionError("must not call")))
    out = support.answer("s1", "兵马俑")
    assert out["generated"] is False


# ---- 会话留痕 ----

def test_conversation_persisted_and_trimmed(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(support, "rag_ask", lambda q, **kw: _rag_out(_hit()))
    monkeypatch.setattr(support, "CONVERSATION_MAX_TURNS", 2)
    for i in range(3):
        support.answer("s1", f"第{i}问")
    msgs = support.load_conversation("s1")
    assert len(msgs) == 2
    assert msgs[-1]["q"] == "第2问"
    assert msgs[-1]["action"] in (support.ACTION_ANSWER, support.ACTION_CAVEAT,
                                  support.ACTION_HANDOFF)


def test_load_conversation_validates_session():
    try:
        support.load_conversation("../escape")
        raise AssertionError("should raise")
    except ValueError:
        pass


# ---- 真实 rag 集成（不复用 mock，锁能力边界的真实行为） ----

def test_real_rag_unsupported_question(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    out = support.answer("s1", "故宫需要预约吗")        # 预约规则在 _UNSUPPORTED 表里
    assert out["action"] == "handoff"
    assert out["gap"]["kind"] == "unsupported"


def test_real_rag_food_question_answered(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    out = support.answer("s1", "西安有什么好吃的")
    assert out["action"] == "answer"
    assert out["citations"], "同城美食兜底保证必有引用"
    assert all(c["verified"] for c in out["citations"])

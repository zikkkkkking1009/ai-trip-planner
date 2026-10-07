"""公众号测试号回调测试：验签、去重、事件分流、与真实客服决策的端到端离线链路。

全部离线：不发任何真实微信请求，XML 用官方格式的 fixture。
"""
from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET

import leads
import support
import wechat_mp
from fastapi.testclient import TestClient
from main import app

TOKEN = "tok123"
TS, NONCE = "1700000000", "nonce1"


def _sig(token: str = TOKEN, ts: str = TS, nonce: str = NONCE) -> str:
    return hashlib.sha1("".join(sorted([token, ts, nonce])).encode("utf-8")).hexdigest()


def _text_xml(content: str, msg_id: str = "1001", from_user: str = "oABC") -> bytes:
    return (f"<xml><ToUserName><![CDATA[gh_test]]></ToUserName>"
            f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
            f"<CreateTime>1700000000</CreateTime>"
            f"<MsgType><![CDATA[text]]></MsgType>"
            f"<Content><![CDATA[{content}]]></Content>"
            f"<MsgId><![CDATA[{msg_id}]]></MsgId></xml>").encode("utf-8")


def _redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(support, "CONVERSATIONS_DIR", tmp_path / "conversations")
    monkeypatch.setattr(leads, "LEADS_FILE", tmp_path / "leads.json")


# ---- 验签（纯函数） ----

def test_verify_signature_accepts_correct():
    assert wechat_mp.verify_signature(TOKEN, TS, NONCE, _sig())


def test_verify_signature_rejects_wrong():
    assert not wechat_mp.verify_signature(TOKEN, TS, NONCE, "deadbeef")
    assert not wechat_mp.verify_signature("", TS, NONCE, _sig())
    assert not wechat_mp.verify_signature(TOKEN, TS, NONCE, "")


# ---- handle_message（纯逻辑，stub 客服应答） ----

def test_handle_message_dedup(monkeypatch):
    calls = []

    def _stub(sid, text, **kw):
        calls.append((sid, text))
        return {"reply": f"答：{text}", "action": "answer"}

    xml1 = wechat_mp.handle_message(_text_xml("你好", msg_id="777"), answer_fn=_stub)
    assert "答：你好" in xml1 and len(calls) == 1
    xml2 = wechat_mp.handle_message(_text_xml("你好", msg_id="777"), answer_fn=_stub)
    assert xml2 == "" and len(calls) == 1           # 重发窗口内不二次应答


def test_handle_message_session_id_sanitized(monkeypatch):
    monkeypatch.setattr(wechat_mp, "_msg_seen", {})    # 全局去重表：用例间隔离
    sids = []

    def _stub(sid, text, **kw):
        sids.append(sid)
        return {"reply": "ok", "action": "answer"}

    wechat_mp.handle_message(_text_xml("hi", from_user="oX/ab+cd"), answer_fn=_stub)
    assert sids and "/" not in sids[0] and "+" not in sids[0]
    assert sids[0].startswith("wx-")


def _event_xml(msg_type: str, event: str = "") -> bytes:
    return (f"<xml><ToUserName><![CDATA[gh]]></ToUserName>"
            f"<FromUserName><![CDATA[o1]]></FromUserName>"
            f"<CreateTime>1</CreateTime><MsgType><![CDATA[{msg_type}]]></MsgType>"
            f"<Event><![CDATA[{event}]]></Event></xml>").encode("utf-8")


def test_handle_message_subscribe_and_non_text():
    welcome = wechat_mp.handle_message(_event_xml("event", "subscribe"),
                                       answer_fn=lambda *a, **k: None)
    assert "欢迎" in welcome
    other = wechat_mp.handle_message(_event_xml("image"),
                                     answer_fn=lambda *a, **k: None)
    assert "文字" in other


def test_handle_message_rejects_value_error(monkeypatch):
    monkeypatch.setattr(wechat_mp, "_msg_seen", {})    # 全局去重表：用例间隔离

    def _stub(sid, text, **kw):
        raise ValueError("消息过长（上限 500 字）")

    out = wechat_mp.handle_message(_text_xml("x" * 10), answer_fn=_stub)
    assert "没法处理" in out


def test_handle_message_bad_xml():
    assert wechat_mp.handle_message(b"<not-xml") == ""


# ---- 回调路由（TestClient 全链路：真实 support 决策 + 落盘重定向） ----

def test_get_verify_echo(monkeypatch):
    monkeypatch.setenv("WECHAT_MP_TOKEN", TOKEN)
    client = TestClient(app)
    r = client.get(f"/support/wechat?signature={_sig()}&timestamp={TS}&nonce={NONCE}"
                   "&echostr=echo123")
    assert r.status_code == 200 and r.text == "echo123"
    bad = client.get(f"/support/wechat?signature=bad&timestamp={TS}&nonce={NONCE}"
                     "&echostr=x")
    assert bad.status_code == 403


def test_get_verify_disabled_without_token(monkeypatch):
    monkeypatch.delenv("WECHAT_MP_TOKEN", raising=False)
    client = TestClient(app)
    r = client.get("/support/wechat?signature=x&timestamp=y&nonce=z&echostr=e")
    assert r.status_code == 503


def test_post_text_message_end_to_end(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setenv("WECHAT_MP_TOKEN", TOKEN)
    monkeypatch.setattr(wechat_mp, "_msg_seen", {})
    client = TestClient(app)
    r = client.post(f"/support/wechat?signature={_sig()}&timestamp={TS}&nonce={NONCE}",
                    content=_text_xml("故宫需要预约吗"),
                    headers={"content-type": "application/xml"})
    assert r.status_code == 200
    root = ET.fromstring(r.text)
    assert root.findtext("FromUserName") == "gh_test"       # 收发人对调
    assert root.findtext("ToUserName") == "oABC"
    assert "转人工" in (root.findtext("Content") or "")     # 能力边界 → 转人工话术
    handoff = leads.list_leads(status="handoff")
    assert len(handoff) == 1 and handoff[0]["channel"] == "wechat_mp"


def test_post_dedup_second_send_empty(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setenv("WECHAT_MP_TOKEN", TOKEN)
    monkeypatch.setattr(wechat_mp, "_msg_seen", {})
    client = TestClient(app)
    qs = f"signature={_sig()}&timestamp={TS}&nonce={NONCE}"
    hdrs = {"content-type": "application/xml"}
    r1 = client.post(f"/support/wechat?{qs}", content=_text_xml("兵马俑", msg_id="99"),
                     headers=hdrs)
    r2 = client.post(f"/support/wechat?{qs}", content=_text_xml("兵马俑", msg_id="99"),
                     headers=hdrs)
    assert r1.status_code == 200 and r1.text
    assert r2.status_code == 200 and r2.text == ""          # 重发去重：空串不二次应答


def test_post_signature_fail_and_encrypted(monkeypatch):
    monkeypatch.setenv("WECHAT_MP_TOKEN", TOKEN)
    client = TestClient(app)
    bad = client.post("/support/wechat?signature=bad&timestamp=x&nonce=y",
                      content=_text_xml("hi"), headers={"content-type": "application/xml"})
    assert bad.status_code == 403
    enc = client.post(f"/support/wechat?signature={_sig()}&timestamp={TS}&nonce={NONCE}"
                      "&encrypt_type=aes", content=_text_xml("hi"),
                      headers={"content-type": "application/xml"})
    assert enc.status_code == 501                           # 只支持明文，如实拒绝


def test_channel_reply_never_generates(monkeypatch, tmp_path):
    """渠道侧 R4 纪律：公众号消息永远不烧 LLM——generate_answer 被调即失败。"""
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setenv("WECHAT_MP_TOKEN", TOKEN)
    monkeypatch.setattr(wechat_mp, "_msg_seen", {})

    def _boom(*a, **kw):
        raise AssertionError("渠道回复不允许触发生成层")

    monkeypatch.setattr(support, "generate_answer", _boom)
    client = TestClient(app)
    r = client.post(f"/support/wechat?signature={_sig()}&timestamp={TS}&nonce={NONCE}",
                    content=_text_xml("西安有什么好吃的"),
                    headers={"content-type": "application/xml"})
    assert r.status_code == 200 and "知识库" in r.text

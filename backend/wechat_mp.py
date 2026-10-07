"""公众号测试号适配层（国内化一期）：微信消息 ↔ 客服会话的最小官方回调实现。

对应公司项目模块 3 的「通过官方接口连接客户咨询」。国内等价物选择**公众号测试号**：
个人即可注册、回调免 AES 加密（明文模式，sha1 验签标准库可算）——正式客服消息
API 要认证服务号（个人主体办不了），这条能力边界如实写进文档，本身就是
「官方接口能力调研」的结论。

被动回复（本实现）与客服消息（认证号能力）的取舍：
- 被动回复必须 **5s 内**返回 XML ⇒ 渠道侧永远**不烧 LLM**（support.answer
  allow_generate=False），检索是毫秒级 BM25，稳稳落在时限内；
- 转人工落地为线索工单（leads.status=handoff），测试号无法主动推送——
  主动外呼需要认证服务号的客服消息接口（48h 窗口 + 模板审核）。

可靠性细节：
- 微信 5s 收不到回复会**重发同一 MsgId** ⇒ MsgId 去重（TTL 120s），处理成功才记账；
- 只支持明文模式：带 encrypt_type 的回调直接 501，绝不假装处理（零依赖的边界）；
- XML 解析用 xml.etree（外部实体默认禁止，无 XXE 注入面）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from secrets import compare_digest
from typing import Callable
from xml.sax.saxutils import escape

from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse, Response

import support

log = logging.getLogger(__name__)

router = APIRouter(prefix="/support/wechat", tags=["support-wechat"])

DEDUP_TTL_S = 120              # 微信重发窗口（5s 重试，留足余量）
_msg_seen: dict[str, float] = {}

# openid → 会话 id：微信侧字符集不可控，白名单外的字符压成 _（防拼路径出格）
_SID_CLEAN = re.compile(r"[^A-Za-z0-9_-]")


def wechat_token() -> str:
    """验签 token（测试号后台填的那个）。留空 = 渠道停用。"""
    return os.environ.get("WECHAT_MP_TOKEN", "").strip()


def verify_signature(token: str, timestamp: str, nonce: str, signature: str) -> bool:
    """官方验签：sha1(字典序(token, timestamp, nonce))。恒定时间比较。"""
    if not (token and timestamp and nonce and signature):
        return False
    raw = "".join(sorted([token, timestamp, nonce]))
    return compare_digest(hashlib.sha1(raw.encode("utf-8")).hexdigest(), signature)


def parse_callback(payload: bytes) -> dict[str, str]:
    """解析明文回调 XML，取本流程需要的字段（缺失返回空串，不抛键错误）。"""
    root = ET.fromstring(payload)
    return {k: (root.findtext(k) or "") for k in
            ("ToUserName", "FromUserName", "MsgType", "Content", "MsgId", "Event")}


def reply_xml(from_user: str, to_user: str, content: str) -> str:
    """被动回复 XML（官方格式：收发人对调 + 时间戳 + 文本）。"""
    return (
        "<xml>"
        "<ToUserName><![CDATA[{to}]]></ToUserName>"
        "<FromUserName><![CDATA[{frm}]]></FromUserName>"
        "<CreateTime>{ts}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[{content}]]></Content>"
        "</xml>"
    ).format(to=escape(to_user), frm=escape(from_user), ts=int(time.time()),
             content=content)


def _seen_before(msg_id: str) -> bool:
    """True=重复消息（5s 重发窗口内）。顺手清理过期键。"""
    now = time.time()
    for k in [k for k, t in _msg_seen.items() if now - t > DEDUP_TTL_S]:
        _msg_seen.pop(k, None)
    if msg_id in _msg_seen:
        return True
    _msg_seen[msg_id] = now
    return False


def _session_id(openid: str) -> str:
    return ("wx-" + _SID_CLEAN.sub("_", openid).strip("_"))[:60] or "wx-anon"


def handle_message(payload: bytes, *, answer_fn: Callable = support.answer) -> str:
    """回调处理核心（纯逻辑，离线可测）：返回被动回复 XML 或空串。

    - 文本 → support.answer 三档决策 → 回复；
    - 关注等事件 → 欢迎语；其它类型 → 如实说明只支持文字；
    - 重复 MsgId → 空串（上一轮已回复过，不再二次应答）。
    """
    try:
        msg = parse_callback(payload)
    except ET.ParseError as e:
        log.warning("微信回调 XML 解析失败: %s: %s", type(e).__name__, e)
        return ""
    msg_type = msg.get("MsgType", "")
    openid = msg.get("FromUserName", "")
    if msg_type != "text":
        if msg_type == "event" and msg.get("Event") == "subscribe":
            return reply_xml(msg["ToUserName"], openid,
                             "欢迎关注！直接发文字消息提问，例如「西安有什么好玩的」。")
        return reply_xml(msg.get("ToUserName", ""), openid,
                         "当前仅支持文字提问，请发送文字消息。")
    msg_id = msg.get("MsgId", "")
    if msg_id and _seen_before(msg_id):
        log.info("微信重发消息已去重 msgid=%s", msg_id)
        return ""
    content = (msg.get("Content") or "").strip()
    if not content:
        return reply_xml(msg["ToUserName"], openid, "请输入你想问的问题～")
    try:
        out = answer_fn(_session_id(openid), content, channel="wechat_mp",
                        contact=openid, allow_generate=False)
    except ValueError as e:
        # 超长/空白等输入问题：如实告知，不算服务故障
        log.info("微信消息被拒： %s", e)
        return reply_xml(msg["ToUserName"], openid, f"这条消息没法处理：{e}")
    return reply_xml(msg["ToUserName"], openid, out["reply"])


# ---------- 回调路由（验签即鉴权：微信服务器带不了 APP_TOKEN） ----------

def _signature_params(timestamp: str, nonce: str, signature: str) -> tuple[str, bool]:
    token = wechat_token()
    return token, verify_signature(token, timestamp, nonce, signature)


@router.get("")
def wechat_verify(signature: str = Query(""), timestamp: str = Query(""),
                  nonce: str = Query(""), echostr: str = Query("")) -> Response:
    """接入验证：原样返回 echostr（官方规定），验签失败 403。"""
    if not wechat_token():
        return PlainTextResponse("未配置 WECHAT_MP_TOKEN，公众号渠道停用（见 .env.example）",
                                 status_code=503)
    _, ok = _signature_params(timestamp, nonce, signature)
    if not ok:
        log.warning("公众号接入验证验签失败")
        return PlainTextResponse("signature 校验失败", status_code=403)
    return PlainTextResponse(echostr)


@router.post("")
async def wechat_callback(request: Request,
                          signature: str = Query(""),
                          timestamp: str = Query(""),
                          nonce: str = Query(""),
                          encrypt_type: str = Query("")) -> Response:
    """消息回调：只支持明文模式；处理成功返回文本 XML，无需回复返回空串。"""
    if not wechat_token():
        return PlainTextResponse("未配置 WECHAT_MP_TOKEN，公众号渠道停用",
                                 status_code=503)
    _, ok = _signature_params(timestamp, nonce, signature)
    if not ok:
        return PlainTextResponse("signature 校验失败", status_code=403)
    if encrypt_type:     # aes / compat 都不做：零依赖边界，明文模式是测试号默认
        log.warning("收到加密模式回调 encrypt_type=%s（仅支持明文）", encrypt_type)
        return PlainTextResponse("仅支持明文模式（测试号默认）", status_code=501)
    payload = await request.body()
    try:
        xml_out = handle_message(payload)
    except Exception:        # noqa: BLE001 —— 回调对微信必须 200，异常只留痕
        log.exception("公众号回调处理异常")
        xml_out = ""
    return Response(xml_out or "", media_type="application/xml")

"""客服会话层（国内化一期）：把 /ask 的检索问答升级成「可转人工」的客服动作。

与公司项目模块 3（AI 知识库客服、不确定时转人工）的对应关系——
三档决策**完全复用** rag.py 已有的能力边界（R3 realbench 的三档拒答）：

  gap.kind = unsupported → 库里压根没有这类数据 → **自动转人工**（登记线索）
  gap.kind = attr / soft → 能答一部分但缺被问的字段 → 给部分答案 + 明确说缺什么
  gap 为 None             → 正常引用作答

为什么转人工是这里的正确动作：客服语境下「不知道装知道」的代价远高于
检索问答页（那里只是少一条引用，这里会误导客户决策）。R2 引用核查与
「宁可转人工不硬凑」是同一条原则在客服侧的延伸。

生成层纪律（R4 默认关的延续）：allow_generate=False 时**零 LLM 成本**——
回复由检索结果确定性组句；渠道侧（公众号被动回复）永远关闭生成
（被动回复须在 5s 内返回，LLM 延迟不可控）；web 端由 main.py 的
生成配额闸门决定是否放开（复用 /ask with_answer 的全局桶）。

会话与线索全部落盘（data/conversations/、data/leads.json）：每轮的
决策档位留痕，可审计「机器人当时为什么这么答」——这也是评测
「转人工正确率」的数据来源。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import leads
from rag import ask as rag_ask
from rag import generate_answer

log = logging.getLogger(__name__)

# session_id 同时是会话文件名的一部分：白名单字符，杜绝路径穿越
# （同 _TASK_ID_RE / _FOOD_CITY_RE 的教训——任何来自外部的 id 不许直接拼路径）
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

SUPPORT_MAX_CHARS = 500        # 单条消息上限（防匿名刷长文本）
REPLY_MAX_CHARS = 600          # 回复上限（公众号被动回复 XML 有 2048 字节红线）
CONVERSATION_MAX_TURNS = 100   # 单会话留痕轮数上限（超出丢最旧）
HUMAN_KEYWORDS = ("转人工", "人工客服", "人工")

DATA_DIR = Path(__file__).parent.parent / "data"
CONVERSATIONS_DIR = DATA_DIR / "conversations"

_conv_lock = threading.Lock()

ACTION_ANSWER = "answer"
ACTION_CAVEAT = "answer_caveat"
ACTION_HANDOFF = "handoff"


def _session_file(session_id: str) -> Path:
    return CONVERSATIONS_DIR / f"{session_id}.json"


def _append_turn(session_id: str, turn: dict) -> None:
    """会话留痕：一轮（问+答+决策档位）追加进会话文件。"""
    try:
        with _conv_lock:
            CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
            f = _session_file(session_id)
            doc: dict = {"messages": []}
            if f.exists():
                try:
                    loaded = json.loads(f.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict) and isinstance(loaded.get("messages"), list):
                        doc = loaded
                except (json.JSONDecodeError, OSError) as e:
                    # 损坏则重开一份：留痕是旁路能力，不能挡住应答主流程
                    log.warning("会话文件损坏，重开: %s: %s", f.name, type(e).__name__, e)
            doc["messages"].append(turn)
            doc["messages"] = doc["messages"][-CONVERSATION_MAX_TURNS:]
            tmp = f.with_name(f.name + ".tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, f)
    except OSError as e:
        log.warning("会话留痕写入失败 sid=%s: %s: %s",
                    session_id, type(e).__name__, e)


def load_conversation(session_id: str) -> list[dict]:
    """读会话留痕（管理端查询用）。"""
    if not SESSION_ID_RE.fullmatch(session_id or ""):
        raise ValueError("session_id 格式非法")
    with _conv_lock:
        f = _session_file(session_id)
        if not f.exists():
            return []
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("会话文件读取失败 sid=%s: %s: %s", session_id, type(e).__name__, e)
        return []
    msgs = doc.get("messages") if isinstance(doc, dict) else None
    return [m for m in msgs if isinstance(m, dict)] if isinstance(msgs, list) else []


def _snippet(text: object, limit: int = 70) -> str:
    """语料文本裁一句：展示用，宁可截断不给超长块。"""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s[:limit] + ("…" if len(s) > limit else "")


def _compose_reply(results: list[dict], gap: dict | None, q: str,
                   action: str, generated: str | None) -> str:
    """从检索结果确定性组句（不烧 LLM）。generated 是已过 grounding 的生成答案。"""
    if action == ACTION_HANDOFF:
        if gap and gap.get("kind") == "unsupported":
            reason = str(gap.get("note") or "知识库没有这类数据")
            return (f"这个问题超出了知识库范围：{reason}"
                    "已为你登记转人工，请留意后续回复；也可以换个问法试试。")
        return f"知识库里没有找到与「{q[:60]}」直接相关的内容，已为你登记转人工。"

    lines: list[str] = []
    if generated:
        lines.append(generated)
    elif results:
        lines.append("根据知识库，为你找到：")
        for r in results[:2]:
            lines.append(f"· {r['name']}（{r['city']}）{_snippet(r['text'])}")
    else:
        lines.append(f"关于「{q[:60]}」，知识库里暂时没有内容。")
    if results:
        ok = sum(1 for r in results if r.get("verified"))
        lines.append(f"（引用 {ok}/{len(results)} 条已核查到库内实体）")
    if action == ACTION_CAVEAT and gap:
        lines.append(f"⚠️ {gap.get('note', '')}如需人工确认，请回复「人工」。")
    return "\n".join(lines)[:REPLY_MAX_CHARS]


def answer(session_id: str, text: str, *, channel: str = "web",
           contact: str = "", allow_generate: bool = False) -> dict:
    """客服一问一答：检索 → 三档决策 → 组句（可选生成）→ 留痕 → 线索联动。

    抛 ValueError：session_id 格式非法 / 消息为空或超长。其余情况一律返回
    结构化结果——客服对用户不 500，实在答不了就走转人工。
    """
    sid = str(session_id or "").strip()
    if not SESSION_ID_RE.fullmatch(sid):
        raise ValueError("session_id 格式非法（限字母数字下划线短横线，≤64 字）")
    q = str(text or "").strip()
    if not q:
        raise ValueError("消息为空")
    if len(q) > SUPPORT_MAX_CHARS:
        raise ValueError(f"消息过长（上限 {SUPPORT_MAX_CHARS} 字）")

    # 用户主动要人工：不进检索，直接落工单（最诚实的分支）
    results: list[dict] = []
    gap: dict | None = None
    generated: str | None = None
    reply: str | None = None        # 下方两个分支直接给定话术；其余走统一组句
    if any(kw in q for kw in HUMAN_KEYWORDS):
        action = ACTION_HANDOFF
        reply = "好的，已为你转人工，请稍候；人工回复前也可以继续提问。"
    else:
        try:
            out = rag_ask(q)
        except Exception as e:      # noqa: BLE001 —— 检索失败也要给用户一个出口
            log.exception("客服检索异常 sid=%s: %s: %s", sid, type(e).__name__, e)
            action = ACTION_HANDOFF
            reply = "客服服务暂时不可用，已为你登记转人工，请稍候。"
        else:
            results = [r for r in (out.get("results") or []) if isinstance(r, dict)]
            gap = out.get("gap") if isinstance(out.get("gap"), dict) else None
            if out.get("abstain") or (gap and gap.get("kind") == "unsupported"):
                action = ACTION_HANDOFF          # 库里没有的数据：不硬塞，转人工
            elif not results:
                action = ACTION_HANDOFF          # 什么都不明白：宁可转人工不硬凑
            elif gap:                            # attr / soft：能答一部分但要说缺什么
                action = ACTION_CAVEAT
            else:
                action = ACTION_ANSWER
            generated = None
            if allow_generate and results and action != ACTION_HANDOFF:
                gen = generate_answer(q, results)
                if gen.get("text") and gen.get("grounded"):
                    generated = str(gen["text"])
                else:
                    log.info("客服生成降级 sid=%s note=%s", sid, gen.get("note"))

    if reply is None:
        reply = _compose_reply(results, gap, q, action, generated)
    _append_turn(sid, {"ts": int(time.time()), "channel": channel, "q": q[:200],
                       "reply": reply, "action": action,
                       "gap_kind": (gap or {}).get("kind") if gap else None,
                       "generated": bool(generated)})
    lead = leads.upsert_lead(sid, channel=channel, question=q,
                             reply_kind=action, handoff=(action == ACTION_HANDOFF))
    log.info("客服应答 sid=%s channel=%s action=%s results=%d lead=%s",
             sid, channel, action, len(results), lead.get("id"))
    return {"session_id": sid, "reply": reply, "action": action,
            "citations": [{"name": r["name"], "city": r["city"],
                           "verified": bool(r.get("verified"))} for r in results],
            "gap": gap, "lead_id": lead.get("id"), "generated": bool(generated)}

"""客户线索存储（国内化一期）：客服会话产生/更新的 lead 落盘。

对应公司项目模块 2「客户线索记录」与模块 3「客户管理」的最小闭环：
匿名咨询也留痕，命中不了的问题自动转人工 → 运营在 `GET /support/leads`
看到待跟进清单。存储沿用 favorites 的纪律：读-改-写全程持锁 + 临时文件
原子替换（读方不会读到半截 JSON），损坏按空处理并留日志（服务能恢复）。

status 语义（刻意只有三态）：
- open      默认态：有咨询记录
- handoff   转人工：知识库答不了或用户主动要人工——运营侧唯一要看的态
- closed    人工已跟进完毕；再来新消息自动重开为 open（找回客户不是新建客户）
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
LEADS_FILE = DATA_DIR / "leads.json"

MAX_LEADS = 500              # 上限防匿名刷爆文件；超出丢最旧的
SESSION_ID_MAX = 64          # session_id 最大长度（入库前截断，防脏数据撑爆行）

STATUS_OPEN = "open"
STATUS_HANDOFF = "handoff"
STATUS_CLOSED = "closed"
VALID_STATUS = {STATUS_OPEN, STATUS_HANDOFF, STATUS_CLOSED}

_lock = threading.Lock()


def _load() -> list[dict]:
    if not LEADS_FILE.exists():
        return []
    try:
        raw = json.loads(LEADS_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except (json.JSONDecodeError, OSError) as e:
        # 损坏按空处理并留痕：线索接口不能因为一个坏文件整体 500
        log.warning("线索文件解析失败，按空处理: %s: %s", type(e).__name__, e)
        return []


def _save(rows: list[dict]) -> None:
    LEADS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEADS_FILE.with_name(LEADS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, LEADS_FILE)


def upsert_lead(session_id: str, *, channel: str, question: str,
                reply_kind: str, handoff: bool) -> dict:
    """按 session 定位线索并更新；不存在则新建。

    - handoff=True：置 handoff（运营侧待跟进）
    - 非 handoff：handoff 态**保持**（人工还在处理），closed 重开为 open
    返回更新后的 lead。
    """
    sid = str(session_id)[:SESSION_ID_MAX]
    now = int(time.time())
    with _lock:
        rows = _load()
        lead = next((r for r in rows if r.get("session_id") == sid
                     and r.get("channel") == channel), None)
        if lead is None:
            lead = {"id": uuid.uuid4().hex[:12], "session_id": sid,
                    "channel": channel, "contact": sid,
                    "created_at": now, "handoff_count": 0}
            rows.append(lead)
        lead["last_question"] = str(question)[:200]
        lead["last_reply_kind"] = reply_kind
        lead["updated_at"] = now
        if handoff:
            lead["status"] = STATUS_HANDOFF
            lead["handoff_count"] = int(lead.get("handoff_count") or 0) + 1
        elif lead.get("status") == STATUS_CLOSED:
            lead["status"] = STATUS_OPEN
        elif "status" not in lead:
            lead["status"] = STATUS_OPEN
        if len(rows) > MAX_LEADS:
            rows = rows[-MAX_LEADS:]
            log.warning("线索数超上限 %d，已丢弃最旧的", MAX_LEADS)
        _save(rows)
        return dict(lead)


def set_status(lead_id: str, status: str) -> dict | None:
    """人工跟进后改状态；lead 不存在返回 None（调用方转 404）。"""
    if status not in VALID_STATUS:
        raise ValueError(f"status 必须是 {sorted(VALID_STATUS)} 之一")
    with _lock:
        rows = _load()
        lead = next((r for r in rows if r.get("id") == lead_id), None)
        if lead is None:
            return None
        lead["status"] = status
        lead["updated_at"] = int(time.time())
        _save(rows)
        return dict(lead)


def list_leads(status: str | None = None, limit: int = 100) -> list[dict]:
    """按更新时间倒序列线索；status 可过滤（None=全部）。"""
    if status is not None and status not in VALID_STATUS:
        raise ValueError(f"status 必须是 {sorted(VALID_STATUS)} 之一")
    with _lock:
        rows = _load()
    rows.sort(key=lambda r: r.get("updated_at") or 0, reverse=True)
    if status is not None:
        rows = [r for r in rows if r.get("status") == status]
    n = max(1, min(int(limit), 200))
    return rows[:n]

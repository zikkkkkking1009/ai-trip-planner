"""LLM 调用成本台账（国内化一期·底座件）：每次 LLM 调用只记账、不改行为。

对应公司项目模块 6 的「API 成本」能力。为什么做它：限流挡的是滥用，
账本回答的是「钱花在哪」——现在评价生成 / 攻略抽取 / 意图解析 / 总评 / RAG 生成
共用两个 Key，出了问题只能翻日志逐条数；有了台账，`GET /admin/usage?days=7`
直接给出按用途 × 模型聚合的调用量与 token 数。

设计约束：
- **只记账**：record() 永远不抛异常、不阻塞业务——记账失败宁可丢一条，不能把
  正在跑的规划/问答拖死（与媒体缓存写入同一取舍）。
- **零依赖**：JSONL 追加写 + threading.Lock，与 favorites / food 同一套落盘纪律。
- **轮转**：文件超 2MB 时保留最近 2000 行重写，防止无限膨胀（演示规模够用）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict
from pathlib import Path

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
LEDGER_FILE = DATA_DIR / "llm_usage.jsonl"

LEDGER_ROTATE_BYTES = 2_000_000      # 超过即轮转
LEDGER_KEEP_LINES = 2000             # 轮转时保留的最近行数

_ledger_lock = threading.Lock()


def record(purpose: str, model: str | None = None, resp: object = None,
           t0: float | None = None) -> None:
    """记一次 LLM 调用。

    purpose：用途标签（edit_parse / reviews / plan_review / extract /
    align_arb / rag_answer / support_answer……），聚合维度，写错只是报表难看。
    model：模型名；resp：OpenAI 响应对象（取 usage token 数，取不到就记 None）；
    t0：调用方在发起请求前取的 time.monotonic()（换算耗时，None 则不记耗时）。
    """
    entry: dict[str, object] = {
        "ts": int(time.time()),
        "purpose": purpose,
        "model": model,
        "prompt_tokens": _token_of(resp, "prompt_tokens"),
        "completion_tokens": _token_of(resp, "completion_tokens"),
        "latency_ms": (round((time.monotonic() - t0) * 1000) if t0 is not None else None),
    }
    try:
        with _ledger_lock:
            LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
            with LEDGER_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            _rotate_if_needed()
    except OSError as e:
        # 记账是旁路能力：写失败不影响主流程，但要留痕（静默吞异常是踩过的坑）
        log.warning("LLM 台账写入失败: %s: %s", type(e).__name__, e)


def _token_of(resp: object, field: str) -> int | None:
    """从 OpenAI 响应对象上取 usage token 数；假响应 / 无 usage 时返回 None。"""
    try:
        usage = getattr(resp, "usage", None)
        if usage is None:
            return None
        v = getattr(usage, field, None)
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _rotate_if_needed() -> None:
    """文件超限 → 只保留最近 LEDGER_KEEP_LINES 行（调用方须持锁；追加后调用，
    稳态行数恰好等于 KEEP_LINES）。"""
    try:
        if not LEDGER_FILE.exists() or LEDGER_FILE.stat().st_size <= LEDGER_ROTATE_BYTES:
            return
        lines = LEDGER_FILE.read_text(encoding="utf-8").splitlines()
        tmp = LEDGER_FILE.with_name(LEDGER_FILE.name + ".tmp")
        tmp.write_text("\n".join(lines[-LEDGER_KEEP_LINES:]) + "\n", encoding="utf-8")
        os.replace(tmp, LEDGER_FILE)
        log.info("LLM 台账已轮转：%d 行 → 保留最近 %d 行", len(lines), LEDGER_KEEP_LINES)
    except OSError as e:
        # 轮转失败只是文件大了点，不值得让记账主路径失败
        log.warning("LLM 台账轮转失败: %s: %s", type(e).__name__, e)


def summary(days: int = 7) -> dict[str, object]:
    """按「用途 × 模型」聚合最近 N 天的调用量与 token 数（/admin/usage 数据源）。"""
    cutoff = time.time() - max(1, days) * 86400
    groups: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
    total = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    try:
        raw = LEDGER_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        # 文件不存在是常态（还没有任何调用）；其它读失败留痕后按空处理
        if not isinstance(exc, FileNotFoundError):
            log.warning("LLM 台账读取失败: %s: %s", type(exc).__name__, exc)
        raw = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue    # 半截行/脏行跳过——台账是旁路数据，不值得为它报错
        if not isinstance(e.get("ts"), (int, float)) or e["ts"] < cutoff:
            continue
        key = (str(e.get("purpose") or "unknown"), str(e.get("model") or "unknown"))
        g = groups[key]
        g["calls"] += 1
        total["calls"] += 1
        for f in ("prompt_tokens", "completion_tokens"):
            v = e.get(f)
            if isinstance(v, int) and v > 0:
                g[f] += v
                total[f] += v
    return {
        "days": max(1, days),
        "file": str(LEDGER_FILE.name),
        "total": total,
        "groups": [
            {"purpose": p, "model": m, **v}
            for (p, m), v in sorted(groups.items(),
                                    key=lambda kv: (-kv[1]["calls"], kv[0][0]))
        ],
    }

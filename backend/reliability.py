"""统一的超时与重试：LLM、高德等外部调用共用。

策略（刻意保守，避免放大上游故障）：
- **只重试可重试错误**：超时、连接错误、429、5xx。鉴权失败（401/403）、
  参数错误（400/422）以及解析类错误直接抛出，重试没有意义
- 指数退避 1s → 2s → 4s，叠加 ±20% 抖动，避免多实例同时重试打爆上游
- 每次重试都写日志（与 logging_setup 联动），最终失败抛出最后一次异常
- 全部使用标准库，不引 tenacity

用法：
    from reliability import retry_call
    data = retry_call(lambda: api_call(), what="高德驾车时长")
"""
from __future__ import annotations

import logging
import random
import time
from typing import Callable, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
RETRYABLE_EXC_NAMES = {
    "APITimeoutError", "APIConnectionError", "InternalServerError", "RateLimitError",
    "Timeout", "ReadTimeout", "ConnectTimeout", "ConnectError", "ConnectionError",
    "ConnectionResetError", "RemoteProtocolError", "URLError", "IncompleteRead",
    "SSLError", "ProtocolError",
}


def is_retryable(exc: Exception) -> bool:
    """判断异常是否值得重试（网络/限流/服务端错误 → 是）。"""
    if type(exc).__name__ in RETRYABLE_EXC_NAMES:
        return True
    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
    return isinstance(status, int) and status in RETRYABLE_STATUS


def retry_call(fn: Callable[[], T], *, attempts: int = 3, base_delay: float = 1.0,
               what: str = "外部调用") -> T:
    """执行 fn，按策略重试；不可重试或次数用尽时抛出最后一次异常。"""
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 —— 需要按类型决定是否重试
            if i >= attempts or not is_retryable(exc):
                log.warning("%s 失败（第 %d/%d 次，不重试）: %s: %s",
                            what, i, attempts, type(exc).__name__, exc)
                raise
            delay = base_delay * (2 ** (i - 1)) * (1 + random.random() * 0.2)
            log.warning("%s 第 %d/%d 次失败，%.1fs 后重试: %s: %s",
                        what, i, attempts, delay, type(exc).__name__, exc)
            time.sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover

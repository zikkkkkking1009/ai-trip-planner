"""统一日志配置与请求上下文。

设计要点：
- 用标准库 `logging` + `dictConfig`，**零第三方依赖**
- `request_id` 用 `contextvars` 注入，日志格式自动带上——一次规划的全链路可串起来
- 级别由环境变量 `LOG_LEVEL` 控制（默认 INFO，排查问题时可设 DEBUG）
- 可重定向到文件：`LOG_FILE=/path/app.log`

用法：
    from logging_setup import setup_logging, request_id_var
    setup_logging()
"""
from __future__ import annotations

import contextvars
import logging
import logging.config
import os
import uuid

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-")

_FMT = "%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s"


class RequestIdFilter(logging.Filter):
    """把当前请求 ID 注入到每条日志记录。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.request_id = request_id_var.get()
        return True


def new_request_id() -> str:
    return uuid.uuid4().hex[:8]


def setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    handlers: dict = {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "std",
            "filters": ["reqid"],
            "stream": "ext://sys.stdout",
        }
    }
    root_handlers = ["console"]

    log_file = os.environ.get("LOG_FILE")
    if log_file:
        handlers["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "std",
            "filters": ["reqid"],
            "filename": log_file,
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 3,
            "encoding": "utf-8",
        }
        root_handlers.append("file")

    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"reqid": {"()": RequestIdFilter}},
        "formatters": {"std": {"format": _FMT, "datefmt": "%H:%M:%S"}},
        "handlers": handlers,
        "root": {"level": level, "handlers": root_handlers},
        # 访问日志降噪：uvicorn 自己的 access log 与我们的请求日志重复
        "loggers": {
            "uvicorn.access": {"level": "WARNING"},
            "httpx": {"level": "WARNING"},
            "openai": {"level": "WARNING"},
        },
    })

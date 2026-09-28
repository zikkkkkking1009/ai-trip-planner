"""pytest 路径引导：让测试文件能 import backend 下的模块。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import selftest


@pytest.fixture(autouse=True)
def isolated_rate_limit(monkeypatch):
    """默认关闭限流，避免测试套件把自己的额度打满。

    为什么必须做：限流前缀 `'/plan'` 会覆盖 `/plans`、`/plans/delete`、`/plan/async`……
    整个测试套件跑下来对这类路径的请求远超 30 次/分钟，而 pytest 全程不到一分钟 ——
    不隔离的话会出现"单独跑绿、全量跑红"的假故障（而且报的是 429，很难看出是限流）。
    顺带每条用例重置计数，保证用例之间互不影响；要测 429 的用例自己把阈值调小。
    """
    from collections import defaultdict, deque

    import main
    monkeypatch.setattr(main, "_rate_hits", defaultdict(deque))
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MIN", 10 ** 9)


@pytest.fixture(autouse=True)
def no_selftest_network(monkeypatch):
    """测试期绝不真发 Key 探测请求。

    为什么必须在 conftest 里做（而不是在某个测试里）：test_tasks.py 用的是
    `with TestClient(app)` —— 这会触发 FastAPI lifespan → selftest.schedule_probe()。
    CI 里没有 Key 所以天然零外呼，但**开发者本机一旦配了 Key，跑 pytest 就会
    真发请求、真花钱**。必须在唯一能罩住所有测试的收口点掐掉，
    不能依赖「CI 没有 Key」这个巧合。

    _spawn 直接置空：保证后台线程根本不会被创建（比事后等它退出可靠）。
    """
    monkeypatch.setenv("SELFTEST_SKIP", "1")
    monkeypatch.setattr(selftest, "_spawn", lambda fn: None)

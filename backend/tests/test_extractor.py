"""LLM 抽取模块测试：常量与容错解析（不依赖网络与密钥）。"""
from __future__ import annotations

import pytest

from extractor import LLM_TIMEOUT_S, _tolerant_json_parse


def test_llm_timeout_constant_is_30s():
    """回归：之前 LLM_TIMEOUT_S 在 extractor 模块内未定义，调用 extract_spots 必崩。

    该常量与 editor.py 同名常量必须独立维护（避免一处改动静默影响另一处），
    并保持合理范围（30s 上下，不超过 2 分钟，否则前端超时先触发）。
    """
    assert isinstance(LLM_TIMEOUT_S, (int, float))
    assert 10 <= LLM_TIMEOUT_S <= 120


def test_no_key_raises_runtimeerror_not_nameerror(monkeypatch):
    """关键回归：缺 LLM_API_KEY 时必须抛 RuntimeError，而非 NameError。

    之前 LLM_TIMEOUT_S 未定义导致 NameError 在客户端构造那一行抛出，
    上层只 catch 了 RuntimeError / ValueError / KeyError，NameError 会
    穿透到 HTTP 500 而非友好的 503。
    """
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    from extractor import extract_spots
    with pytest.raises(RuntimeError, match="LLM_API_KEY|未配置"):
        extract_spots("随便一段攻略文本")


def test_tolerant_json_parses_fenced_json():
    assert _tolerant_json_parse('```json\n{"spots": []}\n```') == {"spots": []}


def test_tolerant_json_strips_trailing_comma():
    raw = '{"spots": [{"name": "A"}, {"name": "B"},]}'
    out = _tolerant_json_parse(raw)
    assert out == {"spots": [{"name": "A"}, {"name": "B"}]}


def test_tolerant_json_recovers_when_outer_braces_only():
    raw = 'LLM 啰嗦了一句话：{"spots": [{"name": "兵马俑"}]}，后面再说点别的。'
    out = _tolerant_json_parse(raw)
    assert out == {"spots": [{"name": "兵马俑"}]}


def test_tolerant_json_no_json_raises_value_error():
    with pytest.raises(ValueError):
        _tolerant_json_parse("hi 没有任何 JSON")
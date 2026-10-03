"""R4 生成层测试：实体回链核查（纯函数）+ /ask with_answer 接线与降级。

LLM 真调用不进测试（rag._llm 一律 mock）——检索质量口径见 eval_rag.py。
"""
from __future__ import annotations

import rag
from rag import ask, check_grounding

_ALL = {"大同古城", "乳扇", "秦始皇兵马俑博物馆", "洪崖洞民俗风貌区"}


# ---- check_grounding：答案实体必须来自引用片段（反幻觉闸门）----

def test_grounding_passes_when_entities_cited():
    g = check_grounding("大同古城里有小吃街。", {"大同古城"}, _ALL)
    assert g == {"grounded": True, "outside": []}


def test_grounding_flags_uncited_entity():
    g = check_grounding("大同古城旁还能吃到乳扇。", {"大同古城"}, _ALL)
    assert g["grounded"] is False
    assert g["outside"] == ["乳扇"]


def test_grounding_substring_of_cited_is_exempt():
    # 库内另有实体「古城」，但它只是被引用「大同古城」的子串——同一提及，不违规
    g = check_grounding("大同古城始建于明代。", {"大同古城"}, _ALL | {"古城"})
    assert g == {"grounded": True, "outside": []}


def test_grounding_uncited_standalone_still_flagged():
    g = check_grounding("古城值得一去。", {"乳扇"}, _ALL | {"古城"})
    assert g["grounded"] is False
    assert g["outside"] == ["古城"]


def test_grounding_normalization_ignores_whitespace():
    g = check_grounding("乳扇，牛奶做的干酪", {"乳  扇"}, _ALL)  # 引用名带空白
    assert g == {"grounded": True, "outside": []}


# ---- generate_answer：降级与 grounded 接线（LLM mock）----

class _FakeMsg:
    content = "大理的乳扇是牛奶做的扇形干酪。"


class _FakeChoice:
    message = _FakeMsg()


class _FakeResp:
    choices = [_FakeChoice()]


class _FakeCompletions:
    def create(self, **kw):
        return _FakeResp()


class _FakeChat:
    completions = _FakeCompletions()


class _FakeClient:
    chat = _FakeChat()


def test_generate_answer_degrades_on_channel_error(monkeypatch):
    def _boom(fast: bool = False):
        raise RuntimeError("通道挂了")

    monkeypatch.setattr(rag, "_llm", _boom)
    out = rag.generate_answer("乳扇", ask("乳扇")["results"])
    assert out["text"] is None
    assert "通道挂了" in str(out["note"])
    assert out["grounded"] is None


def test_generate_answer_grounds_against_cited(monkeypatch):
    monkeypatch.setattr(rag, "_llm", lambda fast=False: (_FakeClient(), "fake-model"))
    out = rag.generate_answer("乳扇", ask("乳扇")["results"])
    assert out["text"] == "大理的乳扇是牛奶做的扇形干酪。"
    assert out["model"] == "fake-model"
    assert out["grounded"] is True


def test_ask_default_has_no_generation():
    res = ask("乳扇")
    assert "answer" not in res


def test_ask_with_answer_wires_generation(monkeypatch):
    monkeypatch.setattr(rag, "_llm", lambda fast=False: (_FakeClient(), "fake-model"))
    res = ask("乳扇", with_answer=True)
    assert res["answer"]["grounded"] is True
    assert res["results"][0]["name"] == "乳扇"

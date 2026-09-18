"""实体对齐评估：跑标注集，输出 Precision / Recall / F1。

判定规则（避免「高德名字比我标注的多个后缀」被误判为错）：
correct = expected 与 predicted 完全相等，或互为子串（归一化后）

指标口径：
- Precision = 判对数 / 尝试对齐数（no_candidate 不算尝试）
- Recall    = 判对数 / 标注总数（没召回、转人工都算 miss）
- F1 = 2PR / (P+R)
另报：转人工率（needs_review 占比）——这是「用户修正返回链路」的设计依据。

用法：python eval_aligner.py
"""
from __future__ import annotations

import json
from pathlib import Path

from aligner import POIAligner, _normalize

EVAL_SET = Path(__file__).parent / "eval_set.json"


def is_correct(expected: str, predicted: str) -> bool:
    e, p = _normalize(expected), _normalize(predicted)
    if not e or not p:
        return False
    return e == p or e in p or p in e


_NOTE_MAP = {"renamed_main": "主名重查", "llm_arbitrated": "LLM仲裁", "ok": ""}


def _note(r) -> str:
    if r.needs_review:
        return "转人工"
    return _NOTE_MAP.get(r.reason, "")


def main() -> None:
    cases = json.loads(EVAL_SET.read_text(encoding="utf-8"))
    aligner = POIAligner()

    rows, correct, attempted, reviewed = [], 0, 0, 0
    for c in cases:
        r = aligner.align(c["alias"], c["city"])
        pred = r.best.name if r.best else "—"
        ok = bool(r.best and is_correct(c["expected"], pred))
        if r.best:
            attempted += 1
        if r.needs_review:
            reviewed += 1
        if ok:
            correct += 1
        rows.append((c["alias"], c["city"], c["expected"], pred,
                     f"{r.confidence:.2f}", "✅" if ok else "❌",
                     _note(r)))

    print(f"{'别名':<8}{'城市':<5}{'标注':<14}{'对齐结果':<24}{'置信':<6}{'判定':<4}备注")
    for row in rows:
        print(f"{row[0]:<8}{row[1]:<5}{row[2]:<14}{row[3]:<24}{row[4]:<6}{row[5]:<4}{row[6]}")

    n = len(cases)
    precision = correct / attempted if attempted else 0.0
    recall = correct / n if n else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    print(f"\n标注集 {n} 条 | 召回成功 {attempted} | 判对 {correct}")
    print(f"Precision = {precision:.1%}（判对 / 尝试对齐）")
    print(f"Recall    = {recall:.1%}（判对 / 标注总数）")
    print(f"F1        = {f1:.1%}")
    print(f"转人工率  = {reviewed / n:.1%}（needs_review，走用户点选兜底）")
    print(f"POI搜索统计：{aligner.stats}，缓存命中率 "
          f"{aligner.stats['cache_hits'] / max(1, aligner.stats['cache_hits'] + aligner.stats['api_calls']):.0%}")


if __name__ == "__main__":
    main()

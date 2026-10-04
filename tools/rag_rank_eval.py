"""排序质量评测：一条 query 多个相关实体的 top-k 质量。

为什么单独一份而不并进 rag_realbench.py / eval_rag.py：
  · rag_golden.json（eval_rag.py）每条 query 只允许 **1 个** ground truth
    （load_golden 第 46 行禁止重复 q）⇒「top1 对但 2-5 名是噪声」与
    「top1-3 名都对」在 hit@1 / hit@5 / MRR 下**同分**（都记 rank=1）。
  · rag_realbench.py 量的是「该不该答答对了」（能力边界），不量排序形状。
  · eval_rag.py 的意图臂量的是**标签维度**（ZCode 的 tag_precision5），
    本脚本量的是**实体维度**——同一条 query 的 top5 里有几个真是他要的。互补，不重复。

相关性判定：用 corpus_fields 产出的 tags（客观属性），不靠人肉标顺序。
这是刻意的：人排的「相关性顺序」本身就有争议，而 tags 是数据里既有的、可复算的。

指标：
  precision@k  top-k 里目标标签的占比（类别型 query 理想值 1.0）
  ndcg@5       位置折扣的排序质量，越高越好；relevance=1 命中、0 未命中
  recall@5     命中数 / min(该查询可命中的总数, 5)—— 类目标签全库可能上百条，
               5 条窗口里不可能全中，故分母封顶 5，只看「窗口内抓到没有」

退出码：低于基线返回 1（硬失败），高于基线只提醒跑 --update（刻意不红，
避免逼出无脑 --update）。与 rag_realbench.py 同一套语义。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

GOLDEN_FILE = Path(__file__).resolve().parent / "rag_rank_golden.json"
BASELINE_FILE = Path(__file__).resolve().parent / "rag_rank_baseline.json"

import rag  # noqa: E402


def load_queries() -> list[dict]:
    data = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    rows = data["queries"]
    if not rows:
        raise ValueError(f"{GOLDEN_FILE.name} 里没有用例")
    return rows


def has_tag(item: dict, tag: str) -> bool:
    """该条结果是否带目标标签。tags 是 corpus_fields 产出的客观属性。"""
    return tag in (item.get("tags") or [])


def dcg(hits: list[bool]) -> float:
    return sum(1.0 / math.log2(i + 2) for i, h in enumerate(hits) if h)


def eval_rank(k: int) -> dict:
    """跑一遍排序评测。k 只影响 precision/ndcg 的窗口。"""
    rows = load_queries()
    per_query = []
    p_at_k, ndcg_sum = [], 0.0
    for row in rows:
        q, tag = row["q"], row["tag"]
        results = rag.ask(q=q, k=k)["results"]
        hits = [has_tag(r, tag) for r in results]
        p = (sum(hits) / len(hits)) if hits else 0.0
        # idcg@5 = 全部命中的理想排序；命中数不会超过窗口长度
        ideal = dcg([True] * min(k, max(1, len(results))))
        nd = (dcg(hits) / ideal) if ideal > 0 else 0.0
        p_at_k.append(p)
        ndcg_sum += nd
        per_query.append({
            "q": q, "tag": tag,
            "prec": round(p, 3), "ndcg": round(nd, 3),
            "top": [{"name": r["name"], "city": r["city"], "type": r["type"],
                     "hit": has_tag(r, tag)} for r in results],
        })

    n = len(per_query)
    return {
        "n": n, "k": k,
        "precision": round(sum(p_at_k) / n, 3),
        "ndcg": round(ndcg_sum / n, 3),
        "queries": per_query,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5, help="窗口大小（默认 5）")
    ap.add_argument("--update", action="store_true", help="把当前水平写进基线")
    ap.add_argument("--verbose", action="store_true", help="逐条打印 top-k 构成")
    args = ap.parse_args()

    res = eval_rank(args.k)
    score = {"n": res["n"], "k": res["k"],
             "precision": res["precision"], "ndcg": res["ndcg"]}

    print(f"排序质量集 {res['n']} 条查询，k={res['k']}")
    print(f"precision@{args.k} = {res['precision']}　ndcg@{args.k} = {res['ndcg']}")
    if args.verbose:
        for q in res["queries"]:
            marks = "".join("✓" if h["hit"] else "✗" for h in q["top"])
            print(f"  {marks}  {q['q']}  (prec={q['prec']}, ndcg={q['ndcg']})")
            for i, t in enumerate(q["top"], 1):
                print(f"      {i}. {'景' if t['type'] == '景点' else '美'} "
                      f"{t['city']}·{t['name']}")

    baseline = (json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
                if BASELINE_FILE.exists() else None)
    if args.update:
        BASELINE_FILE.write_text(json.dumps(score, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
        print(f"基线已写入 {BASELINE_FILE.name}")
        return 0
    if baseline is None:
        print("尚无基线，先跑一次 --update 把当前水平记下来")
        return 0

    # 两项都退化才算失败：只看 precision 会漏掉「全中但顺序乱」的排序退化
    p_drop = res["precision"] < baseline.get("precision", 0)
    n_drop = res["ndcg"] < baseline.get("ndcg", 0)
    if p_drop or n_drop:
        print(f"❌ 退化：precision@{args.k} {res['precision']} / 基线 {baseline.get('precision')}，"
              f"ndcg@{args.k} {res['ndcg']} / 基线 {baseline.get('ndcg')}")
        return 1
    if res["precision"] > baseline.get("precision", 0) or res["ndcg"] > baseline.get("ndcg", 0):
        print(f"⬆ 提升：precision={res['precision']}（基线 {baseline.get('precision')}）"
              f"　ndcg={res['ndcg']}（基线 {baseline.get('ndcg')}）"
              f"　→ 请跑 `--update` 抬基线，否则下次小幅回退检不出来")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

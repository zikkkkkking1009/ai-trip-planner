"""RAG 真实问题基准（realbench）——用「用户真会问的话」给检索层打分。

为什么要有它（2026-10-04, workbuddy）：
    现有的 `backend/eval_rag.json` + `rag_golden.json` 是**自造**的 30 条（query→专名），
    BM25 在上面 hit@1 90.0 / MRR 0.940。指标是真的，但它只测一种能力 ——
    我把这 30 条的 query 换成人会脱口而出的问法（「西安有什么好吃的」「兵马俑玩多久」），
    真机实测 5/6 答非所问。**分数高和能用是两回事**，缺的就是这把尺子。

和 eval_rag 的区别：
    eval_rag   —— 自造 query，答案是造题时写的专名，测「能不能把专名捞回来」
    realbench  —— 真实问法，断言是**客观属性**（城市对不对 / 类型对不对 / 有没有那个字段），
                  不含「我认为 top1 应该是 X」这种主观期望，避免为了刷分调期望

三类断言（每条用例按需启用）：
    city  返回的**每一条**结果都必须落在期望城市 —— 跨城市污染是实测最大的翻车点
    kind  top1 的类型（景点/美食）必须对 —— 「有什么好吃的」返回景点就是没听懂
    attr  top1 的语料文本里必须真的含有被问的那个字段（门票/时长/开放时间）——
          捞回正确的景点但文本里没有答案，用户拿到手还是空的

不计分项（gap）：
    库里**根本没有**这个字段的，不算缺陷、不计入分母，但会打印出来提醒。
    判据是硬数据：375 个景点里 313 个 ticket=0（无票价源数据）、358 个开放时间
    是默认值 8:00–18:00。把默认值写进语料等于编造，宁可缺。
    这些缺口要补只能补源头数据，改检索算法没用 —— 这条结论本身就是产出。

用法：
    python tools/rag_realbench.py                 # 与基线比对（防退化）
    python tools/rag_realbench.py --update        # 分数提高后抬基线
    python tools/rag_realbench.py --json          # 机器可读输出
退出码：分数低于基线 = 1（退化，硬失败）；高于基线 = 0 但喊你抬基线（不抬的话
下次小幅回退就检测不出来了）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent / "backend"
sys.path.insert(0, str(BACKEND))          # backend 下是平铺模块（from demo_data import ...）

import rag                                 # noqa: E402
from rag import FACT_PATTERNS              # noqa: E402  字段形态的**单一事实源**在 rag.py

BASELINE_FILE = HERE / "rag_realbench_baseline.json"

# attr → 语料文本里必须出现的形态。直接复用后端的正则，两边各写一份必然漂移——
# 后端改了拼法而这边没跟上，基准就会拿「格式变了」当成「能力退化了」，或者反过来。
ATTR_PATTERNS = FACT_PATTERNS

# ---------------------------------------------------------------------------
# 用例：全部是真实问法，断言只写客观属性
# city  = 期望城市（None = 不约束城市，但结果仍须同属一个城市才不叫污染）
# kind  = top1 期望类型
# attr  = top1 语料文本必须含有该字段
# gap   = 期望 /ask 返回的能力缺口类型（unsupported / attr / soft）
# abstain = 期望硬拒答（必须 0 条 + 说清为什么）
# gap_note = 非空则该用例不计分（源数据缺失），但仍打印
# ---------------------------------------------------------------------------
CASES: list[dict] = [
    {"q": "西安有什么好吃的", "city": "西安", "kind": "美食"},
    {"q": "成都火锅推荐", "city": "成都", "kind": "美食"},
    {"q": "西安回民街小吃", "city": "西安", "kind": "景点"},
    {"q": "拉萨必去的景点", "city": "拉萨", "kind": "景点"},
    {"q": "杭州西湖有什么玩的", "city": "杭州", "kind": "景点"},
    {"q": "苏州园林哪个值得去", "city": "苏州", "kind": "景点"},
    {"q": "黄山要爬多久", "city": "黄山", "kind": "景点", "attr": "时长"},
    {"q": "兵马俑门票多少钱", "city": "西安", "kind": "景点", "attr": "门票"},
    {"q": "兵马俑要玩多久", "city": "西安", "kind": "景点", "attr": "时长"},
    {"q": "西安城墙几点关门", "city": "西安", "kind": "景点", "attr": "开放"},
    # 交通类：能给出景点身份，但必须说清没有路线数据
    {"q": "鼓浪屿怎么去", "city": "厦门", "kind": "景点", "gap": "soft"},
    # 库里压根没有的数据类型：必须硬拒答，不能拿景点凑
    {"q": "哈尔滨冬天穿什么", "abstain": "unsupported"},
    {"q": "故宫需要预约吗", "abstain": "unsupported"},
    {"q": "九月去拉萨合适吗", "abstain": "unsupported"},
    # 条目在、但缺被问的字段：报缺口（附实数覆盖率），不静默不答、也不编
    {"q": "大理古城门票多少钱", "city": "大理", "kind": "景点", "gap": "attr"},
    {"q": "丽江古城几点关门", "city": "丽江", "kind": "景点", "gap": "attr"},
    # ↓ 正向形态仍要单独验：缺口机制不能把「有数据」也一起拒了
    #   （黄山风景区 ticket=190，属于 62 个有票价数据的景点之一）
    {"q": "黄山门票多少钱", "city": "黄山", "kind": "景点", "attr": "门票"},
]


def check_case(case: dict) -> tuple[list[bool], list[str]]:
    """返回 (每项检查是否通过, 失败原因)。"""
    q = case["q"]
    out = rag.ask(q=q, k=5)
    results = out.get("results", [])
    gap = out.get("gap") or {}
    ok: list[bool] = []
    why: list[str] = []

    # 拒答类：必须真的 0 条 + 说清缺什么。两项合成一条断言，避免「说了不做」
    if case.get("abstain"):
        good = (bool(out.get("abstain")) and not results
                and gap.get("kind") == case["abstain"])
        return [good], [] if good else [
            f"应硬拒答（{case['abstain']}），实际返回 {len(results)} 条 / gap={gap.get('kind')}"]

    if not results:
        return [False], ["返回 0 条（库里应该有东西）"]
    ok.append(True)
    why.append("")

    # city：每一条都必须在期望城市
    want_city = case.get("city")
    if want_city is not None:
        bad = [f"{r['city']}·{r['name']}" for r in results if r["city"] != want_city]
        ok.append(not bad)
        why.append("跨城市污染：" + "、".join(bad[:3]) if bad else "")

    # kind：top1 类型
    want_kind = case.get("kind")
    if want_kind is not None:
        top = results[0]
        ok.append(top["type"] == want_kind)
        why.append(f"top1 是{top['type']}·{top['name']}，问的是{want_kind}" if top["type"] != want_kind else "")

    # attr：top1 文本里真的有那个字段
    want_attr = case.get("attr")
    if want_attr is not None:
        key = {"门票": "门票", "时长": "游玩时长", "开放": "开放时间"}[want_attr]
        text = str(results[0].get("text", ""))
        hit = bool(ATTR_PATTERNS[key].search(text))
        ok.append(hit)
        why.append(f"top1={results[0]['name']} 的语料里没有「{want_attr}」字段" if not hit else "")

    # gap：必须如实报出缺口类型
    want_gap = case.get("gap")
    if want_gap is not None:
        good = gap.get("kind") == want_gap
        ok.append(good)
        why.append("" if good else
                   f"期望报缺口 {want_gap}，实际 {gap.get('kind') or '没报'}")

    return ok, why


def main() -> int:
    ap = argparse.ArgumentParser(description="RAG 真实问题基准")
    ap.add_argument("--update", action="store_true", help="把当前分数写为基线")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    passed = 0
    total = 0
    case_pass = 0
    case_total = 0
    rows: list[dict] = []
    gaps: list[dict] = []

    for case in CASES:
        ok, why = check_case(case)
        fails = [w for w in why if w]
        rows.append({"q": case["q"], "ok": not fails, "fails": fails, "gap_note": case.get("gap_note")})
        if case.get("gap_note"):
            gaps.append({"q": case["q"], "gap_note": case["gap_note"], "fails": fails})
            continue
        case_total += 1
        total += len(ok)
        passed += sum(1 for x in ok if x)
        if not fails:
            case_pass += 1

    score = {"checks": f"{passed}/{total}", "cases": f"{case_pass}/{case_total}",
             "passed": passed, "total": total, "case_pass": case_pass,
             "case_total": case_total}
    baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8")) if BASELINE_FILE.exists() else None

    if args.json:
        print(json.dumps({"score": score, "baseline": baseline, "rows": rows,
                          "gaps": gaps}, ensure_ascii=False, indent=2))
    else:
        print("RAG 真实问题基准（realbench）——断言只写客观属性，不自造期望答案")
        print("=" * 72)
        for r in rows:
            if r["gap_note"]:
                print(f"  -  {r['q']}   〔不计分：{r['gap_note']}〕")
            elif r["ok"]:
                print(f"  ✓  {r['q']}")
            else:
                print(f"  ✗  {r['q']}")
                for f in r["fails"]:
                    print(f"        └ {f}")
        print("=" * 72)
        print(f"检查项 {score['checks']}　用例全绿 {score['cases']}")
        if baseline:
            print(f"基线   {baseline['checks']}　{baseline['cases']}")

    if args.update:
        BASELINE_FILE.write_text(json.dumps(score, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
        print(f"基线已写入 {BASELINE_FILE.name}")
        return 0

    if baseline is None:
        print("尚无基线，先跑一次 --update 把当前水平记下来")
        return 0
    if passed < baseline["passed"]:
        print(f"❌ 退化：{passed} < 基线 {baseline['passed']} 项通过")
        return 1
    if passed > baseline["passed"]:
        # 提升不判负（红着逼人抬基线只会逼出无脑 --update），但必须喊一声：
        # 基线不抬，下次小幅回退就检测不出来了。
        print(f"⬆ 提升：{passed} > 基线 {baseline['passed']}"
              f"　→ 请跑 `python tools/rag_realbench.py --update` 抬基线，"
              f"否则下次小幅回退检不出来")
    return 0


if __name__ == "__main__":
    sys.exit(main())

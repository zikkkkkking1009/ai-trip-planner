"""模型选型基准：把「抽取 / 意图解析 / 评价生成」三类任务在两个模型上各跑 N 次。

用途：技术选型实验的可复现脚本（结果写入 docs/experiments.md）。
判据不只延迟，还包括**格式遵从性**——轻量模型常照抄提示词模板、漏字段，
这类失败在线上表现为「功能静默降级」，必须量化。

用法：
    python bench_models.py              # 默认 3 次/任务
    python bench_models.py --repeat 5
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import statistics
import time

from commute import load_env_file

GUIDE = """西安三日游攻略：第一天去秦始皇兵马俑博物馆和華清宫，晚上回民街吃小吃；
第二天陕西历史博物馆（记得提前预约）、大雁塔、大唐不夜城看夜景；
第三天西安城墙骑行，然后去永兴坊喝摔碗酒。钟楼鼓楼晚上灯光很美。"""


def _load_env() -> None:
    for k, v in load_env_file().items():
        os.environ.setdefault(k, v)


def _providers() -> dict[str, dict]:
    """主模型（重任务）与快模型（轻任务）两套配置。"""
    out = {}
    if os.environ.get("LLM_BASE_URL"):
        out["main"] = {"base": os.environ["LLM_BASE_URL"],
                       "key": os.environ.get("LLM_API_KEY", ""),
                       "model": os.environ.get("LLM_MODEL_ID", "")}
    if os.environ.get("LLM_FAST_BASE_URL"):
        out["fast"] = {"base": os.environ["LLM_FAST_BASE_URL"],
                       "key": os.environ.get("LLM_FAST_API_KEY", ""),
                       "model": os.environ.get("LLM_FAST_MODEL", "")}
    return out


def _with_provider(cfg: dict):
    """让 editor 里的 _llm 指向指定 provider（影响意图解析 / 评价生成）。"""
    import editor
    def fake(fast: bool = False):
        from openai import OpenAI
        return OpenAI(api_key=cfg["key"], base_url=cfg["base"], timeout=120), cfg["model"]
    editor._llm = fake
    return editor


@contextlib.contextmanager
def _env_provider(cfg: dict):
    """extractor 直接读环境变量，需临时覆盖 LLM_* 才能测到指定模型。"""
    keys = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL_ID")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ["LLM_API_KEY"] = cfg["key"]
    os.environ["LLM_BASE_URL"] = cfg["base"]
    os.environ["LLM_MODEL_ID"] = cfg["model"]
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def bench(name: str, label: str, fn, repeat: int) -> dict:
    lat, ok = [], 0
    for _ in range(repeat):
        t0 = time.time()
        try:
            r = fn()
            lat.append(time.time() - t0)
            if r:
                ok += 1
        except Exception:
            lat.append(time.time() - t0)
    return {"provider": name, "task": label, "repeat": repeat,
            "latency_mean_s": round(statistics.mean(lat), 2) if lat else None,
            "latency_min_s": round(min(lat), 2) if lat else None,
            "success_rate": round(ok / repeat, 2) if repeat else None}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=3)
    args = ap.parse_args()
    _load_env()

    import editor
    results = []
    for pname, cfg in _providers().items():
        _with_provider(cfg)
        print(f"[{pname}] {cfg['model']} 开始…", flush=True)

        def t_extract():
            import extractor
            with _env_provider(cfg):      # extractor 走环境变量，必须覆盖
                return extractor.extract_spots(GUIDE)

        def t_parse():
            return editor.parse_instruction(
                "换到西安站附近的酒店",
                "Day1: 兵马俑、华清宫\nDay2: 大雁塔\n当前住宿：汉庭酒店(西安北站店)",
                history=[{"q": "我的酒店是汉庭酒店", "ops": [], "reply": "已设为住宿"}])

        def t_reviews():
            return editor.generate_reviews("西安城墙", "科教文化服务 · 评分4.8 · 钟楼商圈")

        for label, fn in [("攻略抽取", t_extract), ("意图解析", t_parse),
                          ("评价生成", t_reviews)]:
            r = bench(pname, label, fn, args.repeat)
            results.append(r)
            print(f"  {label}: 均值 {r['latency_mean_s']}s "
                  f"(最快 {r['latency_min_s']}s) 成功率 {r['success_rate']}", flush=True)

    out = {"guide_chars": len(GUIDE), "repeat": args.repeat, "results": results}
    with open("bench_results.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n已写入 bench_results.json")


if __name__ == "__main__":
    main()

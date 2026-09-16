"""FastAPI 服务入口。

启动：uvicorn main:app --reload --port 8000
文档：http://localhost:8000/docs

接口：
- GET  /health          健康检查
- POST /plan            排期主接口：景点列表 → 求解 → 校验 → 行程
- POST /extract         攻略文本 → LLM 抽取景点（需配 LLM Key）
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from commute import CommuteMatrix
from constraint_check import check_plan
from extractor import extract_spots
from models import PlanRequest, PlanResult
from solver import Solver

app = FastAPI(title="AI 行程规划 API", version="0.2.0")


@app.get("/health")
def health() -> dict:
    cm = CommuteMatrix()
    return {"status": "ok", "solver": "greedy+2opt",
            "amap": "enabled" if cm.key else "fallback(estimate)"}


@app.post("/plan", response_model=PlanResult)
def make_plan(req: PlanRequest) -> PlanResult:
    """排期主接口。有 AMAP_KEY 时通勤走高德真实数据（带缓存），否则估算降级。"""
    if not req.spots:
        raise HTTPException(400, "景点列表为空")

    cm = CommuteMatrix()
    day_plans, unplanned, total_cost, total_score = Solver(req, cm.minutes).solve()
    report = check_plan(req, day_plans, total_cost)
    report["stats"]["commute_api"] = cm.stats
    report["stats"]["cache_hit_rate"] = round(cm.hit_rate(), 3)

    return PlanResult(
        city=req.city, days=day_plans, total_cost=total_cost,
        total_score=total_score, unplanned=unplanned, check_report=report,
    )


@app.post("/extract")
def extract(text: str, city: str = "西安") -> dict:
    """攻略文本 → LLM 抽取 → 实体对齐到高德 POI（坐标校正为真实值）。

    链路：LLM 抽取（占位坐标）→ POIAligner（标准名 + 真实坐标）
    低置信度的条目标记 needs_review，坐标保持占位值，由前端让用户点选。
    """
    try:
        spots = extract_spots(text)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except (ValueError, KeyError) as e:
        raise HTTPException(422, f"抽取失败：{e}")

    from aligner import POIAligner, align_spot
    aligner = POIAligner()
    aligned, review = [], []
    for s in spots:
        s, r = align_spot(s, aligner, city)
        item = s.model_dump() | {"confidence": r.confidence,
                                 "needs_review": r.needs_review}
        (review if r.needs_review else aligned).append(item)
    return {"count": len(spots), "spots": aligned + review,
            "needs_review_count": len(review),
            "poi_api_stats": aligner.stats}

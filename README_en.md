# AI Trip Planning System v0.5

[![CI](https://github.com/zikkkkkking1009/ai-trip-planner/actions/workflows/ci.yml/badge.svg)](https://github.com/zikkkkkking1009/ai-trip-planner/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Tests](https://img.shields.io/badge/tests-19%20passed-brightgreen)

[简体中文](README.md) | [English](README_en.md)

Turn a plain-text travel guide into a **verifiable, interactive, map-visualized** day-by-day itinerary:

```
Guide text ──LLM extraction──▶ POI aliases ──Entity alignment (AMap, F1 90%)──▶ Itinerary solver (OPTW)
                                                                                    │
User (date range / budget / time window) ─────────────────────▶ Constraint checker ◀┘
                                                                                    │
                                                            Daily itinerary JSON + WebSocket progress + map routes
```

Core design: **the LLM only understands and extracts; every hard constraint (budget / time window / opening hours) is enforced by deterministic algorithms**.
This is not a gut feeling — a three-way evaluation over 50 scenarios shows pure-LLM planning exceeds the budget in 18% of cases, while this solver: 0 (see [Evaluation](#evaluation)).

## Features

- **Itinerary solver**: trip planning modeled as an Orienteering Problem with Time Windows (OPTW); greedy construction + intra-day 2-opt + inter-day relocation; budget is enforced during construction; dropped spots carry a reason (budget / time window)
- **Geo entity alignment**: guide aliases ("兵马俑", "紫禁城") → canonical AMap POIs. Top-5 recall → 4-way score fusion (containment / char similarity / type prior / suffix extension) → hard filtering of noisy types (bus stops, admin districts) → low-confidence results routed to human review. F1 improved from 80% to 90% across three iterations on a 20-case labeled set
- **Commute matrix**: real driving durations via AMap distance API + three-tier cache (in-process memo → file → API) + QPS throttling (personal key: 3 QPS) + graceful fallback to estimation without a key. Repeated solves issue **zero** API calls
- **Async task system**: `POST /plan/async` returns a task_id instantly; a background coroutine runs the full pipeline; WebSocket streams stage-level progress; polling endpoint as fallback — no more gateway 504s on long requests
- **Constraint checker**: budget overrun (hard) / overloaded days (soft) / commute share > 40% (soft, possible detour) / empty days
- **Roadbook frontend**: mobile-card UI, date-range picker, per-day tabs, Leaflet map with per-day colored routes + legend toggles; configurable basemap (AMap / OSM, incl. GCJ-02↔WGS84 conversion)
- **LLM extraction**: OpenAI-compatible endpoints (DeepSeek/Qwen/…) + tolerant JSON parsing

## Quick Start (30 seconds, no API key)

```bash
git clone https://github.com/zikkkkkking1009/ai-trip-planner.git
cd ai-trip-planner/backend
pip install pydantic
python run_demo.py
```

Run the full service (with web UI):

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
# open http://localhost:8000 → click 开始规划 → switch to the map tab
```

Optional keys (real commute data + LLM extraction): `cp .env.example .env` and fill in `AMAP_KEY` / `LLM_API_KEY`.

## API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Web roadbook UI |
| GET | `/health` | Health check |
| GET | `/demo/spots` · `/demo/config` | Demo data / basemap config |
| POST | `/plan` | Synchronous planning |
| POST | `/plan/async` | Async planning, returns task_id |
| GET | `/task/{id}` | Task status polling |
| WS | `/ws/{id}` | Live progress stream |
| POST | `/extract` | Guide text → LLM extraction → entity alignment |

## Evaluation

50 scenarios (seed=42, reproducible); quality = 0.4·(1-commute share) + 0.4·(1-conflict rate) + 0.2·(1-load imbalance):

| Metric | Naive baseline | Pure LLM (deepseek-chat) | OPTW solver |
|--------|---------------|--------------------------|-------------|
| Avg time conflicts / plan | 0.38 | 0.06 | **0** |
| Conflict-free plans | 62% | 94% | **100%** |
| Commute share | 23.5% | 13.0% | **13.1%** |
| **Budget violations** | 12/50 | **9/50 (18%)** | **0** |
| Latency per solve | <1ms | ~800ms + API cost | **~1ms, free** |

Takeaway: with fully structured input, LLM planning quality is decent — but it cannot enforce budget constraints (18% overruns) and costs latency + money. The solver guarantees all hard constraints at zero marginal cost. See `backend/eval_results.json`.

## Project Layout

```
backend/
├── models.py            # Data models (Spot / PlanRequest / DayPlan / UnplannedSpot / PlanResult)
├── solver.py            # OPTW heuristic: greedy construction (hard budget) + 2-opt + relocation
├── commute.py           # Commute matrix: AMap /v3/distance + 3-tier cache + throttle + fallback
├── aligner.py           # Entity alignment: alias → AMap POI (recall → scoring → hard filter → threshold)
├── eval_aligner.py      # Alignment eval: labeled set + P/R/F1 + review rate
├── evaluation.py        # 3-way evaluation experiment (naive / LLM / solver)
├── tasks.py             # Async tasks: TaskManager + background pipeline + progress push
├── constraint_check.py  # Constraint checker
├── extractor.py         # LLM extraction (OpenAI-compatible + tolerant JSON parsing)
├── main.py              # FastAPI entrypoint
├── tests/               # 19 unit tests (solver / checker / alignment scoring / async tasks)
└── ...
static/index.html        # Roadbook frontend (vanilla JS + Leaflet, no build step)
```

## Roadmap

- [ ] Conversational itinerary editing (function calling: "add a nearby café tomorrow afternoon")
- [ ] Long-image export / share page
- [ ] OR-Tools CP-SAT optimal-solution comparison (quantify the heuristic gap)
- [ ] Task persistence (disk/Redis); grow the labeled set to 200; LLM alias canonicalization

## Compliance & Key Management

- **Keys**: everything lives in `backend/.env` (gitignored). The full commit history has been audited — no secrets. If a key leaks, reset it in the vendor console and update `.env`; no code changes needed
- **Basemap**: the default AMap raster tiles are for local development/demo only. For production, use the official JS API (Web-side key), or set `TILE_PROVIDER=osm` in `.env` (the frontend converts GCJ-02→WGS84 automatically)
- **Data**: demo data is hand-curated from public information; no platform scraping is used — the primary input path is pasted guide text
- **License**: MIT

## Acknowledgements

Architecture ideas drawn from [liketrek/TREK](https://github.com/liketrek/TREK), [1sdv/TripStar](https://github.com/1sdv/TripStar), and [OSU-NLP-Group/TravelPlanner](https://github.com/OSU-NLP-Group/TravelPlanner) (design inspiration only, no code copied).

## Changelog

- **v0.5** Async task system (task_id + WebSocket progress), roadbook frontend (date range + day tabs + map routes), configurable basemap
- **v0.4** Engineering: unit tests + GitHub Actions CI + MIT License
- **v0.3** 3-way evaluation experiment (naive / LLM / solver, 50 scenarios) + geo entity alignment pipeline (F1 80%→90%)
- **v0.2** AMap commute matrix (3-tier cache + throttling); budget as a construction-time hard constraint
- **v0.1** OPTW heuristic solver + constraint checker + LLM extraction module

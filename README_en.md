# AI Trip Planning System

[![CI](https://github.com/zikkkkkking1009/ai-trip-planner/actions/workflows/ci.yml/badge.svg)](https://github.com/zikkkkkking1009/ai-trip-planner/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.12-blue)
![Tests](https://img.shields.io/badge/tests-228%20passed-brightgreen)
![License](https://img.shields.io/badge/License-MIT-green)

[简体中文](README.md) | [English](README_en.md)

> The Chinese README is the authoritative version and is updated first: [README.md](README.md) ·
> Experiment report [docs/experiments.md](docs/experiments.md) · Roadmap [ROADMAP.md](ROADMAP.md) ·
> Session handoff [HANDOFF.md](HANDOFF.md)

Turn a plain-text travel guide into a **constraint-verifiable, conversationally editable, map-visualized** day-by-day itinerary.

```
Guide text ──LLM extraction──▶ POI candidates ──Entity alignment (AMap POI)──▶ Solvable instance
                                                                                      │
User (dates / budget / time window / hotel) ────────────────▶ OPTW solver (greedy + 2-opt + inter-day relocation)
                                                                                      │
                                                              Constraint checker (budget / time window / commute share)
                                                                                      │
                                              Daily itinerary JSON + WebSocket progress + roadbook frontend
```

**Core design decision: the LLM only "understands" — every hard constraint is enforced by deterministic algorithms.**

This is not a gut feeling. A three-way evaluation over 50 programmatically generated scenarios (fixed seed 42) shows that letting the LLM schedule directly produces a slightly *higher* quality score (0.921 vs 0.915) but **exceeds the budget 8 times and leaves time conflicts in 2% of scenarios**; this project's solver is **100% compliant, 0 violations**. Data in [docs/experiments.md](docs/experiments.md).

---

## Quantified results

**Three-way scheduling comparison** (50 generated scenarios, fixed seed 42):

| Metric | Naive packing baseline | **This solver** | Pure LLM scheduling |
|--------|----------------------|-----------------|---------------------|
| Time conflicts per plan | 0.38 | **0** | 0.02 |
| Conflict-free plans | 62% | **100%** | 98% |
| Budget violations | 12 | **0** | 8 |
| Commute share | 23.5% | **13.1%** | 13.1% |
| Itinerary quality score | 0.857 | 0.915 | 0.921 |

**Entity alignment**: Precision 100% / Recall 100% / F1 100% on a 22-case labeled set (was 90%); low-confidence human-review rate 15% (was 20%). Key mechanisms: tailed-subvenue penalty ("X Park - distant view of the Forbidden City" ≠ the Forbidden City), renamed-venue authority prior (Huaqing Pool → Huaqing Palace, re-query by main name), and LLM arbitration locked to the recalled candidate set (prevents hallucination).

**Optimality gap** (vs OR-Tools CP-SAT, same model, same 50 scenarios, same seed): **mean −0.03%, median 0%, max 0.03%, 100% of scenarios within 3%**; mean reward 79.5 (optimal 79.4); **501 ms per solve** (CP-SAT averages 4.18 s). This result came out of a measure → locate → improve → re-measure loop: an earlier experiment found the pure greedy hit a 24% gap on tight instances, and the bottleneck was **spot selection, not ordering** — so construction was changed to **multi-start randomized restarts**, dropping the gap from 7.53% to 0.

**Model routing** (5 repetitions per task): free models are 3–6× slower on the **user-facing path** (extraction 12.12 s vs 1.88 s; intent parsing 2.56 s vs 0.78 s), so interactive tasks keep the primary model; **background review generation** uses a free model (5.74 s vs 1.98 s, but it happens during prefetch, so the user never notices) — "no slowdown in interaction, zero cost in generation".

---

## Features

**Planning pipeline (backend)**

- **OPTW solver**: trip planning modeled as an Orienteering Problem with Time Windows — **multi-start randomized restarts** (round 0 is score-descending as a safety net, later rounds re-run with jittered ordering, best objective `1000·reward − commute` wins) + intra-day 2-opt + inter-day relocation; budget is a hard constraint during construction, and spots that don't fit carry a reason (insufficient budget / doesn't fit the time window)
- **Preference weights**: balanced / walk-less / save-money / see-more. Weights are scan-calibrated — "walk-less" actively drops distant spots to save commute; "see-more" extends each day by 1.5 h (because the real ceiling on spot count is the time window, not the objective function)
- **Robustness simulation** (Monte Carlo): 1,000 samples estimate the probability of finishing on time, and **common random numbers** rank the risk spots — "how much would dropping this one buy me"
- **Robust scheduling**: when you state a target confidence (e.g. 85%) and the simulated probability falls short, the solver **narrows the scheduling window to leave slack** and re-solves, trading a few spots for certainty (measured: 7 spots → 3.3% on-time; 6 spots → 95.3%)
- **Hotel anchor**: the hotel is each day's start *and* end — round-trip commute counts toward the time window and the stats, rather than being inserted as a spot
- **Geo entity alignment**: guide aliases → canonical AMap POIs. Top-5 recall → 4-way score fusion (containment / character similarity / type prior / suffix extension) → hard filtering of noisy types → low confidence goes to human review
- **Commute matrix**: real AMap driving durations + three-tier cache (in-process → disk → API) + QPS throttling; degrades to straight-line estimation without a key
- **Constraint checker**: independently re-verifies the solver output (budget / time window / commute share / empty days) — it does not trust the solver's own claims
- **Async tasks + live progress**: `/plan/async` returns a task id instantly, a background coroutine runs the pipeline, WebSocket streams stage progress, polling as fallback
- **Conversational editing**: the LLM parses intent (remove / add / replace / pin_add / hotel / pref) → deterministic code executes → re-solve + re-verify; supports **multi-turn anaphora** ("the hotel is HanTing" → then "move it near Xi'an station")
- **Multi-city**: a city-center table (`cities.py`) + LLM city detection + a frontend city picker; **25 cities / 254 spots** preloaded. Unknown cities are surfaced explicitly and **never silently fall back to a default city**
- **AI trip review**: a single holistic assessment (score / summary / highlights / actionable warnings); the cost line says "tickets only, excluding transit and meals" — unreliable numbers are not invented
- **Spot media & AI reviews**: AMap photos + opening hours + address; review summaries are LLM-generated and cached, **prefetched in the background** while planning, so opening a card is instant
- **Trip weather**: day-by-day weather for the trip dates (open-meteo, **no signup, no API key**), rainy days highlighted with one actionable hint (bring an umbrella / leave slack). **Deliberately positioned as a footnote to the itinerary**: no hourly data, no historical climate, no separate page. When it cannot be fetched (unknown city / beyond the 16-day forecast window / service down) it says so **rather than filling in fake data**
- **Five-dimension robustness audit**: `tools/check_backend_health.py` statically checks timeouts / retries / task state / logging / cost via AST. **It runs in CI** and needs no API keys

**Roadbook frontend (`static/index.html`, zero build, zero CDN)**

- Two views: **Plan** (form → progress log → day-tabbed itinerary → map) and **My** (history + favorite spots)
- Date-range picker (start before end, highlighted range, click a selected date to cancel)
- Leaflet map with per-day colored routes, legend toggles, hotel-anchor marker; basemap switchable between AMap and OSM (incl. GCJ-02 ↔ WGS84 conversion)
- Spot detail card: photo lightbox, AI intro, pros/cons cards, one-tap AMap navigation
- Hotel picker: search → thumbnail → inline mini-map (orange dot = hotel, colored dots = current spots) to judge whether the location is convenient
- **Desk-pet AI assistant "XiaoZhou"**: a persistent sprite-animated character in the corner (blinking / breathing / cursor-following / multiple states); click to open a chat bubble. It edits the itinerary **and answers trip questions**, with **cross-session long-term memory**
- **AI review card**: score + summary + total cost + highlights / warnings, above the robustness simulation
- **Share long image**: hand-drawn on Canvas and exported as a vertical PNG (includes the AI review), **zero dependencies** — no screenshot library. The map is deliberately omitted (AMap static images taint the canvas cross-origin and `toBlob` throws `SecurityError`)
- Planning progress bar (progress used to be buried in a collapsed log, making the app look frozen), itinerary-list thumbnails, click-to-switch city in the header

---

## Quick start

**Zero API key** (built-in demo data + straight-line distance estimation):

```bash
git clone https://github.com/zikkkkkking1009/ai-trip-planner.git
cd ai-trip-planner/backend
pip install pydantic
python run_demo.py
```

**Run the full service**:

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --port 8000
# open http://localhost:8000 → click 「开始规划」→ switch to 「🗺 地图」 for routes
```

**Configure keys** (optional; unlocks real commute data, LLM extraction and AI reviews):

```bash
cp backend/.env.example backend/.env
```

| Variable | Purpose | Notes |
|----------|---------|-------|
| `AMAP_KEY` | AMap Web-service key | Commute durations, POI search, photos |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL_ID` | Primary model | Heavy tasks like guide extraction (any OpenAI-compatible endpoint) |
| `LLM_FAST_API_KEY` / `LLM_FAST_BASE_URL` / `LLM_FAST_MODEL` | Light-task fast model | Intent parsing, review generation; **may point at a free model; falls back to the primary model if unset** |
| `TILE_PROVIDER` | Basemap | AMap by default; set `osm` for OpenStreetMap |

> A free light-task model verified to work: Zhipu `glm-4-flash-250414` (0.5–0.8 s per turn; note `glm-4.5-flash` and `glm-4.7-flash` measured 22 s / overloaded — do not use them).

---

## API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` · `/app` | Landing page / planner |
| GET | `/health` | Health check (incl. solver and AMap status) |
| GET | `/demo/spots` · `/demo/config` | Demo spots (with photos and intros) / basemap config |
| GET | `/cities` | Demoable city list + city centers (frontend picker data source) |
| POST | `/extract` | Guide text → LLM extraction (incl. city detection) → entity alignment |
| POST | `/plan` · `/plan/async` | Synchronous / asynchronous planning |
| GET | `/task/{id}` · WS `/ws/{id}` | Task status (polling / live progress) |
| POST | `/plan/edit` | Conversational itinerary editing (multi-turn memory) |
| POST | `/plan/simulate` | Robustness simulation: on-time probability + risk-spot suggestions (Monte Carlo) |
| POST | `/plan/review` | AI trip review: score / summary / highlights / warnings |
| GET | `/weather` | Day-by-day weather for the trip dates (open-meteo, keyless); returns `available=false` + a reason when it cannot be fetched |
| POST | `/hotel/search` · `/hotel/set` | Hotel search / set as anchor and re-plan |
| GET | `/poi/detail` · `/poi/reviews` | Place details (fast endpoint) / AI reviews (can be fetched async) |
| GET | `/plans` | Plan history |
| DELETE | `/plans/{id}` · POST `/plans/delete` | Delete / batch delete (soft delete, recoverable) |
| GET | `/favorites` · POST `/favorites` | Favorites list / add-remove |

**25 endpoints in total** (13 GET / 10 POST / 1 DELETE / 1 WebSocket).

---

## Engineering practices

- **Tests and CI**: **228 unit tests** all passing (alignment / solving / checking / editor / task pipeline / robustness / preferences / media keys / city-mismatch guards / weather), GitHub Actions green
- **Five CI gates**: unit tests + **frontend static check** (syntax gate + variable-shadowing guard + hardcoded-coordinate guard) + **backend five-dimension robustness audit** + **jsdom runtime smoke test** (14 DOM assertions) + a keyless solver demo
- **Caching and throttling**: three-tier commute cache (repeat solves issue **zero** API calls); AMap throttled at 0.35 s
- **Fast/slow endpoint split**: the details endpoint returns only millisecond-level AMap data; AI reviews arrive via a second async request, so the first paint never waits
- **Background prefetch**: media is prefetched concurrently (3-way) when planning starts, in parallel with solving, so opening a detail usually hits cache
- **Soft delete**: deleting a plan only sets `deleted`, so mistakes are recoverable; no bulk physical deletion
- **Observability**: stdlib `logging` + a request-id middleware — one request id ties together the whole chain; controllable via `LOG_LEVEL` / `LOG_FILE`
- **Reliability**: unified timeouts and retries (`reliability.py`) — only timeouts / 429 / 5xx are retried, with exponential backoff + jitter, and every retry is logged
- **Memory protection**: task table capped at 200 with a 2-hour TTL for terminal tasks, lazily swept; running tasks are never evicted (`/health` exposes the stats)
- **Pinned dependencies**: three requirement sets (runtime / dev / experiments), all exact versions, reproducible deploys
- **Degradation**: no AMap key → straight-line estimation; no LLM key → the review block hides itself; unavailable model → falls back to the primary model
- **Multi-city fallback discipline**: the city must be passed explicitly by the caller; unknown cities are surfaced to the user and **never silently fall back to a default city**. `check_backend_health.py` blocks hardcoded city names and coordinates

---

## Project layout

```
backend/
  main.py                 FastAPI entrypoint, 25 endpoints (24 HTTP + 1 WebSocket)
  models.py               Pydantic domain models (Spot / PlanRequest / DayPlan / Hotel)
  extractor.py            Guide text → spot candidates (LLM + tolerant JSON parsing + city detection)
  aligner.py              Entity alignment (AMap POI search + score fusion + type filter + LLM arbitration)
  solver.py               OPTW solver (multi-start greedy + 2-opt + inter-day relocation + preference weights)
  solver_cpsat.py         OR-Tools CP-SAT exact solution (comparison experiments / optional better solutions)
  simulation.py           Robustness simulation (Monte Carlo + common-random-number risk ranking)
  robustness.py           Robust scheduling (shrink the window to leave slack + adaptive buffer)
  constraint_check.py     Independent constraint verification
  commute.py              Commute matrix (AMap API + 3-tier cache + throttling)
  editor.py               Conversational editing (intent parsing + deterministic execution + review generation + AI trip review)
  tasks.py                Async tasks, progress push, media prefetch
  cities.py               City-center table (25 cities, coordinates from AMap geocoding)
  demo_data.py            Demo spots (14 hand-written for Xi'an + 240 script-fetched across 24 cities)
  media_cache.py          Media cache key rule (`city|name`) and atomic writes
  reliability.py          Unified timeouts and retries (shared by LLM and AMap)
  weather.py              Trip weather (open-meteo, keyless; admits "unavailable" rather than inventing data)
  logging_setup.py        Logging config and request-id context
  tests/                  228 unit tests
static/index.html         Roadbook frontend (single file, zero build, zero CDN)
tools/                    Check and verification scripts (five-dimension audit / frontend static check / multi-city end-to-end)
data/plans/               Plan snapshots (soft-delete flag)
docs/experiments.md       Experiment report
ROADMAP.md                Project retrospective and roadmap
HANDOFF.md                The first file to read when picking up context in a new session
```

---

## Deployment

```bash
# Option 1: Docker (recommended)
docker build -t ai-trip-planner .
docker run -p 8000:8000 --env-file backend/.env -v $(pwd)/data:/app/data ai-trip-planner

# Option 2: docker compose (healthcheck + data volume included)
docker compose up -d
```

Key points: **secrets never enter the image** (`.dockerignore` excludes `.env`; inject at runtime); mount `data/` to persist plan snapshots and favorites; a single worker is enough (solving is a millisecond-scale CPU task — scale out with more instances if you need throughput).

When deploying to a managed platform (Render / Fly.io / …): set `AMAP_KEY` / `LLM_*` as environment variables and mount the persistent disk at `/app/data`. **Not yet deployed publicly — link to be added.**

---

## Evaluation and experiments

```bash
cd backend
python evaluation.py --n 50 --seed 42 --with-llm   # scheduling: naive vs solver vs pure LLM
python eval_aligner.py                             # alignment: P / R / F1 / review rate
python bench_models.py --repeat 3                  # model selection: extraction / parsing / review generation
python eval_gap.py --n 50 --seed 42 --limit 8      # heuristic vs CP-SAT gap (needs requirements-eval.txt)
```

Methodology, results and failure-case analysis: **[docs/experiments.md](docs/experiments.md)**.

---

## Known limitations

- **The persistence layer is JSON files** — no database, no user accounts, so `/plans` and `/favorites` are global; multi-user isolation is pending (B1/B2 in ROADMAP.md)
- **Not deployed yet**: Dockerfile and docker-compose are ready; what's missing is creating the service on a platform (needs an account + key injection)
- **Demo ticket prices and dwell times are approximations**: coordinates come from real AMap geocoding, but prices and dwell times are demo parameters; `ticket` is 0 when the guide doesn't state a price. **AMap's `place/text` endpoint does not return ticket prices** (measured coverage: 0% across 8 well-known paid attractions), so "fetch prices from AMap" is a dead end — the options are a local price table or simply not pretending to know
- **The alignment labeled set is only 22 cases**: an F1 of 100% has limited statistical meaning; it should grow to 100–200 (A3 in ROADMAP.md)
- **The simulation noise parameters are estimates**: dwell time uses a triangular distribution and commute uses a log-normal; neither is calibrated against real logs ⇒ **absolute probabilities should not be treated as exact** (they swing up to 48.9 pp across parameter sets), but the *relative* ranking of risk spots is insensitive to the parameters, so the product leans on "suggestions" rather than percentages
- **Commute degrades to straight-line estimation offline**: real AMap driving durations require `AMAP_KEY`

---

## Compliance and key management

- **No keys are committed**: `.env`, caches and runtime data are all in `.gitignore`; the repo contains only `.env.example`
- **Map compliance**: the default basemap is AMap tiles (GCJ-02); switching to OSM makes the frontend convert GCJ-02 → WGS84
- **Data**: spot info, photos and addresses in the demo data come from AMap's open public APIs and are used for technical demonstration only
- **Leak self-check**: `git ls-files | xargs grep -l "sk-"` (should return nothing)

---

## Acknowledgements

Architecture ideas drawn from [liketrek/TREK](https://github.com/liketrek/TREK), [1sdv/TripStar](https://github.com/1sdv/TripStar), and [OSU-NLP-Group/TravelPlanner](https://github.com/OSU-NLP-Group/TravelPlanner) (design inspiration only, no code copied).

---

## Changelog

- **v1.5（2026-09-23）**: **One design system for both pages + trip weather + a sweep of silent errors** — the landing page and the planner are split into `/` and `/app`, and both now share **one set of oklch design tokens** (with an extra `--muted-3` step for dense UIs) plus one motion rule (**only react to explicit user actions; never animate data re-renders**); added **trip weather** (open-meteo, keyless; per-day, rain highlighted, honestly unavailable beyond the 16-day window); fixed a batch of **silent errors**: the coordinate check in the five-dimension audit had been neutered by an exemption list (5 hardcoded Xi'an coordinates while CI stayed green), the final commute leg was double-counted when no hotel anchor existed (systematically understating the on-time probability), the media-prefetch lock was a function-local variable (concurrent calls lost updates), `UnplannedSpot` received fields the pydantic model silently dropped, pin-insert commute excluded the hotel legs (making one day's figures incomparable with the rest), and the media-cache entry for "Sichuan Museum" actually held Xi'an data (AMap's loose `citylimit` bled data across cities; a write-side city check was added); the frontend checker gained **CSS self-reference / undefined-variable / attribute-escaping** guards; tests 195 → **228**
- **v1.4（2026-09-21）**: **AI assistant and sharing** — desk-pet assistant "XiaoZhou" (sprite frame animation, **cross-session long-term memory**, answers trip questions as well as editing) + **AI review card** + **share long image** (hand-drawn on Canvas, exported as a vertical PNG, zero dependencies); **robust scheduling** (confidence target → adaptive buffer → shrink scheduling window and re-solve; measured 7 spots → 3.3% vs 6 spots → 95.3%), **alignment round 2: appended sub-venue suffixes** (previously a fully-confident error at `confidence=1.00`); plus one-tap "drop this spot to gain X pp", city switching, planning progress bar, itinerary thumbnails
- **v1.3（2026-09-20）**: **Preferences and robustness** — **tunable preference weights** (walk-less / save-money / see-more; scan-calibrated, with a `_drop_improve` operator so preferences actually bite) and **robustness simulation** (1,000-run Monte Carlo + common-random-number risk ranking); **five-dimension robustness audit** landed and wired into **CI**; media cache keys gained a city dimension (fixes same-name spots bleeding across cities, e.g. "People's Park"); cities expanded to **25 cities / 254 spots**; **geo-clustered day assignment re-measured as a negative result** across 50 scenarios (0 improved / 0 worsened; left off by default with a re-test switch); frontend moved from a phone-shell mockup to a desktop site
- **v1.2（2026-09-19）**: **Multi-city generalization** — city-center table (`cities.py`) + LLM city detection + frontend city picker and "paste a guide to auto-detect" entry; demo data expanded to **5 cities / 54 spots** (Chengdu / Beijing / Hangzhou / Chongqing coordinates fetched from real AMap data by `tools/build_demo_data.py`); fixed the silent error where extraction's fallback coordinates were hardcoded to Xi'an (pasting a Chengdu guide produced Xi'an coordinates with no error); tests grew to **84** (including coordinate-mismatch guards)
- **v1.1（2026-09-18）**: alignment F1 90% → 100% via tailed-subvenue penalty, renamed-venue authority prior (rename detection + main-name re-query), and LLM arbitration locked to the candidate set; tests grew to 45
- **v1.0（2026-09-18）**: CP-SAT exact-solution comparison (gap quantification + lower-bound constraint guaranteeing no worse than the heuristic), logging and request ids, unified retries and timeouts, task eviction and memory protection, pinned dependencies, frontend XSS escaping, Docker deployment files; tests grew to 34
- **v0.9（2026-09-18）**: two-view structure, hotel anchor, spot detail cards with AI reviews, photo lightbox, map navigation, soft delete and batch cleanup for history, multi-turn conversational memory, fast/slow endpoint split with background prefetch, free-model channel for light tasks, fixed random seeds for eval scripts
- v0.5: conversational editing, async tasks with WebSocket progress, per-day colored map routes, alignment evaluation
- v0.3: OPTW solver + constraint checker + commute matrix caching
- v0.1: LLM extraction + entity alignment prototype

# AI Trip Planning System

[![CI](https://github.com/zikkkkkking1009/ai-trip-planner/actions/workflows/ci.yml/badge.svg)](https://github.com/zikkkkkking1009/ai-trip-planner/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.12-blue)
![Tests](https://img.shields.io/badge/tests-349%20passed-brightgreen)
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

**Optimality gap** (vs OR-Tools CP-SAT, same model, same 50 scenarios, same seed; **scenario size 8–14 spots**): **mean −0.03%, median 0%, max 0.03%, 100% of scenarios within 3%**; mean reward 79.5 (optimal 79.4); **501 ms per solve** (CP-SAT averages 4.08 s). This result came out of a measure → locate → improve → re-measure loop: an earlier experiment found the pure greedy hit a 24% gap on tight instances, and the bottleneck was **spot selection, not ordering** — so construction was changed to **multi-start randomized restarts**, dropping the gap from 7.53% to 0.

**Scale boundary** (`backend/eval_scale.json`, a separate 8/14/25/50-spot comparison): at N≤8 CP-SAT **proves optimality in 0.13 s**, so small instances can use the exact solver directly; from N≥25 CP-SAT hits its time limit and only returns FEASIBLE, staying within 5% of the heuristic (at N=50 CP-SAT is 4% *better*: 122.8 vs 117.8). **The "100% within 3%" claim therefore applies to 8–14 spots and must not be extrapolated to arbitrary sizes.**

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
- **Hotel recommendation & search**: `POST /hotel/recommend` suggests nearby hotels the moment the picker opens (no keyword needed), while `/hotel/search` narrows by keyword — both paginate via `page`; POI queries gained `types` (lodging-category filter), `extensions` (to fetch ratings, etc.) and offset paging; results are normalized and **graded** (economy / comfort / upscale / luxury / homestay). `PlanRequest.hotel` is now **passed through and echoed back**, so "pick a hotel before planning" is no longer blocked by a frontend gate
- **Multi-city**: a city-center table (`cities.py`) + LLM city detection + a frontend city picker; **25 cities / 254 spots** preloaded. Unknown cities are surfaced explicitly and **never silently fall back to a default city**
- **AI trip review**: a single holistic assessment (score / summary / highlights / actionable warnings); the cost line says "tickets only, excluding transit and meals" — unreliable numbers are not invented
- **Spot media & AI reviews**: AMap photos + opening hours + address; review summaries are LLM-generated and cached, **prefetched in the background** while planning, so opening a card is instant
- **Trip weather**: day-by-day weather for the trip dates (open-meteo, **no signup, no API key**), rainy days highlighted with one actionable hint (bring an umbrella / leave slack). **Deliberately positioned as a footnote to the itinerary**: no hourly data, no historical climate, no separate page. When it cannot be fetched (unknown city / beyond the 16-day forecast window / service down) it says so **rather than filling in fake data**
- **Five-dimension robustness audit**: `tools/check_backend_health.py` statically checks timeouts / retries / task state / logging / cost via AST. **It runs in CI** and needs no API keys

**Roadbook frontend (`static/index.html`, zero build, zero CDN)**

- Two views: **Plan** (form → progress log → day-tabbed itinerary → map) and **My** (history + favorite spots)
- Date-range picker (start before end, highlighted range, click a selected date to cancel)
- Leaflet map with per-day colored routes (**7 distinct hues**: blue / orange / teal / purple / rose / yellow-green / indigo, shared by the map, legend, timeline and overview), legend toggles, hotel-anchor marker; basemap switchable between AMap and OSM (incl. GCJ-02 ↔ WGS84 conversion)
- Spot detail card: photo lightbox, AI intro, pros/cons cards, one-tap AMap navigation
- **Hotel picker**: opens straight into **nearby hotel recommendations** (Ctrip-style, no search needed), narrowable by keyword, with **paginated "load more"** (25 per page); each card shows rating, **grade** (economy / comfort / upscale / luxury / homestay), district, **real distance to the itinerary center**, address, phone and a multi-photo gallery, with "recommended / nearest first / top-rated" sorting plus grade and rating filters and one-tap switching (current hotel highlighted); clicking a name expands an inline mini-map (orange dot = hotel, colored dots = current spots) to judge whether the location is convenient. **There is no real room price** — AMap's free API does not return prices, so the field stays empty instead of being invented
- **Desk-pet AI assistant "XiaoZhou"**: a persistent sprite-animated character in the corner (blinking / breathing / cursor-following / multiple states); click to open a chat bubble. It edits the itinerary **and answers trip questions**, with **cross-session long-term memory**
- **AI review card**: score + summary + total cost + highlights / warnings, above the robustness simulation
- **Share long image**: hand-drawn on Canvas and exported as a vertical PNG (includes the AI review), now with a **preview overlay before "Download / Cancel"** (previously it downloaded silently); **zero dependencies** — no screenshot library. The map is deliberately omitted (AMap static images taint the canvas cross-origin and `toBlob` throws `SecurityError`)
- Planning progress bar (progress used to be buried in a collapsed log, making the app look frozen), itinerary-list thumbnails, click-to-switch city in the header
- **modern-minimal visual unification**: the left form, right results column, "My" view and calendar popover all follow the OpenDesign `modern-minimal` direction (hairline borders, no card shadows, tight letter-spacing, tabular numerals, collapsed font weights)
- **Icon standard**: all icons are now **linear SVG** (16 viewbox, `currentColor`, 1.8px stroke; the hotel icon uses Lucide's `hotel`), replacing emoji — only the desk-pet 🐋 and emoji inside code comments remain
- **User-facing wording de-jargoned**: the hero subtitle, explainer chips and detection hints are plain language; the planning progress bar uses friendly stage names (the collapsed log keeps the raw stage names)
- **Retrieval QA (RAG)**: a corpus of 375 spots across 38 cities plus 223 dishes, retrieved with `BM25 + character bigrams` (**no API key, no new dependency**). Every hit carries a `source` citation and is **back-checked** against a real entity in the corpus (spots resolve to coordinates). `with_answer=1` adds a grounded generation layer: **every corpus entity mentioned in the answer must appear in the cited snippets, otherwise it is flagged as "ungrounded"** — under-report rather than false-alarm; if the LLM fails it degrades to pure retrieval. On a 30-item golden set, BM25 scores **hit@1 90.0% / hit@5 100% / MRR 0.940** (before corpus enrichment: 66.7% / 70.0% / 0.689 — **the bottleneck was the corpus, not the algorithm**). **Vector reranking shows no significant gain** (MRR 0.950; a 0.010 delta on 30 items is noise) while adding an embedding channel and quota dependency, so **BM25 stays**. The homepage has a live demo box you can try.

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
# open http://localhost:8000 → click 「开始规划」→ switch to 「地图」 for routes
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
| GET | `/meta` | Service metadata: an endpoint list built by walking `app.routes` at runtime + service status |
| GET | `/demo/spots` · `/demo/config` | Demo spots (with photos and intros) / basemap config |
| GET | `/cities` | Demoable city list + city centers (frontend picker data source) |
| POST | `/extract` | Guide text → LLM extraction (incl. city detection) → entity alignment |
| POST | `/plan` · `/plan/async` | Synchronous / asynchronous planning |
| GET | `/task/{id}` · WS `/ws/{id}` | Task status (polling / live progress) |
| POST | `/plan/edit` | Conversational itinerary editing (multi-turn memory) |
| POST | `/plan/simulate` | Robustness simulation: on-time probability + risk-spot suggestions (Monte Carlo) |
| POST | `/plan/review` | AI trip review: score / summary / highlights / warnings |
| GET | `/weather` | Day-by-day weather for the trip dates (open-meteo, keyless); returns `available=false` + a reason when it cannot be fetched |
| POST | `/hotel/recommend` · `/hotel/search` | Nearby hotel recommendations (no keyword needed) / keyword search (both paginate via `page`) |
| POST | `/hotel/set` | Set as anchor and re-plan |
| GET | `/poi/detail` · `/poi/reviews` | Place details (fast endpoint) / AI reviews (can be fetched async) |
| GET | `/plans` | Plan history |
| DELETE | `/plans/{id}` · POST `/plans/delete` | Delete / batch delete (soft delete, recoverable) |
| GET | `/favorites` · POST `/favorites` | Favorites list / add-remove |

**26 endpoints in total** (13 GET / 11 POST / 1 DELETE / 1 WebSocket).

---

## Engineering practices

**Test and gate commands** (all run without an API key):

```bash
cd backend && python -m pytest -q          # unit tests (349 passed; AMap-dependent hotel cases skip without AMAP_KEY)
cd .. && node tools/frontend_smoke.js      # frontend jsdom runtime smoke (18 assertions)
node tools/hotel_smoke.js                  # hotel picker + long-image preview + map view jsdom smoke (30 assertions)
python tools/check_frontend.py             # frontend static check (10 blocking gates)
cd backend && ruff check .                 # lint (pyflakes + pycodestyle error-class rules; 0 findings today)
cd backend && mypy .                       # type check (gradual; 0 findings and 0 `# type: ignore` today)
python tools/check_backend_health.py       # backend five-dimension robustness audit (AST, keyless)
```

> Both local and CI need **Node 22**: a transitive dependency of jsdom 30.x (undici) uses an API
> that only exists in Node 22, so under Node 20 `require('jsdom')` crashes outright (that is what
> broke CI 10 times in a row on 2026-09-28). CI pins jsdom to `30.1.1`; to reproduce the CI
> environment run `npx -y node@20 tools/frontend_smoke.js`.

- **Tests and CI**: **349 unit tests** all passing (alignment / solving / checking / editor / task pipeline / robustness / preferences / media keys / city-mismatch guards / weather / hotel endpoints), GitHub Actions green
- **Nine CI gates**: unit tests + **frontend static check** (10 blocking gates: syntax / variable shadowing / hardcoded coordinates / attribute escaping / CSS self-reference / undefined CSS variables / document integrity) + **two-level contrast gates** (page-level token parsing + spec-level light/dark) + **backend five-dimension robustness audit** + **ruff lint** + **mypy type check** + **jsdom runtime smoke** (`frontend_smoke.js` 22 assertions + `hotel_smoke.js` 48 assertions) + a keyless solver demo
- **Static-check strategy**: ruff enables only **bug-catching rules** (`E4/E7/E9/F`) rather than every style rule — turning everything on at once produces a wall of `# noqa` and the gate stops being read. `main.py`'s `E402` is an **intentional exemption** (`.env` must load before logging is initialised); the reason is recorded in `ruff.toml`. mypy runs **gradually** (bodies of unannotated functions are not checked by default), so genuine type errors in annotated code surface first — **zero `# type: ignore`**
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
  main.py                 FastAPI entrypoint, 26 endpoints (25 HTTP + 1 WebSocket)
  models.py               Pydantic domain models (Spot / PlanRequest / DayPlan / Hotel)
  extractor.py            Guide text → spot candidates (LLM + tolerant JSON parsing + city detection)
  aligner.py              Entity alignment (AMap POI search + score fusion + type filter + LLM arbitration)
  solver.py               OPTW solver (multi-start greedy + 2-opt + inter-day relocation + preference weights)
  solver_cpsat.py         OR-Tools CP-SAT exact solution (comparison experiments / optional better solutions)
  simulation.py           Robustness simulation (Monte Carlo + common-random-number risk ranking)
  robustness.py           Robust scheduling (shrink the window to leave slack + adaptive buffer)
  constraint_check.py     Independent constraint verification
  commute.py              Commute matrix (AMap API + 3-tier cache + throttling)
  editor.py               Conversational editing (intent parsing + deterministic execution + review generation + AI trip review); POI normalization and hotel-card / grade normalization
  tasks.py                Async tasks, progress push, media prefetch
  cities.py               City-center table (25 cities, coordinates from AMap geocoding)
  demo_data.py            Demo spots (14 hand-written for Xi'an + 240 script-fetched across 24 cities)
  media_cache.py          Media cache key rule (`city|name`) and atomic writes
  reliability.py          Unified timeouts and retries (shared by LLM and AMap)
  weather.py              Trip weather (open-meteo, keyless; admits "unavailable" rather than inventing data)
  logging_setup.py        Logging config and request-id context
  tests/                  349 unit tests
static/index.html         Roadbook frontend (single file, zero build, zero CDN)
tools/                    Check and verification scripts (five-dimension audit / frontend static check / frontend & hotel jsdom smoke / multi-city end-to-end)
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
- **Ticket price distinguishes "known free" from "unknown" and is never shown as ¥0**: AMap's free API **does not return ticket prices** (`biz_ext.cost` is an empty array for attractions, just like `lowest_price` for hotels), and when the guide doesn't mention a price the LLM can only emit 0 — displaying that as ¥0 would claim the place is free. So `Spot` / `VisitedSpot` each carry a `ticket_known` flag; when it is false the UI shows "**票价待查**" (price unknown). Known prices are first back-filled from the local price table (`demo_data`) using **exact-name matching only** — no fuzzy matching, because assigning another spot's price is worse than admitting ignorance. **Known limitation**: AMap's canonical names don't always match the local table (e.g. "秦始皇帝陵博物院" vs "秦始皇兵马俑博物馆"), so back-fill coverage is not 100%
- **Hotels have no real room price**: AMap's free API does not return prices (`biz_ext.lowest_price` / `cost` are empty) and no OTA source is wired up — the rating, grade, district, distance and phone on each card are real, but **the price field stays empty when unknown**; review counts, room types, availability and cancellation policies are likewise unavailable (no estimation, no invention)
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

- **v1.17（2026-09-28）**: **Response gzip enabled — public bandwidth capacity ×3** — the two pages measure **310 KB** uncompressed (`/` 87 KB + `/app` 223 KB; single-file SPA with inline JS/CSS), while the tunnel only gives 139–185 KB/s ⇒ loading the planner took **1.28 s**. With `GZipMiddleware`: `/app` 223→**74 KB**, `/` 87→**28 KB** (3.08×/3.15×), measured **0.69 s** through the tunnel, and the monthly 1 GB now covers about 3000 → **~9700** full sessions. Two traps are documented in the code: (1) it must sit on the **inner** side (`add_middleware` must precede `@app.middleware("http")`) — Starlette makes the last-added middleware the outermost, and out there `BaseHTTPMiddleware` strips `content-length`, so `minimum_size` silently stops working (the 127-byte `/health` got compressed too); (2) it only applies when the client sends `Accept-Encoding: gzip`. Four endpoint-level assertions added. pytest 349→**350**
- **v1.16（2026-09-28）**: **Correction + final call: the hero deliberately ignores `prefers-reduced-motion`** — the previous version got this backwards. **This machine has Windows "Animation effects" turned off** (`SPI_GETCLIENTAREAANIMATION = 0`, read directly via ctypes), so the browser **always reports reduce** and the fallback froze the hero into a static frame — the site owner's screen recording is the proof (frames at 0.15s and 1.36s are **0.00%** different pixel-wise, versus 6%/0.8s on a dev machine). The previous version concluded "the premise does not hold" because **Playwright emulates `reducedMotion` as no-preference by default**, so "testing reduce with Playwright" can never observe it. **Final call: the hero illustration intentionally opts out of reduce** — done by putting `!important` on each of its animation declarations (`.hero-card svg .x` = 0,2,1 beats the global `*` = 0,0,0), while every other motion (scroll reveal / theme switch / desktop pet / view transitions) **still** respects the preference. The gate now asserts **both environments** (≥4% under normal *and* reduce) and was **reverse-validated** (against the previous version the reduce row fails, exit 1, measuring 0.00%)
- **v1.15（2026-09-28）**: **Fixed the homepage hero reading as "frozen" + added a motion-visibility gate** — the animation was never broken (it ran, with correct 5.5s/7s durations); the problem was **amplitude**: only **2.61%** of the hero's pixels changed per 0.8s, so it read as a static illustration. Fix: the main route now **draws itself** (a dashed ghost underneath marks "not yet drawn"), the full-route dash march is kept, the comet segment goes 12→38 and its width 3.5→5px, anchors light up in tip order, and the whole illustration drifts slowly (amplitude kept inside the card padding). Measured **2.61% → 6.00% (peak 6.77%)**. This entry also once judged the "reduce premise" to be false (the old accessibility fallback was re-instated as a result) — **that judgement was wrong**: it rested on a Playwright measurement, and Playwright emulates `reducedMotion` as no-preference by default. Reading `SPI_GETCLIENTAREAANIMATION` directly yields **0**, so the premise holds; see v1.16 for the final call. New `tools/hero_motion_check.js` (local gate: real-render per-window pixel diff, with a "frozen must be ≈0" self-validating control; **reverse-validated** — it fails with exit 1 on the previous version). Count corrections: pytest 348→**349**, hotel_smoke 49→**48**
- **v1.14（2026-09-28）**: **Public-deployment gates: access token + per-IP rate limiting** (see `docs/deploy-runbook.md`) — the site was deliberately "demo = no auth", but the moment it is exposed to the internet, favorites and history are **globally shared** and anyone with the link can trigger LLM and AMap calls (i.e. let strangers burn your keys). A `verify_token` dependency now guards the **18** side-effecting/costly endpoints (`/health`, pages, `/static`, `/demo/*`, `/cities`, `/meta` stay open for monitoring and first paint). Because the browser WebSocket API cannot send custom headers, `/ws/{task_id}` takes `?token=` and on failure **accepts first, then closes with 4401** (closing before accept makes Starlette reject the handshake with HTTP 403, so the client never sees the business code). When `APP_TOKEN` is unset everything is allowed (local dev / CI unaffected) but startup logs a loud warning and `/meta` reports `token_required=false`, so an operator cannot miss an unlocked deployment. Per-IP rate limiting is 30 requests/minute counting only costly routes, tunable via `RATE_LIMIT_PER_MIN`. **Two tunnel-specific pitfalls**: ① the NAT-traversal client runs on the same host, so `request.client.host` is always `127.0.0.1` — rate limiting on it would treat every friend as one user; ② `X-Forwarded-For` must be read as the **last** entry, because standard proxies **append** (`client-supplied` + `real peer`) and the first entry is exactly the attacker-controlled one, so using it lets a single spoofed header buy a fresh rate-limit bucket. Frontend: `window.fetch` is wrapped to inject the header **only for same-origin** requests (never cross-origin, preventing token leakage), WebSocket URLs get `?token=`, a token panel sits in the nav, and 401/429/4401 each produce one clear message (reported **once per class**, not per request). Tests pytest 331 → **349**, frontend_smoke 18 → **22**, hotel_smoke 47 → **48**, including a gate-style case that **walks the real route table** and asserts everything that should be guarded is guarded (adding a write endpoint without the dependency turns it red)
- **v1.13（2026-09-28）**: **External full-project review fixes (ZCode)** — two rounds across 13 files (+744/−136), report at `docs/reviews/2026-09-28-zcode-review.md`. **6 High**: **path traversal** on `/plans` and 3 more endpoints (unauthenticated `task_id` concatenated into a filesystem path — `../x` could read/write .json files outside the plans dir; now centralised in `_plan_snapshot_file()` validating a 12-hex-char id); a **stored XSS** in the detail-card title (`${name}` missed `esc` — Chinese payloads couldn't catch it, ASCII ones like `<img src=x onerror=…>` could); `run_set_hotel` **blocking the event loop** (called the rate-limited, HTTP-bound commute lookup directly in async — 19 spots froze the service for tens of seconds; now `asyncio.to_thread`); **shared `req_params` mutation** (two tasks aliasing one dict → `deepcopy`); `pin_add` skipping persistence and chat history; the **image lightbox entirely broken** (`lbUrls` was never assigned). **Medium**: media-cache **merge-write + thread lock** (endpoints used to overwrite the whole file without the lock, wiping reviews the prefetcher had just written; `put_media` renamed `update_media`), favorites read-write lock + atomic write + corruption tolerance (previously a corrupted file 500'd `GET /favorites`), `MANAGER.spawn()` wrapping background coroutines (fixes zombie `running` tasks that were never collected), `/extract` moved to a **JSON body** with a 12,000-char cap (the old query-string form encoded Chinese ×9 and hit gateway line limits before validation), WS **incremental consumption** (each frame used to resend the full `progress[]` — O(n²)), WS disconnect handling, a pickHotel polling cap, and 5 CSS typos (`var(--surface)-space`). **Gates**: pytest 315 → **331** (+17 regressions), frontend_smoke 14 → **18**, frontend static check gained `check_css_invalid_props` (now 10 blocking + 2 warnings); key assertions were **reverse-verified** against the pre-fix page. ⚠️ **Breaking changes**: `/extract` now takes `{"text","city"}` as a JSON body; `media_cache.put_media()` is renamed `update_media()` with merge semantics. 10 remaining Low items are listed in the report
- **v1.12（2026-09-28）**: **Fixed the broken "less walking" preference and the preference-switch interaction** — ① **Root cause**: "less walking" works only through `_drop_improve` (dropping far spots to save commute), which triggers when `commute_weight × minutes_saved > 1000 × spot_score`. The weight (120) had been calibrated **only against the haversine fallback**, while production uses **real AMap driving times** (smaller magnitudes) ⇒ it never triggered and **"less walking" produced exactly the same itinerary as "balanced"** (183.3 vs 183.3 min in Xi'an). Recalibrated to **200** after re-measuring with the real commute matrix across Xi'an / Chengdu / Hangzhou (183→135, 114→75, 114→57 min), and verified the haversine path is unchanged (no regression). **Test gap**: the old `test_less_walk_reduces_commute` only exercised the haversine path, so it stayed green; a **real-commute regression test** was added (skipped without `AMAP_KEY`, requires a >5 min difference) and **reverse-verified** — with the weight back at 120 it fails with exactly the reported 183.3 vs 183.3. ② **Interaction**: switching preference no longer pops the desk pet with a long "shall I re-plan?" message that covered the results (and still left the user to click "开始规划"); it now shows an inline **"按新偏好重排"** button that re-plans in one click. The button is hidden until a plan exists. Tests 314 → **315**, smoke 42 → **47**
- **v1.11（2026-09-28）**: **Ticket price no longer renders "unknown" as "free"** — the root cause is that ticket price has two different zeros: **known free** and **we don't know**. AMap's free API **does not return ticket prices** (`biz_ext.cost` is an empty array for attractions), and when the guide omits a price the LLM can only emit 0, so the UI's "总门票 ¥0" was claiming the trip was free. Fix: `Spot` / `VisitedSpot` carry a `ticket_known` flag (aggregated into `PlanResult.cost_known`), and the frontend gained a single `moneyTxt()` helper that shows "票价待查" (price unknown) when it is false and hides the derived "per-day" estimate; 7 hard-coded `¥N` sites were replaced, including the shared long image. On the backend, prices are back-filled after entity alignment from the local table via `demo_data.ticket_of()` using **exact-name matching only** — no fuzzy matching, because AMap's canonical names don't always match the local table (e.g. "秦始皇帝陵博物院" vs "秦始皇兵马俑博物馆"), and guessing is worse than admitting ignorance. Added `backend/tests/test_ticket.py` (7 cases locking down the 0-vs-None semantics and back-fill rules) and 3 runtime assertions in `hotel_smoke.js`, all **reverse-verified** to actually fail on regression — that verification also exposed that the static assertion's regex was too strict (`¥${x.toFixed(0)}` slipped through), now fixed. Tests 307 → **314**
- **v1.10（2026-09-28）**: **Endpoint-level integration tests + three more CI gates + a manual theme toggle** — **Tests**: added `backend/tests/test_api_endpoints.py` (21 cases) covering the **14 endpoints that previously had zero test references** (`/demo/spots` `/demo/config` `/poi/detail` `/poi/reviews` `/plans` `DELETE /plans/{id}` `/plans/delete` `GET|POST /favorites` `/task/{id}` `/plan/simulate` `/plan/review` `/plan/edit` `/ws/{id}`); they run fully offline and **redirect persistence paths to tmp** so a test run never touches real history or favorites. The most valuable case locks in: **"data fetched for the wrong city must 404 and must never be cached"** (a past incident stored Xi'an data under a Sichuan museum — once the cache is poisoned it is permanent), asserted with a spy proving `save_media` is never called. Unit tests 286 → **307**. **CI**: added three gates — two levels of **contrast gates** (page-level `check_contrast.py` and spec-level `contrast-audit.py --check` for light + dark) and **`hotel_smoke.js` (39 runtime assertions)**; also fixed two jsdom capability gaps that made `hotel_smoke.js` **always exit 1** (stubbed `URL.createObjectURL`; whitelisted the "navigation not implemented" error jsdom raises when clicking `<a href>`), without which CI would go red on every push. `contrast_runtime.js` stays local-only (needs playwright + a real browser). **Features**: in single-day view the **weather now shows only that day** (same scope as the map, follows day switching; overview still lists the whole trip); **dark theme gained a manual toggle** (auto → light → dark via `data-theme` + localStorage, with a single copy of dark tokens; the head script still reads `matchMedia`, so the runtime gate that simulates system preference keeps working)
- **v1.9（2026-09-24）**: **Hotel picker rebuilt + modern-minimal visual unification + a round of user-facing frontend fixes** — the hotel picker moved from "search first, then look" to a **Ctrip-style open-then-recommend** flow (nearby hotels appear immediately, narrowable by keyword, with **paginated "load more"**, 25 per page); cards gained **rating / grade (economy·comfort·upscale·luxury·homestay, normalized from AMap keytag) / district / real distance to the itinerary center / address / phone / multi-photo gallery**, plus "recommended / nearest-first / top-rated" sorting, grade and rating filters and one-tap switching (current hotel highlighted); **honestly labelled as having no real room price** (AMap's free API leaves `biz_ext.lowest_price` empty and no OTA source is wired up), so the price field stays blank instead of being invented; **the left form, right results column, "My" view and calendar popover were unified onto OpenDesign `modern-minimal`** (hairline borders, no card shadows, tight letter-spacing, tabular numerals, collapsed font weights; an earlier "conversation" left column was **reverted** after the user rejected it); **all emoji icons were replaced with linear SVG** (16 viewbox / `currentColor` / 1.8px stroke; the hotel icon uses Lucide `hotel`; the desk-pet 🐋 and comment emoji stay); **user-facing jargon removed** (hero subtitle, chips and detection hints rewritten in plain language; progress-bar stage names made friendly while the collapsed log keeps the raw stages); **per-day map colors changed from "one hue, varying lightness" to 7 distinct hues** (blue / orange / teal / purple / rose / yellow-green / indigo, shared by map / legend / timeline / overview); **the long image now shows a preview overlay before "Download / Cancel"** (it used to download silently), with a cleaner modern canvas header; **fixes**: ① blank right column on first open (init missed `renderItin`) ② "pick a hotel before planning" was blocked by a frontend gate (and the backend `/plan` now **passes through and echoes** `hotel`) ③ hotel search passed a click event as `page` and got a 500 ④ hotel `intro` picked the first of AMap's multi-segment categories, describing a hotel as "catering"; **backend** added `POST /hotel/recommend`, both it and `/hotel/search` support `page`, POI queries gained `types` / `extensions` / `offset` with unified `_poi_row()` normalization and `_grade()` grading; **tests** added `tools/hotel_smoke.js` (jsdom runtime smoke, 30 assertions covering the hotel overlay + long-image preview + map view + first-paint empty state) and `backend/tests/test_hotels.py` (4 cases, skipped without `AMAP_KEY`); unit tests 260 → **286**, endpoints 25 → **26**
- **v1.8（2026-09-23）**: **Key-availability self-check** — the landing page's "service status" used to turn green as long as an env var was non-empty, so a stale key lit up; it now **probes for real in the background at startup** (one AMap distance call, one probe per LLM channel with `max_tokens=1`), caches the result for 6 hours, and reports one of **missing / ok / invalid / unreachable** — **a network failure and an invalid key are reported separately** (they need opposite fixes); a misconfiguration where the fast channel points elsewhere without its own key is **statically detected without sending a request**; added a CLI entrypoint `python backend/selftest.py` (verify keys before starting the service); the planner had a hardcoded "real AMap commute" chip that lied under degradation (now generated from actual usage); a missing-LLM known limitation was added; tests 238 → **260**
- **v1.7（2026-09-23）**: **Landing-page narrative rebuilt + a real type scale on both pages** — what used to be five equally-sized charts in one grid (you could not tell which one mattered) is now **conclusion first, evidence second**: the core claim (solver and LLM land within a hair of each other on trip-quality score, 0.915 vs 0.921, yet one has **0** violations and the other has 8) is stated as a 28px claim line above the fold, the main chart takes the full width and the page's only elevated shadow, and the other four fold into a native `<details>` (no JS, keyboard-expandable, still readable with JS off); **semantic chart colors** — the solver and the LLM baseline used to be two tints of the same blue (the single most important comparison in the piece, and the hardest to tell apart); they are now **solid saturated blue / solid mid grey / hollow pale outline**, so hue and fill-style give two independent channels and it still reads in greyscale; **type scale collapsed to 12/14/16/20/28/44/62 with weights 400/500/600/700/800** (previously every weight on the page was ≥650 — not one light word existed), and the planner's 17 font sizes became 5; **vertical rhythm split into 128/96/64** (all eight sections used to share one 96px step); the `#results` section's share of page copy dropped **51.6% → 18.6%**; the frontend checker gained **document-integrity** and **leaked-Markdown** guards
- **v1.6（2026-09-23）**: **Footer dead links fixed + real experiment charts** — the footer's "API docs" pointed at `/docs` (FastAPI's bundled Swagger UI, whose CSS/JS come from a CDN, breaking this site's zero-CDN rule) and "service status" pointed at a 137-byte JSON blob; both were "click through and there is nothing there". They are now **in-page sections** fed by a new `GET /meta`, which walks `app.routes` **at runtime** to build the endpoint list (not a hand-maintained second copy, so it cannot drift) — which immediately surfaced four endpoints missing their docstrings, now added; added **five experiment charts** (inline SVG only, zero JS, every data point labelled with its exact value, chart 1 being the three-way scheduling comparison); added a **cross-page transition** between the landing page and the planner (with an explicit way back); endpoints 24 → **25**, tests 228 → **238**
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

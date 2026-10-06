# Sentiment Analysis of Social Media Data

**Domain:** Database / Big Data
**Initial Platform:** YouTube
**Clients:** Chrome/Chromium Extension (Manifest V3) + FastAPI backend
**Current Sprint:** **Sprint 5 — Advanced Sentiment Intelligence Engine** ✅

## Description

An AI-powered **sentiment intelligence layer for YouTube**. A Chrome extension
floats a ✦ AI entry point over the normal YouTube page; the moment a video is
detected the dark "intelligence console" overlay opens itself and analysis
starts automatically — **zero clicks, no Analyze button**. The extension sends
the detected video to a local FastAPI backend (started automatically on
Windows through a Chrome Native Messaging host), which securely fetches
**real video metadata and
comments** from the official YouTube Data API, validates, normalizes and
deduplicates them into a SQLite dataset store, and — Sprint 4 — runs the
**sentiment analysis itself**: stored comments are classified
POSITIVE/NEUTRAL/NEGATIVE through a documented CPU-only model (VADER),
persisted through the existing processing state machine, aggregated in SQL
(`dominantSentiment`, exact integer percentages), and served to the overlay
through `GET /api/videos/{id}/sentiment`.

> **Sprint 4.3 status:** analysis no longer happens inside one long HTTP
> request. `POST /api/videos/{id}/analysis` returns **202 Accepted** in
> milliseconds and runs acquisition + sentiment as a **background job**
> (one worker thread, state persisted in SQLite); the extension polls
> `GET /api/videos/{id}/analysis/status` and renders REAL progress
> (collected / analyzed / skipped counts) until `COMPLETED`, then reads the
> finished results with two fast GETs. A measured 5 000-comment video took
> ~74 s of pipeline time behind a request that used to hold the browser
> open past its 15 s timeout — now the browser never waits for it.
>
> **Sprint 4.4 status:** the experience is now **zero-click**. Video
> detection (boot + SPA navigation) auto-opens the overlay and starts the
> pipeline; duplicate events for one video start exactly one job. Before
> anything starts, a bounded availability gate proves the backend is up
> (`GET /health`) and, if not, asks a **Chrome Native Messaging host**
> (`launcher/`) to start it idempotently, then re-checks health — with an
> honest CONNECTING state while it runs and a friendly BACKEND UNAVAILABLE
> + **Retry** (the only manual control, failure states only) when it can't.
> End users never open a terminal.
>
> **Sprint 5 status:** sentiment is now **audience intelligence**. The same
> batched pass adds real **emotion** inference (NRC Emotion Lexicon via
> `nrclex`, offline/deterministic), a documented **intensity** band derived
> from the VADER compound, the existing genuine **decision-margin
> confidence** surfaced as an average, and a deterministic **audience
> mood** over the real aggregates — all exposed as defaulted blocks on the
> same `GET /sentiment` contract and rendered as new overlay cards. No new
> pipeline, no new job framework, no fake metrics: every number is model
> output or a documented derivation (docs/architecture/audience-intelligence.md).

> **Sprint 4 status:** the full pipeline works end-to-end — acquisition →
> ingestion → `READY_FOR_ANALYSIS` → sentiment processing → `PROCESSED` →
> analysis endpoint → console overlay. English-only verdicts by explicit
> policy (other languages are counted as `skipped`, never mislabeled);
> **no emotions, topics, aspects, or LLM summaries** — those belong to later
> sprints.

## What currently works

```text
✔ Chrome Manifest V3 extension (loopback host permissions only, YouTube-only content script)
✔ YouTube page detection + video-id detection (watch/shorts/live, validated)
✔ SPA navigation detection; new video auto-opens the overlay with cleared state (FR-11)
✔ Scroll-aware overlay: ~1s of sustained scrolling minimizes it to the compact pill, never closes (flicks & own-body scroll kept, navigation grace)
✔ ✦ AI FAB + futuristic overlay (open/close/minimize/restore, a11y, Shadow DOM isolation)
✔ FastAPI backend: GET /health, GET /api/videos/{video_id}
✔ Official YouTube Data API integration (videos.list + paginated commentThreads.list)
✔ Video-id validation (11-char rule), categorized error taxonomy (10 codes → honest UI copy)
✔ Ingestion-safe normalization: unicode/whitespace, string→int counters, thread→flat comments
✔ Configurable pagination: page size MAX_COMMENTS_PER_REQUEST=100 vs dataset limit COMMENT_ACQUISITION_MAX_COMMENTS=5000, hasMore/limitReached reporting
✔ commentsDisabled / no-comments → explicit UI states, never fake data
✔ Ingestion pipeline: validation → Unicode-safe normalization → language metadata → dedup on commentId
✔ SQLite dataset store: videos/comments/ingestion_runs, FK video isolation, 2 purposeful indexes
✔ Raw vs processed separation: rawText preserved verbatim beside normalizedText
✔ Processing states: READY_FOR_ANALYSIS → PROCESSING → PROCESSED / FAILED, state machine enforced in DB
✔ Dataset statistics + data-quality runs via GET /api/videos/{id}/stats
✔ Three-layer cache: memory TTL → fresh dataset store (DATASET_CACHE_TTL_SECONDS) → YouTube
✔ commentsDisabled / no-comments → explicit UI states, never fake data
✔ Overlay states: READY → LOADING (ACQUIRING DATA) → DATA ACQUIRED | ERROR (retry)
✔ Server-side API key only: .env git-ignored, never appears in responses or the bundle
✔ Sentiment engine: VADER (deterministic, CPU-only, documented) + explicit English-only language policy
✔ Sentiment processing: batched claim→classify→persist through the state machine, idempotent, per-row failure isolation
✔ Sentiment aggregation: SQL GROUP BY label, exact integer percentages, documented tie-breaks, null dominant without data
✔ GET /api/videos/{id}/sentiment (NOT_ANALYZED / PROCESSING / PROCESSED / FAILED) with backend-owned trigger
✔ Dark intelligence console overlay: dominant verdict, distribution bars, coverage, pipeline & honest error/empty states
✔ Incremental acquisition: page → validate/normalize/language/dedup → batched SQLite write → next page (bounded memory, partial runs kept)
✔ Truthful dataset metrics: collected / stored / analyzed / skipped / failed + hasMore / limitReached on GET /sentiment (denominator = analyzed)
✔ Truthful overlay status: single backend-derived dataset status, "Based on N analyzed comments", exact counts beside one-decimal percentages, no "more available" claims without backend evidence
✔ Stale-protection: Video A's analysis can never render under Video B (store guard + navigation reset + post-await checks)
✔ Single active-video working dataset (Sprint 4.2): one explicit `is_active` row owns the store; switching videos atomically deletes the previous video's comments + runs
✔ Generation-guarded race safety: a superseded acquisition/analysis run can never repopulate the database after a switch
✔ L1 cache follows the single-active policy: cleared on switch, memory never grows with visited videos
✔ Active-video indicator in the overlay (subtle "ACTIVE VIDEO" marker); old data cleared before new video data loads
✔ Background analysis jobs (Sprint 4.3): POST /analysis → 202 immediately; one worker thread runs paginated acquisition → batched sentiment → completion (no long-lived browser request)
✔ Job progress API: GET /analysis/status with real collected/stored/analyzed/skipped/failed/pending counts; QUEUED → ACQUIRING → ANALYZING → COMPLETED | FAILED | CANCELLED | STALE
✔ Overlay progress UI: "Preparing analysis…" → "N comments collected" → "N analyzed of M" → dashboard; interrupted jobs show real counts + Retry
✔ Job dedupe: duplicate triggers for one video reuse the running job (one acquisition, one worker)
✔ Zero-click analysis (Sprint 4.4): video detection auto-opens the overlay and starts the pipeline — no Analyze/Run-analysis control exists; same-id navigation events never restart a run
✔ Backend availability gate (Sprint 4.4): health probe → native-host `ensure_backend` → bounded health re-probe; CONNECTING while it runs, friendly BACKEND UNAVAILABLE + Retry on failure (never connection/stack details in the UI)
✔ Automatic local backend startup (Sprint 4.4, Windows): Chrome Native Messaging host (`launcher/`) idempotently starts FastAPI on demand; readiness is proven by `GET /health`, not by an open port
✔ Switch invalidation reuses Sprint 4.2: the active-video switch cancels in-flight jobs in the same transaction; generation guards block stale writes (rapid A→B→C→D safe)
✔ Restart recovery: jobs orphaned by a backend restart are swept to STALE at startup (retryable, never a stuck spinner)
✔ Bounded YouTube retries: transient failures only (network/timeout/5xx/429), exponential backoff, quota errors never retried
✔ Sprint 5 audience intelligence: emotion (NRC lexicon via nrclex, real inference, 8 categories + honest NEUTRAL), derived intensity bands (LOW/MEDIUM/HIGH), average decision-margin confidence, dominant emotion, deterministic audience mood — inside the same batched generation-guarded job pass
✔ Honest intelligence metrics: emotion/intensity denominators = analyzed rows (skipped/failed excluded), confidence null when unavailable, mood claims nothing below 10 analyzed comments — never fabricated AI metrics
✔ One-time legacy migration: pre-Sprint-5 analyzed rows reopen once for full reprocessing (raw data untouched)
✔ 313 backend tests + 4 opt-in real-API tests + 155 extension tests, all green
```

## What is coming later

```text
→ Sprint 5+: Topic extraction · Discussion clustering · Aspect-based sentiment
→ Evidence-grounded AI insights
→ Persistent distributed caching (TDR-07 remainder)
→ Real interpreted intelligence rendered through this exact overlay
```

## Quick start

**Backend** (needs a YouTube Data API key from Google Cloud):

```bash
cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt    # .venv/bin/... on unix
cp .env.example .env        # add YOUTUBE_API_KEY=...
.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/health     # {"status":"ok","version":"0.5.0"}
.venv/Scripts/python -m pytest        # 313 passed (+4 opt-in real-API tests)
```

The dataset store is created automatically on first use
(`backend/data/sentiment.db`, git-ignored; `DATABASE_URL` configurable).
Run the real YouTube API integration test explicitly (uses your `.env` key,
never committed):

```bash
RUN_REAL_API_TESTS=1 .venv/Scripts/python -m pytest tests/test_real_api.py
```

Without a key the backend still runs: `/health` works and acquisition returns
an honest `503 server_not_configured`.

**Extension:**

```bash
npm install
npm run build      # → dist/
npm run verify     # typecheck + 155 tests + build
```

`chrome://extensions` → **Developer mode** → **Load unpacked** → select
**`dist/`** → open a YouTube video. The overlay auto-opens, shows
CONNECTING while it verifies (and if needed starts) the local backend, and
sentiment results appear automatically — **there is no Analyze button**.

**Dev mode:** starting the backend manually with uvicorn (above) still
works; the availability gate detects the running service via `/health` and
never touches the native host.

**Automatic backend startup (Windows):** register the native host once per
Chrome profile after building:

```bash
python launcher/register_host.py --extension-id <your-extension-id>   # id from chrome://extensions
python launcher/register_host.py --unregister                        # to remove
```

### Troubleshooting analysis jobs

| Symptom | Meaning / fix |
|---|---|
| Overlay shows "ANALYSIS INTERRUPTED · N comments collected" | The background job failed (`status=FAILED`). The message carries the categorized cause (timeout/quota/store); click **Retry** — already-stored comments are kept and deduplicated, no data is lost. |
| Status shows `CANCELLED` | Another video became active while the job ran (Sprint 4.2 invalidation) — expected; open the video you want and analysis restarts automatically. |
| Overlay shows "BACKEND UNAVAILABLE" / "Local AI service is unavailable" | The availability gate could not reach or start the local backend. Click **Retry** once. If it keeps failing: start the backend manually (Quick start above), or re-register the native host (`python launcher/register_host.py --extension-id <id>`), then Retry. No terminal is required in the normal case. |
| Native host does nothing / `ensure_backend` fails | Re-register (`--unregister` then `--extension-id`), confirm the manifest path points at `launcher\host.bat`, and that `backend\.venv` exists (host.bat falls back to system `python`). Host + protocol details: [docs/architecture/local-backend-launcher.md](docs/architecture/local-backend-launcher.md). |
| Port 8000 already in use by a foreign process | The gate only trusts `GET /health` — a process that answers anything else (or nothing) counts as unavailable. Stop the squatter or run uvicorn on another port (`BACKEND_PORT`), then Retry. |
| Status shows `STALE` after a backend restart | The job was interrupted by the restart (detected at startup). Retry. |
| `503 server_not_configured` on start | Set `YOUTUBE_API_KEY` in `backend/.env` (only needed when the stored dataset is not fresh). |
| `429 quota_exceeded` | Daily YouTube quota exhausted — retry after reset (UTC midnight); lower `COMMENT_ACQUISITION_MAX_COMMENTS` if needed. |
| Job stuck in ACQUIRING for a long time | Normal for very large videos (pages are ~1–2 s each); `collected` should climb each poll. If it does not, check backend logs (`ANALYSIS_JOB_*` events; no keys or comment text are ever logged). |

## Android client (mobile/ — Sprint 10.1)

A separate **Flutter + Kotlin** client that hosts the AI overlay runtime on
**top of the YouTube Android app**. It is a pure second client: it consumes
the same backend unchanged, keeps no API keys, and shares no code with the
extension. Sprint 10.1 is foundation only — **no video detection and no
intelligence yet** (Sprint 10.2+); the overlay is a minimal "AI" badge that
proves permission → service → window → stop end to end.

**Run / build:**

```bash
cd mobile
flutter analyze && flutter test        # static analysis + widget/channel tests

# Android build (a JDK 17–21 is required; Java 8 breaks AGP and the
# Java 25 IDE JBRs break Gradle 8.14's Kotlin-DSL compiler):
cd android && JAVA_HOME=<jdk21-path> ./gradlew test assembleDebug
# or, with the Android SDK configured:
cd mobile && flutter build apk --debug  # → build/app/outputs/flutter-apk/
```

**Permission flow (first run):**

1. Install the APK and open the app — the setup screen shows "○ Required".
2. Tap **Enable Overlay** → grant *Display over other apps* → go back
   (permission is re-checked automatically on resume).
3. Tap **Start AI Overlay** → the small "AI" badge appears above YouTube.

**Stop / restart semantics:** **Stop AI Overlay** tears the badge down;
stopping an already-stopped runtime (or starting an active one) is a safe
no-op. The overlay **never restarts by itself** — the service is
`START_NOT_STICKY`, so after a process kill or swipe-away only an explicit
tap brings it back (deliberate, predictable behavior). Revoking the
permission mid-run stops the service immediately.

**Known limitations:** the badge is a static placeholder (detection and the
real AI overlay arrive in Sprint 10.2/10.3); on Android 13+ the foreground
notification stays out of the notification shade until `POST_NOTIFICATIONS`
is granted (the overlay itself is unaffected); on-device verification is a
manual pass. Full log, architecture, and the device checklist:
[docs/development/sprint-10.1.md](docs/development/sprint-10.1.md).

## Problem → Solution

Popular videos accumulate thousands of unstructured comments; engagement metrics
say *how much*, never *why*. This project is a scalable **Database / Big Data
system** that collects, processes, analyzes, and caches large volumes of
comments, then presents the intelligence as an overlay inside YouTube.
Sprint 2 implements the **COLLECT → VALIDATE → NORMALIZE → SERVE** stages,
Sprint 3 the **INGEST → STORE → PROCESS** stages, and Sprint 4 the
**ANALYZE → AGGREGATE → VISUALIZE** stages;
formal problem statement: [docs/02-problem-statement.md](docs/02-problem-statement.md).

## Architecture overview (as built)

```text
                    YOUTUBE                     YOUTUBE DATA API v3
                        │                        (official, key server-side)
                        ▼                                ▲
              CHROME EXTENSION ──── POST /api/videos/{id}/analysis ──┤
              (no API keys)  │        (202 Accepted, returns fast)   │
                    │        ▼                                       │
                    │   BACKGROUND JOB (worker thread)               │
                    │        paginated fetch ────────────────────────┘
                    │              │ page → batch → SQLite (guarded)
                    │              │ batched sentiment + emotion (VADER, NRC) → SQL aggregation
                    │        GET /api/videos/{id}/analysis/status  (poll 750 ms)
                    │        ◀── real counts: collected/stored/analyzed/…
                    │        GET /api/videos/{id} + /sentiment (fast, on COMPLETED)
                    ▼
              DARK INTELLIGENCE CONSOLE OVERLAY
              (auto-opens on video detection - no Analyze button)
              CONNECTING → LOADING → COLLECTING (N) → ANALYZING (N/M) → ANALYSIS COMPLETE

  STARTUP (Sprint 4.4): extension ──native messaging──▶ launcher/host.bat
      ──▶ ensure_backend (idempotent: /health → spawn uvicorn → /health) ──▶ FastAPI + SQLite
```

Pipeline status: `COLLECT ✅ INGEST ✅ VALIDATE ✅ CLEAN ✅ NORMALIZE ✅
SERVE ✅ STORE ✅ PROCESS ✅ ANALYZE ✅ AGGREGATE ✅ EMOTION ✅ · TOPIC ⬜ (Sprint 5+)`

## Technology overview

| Layer | Choice | Notes |
|---|---|---|
| Extension | Chrome MV3, TypeScript (strict), React, Tailwind v4, Vite | TDR-01/02/03 |
| Backend | Python, FastAPI, Pydantic, httpx, uvicorn | TDR-04 (implemented) |
| Data source | YouTube Data API v3, backend-only key | TDR-05, docs/12 (verified facts) |
| Cache | 3 layers: memory TTL/LRU → SQLite dataset freshness → YouTube | AD-05 + caching.md |
| Database | SQLite (stdlib `sqlite3`) behind a repository layer | **TDR-06 DECIDED** (Sprint 3); Postgres path in database.md |
| Sentiment engine | VADER (`vaderSentiment`), lexicon + rules, CPU-only, deterministic | **Sprint 4**; rationale, score semantics & language policy in sentiment-analysis.md |
| Emotion engine | NRC Emotion Lexicon via `nrclex`4.x, own tokenizer, CPU-only, offline | **Sprint 5**; labels, intensity/confidence/mood rules & denominators in audience-intelligence.md |

## Development roadmap

```text
Sprint 0  Blueprint & architecture            ✅ done
Sprint 1  Chrome extension foundation          ✅ done
Sprint 2  YouTube data acquisition + backend   ✅ done
Sprint 3  Data pipeline, storage & processing  ✅ done
Sprint 4  Sentiment intelligence foundation    ✅ done
Sprint 4.1 Full comment acquisition + accurate metrics  ✅ done
Sprint 4.2 Active video dataset lifecycle + cleanup     ✅ done
Sprint 4.3 Asynchronous analysis jobs + progress UI     ✅ done
Sprint 4.4 Zero-click intelligence + auto backend start ✅ done
Sprint 5  Audience intelligence: emotion/intensity/confidence/mood ✅ done (this)
Sprint 5+ Topic extraction, discussion intelligence, caching, dynamic lifecycle
Sprint 6  Overlay intelligence UI + evaluation
```

## Documentation

```text
docs/
├── 01 … 10                      Sprint 0 blueprint (vision, requirements, MVP, evaluation)
├── 11-data-acquisition-architecture.md   Sprint 2 pipeline + security + quota design
├── 12-youtube-api-contract.md   verified YouTube API usage, quota, error reasons
├── 13-backend-api-contract.md   endpoints, schemas, error codes, CORS
├── architecture/
│   ├── system-architecture.md   layered architecture, async jobs, entities, caching
│   ├── data-flow.md             COLLECT → VISUALIZE pipeline
│   ├── database.md              TDR-06: SQLite decision, alternatives, indexes, migration,
│   │                            active-video lifecycle (single working dataset, switch transaction)
│   ├── data-model.md            videos/comments/ingestion_runs, raw vs processed
│   ├── data-pipeline.md         ingestion stages, events, quality, language detection
│   ├── caching.md               3-layer cache + freshness policy
│   ├── processing.md            status lifecycle, batching, Sprint 4 runs
│   ├── sentiment-analysis.md    VADER choice, score/confidence, language policy, aggregation
│   ├── audience-intelligence.md Sprint 5: NRC emotion model, intensity/confidence/mood rules, denominators
│   ├── chrome-extension-architecture.md   as-built client architecture (Sprints 1–2)
│   ├── local-backend-launcher.md  native-messaging host, ensure_backend protocol, troubleshooting
│   └── ai-analysis-spec.md      AI layers, model-selection rule, preliminary contract
├── ui/overlay-design.md         design language, component specs, states, a11y
├── development/
│   ├── sprint-1.md              extension foundation log + test matrix
│   ├── sprint-2.md              data acquisition log + checklist + limitations
│   ├── sprint-4.4.md            zero-click flow, launcher, acceptance checklist (§50)
│   ├── sprint-5.md              audience intelligence log + checklist
│   ├── sprint-5.1.md            performance optimization log + measured benchmarks
│   └── sprint-5.2.md            NLP inference speed log + parity proof
│   └── sprint-10.1.md           Android foundation + overlay runtime log (§30 device checklist)
└── decisions/
    ├── technology-decision-record.md       TDR-01…08 status register
    └── youtube-data-acquisition.md         AD-01…08 acquisition decisions
```

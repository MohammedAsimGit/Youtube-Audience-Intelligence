# Sprint 5.1 — High-Performance Comment Analysis Optimization

**Status:** complete · **Date:** 2026-09-30 · **Version:** 0.5.0

## What This Sprint Delivers

Benchmark-first optimization of the existing pipeline — no architecture
replacement. We profiled the real pipeline before touching it, found the
measured bottleneck, and optimized exactly that. Headline: at 5,000
comments with realistic per-page API latency, **time-to-first-insight
drops from 68.2 s to 1.6 s (~43×)** while total time drops ~1.4×; the
NLP emotion stage is **8.2× faster** in tight-loop A/B. Comment limits,
sampling, accuracy, zero-click, active-video isolation, SQLite, and the
UI contract are all untouched. No new services (no Redis/Kafka/Celery),
no second pipeline, no second cache.

## 1. Inspection & Baseline (§1)

Full inspection of backend acquisition, pagination, retry, normalization,
language detection, dedup, SQLite repository/batching, sentiment
inference, emotion processing, job manager, aggregation, API endpoints,
frontend detection/trigger/polling/overlay, and the test suites. Baseline
recorded **before any change**:

- Backend tests: **313 passed, 4 skipped**
- Extension: **155 passed, 11 files** + build ✓
- Benchmark harness: `backend/bench_pipeline.py` (new, `stages` / `micro`
  / `e2e` / `memory` modes, `--baseline` A/B flag, deterministic seed 42,
  honest ~91%-unique corpus at 5,000) with paired results under
  `backend/benchmarks/results/*-uniq*.json`.

## 2. Root Cause — Measured, Not Guessed (§2)

Per-comment micro-profile of the pipeline:

| Stage | Cost/comment | Share |
|---|---|---|
| **Language detection (langdetect)** | **~5–11 ms** (≈1.6 ms fixed + length-proportional; 42 ch ≈ 1.76 ms, 2,100 ch ≈ 8.1 ms) | **~89%** |
| Emotion (NRCLex) | ~750 µs — of which `NRCLex()` **constructor ~267–590 µs** (filesystem lexicon-resolve probe on every call, `site-packages/nrclex/core.py`) | ~11% |
| VADER classify | ~80–470 µs | <5% |

Secondary: acquisition and analysis ran **strictly sequentially** — the
user saw nothing until every page was fetched (`first insight ≈ full
acquisition` on real API latency). DB layer inspected and **already
batched** (one lock+commit per 500-row chunk, SQL GROUP BY, keyset
reader — no N+1); YouTube client already had persistent `httpx.Client`,
bounded timeout, exponential backoff, dup-token guard. langdetect is
deterministic (`DetectorFactory.seed=0`) → exact-text caching is valid.

**Primary bottleneck: language detection (per-comment, inline in the
fetch path) + strictly sequential acquire-then-analyze. NLP model
inference was NOT the bottleneck** → per §14, no lighter model
replacement.

## 3. Changes Made

1. **`app/core/config.py`** — `sentiment_inference_batch_size: int = 0`
   (0 = follow `COMMENT_BATCH_SIZE`), validator ≥0 / ≤10000, documented
   with the measured sweep curve.
2. **`app/services/language.py`** — bounded exact-text result cache:
   FIFO, 8,192 entries, ≤256-char texts; unknowns cached; non-alpha fast
   path bypasses the detector; long texts detected but never cached.
   Behavior-identical (seed 0). Footprint measured: 4,350 entries ≈ 0.93 MiB.
3. **`app/services/sentiment.py`** —
   - lazy **NRCLex singleton** (`_get_emotion_engine()`, serialized under
     the existing run lock) kills the per-call constructor probe;
   - `_inference_batch()` honoring `SENTIMENT_INFERENCE_BATCH_SIZE` in
     both pending-read and result-save paths;
   - `SENTIMENT_FIRST_INSIGHT` log (real clock) after the first batch
     with `processed > 0`;
   - job-aware fast read: while a job for the video is active,
     `get_analysis` skips the inline drain and reports `PROCESSING` for
     the whole job lifetime (interim never reads as final).
4. **`app/services/acquisition.py`** — bounded producer/consumer overlap
   (`queue.Queue(max=2 pages)` backpressure): fetch thread produces
   pages, `ingest-*` worker normalizes/persists, worker-side authoritative
   cap accounting, `after_page` hook per persisted page, worker joined
   before return, error ordering preserved. Existing threads/queues only.
5. **`app/services/jobs.py`** — `after_page` → `_interim_analysis`:
   non-blocking `process_pending(wait=False)` per persisted page so NLP
   runs *during* acquisition; cancel-checked, exception-swallowing
   (final ANALYZING drain remains authoritative).
6. **`app/services/ingestion.py`** — `fetched` property for cap accounting.
7. **Frontend** — `OverlayBody.tsx`: `ProgressiveInsightView` (real
   bars + `ANALYZING…` badge + `analyzed / stored` counts, shown only
   when `analyzed > 0` and job non-terminal; terminal states unchanged).
   `Overlay.tsx`: throttled progress refresh (1.5 s, one in-flight, only
   when `analyzed` grew, stale-video guarded) piggybacking on the job
   poll — no new UI, no fake %, zero-click untouched.

## 3–5. Model Reuse & Batch Inference (§3/§4)

Model was already loaded once (VADER + lexicon module-level); the
per-call cost hiding inside "loaded once" was the **NRCLex constructor
per comment** — now a documented lazy singleton. Batch size benchmarked
over 32/64/128/256/512 (5,000 comments): throughput rises to ~128 then
plateaus within noise — **after: 1039 / 1245 / 1284 / 1275 / 1226 c/s;
baseline: 549 / 641 / 695 / 675 / 697 c/s** (1024/2048 no better than
512). **Selected default: follow `COMMENT_BATCH_SIZE` (config `0`)** —
already in the plateau band, keeps one knob; explicit
`SENTIMENT_INFERENCE_BATCH_SIZE` available for tuning. Memory flat
across the sweep.

## 6–7. Acquisition Overlap & YouTube API (§6/§7)

Overlap via the bounded queue above (§6: acquire→persist→analyze pipelined
per page, no new middleware). YouTube client verified pre-compliant:
persistent connection reuse, bounded timeouts, exponential backoff (3
attempts, no duplicate rows on retry), duplicate-`nextPageToken` guard,
`COMMENT_ACQUISITION_MAX_COMMENTS`/page-size config retained, no extra
API calls introduced.

## 16–17. Database (§16/§17)

Inspected and measured: already one transaction per 500-row chunk
(`_ID_CHUNK`/`_IN_CHUNK`), SQL-side GROUP BY aggregation, no N+1, no
per-comment commits. **No DB changes were needed** — correctness-first
preserved.

## 10–11. Incremental Analysis (§10/§11)

Unchanged state machine preserved: `PROCESSED` comments are reused, only
text-changed/model-version rows reset to `READY`. No second cache system.

## Measured Results (§23 — unique corpus, paired runs)

**Stages @5,000 (same session):** total **43.8 s after vs 74.3 s
baseline** (acquisition 40.0 vs 66.5 s; langdetect-in-acquisition 38.5 vs
64.7 s; analysis 3.8 vs 7.8 s). @1,000: 9.4 vs 12.3 s. @2,500: 21.4 vs
32.0 s. Fresh-process spot checks showed ±15% run-to-run noise (baseline
5,000 fresh = 57.2 s; after fresh = 49.3 s) — treat paired numbers with
that caveat.

**Micro A/B (tight loops, most reliable):** emotion 750.2 → 91.6 µs
(**−88%**); analysis sweep ~2× faster at equal batch (695 → 1,284 c/s @
128); VADER unchanged (421 vs 466 µs = ~10% noise floor); langdetect
11.0 → 8.2 ms/comment (cache absorbs repeats).

**E2E @ 2,500 comments-equivalent pages with 250 ms simulated API
latency/page (headline):** first insight **after: 2.6/1.5/1.7/1.4/1.6 s**
at 100/500/1000/2500/5000 vs **baseline: 2.6/6.2/13.9/35.5/68.2 s** —
**68.2 s → 1.6 s at 5,000 (~43×)**; total 74.8 → 54.2 s (~1.4×). All
jobs COMPLETED, analyzed+skipped == total, 0 failed. *(Latency is
simulated in-harness, not live YouTube.)*

**Memory (tracemalloc):** acquire/analyze peaks baseline ≈ after across
sizes (e.g. 2,500: 4.4/3.6 MiB after vs 4.4/3.6 baseline-band) →
bounded, unaffected. Language cache ≈ 0.93 MiB at 4,350 entries
(hard-bounded 8,192 × 256 chars).

## Tests Executed (§26 — real results)

| Suite | Command | Result |
|---|---|---|
| Backend | `./.venv/Scripts/python -m pytest -p no:warnings` | **328 passed, 4 skipped** (313 + 15 new) |
| Real API (opt-in) | `RUN_REAL_API_TESTS=1 pytest tests/test_real_api.py` | **4 passed** (~115 s) |
| Extension | `npm run verify` | **156 passed, 11 files** + typecheck + build ✓ (155 + 1 new) |

New coverage (§25): language cache (reuse, unknowns, entry bound,
long-text bypass), emotion singleton (constructed once, no state leak),
inference batching (explicit + default + validation), first-insight log,
job-aware fast read, interim analysis persisting mid-acquisition,
progressive frontend view + stale/terminal guards (§8/§20), plus the full
existing regression: pagination/retry/dup-token, active-video A→B→A,
cancellation, restart, stale jobs.

## UI Changes (§19–§21)

Results area only, existing visual language: while a job runs and real
aggregates exist, the overlay shows the real distribution with an
**`ANALYZING…`** badge and truthful `analyzed / stored` counts,
refreshed by throttled job-poll piggyback. Terminal render, FAB,
zero-click (no Analyze button), and all Sprint 4/5 cards unchanged.

## Known Limitations / Remaining Bottlenecks (§12)

- Language detection still dominates steady-state CPU (~89%) — cache
  only helps duplicate/short texts; a future win would be a cheaper
  detector, **not done here** (§15 forbids removing detection; profiled
  only).
- Sequential pagination latency bounds total time on live API; first
  insight now decouples from it, so this is UX-neutral.
- Batch plateaus past 128 — no gain from larger batches; no prefetch
  depth change (§7: no extra API calls).
- Model versioning: lightweight only — emotion model string already
  recorded per row (`nrclex-4.1`); no new registry (§12 "only if
  appropriate").

## Recommended Follow-ups (NOT in this sprint)

Cheaper/async language detection (batched detector or per-worker warm
pool), speculative prefetch of the next page token within the no-extra-
calls budget, live-API E2E benchmark rig, per-stage tracing endpoint for
ops.

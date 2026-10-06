# Sprint 8 — Real-Time Audience Intelligence

**Status:** complete · **Date:** 2026-10-02 · **Version:** 0.8.0

## What This Sprint Delivers

A **continuously operating audience-intelligence layer**: while the user stays
on a video, a per-video background monitor periodically checks YouTube for
newly available comments, acquires **only the genuinely-new rows** through the
existing ingestion pipeline, runs sentiment/emotion/intensity analysis over
just those `READY_FOR_ANALYSIS` rows, recomputes aggregates, tracks a
sentiment **trend** and an audience **activity** rate, warms the Sprint 7
insight when enough new comments were analyzed — and pushes the changes into
the overlay automatically (a `LIVE` strip with `+N new`, trend delta, and
activity), with no Analyze button and no manual refresh. A steady poll costs
exactly **1 `commentThreads.list` unit** regardless of dataset size (100 →
5,000), measured. This is *not* a fake ticker: every number shown comes from
actual newly-acquired or newly-processed YouTube data (§4).

## 1. Inspection (no guessing)

| Question | Answer (verified) |
|---|---|
| Existing scheduler? | No — jobs are user-triggered (`AnalysisJobService`); the monitor is a new `RealtimeService` **reusing** the job-era lifecycle conventions (active-video guard, generation supersession, shutdown hook) rather than a second scheduler |
| Incremental acquisition? | Did not exist — `acquire_dataset` re-walks from the top; Sprint 8 adds `acquire_incremental` **beside it in the same service**, sharing validation → normalization → language → dedup → ingestion session (no second pipeline) |
| New-comment detection | `DatasetRepository.existing_comment_ids` (set probe) + `count_recent_comments(video_id, since_iso)` (activity window) — both served by the existing `idx_comments_video_published` index |
| Reprocessing risk | None by construction: analysis reads only `READY_FOR_ANALYSIS`; `PROCESSED` rows are never re-inferred |
| Frontend data flow | Existing poll loop pattern (topics/insight triggers) extended with a realtime poll gated on the same conditions; version string drives silent sentiment/insight refetch |
| Constraints honored | §16 (controlled polling), §17 (quota), §12 (stale-video protection via `isCurrentVideo` + generation), §4 (no fabricated data), no new infra (no Redis/Celery/second DB) |

## 2. Approach & Why — one monitor, one pipeline, incremental by construction

```
observe(video_id)  ──starts──▶  monitor thread (daemon, realtime-{video})
      │                             │ wait(poll_interval)
      ▼                             ▼
 GET /realtime                 _tick():
 (status build)                1. active + generation guard (retire if superseded)
                               2. skip while a job is ACTIVE (no double-run)
                               3. acquisition.acquire_incremental(...)   ← 1 API unit
                                  ├─ all-known page → boundary stop (no writes)
                                  └─ new rows → ingestion session (same L3 path)
                               4. sentiment.process_pending(...)  ← READY rows only
                               5. snapshot: distribution vs baseline → RISING/FALLING/STABLE
                               6. insight warm when analyzed − watermark ≥ threshold
      │                             │
      └──── overlay polls ◀─────────┘  (version flip → silent refetch)
```

- **Monitor is a thin daemon thread per active video**, not a framework:
  `_MonitorState` holds the interval, a stop-event, and the trend/insight
  snapshot; the loop `wait(interval)`s, checks `last_touch` (idle-disconnect
  after 3 missed intervals), then runs one guarded `_tick`. `observe()` is
  idempotent (dedupe under lock) and `retire` is idempotent with a logged
  reason (`superseded`, `disabled`, `not_configured`, `idle`).
- **Steady-state = boundary stop.** The probe fetches the newest page in
  `order=time`; if every id is already stored, the run stops before any
  write and preserves stored `has_more` — 1 API unit, no ingestion, no
  analysis. Only when the probe sees unknown ids does it persist a page.
- **Everything reuses existing services**: `acquire_incremental` calls the
  same `client.get_comment_threads` → `normalize_thread_comments` →
  `dataset.existing_comment_ids` → ingestion session (`add_page`) as the
  full acquisition; `process_pending` is the job's own analysis entry point
  with a `should_cancel` hook wired to the monitor's stop-event.
- **Request handlers never block (§15)**: `GET /realtime` only *observes*
  (starts monitor if needed) and builds a status snapshot from repository
  reads; the poll work happens on the monitor thread.

## 3. Incremental processing (how old comments are never reprocessed)

1. **Acquisition**: the probe compares the fetched page's ids against
   `existing_comment_ids`; known rows are skipped (upsert keeps their
   sentiment columns via the generation guard), unknown rows go through the
   ingestion session as new `READY_FOR_ANALYSIS` rows. Stored comments are
   never deleted or rewritten.
2. **Analysis**: `sentiment.process_pending(video_id, generation, …)` selects
   only `READY_FOR_ANALYSIS` rows of the active generation — `PROCESSED`
   rows are structurally excluded, so VADER/NRC inference runs exactly once
   per comment. Test `test_poll_processes_only_new_rows` pins this
   (`sentiment_processed_at` timestamps of seeded rows are unchanged).
3. **Insight**: watermark (`_MonitorState.insight_watermark`) tracks how many
   analyzed comments were already accounted for; regeneration fires only when
   `analyzed − watermark ≥ realtime_insight_min_new_analyzed` (default 25).
4. **Idempotence**: re-polling with no new data performs zero writes
   (benchmark asserts `inserted == 0`, tests assert status equality);
   `upsert_comments`' idempotent keying makes duplicate delivery a no-op.

## 4. Trend calculation

Each tick compares the current distribution (`distribution_percentages`:
positive/neutral/negative share of analyzed comments) against the **baseline
snapshot taken when the monitor started**:

- `changePp = current positive − baseline positive` (signed percentage
  points); per-class deltas (`positivePp`, `neutralPp`, `negativePp`) are
  also reported.
- **Noise floor**: if `|changePp| < realtime_trend_min_change` (default
  1.0 pp, configurable 0–100) the state is `STABLE`; above it → `RISING`,
  below → `FALLING`.
- After each evaluation the baseline **rebases** to the current distribution,
  so the trend describes movement since the last observed change rather than
  drift since page load. Trend state/deltas live in the in-memory monitor
  snapshot (§13) and reset when the monitor retires.

## 5. Audience activity

The activity metric is **genuinely new comments observed per minute**, from
`count_recent_comments(video_id, since_iso)` over a rolling window of
`max(2 × poll_interval, 60)` seconds (≥ 1 minute by construction):

| level | rate |
|---|---|
| `LOW` | ≤ 1.0 new/min |
| `MODERATE` | ≤ 5.0 new/min |
| `HIGH` | > 5.0 new/min |

`newRecent`, `ratePerMinute`, and `windowMinutes` are all reported so the UI
can show `AUDIENCE HIGH · 7.9/min (1m)` — computed from real `published_at`
rows, never simulated.

## 6. AI insight updates

The insight (Sprint 7 engine) is **not** regenerated every tick. The monitor
warms it via `insight.get_insight` (the same cached, evidence-validated path
a cold GET uses) only when at least `REALTIME_INSIGHT_MIN_NEW_ANALIZED`
(default 25) comments have been analyzed since the last warm, and only after
those comments are actually processed. The watermark advances on success, so
a failed warm retries next tick. Log marker: `REALTIME_INSIGHT_REGENERATED`.
On the frontend, a realtime **version flip** (`"{stored}:{analyzed}"`)
triggers a *silent* sentiment refetch; if sentiment is still `PROCESSED`,
topics/insight are cleared and refetched through their existing triggers —
no state-machine changes, no extra phases.

## 7. API Changes

**New endpoint** (pattern-matches existing routes):

```
GET /api/videos/{video_id}/realtime → 200 RealtimeStatusResponse
```

```jsonc
{
  "videoId": "…", "enabled": true, "monitoring": true,
  "pollIntervalSeconds": 30.0,
  "lastCheckedAt": "…", "lastUpdatedAt": "…",
  "newComments": 47, "totalComments": 2429,
  "analyzed": 1211, "pending": 12, "skipped": 1218, "failed": 0,
  "sentiment": { "positive": 68.3, "neutral": 29.0, "negative": 2.7 },
  "trend": { "state": "FALLING", "changePp": -6.8, "positivePp": -6.8,
             "neutralPp": 5.1, "negativePp": 1.7 },
  "activity": { "level": "HIGH", "newRecent": 7, "windowMinutes": 1.0,
                "ratePerMinute": 7.9 },
  "dominantEmotion": "TRUST",
  "version": "1211:1211"
}
```

- Invalid id → 422 (existing `validate_video_id`); unknown/inactive video →
  404 `VideoNotFound`; SQLite failure → 503 `StorageUnavailable`. No query
  parameters. All new models live in `app/models/internal.py`
  (`RealtimeStatusResponse` et al., camelCase via the shared `ApiModel`).
- **No existing endpoint changed** — sentiment/topics/insight/job contracts
  are untouched; the frontend's refetch uses the existing GETs.

## 8. Database Changes

- **No schema changes, no new tables, no migrations.**
- One new repository read: `count_recent_comments(video_id, since_iso)` —
  `SELECT COUNT(*) … WHERE published_at >= ?` (ISO-8601 text comparison,
  served by the existing `idx_comments_video_published` index).
- Supporting reads added on `DatasetService` (not the DB):
  `get_dataset_info` (video row → `DatasetInfo`) and `existing_comment_ids`
  (delegates to the repository's set probe).

## 9. Configuration (`app/core/config.py` + `.env.example`)

| variable | default | bounds (validated) |
|---|---|---|
| `REALTIME_ENABLED` | `true` | — |
| `REALTIME_POLL_INTERVAL_SECONDS` | `30` | 1–3600 |
| `REALTIME_MIN_POLL_INTERVAL_SECONDS` | `15` | 1–3600 |
| `REALTIME_MAX_POLL_INTERVAL_SECONDS` | `120` | 1–3600, min ≤ max (`model_validator`) |
| `REALTIME_TREND_MIN_CHANGE` | `1.0` | 0–100 (pp) |
| `REALTIME_INSIGHT_MIN_NEW_ANALIZED` | `25` | 1–10000 |

The effective interval is `clamp_poll_interval(settings)` — clamped to
[MIN, MAX] at use time, never hardcoded in the service. `.env.example` gained
a documented "Real-Time Incremental Monitoring" block.

**Incident fixed while verifying this sprint:** the local `backend/.env`
carried `CORS_ORIGINS` as a plain comma-separated string (the format
`.env.example` documents), which pydantic-settings 2.15 tries to JSON-decode
for `List[str]` fields — `Settings()` crashed at import, taking the whole
test suite with it. `cors_origins` now uses `NoDecode` + a before-validator
accepting **both** the documented comma format and a JSON array, pinned by
`tests/test_config.py` (8 tests).

## 10. Frontend UX

- **`RealtimeStrip`** (new component, rendered right after Sentiment when
  `status === PROCESSED && analyzed > 0`): `LIVE` / `STANDBY` (monitor not
  running) / `LIVE UPDATES OFF` (feature disabled) pill with a pulsing dot,
  `+N new` badge, trend arrow **plus text label and signed pp value**
  (never color-only: `▼ FALLING -6.8pp`), activity line
  `AUDIENCE {LEVEL} · {rate}/min ({window})`, relative `checked Ns ago`
  timestamp, and a pending-queue note (`N new comments queued for analysis`).
- **Polling loop** in `Overlay.tsx`: self-scheduling `setTimeout`, gated on
  the same conditions as topics/insight triggers (sentiment success +
  `PROCESSED`, job terminal, video unchanged); initial poll on mount; period
  `clamp(pollIntervalSeconds × 500, 5s, 10s)` on success, 10 s backoff on
  error/unavailable. A version change silently refreshes sentiment and then
  topics/insight — the user sees the numbers move, never a spinner reset.
- **Stale-video protection**: `updateRealtime`/`failRealtime` check
  `isCurrentVideo`; context switches reset the slice; errors keep the last
  known data (the strip doesn't blink out on a transient failure).
- **Store/API**: `RealtimeState` slice on `OverlayState`, `RealtimeService`
  port + `HttpRealtimeService` (structural validator, 10 s timeout, typed
  error codes), wired through `mountApp` → `App` → `content/index.ts` and a
  fake in the preview harness (scenes below). CSS: appended `.sai-realtime*`
  block using the existing glass-panel language; animations covered by the
  global `prefers-reduced-motion` rule.

## 11. Measured Performance (`backend/benchmarks/bench_realtime.py`,
results in `benchmarks/results/sprint8-realtime.json`, read stages = min of
20 runs, mutating stages run once, synthetic clearly-labelled data)

| scale | steady probe | **API units/poll** | status build | burst insert (50 new) | full tick | tick #2 | tick peak mem | analyzed after |
|---|---|---|---|---|---|---|---|---|
| 100 | 1.95 ms | **1** | 0.12 ms | 405.5 ms* | 52.0 ms | 27.2 ms | 382 KB | 200 |
| 500 | 2.68 ms | **1** | 0.48 ms | 9.0 ms | 15.0 ms | 47.9 ms | 381 KB | 600 |
| 1,000 | 2.72 ms | **1** | 0.63 ms | 5.4 ms | 9.0 ms | 27.7 ms | 381 KB | 1,100 |
| 2,500 | 4.13 ms | **1** | 1.56 ms | 7.4 ms | 10.9 ms | 29.5 ms | 381 KB | 2,600 |
| 5,000 | 6.49 ms | **1** | 3.23 ms | 14.1 ms | 14.7 ms | 34.2 ms | 381 KB | 5,100 |

\* scale-100 burst includes one-time warm-up (page JIT / first ingestion of
the run); steady-state repeats confirm it is not per-poll cost.

Key claims verified by the benchmark's own asserts: the steady probe issues
**exactly 1 `commentThreads.list` call per poll at every scale** (flat vs
dataset size — §17 quota); a full tick (probe + insert-processing + VADER/NRC
over 50 new rows + trend/activity snapshot) stays ≤ 52 ms including warm-up
and ≤ 35 ms steady; status build (what `GET /realtime` serves) is
sub-4 ms at 5,000; tick memory is ~0.4 MB at every scale. The insight warm
is gated out of the tick (threshold raised to 10,000) so the pipeline itself
is measured — insight cost is Sprint 7's benchmark's subject.

## 12. Tests Executed (real results)

| Suite | Command | Result |
|---|---|---|
| Backend | `cd backend && ./.venv/Scripts/python.exe -m pytest -p no:warnings --tb=short` | **505 passed, 4 skipped in 18.09s** |
| Extension | `npm run verify` | typecheck clean, **214 passed / 16 files**, build ✓ `dist/content.js` 361.82 kB |

```text
Previous tests:  469 backend + 195 frontend   (Sprint 7 baseline)
New tests:        36 backend +  19 frontend   (28 realtime + 8 config / 13 realtime UI + 6 HTTP service)
Total:           505 backend + 214 frontend
Passed:          all (505 + 214)
Failed:          0
Skipped:          4 backend (pre-existing real-API skips) + 0 frontend
```

Backend coverage (`tests/test_realtime.py`, 28): incremental boundary stop
(writes nothing), probe costs 1 page per poll, only-new processing
(`sentiment_processed_at` of seeded rows untouched), idempotent re-poll,
trend states + noise floor + rebase, activity bands + window, insight
threshold gating + watermark, monitor lifecycle (start/dedupe/retire on
supersede/disable/missing-key/idle), active-video A→B isolation, job-Active
skip without advancing `lastCheckedAt`, shutdown ordering, HTTP contract
(200/404/422/503) and status shape. `tests/test_config.py` (8) pins both
`CORS_ORIGINS` formats + realtime settings bounds. Frontend coverage:
`realtime-ui.test.tsx` (13) — strip rendering for every state, signed-delta
and text-label accessibility, store slice (apply/stale-discard/keep-data-on-
error/reset-on-transition), polling flow with fake timers including the
version-flip silent refetch; `http-realtime-service.test.ts` (6) — validator
rejection, error-envelope mapping, timeout/network codes. All Sprint 3–7
suites remain green.

## 13. Visual QA (preview harness, `vite.preview.config.ts`, port 5199)

Verified in the browser, not just tests:

- **`?scene=realtime-falling`** — strip renders between sentiment and
  insight: `LIVE` pill, `+47 new` badge, `▼ FALLING -6.8pp`, `AUDIENCE HIGH
  · 7.9/min (1m)`, `checked 38s ago`, `12 new comments queued for analysis`;
  timestamp ticks forward on its own; footer "SPRINT 8".
- **`?scene=realtime-standby`** — `STANDBY` pill (monitor not running for
  this video), data still shown; `RISING +4.2pp`, `MODERATE · 3.4/min`.
- **`?scene=realtime-off`** — `LIVE UPDATES OFF` pill, non-pulsing dot,
  historical data intact.
- **`?scene=realtime-error`** — transport failure with no prior data keeps
  the strip absent (by design — no half-initialized UI); console shows no
  unhandled errors.
- **Default scene** — `LIVE` + `RISING` strip appears after the poll
  settles; polling continues in the background (console shows repeated
  `realtime:snapshot`).
- **Minimize → restore** — panel minimizes, strip hides with it, polling
  keeps firing (`realtime:snapshot` continues in console); restore shows the
  strip with current data (`checked 1m ago`).
- **Regression:** `?scene=analyzing` — no strip while analysis runs, progress
  UI intact; `?scene=complete` — full stack + strip + insight coexist;
  `?scene=insight-error` — insight error state and realtime strip render
  independently (strip stays LIVE).
- **Console** — clean: only existing debug/info action logs, no errors.

## 14. Known Limitations

- **Trend/activity are in-memory per monitor.** They reset (rebase) when the
  monitor retires (video switch, backend restart, disable) — there is no
  persisted trend history table, by design (no new tables, §8). After a
  restart the first tick shows `STABLE` until fresh movement accumulates.
- **Polling is per-active-video only.** Only the video the user is currently
  on is monitored; background videos are never polled (deliberate — §9 quota
  discipline). Idle monitors stop after 3 missed touch intervals, so a
  disconnected extension doesn't leak threads.
- **One YouTube page unit per poll is still a unit.** At the default 30 s
  interval that's ≤ 120 units/hour while a video page is open; lowering
  `REALTIME_POLL_INTERVAL_SECONDS` trades freshness for quota linearly. At
  the dataset cap (`COMMENT_ACQUISITION_MAX_COMMENTS`) polling is skipped
  entirely (documented behavior, not a cost path).
- **`lastCheckedAt` semantics:** a poll skipped because an analysis job is
  active does *not* advance `lastCheckedAt` (nothing was checked), so the UI
  can show an older timestamp during long jobs — honest, not a bug.
- **Frontend poll period is halved-interval clamped to 5–10 s** — the
  overlay cannot render faster than the backend's own poll; this is a
  deliberate floor to avoid request storms on slow machines.
- **Activity window granularity** is bounded by `published_at` of fetched
  comments — a video whose comments lack timestamps (edge data) would
  under-count; normal YouTube payloads always carry `publishedAt`.
- **No cross-session trend** — two browser sessions on the same video each
  run their own monitor (per-process threads); state is not shared through
  the database. The dataset itself (the expensive part) is shared and
  idempotent, so the only duplication is the bounded 1-unit probe.

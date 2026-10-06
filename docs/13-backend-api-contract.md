# 13 — Backend API Contract (Sprints 2–4.3)

**Base URL (dev):** `http://127.0.0.1:8000`
**Style:** JSON over HTTP, **camelCase** field names; `GET` everywhere plus
one `POST` (the Sprint 4.3 analysis trigger).
**Errors:** always `{ "error": { "code", "message" } }` — `message` is
user-presentable, `code` is stable for programmatic handling. No stack traces
or upstream details ever appear.

> Contract validation: implemented + tested (backend suite + opt-in
> real-API tests) and consumed by the extension's typed contracts
> (`src/shared/types.ts`). Any change to this file requires changing both
> together. **Sprints 3, 4, 4.1, 4.2 and 4.3 are additive**: the
> `/api/videos/{video_id}` contract below is byte-compatible with Sprint 2
> (verified by its original tests), so the extension kept working without
> changes; Sprint 4 adds the `/sentiment` endpoint alongside it,
> Sprint 4.1 extends that response with a `dataset` block (new fields only -
> no existing field changed meaning), Sprint 4.2 changes only *which
> videos have data* (single active working dataset, see the lifecycle note
> below) - no field changed, and Sprint 4.3 adds the background
> `POST /analysis` + `GET /analysis/status` pair (new endpoints; the
> existing GETs keep working unchanged for older consumers).

## `GET /health`

```json
{ "status": "ok", "version": "0.5.0" }
```

- 200, no upstream calls, works without `YOUTUBE_API_KEY` (liveness only).

## `GET /api/videos/{video_id}`

Path param: exactly 11 chars from `[A-Za-z0-9_-]` (same rule the extension's
URL parser enforces). Failure → **422 `invalid_video_id`**.

### Success — 200

```json
{
  "video": {
    "videoId": "dQw4w9WgXcQ",
    "title": "…",
    "description": "…",
    "channelId": "UC…",
    "channelTitle": "…",
    "publishedAt": "2024-03-01T10:00:00+00:00",
    "categoryId": "28",
    "duration": null,
    "statistics": { "viewCount": 123456, "likeCount": 7890, "commentCount": 321 }
  },
  "comments": {
    "items": [
      {
        "commentId": "…",
        "videoId": "dQw4w9WgXcQ",
        "author": "…",
        "text": "original text (trimmed only)",
        "textNormalized": "collapsed whitespace copy",
        "publishedAt": "2024-03-02T08:00:00+00:00",
        "updatedAt": "2024-03-02T08:00:00+00:00",
        "likeCount": 3,
        "isReply": false,
        "parentId": null
      }
    ],
    "count": 1,
    "hasMore": false,
    "status": "ok"
  },
  "source": { "provider": "youtube", "retrievedAt": "2024-03-04T00:00:00+00:00", "cached": false }
}
```

### Field semantics

| Field | Meaning |
|---|---|
| `comments.count` | number of **normalized** items actually returned (≤ `COMMENT_ACQUISITION_MAX_COMMENTS`, default 5000; pagination walks `nextPageToken` until YouTube runs out or the limit is reached) |
| `comments.hasMore` | `true` only when YouTube offered a next page when acquisition stopped (dataset limit / API page-safety bound) — availability is never inferred otherwise |
| `comments.status` | `ok` · `none` (no accessible comments) · `disabled` (YouTube `commentsDisabled`) |
| `source.cached` | `true` when served from the acquisition cache (0 YouTube quota spent) |
| `duration` | always `null` in Sprint 2 (documented gap, see doc 12) |
| nullable fields | `null` = genuinely unavailable — **never invented** |

`comments.status = "none" | "disabled"` is still **200**: metadata is valid
data; the overlay renders an explicit COMMENTS UNAVAILABLE block (§25).

### Errors

| HTTP | `error.code` | Trigger |
|---|---|---|
| 422 | `invalid_video_id` | id fails the 11-char rule |
| 404 | `video_not_found` | Google returned no item for the id |
| 429 | `quota_exceeded` | Google `quotaExceeded` / `dailyLimitExceeded` |
| 502 | `upstream_unavailable` | Google reachable-but-erroring / network failure |
| 502 | `upstream_data_invalid` | malformed Google payload (validated, never crashes) |
| 504 | `upstream_timeout` | Google request exceeded configured timeout |
| 503 | `server_not_configured` | `YOUTUBE_API_KEY` missing/rejected (`keyInvalid`) |

### Active-video lifecycle (Sprint 4.2)

This endpoint is the extension's **explicit activation point** for the
single active-video working dataset:

- Requesting video **B** while video **A** is active performs an atomic
  switch: A's dataset (comments, video row, ingestion runs) is deleted and
  B becomes the active row - in one transaction. The 200 response is then
  B's freshly acquired dataset.
- Requesting the **already-active** video is non-destructive: the normal
  freshness policy (L1 → L2 → L3) decides whether to re-acquire. Same-video
  refreshes (`Analyze again`) are never switches.
- **422 `invalid_video_id` always precedes the switch** - an invalid id can
  never trigger a destructive transition.
- The response carries **no lifecycle fields**: activation stays internal
  (no new fields added).

Read endpoints **never activate** anything. `/stats` and `/sentiment` answer
strictly for their video id: if it is not the active working dataset there
is no stored data, so they return the existing **404 `video_not_found`**
(documented semantics - a stray or superseded video id is never blindly
activated by a read).

Client-side only (no HTTP response): `network_error` (backend not running),
`acquisition_failed` (fallback).

Example:

```json
{ "error": { "code": "quota_exceeded", "message": "The YouTube API quota is exhausted for now. Please try again later." } }
```

## CORS

- Allowed: exact dev origins (`http://localhost:5173`, `http://127.0.0.1:5173`)
  + regex `^chrome-extension://[a-p]{32}$`.
- Never `*`. Verified live: extension origin receives
  `access-control-allow-origin` echo; foreign origins receive none.
- The extension additionally holds loopback `host_permissions`, which exempts
  its content-script fetches from page CORS (belt and braces).

## Consumption Notes (extension)

- Transport: `HttpAnalysisService` → `GET {origin}/api/videos/{id}`,
  `AbortController` timeout 15s, `Accept: application/json`, no auth headers,
  **no credentials of any kind**.
- Sprint 4.3 primary flow: `HttpJobService` → `POST {origin}/api/videos/{id}/analysis`
  (10s timeout, 202) then controlled polling of
  `GET {origin}/api/videos/{id}/analysis/status` every 750 ms (abortable;
  stops on any terminal status or a video switch). The finished dataset and
  aggregates are then read with the two fast GETs below.
- Origin override for future deployments: build-time global
  `__SENTIMENT_AI_BACKEND__` (unset in repo builds → `127.0.0.1:8000`).
- The UI renders acquisition facts (title, comment count, source, status)
  **plus**, from the Sprint 4 endpoint below, backend-derived sentiment
  aggregates — the extension still performs no analysis itself.

## `GET /api/videos/{video_id}/stats` (Sprint 3, additive)

Dataset statistics for one video — acquisition facts only (no sentiment,
that belongs to Sprint 4). Read-only, no query parameters accepted.

- **200** — aggregates computed in SQL over the stored dataset:

```json
{
  "videoId": "dQw4w9WgXcQ",
  "totalComments": 3,
  "uniqueComments": 3,
  "replyCount": 1,
  "topLevelCommentCount": 2,
  "oldestCommentTimestamp": "2024-03-01T08:00:00+00:00",
  "newestCommentTimestamp": "2024-03-03T08:00:00+00:00",
  "lastAcquiredAt": "2024-03-04T00:00:00+00:00",
  "commentsStatus": "ok",
  "processingStatus": { "READY_FOR_ANALYSIS": 3 },
  "lastIngest": {
    "fetched": 3, "valid": 3, "rejected": 0, "duplicates": 0,
    "inserted": 3, "updated": 0, "storageOk": true, "durationMs": 42,
    "finishedAt": "2024-03-04T00:00:00+00:00"
  }
}
```

| HTTP | `error.code` | Trigger |
|---|---|---|
| 422 | `invalid_video_id` | id fails the 11-char rule |
| 404 | `video_not_found` | no stored dataset for this video - including when it is not the active working dataset (Sprint 4.2: reads never activate) |
| 503 | `storage_unavailable` | dataset store failed while reading (logged) |

### Dataset-cache behavior on `GET /api/videos/{video_id}` (Sprint 3)

Response shape is unchanged; only *where* it comes from varies:
`source.cached = true` means it was served from the stored dataset inside
`DATASET_CACHE_TTL_SECONDS` (0 YouTube quota spent). `source.retrievedAt`
then reflects the original acquisition time.

## `GET /api/videos/{video_id}/sentiment` (Sprint 4, additive · Sprint 5 extended)

Sentiment analysis state + aggregates for one video. Read-only, no query
parameters accepted. **The backend owns analysis**: when the stored dataset
has pending rows this request runs the sentiment engine inline (batched,
state-machine transitions) before answering; if a run is already active it
answers `PROCESSING`. The extension only GETs — it never runs a model.
*Sprint 4.3: the extension normally triggers processing through the
background job below and calls this endpoint afterwards (nothing pending →
pure read). The inline path stays for compatibility (§29) and as the
fallback when no job service is wired.*

### Success — 200

```json
{
  "videoId": "jS3cHV43Uxs",
  "status": "PROCESSED",
  "stats": {
    "totalComments": 2500,
    "analyzed": 2400,
    "skipped": 100,
    "positive": 1680,
    "neutral": 480,
    "negative": 240,
    "positivePercent": 70,
    "neutralPercent": 20,
    "negativePercent": 10
  },
  "dataset": {
    "collected": 2500,
    "stored": 2500,
    "analyzed": 2400,
    "skipped": 100,
    "failed": 0,
    "hasMore": false,
    "limitReached": false
  },
  "dominantSentiment": "POSITIVE",
  "emotion": {
    "dominant": "JOY",
    "dominantPercent": 70.0,
    "distribution": {
      "FEAR": { "count": 0, "percent": 0.0 },
      "ANGER": { "count": 48, "percent": 2.0 },
      "ANTICIPATION": { "count": 240, "percent": 10.0 },
      "TRUST": { "count": 480, "percent": 20.0 },
      "SURPRISE": { "count": 120, "percent": 5.0 },
      "SADNESS": { "count": 72, "percent": 3.0 },
      "DISGUST": { "count": 48, "percent": 2.0 },
      "JOY": { "count": 1320, "percent": 55.0 },
      "NEUTRAL": { "count": 72, "percent": 3.0 }
    }
  },
  "intensity": {
    "overall": "MEDIUM",
    "distribution": {
      "LOW": { "count": 600, "percent": 25.0 },
      "MEDIUM": { "count": 1320, "percent": 55.0 },
      "HIGH": { "count": 480, "percent": 20.0 }
    }
  },
  "confidence": { "average": 0.842 },
  "audienceMood": "EXCITED"
}
```

### Field semantics

| Field | Meaning |
|---|---|
| `status` | `NOT_ANALYZED` · `PROCESSING` · `PROCESSED` · `FAILED` (public lifecycle; internal row states are never exposed) |
| `stats.totalComments` | all stored comments for the video (any row state) — equals `dataset.stored` |
| `stats.analyzed` | rows with a `POSITIVE`/`NEUTRAL`/`NEGATIVE` verdict |
| `stats.skipped` | analyzed rows the model's language policy does not support (`UNSUPPORTED_LANGUAGE`) — **never folded into neutral** |
| `stats.*Percent` | multiples of 0.1 from the integer largest-remainder method with **denominator = `analyzed`**; sum to exactly 100 when `analyzed > 0`, all 0 otherwise — no float artifacts (whole values render as `64%`, fractional as `32.5%`) |
| `dataset.collected` / `dataset.stored` | identical by construction: acquisition validates before persisting and every valid comment is written immediately (page → batch → SQLite). Rejected records never enter the dataset — they are reported via `GET /stats` → `lastIngest.fetched/valid/rejected` |
| `dataset.analyzed` / `dataset.skipped` / `dataset.failed` | reconciliation identity: `stored == analyzed + skipped + failed + pending rows` (pending = `READY_FOR_ANALYSIS`/`PROCESSING`, awaiting the next run) |
| `dataset.hasMore` | mirrors the stored acquisition flag: YouTube returned a `nextPageToken` when acquisition stopped |
| `dataset.limitReached` | `true` only when more comments were indicated **while** the stored dataset was at/above `COMMENT_ACQUISITION_MAX_COMMENTS` — the "acquisition limit reached, more may be available" claim requires both |
| `dominantSentiment` | most frequent verdict; ties break `POSITIVE > NEUTRAL > NEGATIVE`; **`null` when nothing was analyzed** |
| `emotion.dominant` | most frequent NRC emotion label over emotion-analyzed rows (`FEAR·ANGER·ANTICIPATION·TRUST·SURPRISE·SADNESS·DISGUST·JOY·NEUTRAL`); ties break by the lexicon's canonical order; **`null` when its axis has no rows**. `NEUTRAL` = zero emotion-word hits (never invented) |
| `emotion.dominantPercent` | that label's share of the emotion-analyzed set (same largest-remainder distribution), multiples of 0.1 |
| `emotion.distribution` | full 9-label vocabulary; `count` + `percent`, **denominator = emotion-analyzed rows (== `stats.analyzed` by the write-path invariant)**; percents sum to 100 when non-empty |
| `intensity.overall` | band with the largest share (`LOW · MEDIUM · HIGH`), ties → the weaker band (never overstated); `null` when no rows |
| `intensity.distribution` | counts + percents over intensity-tagged rows (== `stats.analyzed`); bands derive from \|VADER compound\|: LOW < 0.35 ≤ MEDIUM < 0.70 ≤ HIGH (documented derivation, not model output) |
| `confidence.average` | mean VADER **decision margin** over polarity-analyzed rows, rounded to 3 decimals — a genuine deterministic signal, *not* a calibrated probability; **`null` when nothing analyzed** (never a fabricated 0) |
| `audienceMood` | deterministic rule over real aggregates: `POSITIVE·CALM·EXCITED·MIXED·CONCERNED·NEGATIVE`; **`null` below 10 analyzed comments**; rules documented in `docs/architecture/audience-intelligence.md` (no LLM) |

Sprint 5 fields are **optional with defaults**: a pre-Sprint-5 consumer
ignores them, and an older backend omits them (the extension then renders
only the sentiment section — no client-side invention). Model identity,
per-comment scores, and raw comment text are intentionally **not** exposed
(§8/§26).

### Errors

| HTTP | `error.code` | Trigger |
|---|---|---|
| 422 | `invalid_video_id` | id fails the 11-char rule |
| 404 | `video_not_found` | no stored dataset for this video - including when it is not the active working dataset (Sprint 4.2: reads never activate, never process) |
| 503 | `storage_unavailable` | dataset store failed while reading/processing (logged) |

### Extension consumption (Sprint 4)

- Transport: `HttpSentimentService` → `GET {origin}/api/videos/{id}/sentiment`,
  30 s timeout (first request may include inline processing), no credentials.
- Response bodies are structurally validated before rendering (status enum,
  integer stats, dominant label) — a malformed body becomes
  `upstream_data_invalid`, never garbage UI.
- Lifecycle mapping: `NOT_ANALYZED` → `READY TO ANALYZE`, `PROCESSING` →
  `ANALYZING AUDIENCE`, `PROCESSED` → `ANALYSIS COMPLETE`, `FAILED` →
  `ANALYSIS UNAVAILABLE`; empty dataset → `NO AUDIENCE DATA`; all-skipped →
  `INSUFFICIENT LANGUAGE SUPPORT`; offline backend → `AI SERVICE OFFLINE`.

## `POST /api/videos/{video_id}/analysis` (Sprint 4.3, additive)

Starts a **background analysis job** and returns immediately — the
pipeline (paginated acquisition → batched sentiment → aggregation) runs on
a worker thread, never inside one long-lived HTTP request (§40).

Flow: validate id (422 first) → active-video transition (Sprint 4.2: this
is the extension's explicit activation point) → dedupe an already-running
job for the same video (§21) → persist `QUEUED` → spawn worker → respond.

### Success — 202 Accepted

```json
{ "jobId": "3f2a…", "videoId": "dQw4w9WgXcQ", "status": "QUEUED" }
```

- Triggering again while that job is still running returns the **same**
  `jobId` (its current `status`, e.g. `ACQUIRING`) — Analyze × 3 = one job.
- A finished/failed/cancelled job is replaced by a **new** job id (retry).
- The response never waits for YouTube pages, SQLite batches, or sentiment
  batches.

### Errors

| HTTP | `error.code` | Trigger |
|---|---|---|
| 422 | `invalid_video_id` | id fails the 11-char rule (before any lifecycle change) |
| 503 | `server_not_configured` | no `YOUTUBE_API_KEY` **and** the stored dataset cannot answer without YouTube (fail-fast; a fresh dataset starts fine without a key) |
| 503 | `storage_unavailable` | dataset store failed while starting the job (logged) |

## `GET /api/videos/{video_id}/analysis/status` (Sprint 4.3, additive)

Live progress for the video's **latest** job. Survives browser refreshes
(job state is persisted). Read-only, no query parameters accepted.

### Success — 200

```json
{
  "jobId": "3f2a…",
  "videoId": "dQw4w9WgXcQ",
  "status": "ANALYZING",
  "phase": "SENTIMENT",
  "collected": 2500,
  "stored": 2500,
  "analyzable": 2500,
  "analyzed": 2100,
  "skipped": 350,
  "failed": 50,
  "pending": 0,
  "hasMore": false,
  "errorCode": null,
  "errorMessage": null,
  "createdAt": "2026-09-27T10:00:00+00:00",
  "updatedAt": "2026-09-27T10:00:04+00:00",
  "finishedAt": null
}
```

### Job lifecycle (§6)

```text
QUEUED → ACQUIRING → ANALYZING → COMPLETED
              │            ├─ FAILED       (error_code + error_message set)
              │            └─ CANCELLED    (another video became active,
              └─ CANCELLED                 Sprint 4.2 invalidation)
STALE                            (backend restart mid-job, §32; retryable)
```

Polling stops on any terminal status (`COMPLETED`, `FAILED`, `CANCELLED`,
`STALE`).

### Field semantics

| Field | Meaning |
|---|---|
| `status` / `phase` | job state / where it is working: `NONE`, `ACQUISITION`, `SENTIMENT`, `COMPLETE` |
| `collected` | comments fetched **and persisted** by this job's acquisition (written only after each page lands); equals `stored` when the job reused a fresh dataset |
| `stored` / `analyzable` | rows currently in this video's dataset; every stored row enters the analysis pipeline (language policy resolves inside, as `skipped`) |
| `analyzed` | rows with a polarity verdict — the percentage denominator (§23) |
| `skipped` / `failed` | unsupported-language rows / rows whose processing failed (retryable) |
| `pending` | stored rows not yet through the pipeline (`stored - analyzed - skipped - failed`) |
| `hasMore` | the job's truthful acquisition stop flag (YouTube had a next page) |
| `errorCode` / `errorMessage` | set on `FAILED`/`CANCELLED`/`STALE`: categorized code (`upstream_timeout`, `quota_exceeded`, `storage_unavailable`, `job_cancelled`, `job_interrupted`, …) + short user-presentable copy — never stack traces, secrets, or comment text |
| `finishedAt` | set exactly once, on the terminal transition |

All counts are computed live from the dataset tables at read time (only
`collected`/`hasMore` come from the job row), so they can never drift from
what the store actually holds. If the total is unknown, clients show counts
— never an invented percentage (§7).

### Errors

| HTTP | `error.code` | Trigger |
|---|---|---|
| 422 | `invalid_video_id` | id fails the 11-char rule |
| 404 | `video_not_found` | no job has ever run for this video |
| 503 | `storage_unavailable` | dataset store failed while reading (logged) |

### Extension consumption (Sprint 4.3)

- `HttpJobService.start()` → 202 body, then `waitForTerminal()` polls at
  750 ms with `AbortController`; abort on video switch/unmount (§26/§27).
- Progress renders only from these counts (QUEUED → "Preparing analysis…",
  ACQUIRING → "N comments collected", ANALYZING → "N analyzed of M");
  terminal `COMPLETED` triggers the two fast result GETs; any terminal
  failure renders "ANALYSIS INTERRUPTED · N comments collected" + Retry.
- Store-side guards drop any snapshot whose video id or job id does not
  match the current view, so a superseded job can never repaint the UI.

## Versioning

Sprint 2 = additive contract; Sprint 3 = additive contract (new stats
endpoint, unchanged video endpoint); Sprint 4 = additive contract (new
sentiment endpoint, unchanged video + stats endpoints); Sprint 4.2 =
non-schema change (single active-video dataset - error semantics above,
no field added or removed); Sprint 4.3 = additive contract (new POST
trigger + GET status endpoints, unchanged existing endpoints; CORS now
also allows `POST`). Breaking changes
(renames/removals) require a coordinated update of: this doc ·
`app/models/internal.py` · `src/shared/types.ts` · both test suites.

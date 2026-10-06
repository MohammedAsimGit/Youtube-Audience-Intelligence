# 11 — Data Acquisition Architecture (Sprint 2)

**Status:** as-built · Sprint 2 · replaces nothing from Sprint 0/1 (extends them)

## Where This Sits in the Big Data Pipeline

```text
COLLECT ✅      videos.list + commentThreads.list (official API, backend-only)
INGEST  ✅      request-scoped ingestion into the internal model layer
VALIDATE ✅    pydantic boundary + categorized error taxonomy
CLEAN   ✅      unicode NFC, whitespace collapse, empty-record skip
NORMALIZE ✅    external (YouTube) → internal (project) contract
STORE   ✅      Sprint 3: SQLite dataset store (see architecture/database.md)
PROCESS ✅      Sprint 3: status lifecycle + batch reads (analysis itself → Sprint 4)
ANALYZE ⬜      Sprint 4 (six AI layers)
AGGREGATE ⬜    Sprint 4
CACHE   ⏳ seam ✅ (in-memory TTL/LRU) / persistent ⬜ Sprint 3+
SERVE   ✅      FastAPI endpoints, stable camelCase contract
VISUALIZE ✅    overlay LOADING / DATA ACQUIRED / error states
```

## System Architecture (as built)

```text
┌─────────────────────┐     ┌──────────────────────┐      ┌───────────────────────┐
│  Chrome Extension   │────▶│   FastAPI Backend    │─────▶│  YouTube Data API v3  │
│  (no API keys)      │◀────│   Python + Pydantic  │◀─────│  (official endpoints) │
└─────────────────────┘     └──────────┬───────────┘      └───────────────────────┘
   GET /api/videos/{id}                │
   via HttpAnalysisService             ├─ validate video id (11-char rule)
   + AbortController timeout           ├─ cache lookup (TTL/LRU seam)
                                       ├─ videos.list → metadata
                                       ├─ commentThreads.list × pages (capped)
                                       ├─ normalize (thread→flat comments)
                                       └─ stable VideoDataResponse contract
```

## Backend Directory Structure

```text
backend/
├── app/
│   ├── main.py               # create_app(): CORS, error handler, lifespan, wiring
│   ├── api/routes.py         # GET /health, GET /api/videos/{video_id}
│   ├── core/
│   │   ├── config.py         # pydantic-settings: YOUTUBE_API_KEY, caps, CORS, TTL
│   │   ├── errors.py         # categorized AcquisitionError taxonomy
│   │   └── logging.py        # structured logging (secret-free policy)
│   ├── models/internal.py    # internal contract (VideoDataResponse, Comment, …)
│   ├── schemas/youtube.py    # external response models (validation boundary)
│   ├── clients/youtube.py    # ONLY module touching Google's API (+ error mapping)
│   ├── services/
│   │   ├── acquisition.py    # orchestration: cache → metadata → comments → contract
│   │   └── cache.py          # TTL + LRU in-memory cache (the seam)
│   └── utils/normalize.py    # whitespace/unicode, parse_int/dt, thread→comments
├── tests/                    # 21 tests, fake client injection (no network)
├── requirements.txt
├── pyproject.toml            # pytest config
└── .env.example              # settings template (real .env git-ignored)
```

Dependency rule: `routes → services → clients/schemas/models`. Only
`clients/youtube.py` imports httpx or knows Google's URL/params.

## Request Lifecycle (happy path)

**Sprint 4.3 — the extension's primary flow (no single long request):**

```text
1. POST /api/videos/{id}/analysis           (HttpJobService, 10s timeout)
      → validate → active-video switch → dedupe running job
      → 202 {jobId, status:"QUEUED}          (returns immediately)
2. GET  /api/videos/{id}/analysis/status    (poll every 750 ms, abortable)
      QUEUED → ACQUIRING → ANALYZING → COMPLETED | FAILED | CANCELLED | STALE
      progress = real counts (collected/stored/analyzed/skipped/failed/pending)
3. worker thread runs the pipeline below (steps 3–7) in the background:
      videos.list + paginated commentThreads.list, page → batch → SQLite,
      then batched sentiment — each write generation-guarded (Sprint 4.2)
4. on COMPLETED: GET /api/videos/{id} + GET /api/videos/{id}/sentiment
      (both fast L2 reads) → Chrome renders the dashboard
```

**Legacy synchronous path (kept for compatibility, byte-compatible):**

```text
1. GET /api/videos/dQw4w9WgXcQ          (HttpAnalysisService, 15s timeout)
2. validate_video_id                     → 422 invalid_video_id on failure
3. cache lookup  key="video:{id}"        → hit: return, source.cached=true  (0 quota)
4. videos.list   part=snippet,statistics → 1 quota unit
5. loop commentThreads.list              → 1 quota unit per page
      page size: MAX_COMMENTS_PER_REQUEST (default 100, YouTube max)
      stop when: collected >= COMMENT_ACQUISITION_MAX_COMMENTS (default 5000)
                 OR nextPageToken absent OR MAX_API_PAGES reached
                 OR a page token repeats (loop guard)
                 OR the job was cancelled / video superseded (Sprint 4.3)
6. each page, IN ORDER: validate → normalize → language detect → dedup
      → batched SQLite write          (incremental: page → persist → next page)
7. build VideoDataResponse (camelCase) → cache.set (deep copy) → return
8. Chrome renders DATA ACQUIRED          (READY → LOADING → COMPLETE)
```

Why the split (measured, Sprint 4.3 diagnosis): 5 000 comments ≈ 43 pages
≈ **74 s** end-to-end — far beyond the extension's 15 s fetch timeout —
while the event loop stayed responsive throughout (the bottleneck was the
request *lifetime*, not CPU or SQLite). The job model removes the lifetime
entirely; the legacy GET remains correct for small/fresh datasets and
older consumers.

Page size ≠ dataset limit: `MAX_COMMENTS_PER_REQUEST` is the **page size**
(max results per `commentThreads.list` call, ≤ 100), while
`COMMENT_ACQUISITION_MAX_COMMENTS` is the **dataset limit per video per
acquisition run**. The loop never silently stops at one page.

## Error Taxonomy (internal → HTTP → user copy)

| Internal error | HTTP | `error.code` | User sees |
|---|---|---|---|
| `VideoIdInvalid` | 422 | `invalid_video_id` | Invalid video ID. |
| `VideoNotFound` (empty `items`) | 404 | `video_not_found` | This video was not found or is unavailable. |
| `CommentsDisabled`* | 200 | — (`comments.status="disabled"`) | Comments UNAVAILABLE block (not an error) |
| `QuotaExceeded` | 429 | `quota_exceeded` | YouTube API quota is exhausted… |
| `UpstreamTimeout` | 504 | `upstream_timeout` | The request timed out. |
| `UpstreamUnavailable` | 502 | `upstream_unavailable` | YouTube is currently unreachable… |
| `UpstreamUnstable` (malformed body) | 502 | `upstream_data_invalid` | Received an unexpected response… |
| `MissingApiKey` | 503 | `server_not_configured` | The analysis service is not configured yet. |
| network failure in Chrome | — | `network_error` | Backend is unreachable… (client-side) |

\* comments-disabled is deliberately a **success with an explicit empty state**
(the video and its metadata are still valid data), verified against Google's
error reference (`403 / commentsDisabled`).

No stack traces, upstream messages, or credentials ever cross the boundary —
details are server-logged only.

## Cache Boundary (quota protection)

```text
request → cache.get("video:{id}")
             ├─ hit  → return copy, source.cached = true   (0 YouTube units)
             └─ miss → YouTube acquisition → cache.set(TTL)
```

- Implementation: in-memory TTL (default 600s) + LRU cap (128 entries),
  thread-safe. **This is a seam, not the final architecture** — persistent
  caching/storage lands in Sprint 3 (TDR-06/TDR-07).
  *Sprint 3 update:* the dataset store + `DATASET_CACHE_TTL_SECONDS`
  freshness layer now sits behind this seam (see architecture/caching.md).
- Effect: repeated Analyze clicks (or another user) cost 0 quota within TTL.

## Quota Design (verified facts drive the design)

| Operation | Units | Policy |
|---|---|---|
| `videos.list` | 1/request | once per acquisition (cached afterwards) |
| `commentThreads.list` | 1/page | bounded: ≤ `MAX_API_PAGES` (100) pages and ≤ `⌈COMMENT_ACQUISITION_MAX_COMMENTS / page size⌉` pages; typical video ⇒ a handful of pages |
| `search.list` | 100 | **never used** — ids come from the extension's URL parsing |
| Daily project quota | 10,000 | exhaustion → categorized 429, cache reduces burn |

Both bounds are configuration (`COMMENT_ACQUISITION_MAX_COMMENTS`,
`MAX_API_PAGES`), never an unbounded download. Interrupted acquisitions keep
the pages already persisted and report `hasMore` truthfully.

## Security Model

- `YOUTUBE_API_KEY` exists only in `backend/.env` (git-ignored) → pydantic
  Settings → client constructor. It is never logged (only request params are),
  never serialized, never sent to Chrome (test asserts this).
- Extension: zero secrets; `host_permissions` limited to loopback
  (`127.0.0.1`, `localhost`) so the content script may fetch the local backend.
- Backend CORS: exact dev origins + regex `^chrome-extension://[a-p]{32}$`;
  **never `*`** (verified live: extension origin → 200 + ACAO echo; evil origin
  → no ACAO header).
- Input validation before any upstream call; external responses validated by
  pydantic; malformed → categorized 502, never a crash.

## Logging Events (structured, secret-free)

`request received` → `video id validated` → (`cache hit` | `youtube videos.list started`)
→ `comment page retrieved` (page #, count, units) → `pagination detected` →
`response normalized` → `request completed` (counts, elapsed ms) · failures log
`acquisition error {code}`. Never: keys, auth headers, comment text, user data.

## Chrome-Side State Wiring

```text
Sprint 4.3 (primary):
Analyze click → POST /analysis → 202 → store.beginJob(videoId, jobId)
             → poll status (750 ms, AbortController):
                  ACQUIRING snapshot → status 'loading'  (N comments collected)
                  SENTIMENT snapshot  → status 'analyzing' (N analyzed / M)
             → COMPLETED: GET video data + GET sentiment → 'complete'
             → FAILED/CANCELLED/STALE: clearAcquisition + failJob → 'error'
                  ("ANALYSIS INTERRUPTED · N comments collected" + Retry)
             → video change: abort polling, store clears job + acquisition
             → stale snapshot (wrong video or wrong jobId): discarded, never rendered
Legacy (kept): Analyze click → store.setStatus('loading') + beginAcquisition
             → HttpAnalysisService.analyze({videoId, pageKind})
             → success: completeAcquisition + status 'complete'   ("DATA ACQUIRED" chip)
             → error:   failAcquisition(code, friendly message) + status 'error'
             → stale response (video changed mid-flight): discarded, never rendered
Video change → store clears acquisition + notice + job, status → 'ready'   (FR-11)
```

## Known Limitations (status as of Sprint 4.3)

- In-memory cache only (lost on restart; single instance).
- *Resolved (Sprint 3):* comments persist through the ingestion pipeline into
  the SQLite dataset store; Sprint 4.1 writes them incrementally, page by page.
- *Resolved (Sprint 4.3):* no HTTP request waits for the full pipeline —
  analysis is a persisted background job with polled progress; YouTube
  requests carry bounded retries with exponential backoff
  (`YOUTUBE_RETRY_ATTEMPTS`, `YOUTUBE_RETRY_BACKOFF_SECONDS`, transient
  failures only). One job survives a browser refresh; a backend restart
  surfaces as `STALE` (retryable), never a stuck spinner.
- The httpx client itself is still synchronous (FastAPI threadpool + the
  job worker thread) — fine at MVP volume.
- `duration` unavailable from `videos.list` snippet (needs `contentDetails`
  in a later metadata pass if required).
- Replies collected per thread within the cap; deep reply chains not fully
  expanded (documented contract behavior).
- Acquisition beyond `MAX_API_PAGES` (100) pages requires raising that
  safety bound together with `COMMENT_ACQUISITION_MAX_COMMENTS`.
- Single-process job model (no distributed workers — deliberately, §3/§2).

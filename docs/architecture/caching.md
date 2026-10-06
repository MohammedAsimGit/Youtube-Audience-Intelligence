# Caching & Freshness (Sprint 3)

Three layers in front of the YouTube API. Each exists to save **quota and
latency**, and none can ever serve another video's data — every key and
query is scoped by `video_id` / `comment_id`.

```text
Request ──▶ L1 in-memory TTL/LRU      app/services/cache.py
              │ miss / expired
              ▼
           L2 dataset store           app/services/dataset.py
              │ absent / stale
              ▼
           L3 YouTube Data API        app/services/clients (acquisition)
              │
              ▼
           ingest (validate → normalize → dedup → persist) ──▶ back to client
```

## Layer 1 — hot memory cache

`TTLCache` (thread-safe TTL + LRU, Sprint 2 unchanged): key
`video:{id}`, TTL `CACHE_TTL_SECONDS` (default 600), size bound
`CACHE_MAX_ENTRIES`. Sub-millisecond repeat responses; stores a deep copy of
the contract so callers can never mutate the cache.

**Single-active policy (Sprint 4.2, §29/§30):** when
`DatasetService.ensure_active` detects a video switch, the acquisition
service calls `TTLCache.clear()` before acquiring the new video. The L1
mirror therefore follows the same semantics as the database — at most the
*current* video's dataset is in memory — old videos can never leak through
the cache and memory does not grow as the user browses. Same-video
revisits never clear it (L1 freshness still applies). Superseded
(acquisition raced by a switch) results are never `set` into L1 either.

## Layer 2 — stored dataset with a freshness policy (§24/§25)

`DatasetService.get_fresh_response(video_id)` rebuilds the exact
`VideoDataResponse` contract from SQLite when:

```text
now − last_acquired_at  <  DATASET_CACHE_TTL_SECONDS      (default 3600)
```

- The rule lives in **one place** (`DatasetService._is_fresh_row`) — no
  freshness logic is duplicated anywhere else. `DATASET_CACHE_TTL_SECONDS=0`
  disables freshness (always re-acquire), which the tests use to prove both
  directions.
- A fresh serve sets `source.cached = true` and `source.retrievedAt =
  last_acquired_at` — the client can tell, and never pays quota for it.
- Stored-but-stale data is **never served as fresh**: it triggers a real
  re-acquisition (velocity: new comments arrive on later cycles).
- **Isolation guarantee:** freshness is looked up by `video_id`; the
  response is assembled only from that video's rows. There is no code path
  that can surface another video's dataset (tests:
  `TestVideoIsolationThroughApi`, `test_switch_replaces_the_working_dataset`).
  Since only the active video *has* rows after Sprint 4.2, a non-active id
  is simply a stale/absent L2 entry → the endpoint answers its documented
  404 instead of resurrecting data (reads never activate, §40).

## Layer 3 — YouTube Data API

Quota-aware acquisition (Sprint 4.1): `videos.list` = 1 unit,
`commentThreads.list` = 1 unit/page with page size `MAX_COMMENTS_PER_REQUEST`
(100), paginated until `COMMENT_ACQUISITION_MAX_COMMENTS` (5000 per video
per run), YouTube running out, or `MAX_API_PAGES`.

## Freshness vs the acquisition limit (Sprint 4.1 audit, §34)

The two settings answer different questions and never overwrite each
other:

- `DATASET_CACHE_TTL_SECONDS` decides **when** a stored dataset is re-fetched.
- `COMMENT_ACQUISITION_MAX_COMMENTS` decides **how much** one run fetches.
- `comments.hasMore` / `dataset.hasMore` in the response are the stored
  truth from the last run — a cached serve can honestly say "more exists"
  without re-fetching, and the stored `count` always reflects what was
  actually acquired (a 1 000-row dataset is never reported as 5 000).
- Partial acquisitions (mid-run quota/timeout) persist completed pages and
  refresh `last_acquired_at`; the freshness window then serves the partial
  dataset with `hasMore = true` until the TTL elapses and a full
  re-acquisition runs. The limit can therefore never cache an incomplete
  dataset *forever* — only for the configured TTL.
- Re-acquisition never deletes rows: unchanged comments are refreshed,
  new ones inserted (`comment_id` dedup), sentiment verdicts preserved.

## Policy summary

| Question | Answer |
|---|---|
| Where is freshness defined? | `DATASET_CACHE_TTL_SECONDS` + `DatasetService` (single source of truth) |
| How long is data trusted? | 1 hour by default (config-driven, never hardcoded) |
| What happens when it expires? | Full re-acquisition + incremental dedup merge |
| Is stale data ever shown? | Only with `cached=true` *inside* the TTL window; outside it, re-acquires |
| Cross-video leakage possible? | No — every layer keyed/filtered by video id |
| What happens on a video switch? | L1 `clear()`, L2 rows of the old video deleted atomically — only the new video can be served |
| Does memory grow with visited videos? | No — one active dataset in L1, one in L2 (Sprint 4.2) |

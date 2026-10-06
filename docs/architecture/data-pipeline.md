# Data Pipeline (Sprint 3)

The implemented ingestion path — every stage maps to real code and emits a
structured log event (§39). No stage fabricates data.

```text
YouTube Data API v3            (official API only, key server-side)
        │
        ▼
Acquisition Service            app/services/acquisition.py
  ensure_active(video_id)      ACTIVE_VIDEO_SWITCHED (Sprint 4.2: atomic
  → generation captured          switch on video change, cache cleared,
  videos.list + paginated      ACQUISITION_STARTED / ACQUISITION_COMPLETED
  commentThreads.list          (page size: MAX_COMMENTS_PER_REQUEST=100,
        │                        dataset limit: COMMENT_ACQUISITION_MAX_COMMENTS
        │                        =5000, safety bound: MAX_API_PAGES=100,
        │                        repeating-token loop guard)
        ▼  one page at a time (page → persist → next page)
Ingestion Service              app/services/ingestion.py (IngestionSession)
        │                        generation-guarded: superseded sessions
        │                        drop pages + finish() writes nothing
        ├──▶ Validation        app/services/validation.py   VALIDATION_COMPLETED
        │      structured issues, bad records rejected (never stored)
        │
        ├──▶ Normalization     app/services/text_pipeline.py NORMALIZATION_COMPLETED
        │      HTML entities → invisibles → NFC + whitespace
        │
        ├──▶ Language          app/services/language.py     (metadata per comment)
        │
        ├──▶ Deduplication     repository upsert             DEDUPLICATION_COMPLETED
        │      in-page collapse + comment_id conflict-refresh
        │
        └──▶ Persistence       app/db/repository.py         PERSISTENCE_COMPLETED
               batched executemany (COMMENT_BATCH_SIZE)     PERSISTENCE_FAILED
               + video row (per page) + ONE aggregated      PROCESSING_READY
               ingestion_runs row at finish()
        ▼
Dataset Store (SQLite) ── reads ──▶ DatasetService (freshness / stats)
        ▼
READY_FOR_ANALYSIS  ─────────────▶ Sprint 4 consumes this dataset
```

## Stage details

### 1. Validation (`validation.py`)
Every comment must pass before persistence: id present/length/whitespace,
`video_id` matches the acquisition (video isolation at the pipeline level),
text present and ≤ 10 000 chars, timestamps present/aware/not-in-the-future
(5 min skew), `like_count ≥ 0`, reply ⇒ `parent_id` present and ≠ own id,
author ≤ 256 chars. Failures become `ValidationIssue(field, reason)` —
structured, loggable, **never containing raw text**. One bad record never
fails the batch (`validate_comments` splits valid/rejected).

### 2. Normalization + cleaning (`text_pipeline.py`)
`raw → html.unescape → strip presentation-only invisibles → NFC + whitespace
collapse → normalized`. Deliberately preserved for future sentiment:
emojis (incl. ZWJ sequences ❤️‍🔥👨‍👩‍👧), punctuation runs, hashtags, mentions,
accents, all scripts, and ZWNJ (linguistically meaningful). Raw input is
never mutated; `raw_text` in the DB stays verbatim. A comment that cleans to
empty (e.g. only invisible characters) is rejected as
`empty_after_normalization` — empty rows are never invented.

### 3. Language metadata (`language.py`)
`langdetect` (pure Python), `DetectorFactory.seed = 0` for determinism,
top candidate must reach **0.90 confidence** or the label is `unknown`;
no alphabetic content ⇒ `unknown` without consulting the detector. Output is
ISO 639-1 (`en`, `hi`, `kn`, …; `zh-cn/zh-tw` folded to `zh`). Known
limitation: short/transliterated text (e.g. Latin-script Hinglish) is
unreliable for any statistical detector — the threshold converts that into
`unknown` instead of a fabricated label. No translation happens (§12).

### 4. Deduplication (`repository.upsert_comments`)
Primary key = `comment_id`. Within a fetch, repeated ids collapse to the
latest occurrence (counted as `in_batch_duplicates`). Against the store, a
conflict **refreshes** metadata + `last_seen_at` and reports `updated` —
never a second row. Text is *not* part of dedup (different users may write
identical comments).

### 5. Persistence (incremental, Sprint 4.1)
Each YouTube page runs stages 1–4 and is written in `COMMENT_BATCH_SIZE`
chunks (`executemany` + one commit per chunk) **before the next page is
fetched** — the pipeline never buffers the whole comment dataset in memory
(§12 big-data design: `page → batch → persist → next page`). The parent
video row (metadata + `comments_status` + `has_more` +
`first/last_acquired_at`) is refreshed per page and finalized by
`IngestionSession.finish()`, which also writes **ONE aggregated
`ingestion_runs` quality row for the whole acquisition** (not one row per
page), so the §27 invariants describe the complete run. Any `sqlite3.Error`
degrades to **best-effort persistence**: `PERSISTENCE_FAILED` is logged,
`storage_ok=false` is recorded for the session (no run row claims success
for a partially written acquisition), and the acquired data is still served.

An upstream failure mid-acquisition (quota/timeout) keeps every page already
persisted, finalizes the run truthfully (`ACQUISITION_INTERRUPTED`), then
surfaces the original categorized error — later requests can serve the
partial dataset from the freshness layer while `hasMore` reports that more
comments exist.

## Single active dataset & race safety (Sprint 4.2, §11–§13)

The pipeline writes into the **active video's working dataset only**:

1. `AcquisitionService.get_video_data` first calls
   `DatasetService.ensure_active(video_id)` — on a video change this runs the
   atomic switch (previous video's comments/runs/row deleted, new row
   active, generation bumped) and clears L1; on the same video it is a no-op
   and the freshness policy decides.
2. The activation's **generation token** is threaded into the ingestion
   session and into every repository write. Each write re-verifies
   `video_id AND is_active=1 AND dataset_generation = generation`
   **inside the same lock as the write**.
3. If the user switches mid-run, the guard fails → the session marks itself
   `superseded`: subsequent pages are dropped, `finish()` records **no**
   video refresh and **no** `ingestion_runs` row, L1 is not populated, and
   the outcome carries `superseded=true` (`ACQUISITION_SUPERSEDED` /
   `ACQUISITION_COMPLETED` log fields). Rapid A→B→C→D ends with D only;
   A→B→A re-acquires A fresh (stale rows never resurrect).

Only the dataset *transition* is destructive — per-page batched writes,
keyset reads, dedup, and SQL aggregation are unchanged (§58).

## Data quality (§27) — real numbers only

Per run: `fetched`, `valid`, `rejected`, `duplicates`, `inserted`,
`updated`, `storage_ok`, `duration_ms`. Invariants (tested):

```text
fetched == valid + rejected
valid   == inserted + duplicates      (when storage_ok)
```

Numbers surface in the `ingestion_runs` table and the stats endpoint's
`lastIngest`.

## Observability events (§39)

| Event | Extra fields |
|---|---|
| `ACQUISITION_STARTED` / `ACQUISITION_COMPLETED` | video_id, comments, fetched, pages, has_more, status, storage_ok, elapsed_ms |
| `comment page retrieved` | video_id, page, comments, units, storage_ok |
| `PAGINATION_TOKEN_LOOP` / `ACQUISITION_INTERRUPTED` | video_id, page / pages, fetched, error class |
| `VALIDATION_COMPLETED` / `VALIDATION_REJECTED` | video_id, valid, rejected, comment_id, `field:reason` list |
| `NORMALIZATION_COMPLETED` | video_id, normalized |
| `DEDUPLICATION_COMPLETED` | video_id, in_batch_duplicates, already_stored |
| `PERSISTENCE_COMPLETED` / `PERSISTENCE_FAILED` | video_id, inserted, updated, batches / error class |
| `ACTIVE_VIDEO_SWITCHED` | video_id, previous_video_id, generation (Sprint 4.2) |
| `ACQUISITION_SUPERSEDED` | video_id, generation — stale run discarded after a switch |
| `ANALYSIS_JOB_STARTED` / `ANALYSIS_JOB_REUSED` | video_id, job_id, generation (Sprint 4.3) |
| `ANALYSIS_JOB_COMPLETED` | video_id, job_id, processed, failed, skipped, requeued |
| `ANALYSIS_JOB_FAILED` / `ANALYSIS_JOB_INTERNAL_ERROR` / `ANALYSIS_JOB_STORE_FAILURE` | video_id, job_id, code or error class — never stack traces |
| `ANALYSIS_JOBS_SWEPT` | recovered — startup sweep marked interrupted jobs STALE (§32) |
| `PROCESSING_READY` | video_id, ready |
| `DATASET_SERVED` | video_id, comments, age_seconds |

Never logged: API keys, raw comment text, stack traces in responses.

## Pagination & limits (§17/§18, Sprint 4.1)

Page size and dataset limit are **different settings**:

| Setting | Default | Role |
|---|---|---|
| `MAX_COMMENTS_PER_REQUEST` | 100 | page size: max results per `commentThreads.list` call (YouTube hard cap: 100) |
| `COMMENT_ACQUISITION_MAX_COMMENTS` | 5000 | dataset limit per video per acquisition run |
| `MAX_API_PAGES` | 100 | pathological-token safety bound; effective ceiling = `min(limit, page size × MAX_API_PAGES)` |

The loop walks `nextPageToken` until YouTube runs out, the dataset limit is
reached, the page bound is hit, a token repeats (loop guard — a repeated
token never claims more data exists), **or the owning job was
cancelled/superseded** (Sprint 4.3: `should_cancel` is polled between
pages, so a dead dataset costs no further quota — the generation guard
inside every write remains the hard barrier). Each page's comments flow
through the same batch pipeline and are persisted before the next page is
requested;
re-acquisitions merge incrementally via dedup (`comment_id`), never
duplicating rows. `comments.hasMore` is `true` only when YouTube offered a
next page when acquisition stopped — availability is never inferred.

## Background acquisition (Sprint 4.3, §4)

The pipeline above is unchanged; **where it runs** is not. The analysis
trigger (`POST /api/videos/{id}/analysis`) spawns a worker that executes
this exact page → batch → persist loop off the request path:

- `VideoDataService.acquire_dataset()` is the single L3 code path shared
  by the legacy GET and the job (no duplicate pipeline, no second
  ingestion service).
- After each **persisted** page the job writes real progress
  (`update_job_progress(collected, has_more)`) — progress is only ever
  claimed for data already in the store.
- A fresh, complete dataset short-circuits acquisition entirely
  (`DatasetService.needs_acquisition`, 0 quota) — but a *partial* run
  (hasMore while under the configured limit) counts as incomplete, so a
  retry job **continues** collecting instead of serving the partial result
  (§22).
- Finishing a real acquisition clears L1 so the next read serves the
  freshly stored dataset, never stale memory.

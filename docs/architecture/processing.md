# Processing & Batch Lifecycle (Sprint 3 → Sprint 4)

## Per-comment state machine

Implemented in `backend/app/models/processing.py`; transitions are enforced
in code *and* in the database path
(`DatasetRepository.update_processing_status` validates under the same lock
as the write — an illegal jump raises and changes nothing).

```text
ACQUIRED ──▶ VALIDATED ──▶ NORMALIZED ──▶ DEDUPLICATED ──▶ READY_FOR_ANALYSIS
    │             │             │                │                 │
    └─────────────┴─────────────┴────────────────┴────▶ FAILED ◀────┘
                                                              │
                          PROCESSING ◀── READY_FOR_ANALYSIS   │
                              │                               │
                              ▼                               │
                          PROCESSED      FAILED ──▶ READY_FOR_ANALYSIS (requeue)
```

- **Stage states** (`ACQUIRED → VALIDATED → NORMALIZED → DEDUPLICATED`)
  express pipeline position for staged/batch workers and are fully
  transition-tested (including the §33 path
  `ACQUIRED → VALIDATED → NORMALIZED → READY_FOR_ANALYSIS`).
- **Sprint 3's synchronous ingestion validates, normalizes, and dedups
  *before* persistence**, so rows are written directly at
  `READY_FOR_ANALYSIS` — invalid records never exist in the store at all.
- **Sprint 4 performs the analysis**: `services/sentiment.py` claims a batch
  (`READY_FOR_ANALYSIS → PROCESSING` via `claim_for_processing`), classifies
  it, then persists verdicts + final status in one write
  (`save_sentiment_results`: `PROCESSING → PROCESSED`, or `→ FAILED` for a
  per-row failure). Every step validates the machine under the database
  lock; the service holds no SQL.
- **Requeue**: `FAILED → READY_FOR_ANALYSIS` (pre-existing edge) runs at the
  start of each analysis pass, so recoverable failures retry on the next
  GET. Orphaned `PROCESSING` rows (crashed run) take
  `PROCESSING → FAILED → READY_FOR_ANALYSIS`; rows claimed by a *live* run
  are never touched.
- **Generation guard (Sprint 4.2, §38/§39)**: a run captures the active
  video's `dataset_generation` when it starts and re-checks it
  (`video_id AND is_active=1 AND dataset_generation = generation`) before
  every batch flush. If the user switched videos meanwhile, the run logs
  `SENTIMENT_RUN_SUPERSEDED`, aborts **before** committing the batch, and
  the claimed rows are requeued through the legal path on the next run — a
  stale analysis can never write verdicts into a dataset that replaced it
  (tested: A → B → A mid-run leaves the new A rows untouched at
  `READY_FOR_ANALYSIS`). A read of a non-active video id likewise never
  claims or processes anything (§40: reads never activate).

### What each stored state answers (§9)

| Question | Where |
|---|---|
| What has been acquired? | `videos` row + `last_acquired_at` |
| What is ready for analysis? | `processing_status = READY_FOR_ANALYSIS` (stats endpoint reports counts per state) |
| What failed / needs reprocessing? | `processing_status = FAILED`, plus `storage_ok = 0` ingestion runs |
| What was processed? | `PROCESSED` with `sentiment_label` set (Sprint 4 writes it); re-fetch of unchanged text preserves it, edited text resets to `READY_FOR_ANALYSIS` **and clears the verdict** |

## Batch processing (§16)

- **Write path:** ingestion hands rows to the repository in
  `COMMENT_BATCH_SIZE` chunks (default 500); each chunk is one
  `executemany` + commit. Sprint 4.1 makes this page-granular: each YouTube
  page is validated/normalized/deduped and written **before** the next page
  is fetched, so raising `COMMENT_ACQUISITION_MAX_COMMENTS` to thousands
  exercises exactly the same code with bounded memory.
- **Read path:** `iter_comments` uses **keyset pagination**
  (`comment_id > last … LIMIT batch`), never `OFFSET`, never one unbounded
  cursor — memory per step is bounded by the batch size regardless of
  dataset size.
- **Aggregation path:** statistics are computed by SQL
  (`COUNT/COUNT(DISTINCT)/SUM/MIN/MAX/GROUP BY`); Python never loads a
  dataset just to count it. Sprint 4 adds
  `GROUP BY sentiment_label` over the same bounded reader.
- **No distributed infrastructure** (§37): one process, one connection, one
  lock — deliberately matched to the actual deployment, with the Postgres
  seam documented in `database.md`. Sprint 4's analysis run adds one
  in-process run lock on top (one run at a time; concurrent GETs report
  `PROCESSING`).

## Dataset readiness

A run ends with `PROCESSING_READY` and a persisted `ingestion_runs` row, so
at any moment the stats endpoint answers: total/unique comments, replies vs
top-level, oldest/newest timestamps, per-state counts, and the last run's
quality numbers — the exact hand-off surface Sprint 4's analysis engine
consumes (it reads only `READY_FOR_ANALYSIS` rows, in
`COMMENT_BATCH_SIZE` batches, and moves them through the machine above).
Sprint 4.2 scopes this whole surface to the **single active video**: switching
deletes the old video's rows *and* its `ingestion_runs` in one transaction,
so `lastIngest` can never report another video's run (§28).
See `sentiment-analysis.md` for the analysis side of the lifecycle.

## Background analysis job (Sprint 4.3, §6)

The full pipeline (acquisition → sentiment → aggregation) no longer runs
inside one HTTP request. `POST /api/videos/{id}/analysis` persists a job
row and hands the work to one daemon worker thread:

```text
POST → 202 {jobId, status: QUEUED}          (returns immediately, §9)
              │
       worker thread (services/jobs.py)
              │
   ACQUIRING  │  page → validate → normalize → dedup → SQLite →
              │  progress write → next page        (still §11-batched)
              │  guarded by the Sprint 4.2 generation + cancel event
              │
   ANALYZING  │  READY batch → claim → classify → persist → next batch
              │  (same per-comment machine above; runs OFF the event loop)
              │
   COMPLETED | FAILED | CANCELLED | STALE
```

- **Storage**: one small `analysis_jobs` table (job id, video id,
  generation, status, phase, collected, has_more, error, timestamps). It is
  deliberately **not** FK-bound to `videos`: the job record must survive an
  active-video switch as the truthful history of what ran.
- **Transitions** are guarded exactly like dataset writes: progress/phase
  writes touch only non-terminal rows, and the terminal write fires at most
  once — a worker that wakes up after the switch already cancelled its job
  gets rowcount 0 and stands down (no resurrection).
- **Invalidation reuses Sprint 4.2 wholesale** (§5): `switch_active_video`
  cancels every non-terminal job **inside the same transaction** that
  replaces the dataset; the worker additionally re-checks the generation
  and its cancel event between pages/batches. There is no second,
  independent cancellation mechanism.
- **Restart recovery** (§32): a lifespan startup sweep marks any job still
  `QUEUED/ACQUIRING/ANALYZING` as `STALE` (`job_interrupted`) before the
  app serves traffic, so the UI can never stay pinned at "analyzing" after
  a restart — it shows the stale state and offers Retry.
- **Event loop**: everything (including CPU-bound VADER scoring) runs on
  the worker thread; FastAPI keeps answering `/health` throughout a large
  analysis (measured: p99 < 0.3 s during a 5 000-comment run).

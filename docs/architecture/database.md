# Database Decision — TDR-06 (Sprint 3)

**Status: decided (implemented).** Chosen: **SQLite** through Python's
stdlib `sqlite3`, reached only via the repository layer
(`backend/app/db/repository.py`).

```text
DATABASE_URL = sqlite:///./data/sentiment.db     (file, default)
DATABASE_URL = sqlite:///:memory:                (tests)
```

## Requirements this had to satisfy

| Requirement | Implication |
|---|---|
| Video-level comment grouping | Strong per-video filtering + FK-style integrity |
| Dedup on `comment_id` | Unique constraint enforced **by the store**, atomically |
| Text storage | Comments are short UTF-8 documents (≤ 10k chars) — no blob/FTS need yet |
| Aggregation (stats, status counts) | `GROUP BY` / `MIN` / `MAX` must be push-down to SQL |
| Future sentiment/topic results | Columns + per-row processing status per video |
| Single active working dataset (Sprint 4.2) | Explicit active-video state + transactional cleanup on switch |
| Dev simplicity | No server process, no credentials, works offline |
| Deployment compatibility | Backend runs as one `uvicorn` process on one machine |
| Python/FastAPI compatibility | Sync threadpool handlers → sync driver with a lock fits |

## Why SQLite

1. **The workload is a textbook fit.** Reads dominate (serve datasets),
   writers are rare (page-granular transactions since Sprint 4.1), datasets
   are per-video and bounded by `COMMENT_ACQUISITION_MAX_COMMENTS`
   (default 5000) with an architectural path to 10k+ rows. SQLite
   ACID-upserts on the primary key
   give *real* dedup — the exact guarantee §14 requires.
2. **Zero operational surface.** No server to install/start/patch, no
   credentials to rotate; the file is created lazily on first query and is
   git-ignored (`backend/.gitignore`: `data/`, `*.db`). Tests use
   `sqlite:///:memory:` for full isolation per test app.
3. **Zero extra runtime dependencies.** The driver ships with Python; the
   only `requirements.txt` additions so far are `langdetect` (Sprint 3
   language metadata) and `vaderSentiment` (Sprint 4 sentiment engine —
   pure Python, see `sentiment-analysis.md`).
4. **Aggregates stay in the database.** Dataset statistics
   (`COUNT`, `COUNT(DISTINCT)`, `SUM(is_reply)`, `MIN/MAX(published_at)`,
   `GROUP BY processing_status`) never load rows into Python.

## Active-video lifecycle (Sprint 4.2)

The store keeps **exactly one active video's working dataset** — the
product is a real-time intelligence layer for the currently selected video,
not (yet) a historical analytics platform (Sprint 9 boundary, §57).

**Decision (§7)**: smallest-fit hybrid of Option A + Option B —

- **State lives on the `videos` row**: `is_active INTEGER` (0/1 marker) and
  `dataset_generation INTEGER` (monotonic token bumped on every switch).
  Persistent state is the source of truth — never a process-local Python
  variable (§8), so a future multi-worker deployment stays consistent.
- **Transition lives in `DatasetService.ensure_active(video_id)`** — the
  existing dataset service gained one lifecycle method; no new table, no
  new service, no second repository, no new cache.

```text
GET /api/videos/{B}   (B ≠ active A)
        │
DatasetService.ensure_active(B)
        │  single repository call, one transaction:
        ▼
switch_active_video(B):
  BEGIN
    generation = MAX(dataset_generation) + 1        # monotonic (§12)
    DELETE FROM ingestion_runs WHERE video_id != B  # no FK → explicit
    DELETE FROM videos       WHERE video_id != B    # cascades comments
                                                    # (incl. sentiment cols)
    keep/INSERT B row: is_active=1, dataset_generation=generation
  COMMIT          (any sqlite3.Error → ROLLBACK, re-raise, §32)
        │
        ▼
cache.clear()  →  acquire B  →  analyze B
```

Key rules (all tested in `tests/test_active_lifecycle.py`):

- **Same video → no switch** (§19/§20): freshness alone decides;
  `switch_active_video` is idempotent for the active id.
- **Adoption**: a pre-4.2 / stray row for the requested video is *adopted*
  (its fresh dataset preserved) while every other dataset is removed —
  upgrading never discards a still-fresh dataset. A brand-new activation
  inserts a placeholder row with epoch `last_acquired_at` so it is
  deterministically stale (§18: first load always acquires).
- **Race safety** (§11–§13): every acquisition write
  (`upsert_video`, `upsert_comments`, `record_ingestion_run`) accepts the
  generation captured at activation and re-verifies
  `video_id AND is_active=1 AND dataset_generation=?` **inside the same
  lock as the write**; a mismatch returns `False`/`None` and the session
  marks itself superseded — stale data can never repopulate the store.
- **No orphaned records**: `comments` go by `ON DELETE CASCADE` (verified,
  sentiment columns included); `ingestion_runs` has no FK, so the switch
  deletes its rows explicitly in the same transaction.
- **Reads never activate** (§40): `/stats` and `/sentiment` answer strictly
  for their id and 404 when it is not the working dataset.
- **Scope**: videos, comments, ingestion_runs of the previous video only —
  config, API keys, and future historical-analytics data (Sprint 9) are
  never touched.
- **Retention (§31)**: the local store keeps only the current video's
  working dataset — it is replaced when the user changes videos, so comment
  data does not accumulate locally as browsing continues. This is what the
  implementation does; no broader privacy claim is made.
- **Failure semantics (§32/§33)**: a failed switch rolls back — the
  previous state stays consistent and no "B active" state is ever claimed
  falsely. If B activates but its acquisition fails, the database
  legitimately holds *B active with 0 comments*; the UI shows an honest
  acquisition error — A's dataset is never restored (the user chose B).

## Alternatives considered

| Option | Verdict | Reasoning |
|---|---|---|
| **PostgreSQL** | **Migration target, not now** | Best long-term fit (concurrent writers, richer types, `JSONB` for future analysis payloads). Rejected *for this sprint* because it requires a running server + credentials that this project does not deploy, violating the "no infrastructure for appearance" rule. All SQL kept portable for the move. |
| **MongoDB** | Rejected | Comments are relational in behavior (parent video FK, unique platform id, status `GROUP BY`). MongoDB would buy document flexibility we don't need while making the dedup key and aggregates a bespoke layer. The repo's original "MongoDB vs PostgreSQL" framing also undercounts that both imply a server we don't run. |
| **MySQL** | Rejected | Same server-ops cost as Postgres, no differentiating benefit for this workload. |
| **SQLAlchemy ORM** | Deferred | A thin repository already isolates 100 % of SQL; adding an ORM is a dependency without a need. Revisit together with the Postgres migration (where an ORM/alembic pays for itself). |
| **DuckDB** | Rejected | Analytical engine, not a system of record: weak concurrent-upsert semantics for a live ingestion path. |
| **Redis / cache-only** | Rejected | A cache cannot be the store — datasets must survive restarts for freshness serving. (Redis-class infra is explicitly out of scope until there is a concrete reason.) |

## Expected workload

- **Reads:** `GET /api/videos/{id}` (L2 freshness path) and `/stats` — a few
  requests per minute, each a handful of indexed queries.
- **Writes:** one short transactional burst per acquisition, batched with
  `executemany` in `COMMENT_BATCH_SIZE` (default 500) chunks.
- **Concurrency:** FastAPI's threadpool can call from multiple threads →
  one shared cross-thread connection behind an `RLock` (SQLite serializes
  writers anyway; this matches the real hardware instead of pretending to be
  a distributed store).
- **Size:** ~50 rows/video today; design validated at 10k+ (keyset reads,
  batched writes, SQL-side aggregation).

## Indexing strategy

| Index | Why |
|---|---|
| `comments` PK on `comment_id` | Dedup guarantee: same comment fetched twice can never become two rows. |
| `videos` PK on `video_id` | Freshness lookups are point queries (`last_acquired_at`). |
| `idx_comments_video_published (video_id, published_at)` | Dominant access pattern: the per-video dataset ordered in time (oldest/newest stats, range scans, listings). |
| `idx_comments_video_status (video_id, processing_status)` | "What is ready for analysis / what needs reprocessing" *per video*, and the per-video status-count aggregate. |
| `idx_comments_video_sentiment (video_id, sentiment_label)` (Sprint 4) | The sentiment aggregate `GROUP BY sentiment_label` per video, off full-table scans on large datasets (NULL labels included, so it also serves analyzed-vs-pending probes). |

Deliberately **not** indexed:
- `last_acquired_at` (always read through the `video_id` point lookup).
- A global `processing_status` index (every query filters by `video_id`
  first; a global index would only serve cross-video scans we don't do).
- `ingestion_runs` (append-only, tiny; latest run = `ORDER BY run_id DESC
  LIMIT 1` over insertion order).

## Scalability considerations

- Writes are already **batch-oriented**; reads are **keyset-paginated**
  (`iter_comments`, no `OFFSET`), so memory stays bounded per batch.
- The single-connection/lock model saturates at one machine — the honest
  ceiling for a single-process FastAPI deployment. Growing beyond that
  means Postgres + a connection pool (see migration), *not* more SQLite
  knobs.
- No premature distribution: no Kafka/Spark/Redis/Celery are required to
  ingest, store, or serve these datasets (§37).

## Future migration considerations

The repository layer is the **single seam**: services never emit SQL, routes
never touch the database. Porting to PostgreSQL means:

- `Database` opens a pooled connection instead of `sqlite3.connect`;
  `?` placeholders, `INSERT … ON CONFLICT DO UPDATE` (PostgreSQL ≥ 9.5) and
  ISO-8601 TEXT timestamps are all portable as written — or become native
  `TIMESTAMPTZ` behind the repository.
- DDL moves to a migration tool (alembic) once an ORM arrives; the current
  `CREATE TABLE IF NOT EXISTS` bootstrap stays idempotent for SQLite.
- `DATABASE_URL` parsing (`app/db/connection.py`) currently accepts only
  `sqlite://` forms and **fails fast with a clear error** for anything else —
  no silent misconnection; the check widens deliberately during migration.

# Data Model (Sprint 3 · extended in Sprint 4)

Storage format: SQLite tables defined in `backend/app/db/schema.py`.
Timestamps are **ISO-8601 UTC text** (`2024-03-02T08:00:00+00:00`) — stable,
lexicographically sortable, portable.

## Conceptual layers

```text
RAW        what YouTube returned      → comments.raw_text (verbatim)
PROCESSED  validated + cleaned        → comments.normalized_text, language,
                                        processing_status = READY_FOR_ANALYSIS
ANALYZED   sentiment verdicts         → sentiment_* columns (Sprint 4, one row
                                        per comment — no second database)
```

`raw_text` is **never** rewritten by the pipeline. A future analysis engine
can always re-derive normalized text from the raw source, and auditors can
compare raw ↔ processed byte-for-byte.

## `videos`

One row per video dataset (parent of its comments).

| Column | Type | Notes |
|---|---|---|
| `video_id` | TEXT PK | 11-char YouTube id (validated before any query) |
| `title`, `description`, `channel_id`, `channel_title` | TEXT | `videos.list` snippet; `NULL` = genuinely unavailable |
| `published_at`, `duration`, `category_id` | TEXT | duration always `NULL` today (documented Sprint 2 gap) |
| `view_count`, `like_count`, `comment_count` | INTEGER | counters coerced from YouTube's strings |
| `source` | TEXT | `'youtube'` (official API only) |
| `comments_status` | TEXT | `ok` · `none` · `disabled` (acquisition outcome) |
| `has_more` | INTEGER | YouTube offered a next page when acquisition stopped (dataset limit or page-safety bound) — the truthful “more available” flag (Sprint 4.1) |
| `first_acquired_at` | TEXT | insert-only — when this dataset was born |
| `last_acquired_at` | TEXT | refreshed every acquisition — **freshness input**; a *placeholder* activation row gets epoch `1970-01-01…` so it is deterministically stale (Sprint 4.2, §18) |
| `updated_at` | TEXT | row refresh marker |
| `is_active` | INTEGER 0/1 (Sprint 4.2) | **the explicit active-video marker** — exactly one row has `1`; the working dataset is "the active row's dataset". Never inferred from timestamps (§6) |
| `dataset_generation` | INTEGER (Sprint 4.2) | monotonic token bumped on every activation; every guarded acquisition write re-verifies it under the lock so a superseded run can never persist (§12/§13) |

## `comments`

One row per comment. **Video isolation is structural**: `video_id` is a
required column with a foreign key to `videos(id) ON DELETE CASCADE`, and
every read filters by it — comments can never mix across videos.

| Column | Type | Notes |
|---|---|---|
| `comment_id` | TEXT **PK** | platform-stable id — the dedup key (§14) |
| `video_id` | TEXT FK | dataset membership |
| `parent_comment_id` | TEXT | reply → its top-level comment |
| `author` | TEXT | display name |
| `raw_text` | TEXT NOT NULL | **RAW**: exactly what YouTube returned |
| `normalized_text` | TEXT NOT NULL | **PROCESSED**: pipeline output (§11) |
| `published_at`, `updated_at` | TEXT | YouTube timestamps |
| `like_count` | INTEGER | engagement metadata (never resets status) |
| `is_reply` | INTEGER 0/1 | thread shape |
| `source` | TEXT | `'youtube'` |
| `language` | TEXT | ISO 639-1 from langdetect, or `'unknown'` (§13) |
| `processing_status` | TEXT | state machine (see `processing.md`) |
| `sentiment_label` | TEXT | **ANALYZED** (Sprint 4): `POSITIVE` · `NEUTRAL` · `NEGATIVE` · `UNSUPPORTED_LANGUAGE` · `NULL` = not analyzed (or failed) |
| `sentiment_score` | REAL | VADER compound in `[-1, 1]`; `NULL` when skipped/failed |
| `sentiment_confidence` | REAL | decision margin in `[0, 1]` (see `sentiment-analysis.md`); `NULL` when skipped/failed |
| `sentiment_model` | TEXT | engine identity (`vader-1.0`) — recorded, never exposed through the API |
| `sentiment_processed_at` | TEXT | ISO-8601 UTC of the last analysis attempt |
| `first_seen_at`, `last_seen_at` | TEXT | dedup observability: refresh ≠ re-insert |
| `created_at` | TEXT | insert time (preserved on re-fetch) |

**Update semantics** (`ON CONFLICT(comment_id)`): content fields and
`last_seen_at` refresh; `first_seen_at`/`created_at` are preserved;
`processing_status` resets to `READY_FOR_ANALYSIS` **only if the text
changed** (edited upstream ⇒ must be re-analyzed) — and the same condition
**clears all five `sentiment_*` columns**, because a verdict describes the
text that produced it. `like_count` changes alone reset neither — engagement
is metadata, not content.

**Migration**: a database created before Sprint 4 gains the five columns
idempotently on first open (`schema.migrate(conn)` before the DDL script;
`PRAGMA table_info` decides — no data rewrite, rows keep `NULL` = not yet
analyzed). Fresh databases include them in `CREATE TABLE` directly.
Sprint 4.2 uses the same mechanism for `videos.is_active` /
`videos.dataset_generation`: an upgraded database gets both columns
(`DEFAULT 0` = no active video yet), and the first `GET /api/videos/{id}`
adopts the requested row (or inserts a placeholder) as active — existing
datasets are preserved by adoption, strays removed by the switch.

## `ingestion_runs`

One row per acquisition → data-quality history (§27) with real numbers:
`fetched_count`, `valid_count`, `rejected_count`, `duplicate_count`,
`inserted_count`, `updated_count`, `storage_ok`, start/finish timestamps,
`duration_ms`. Exposed through `lastIngest` on the stats endpoint.

## Invariants (asserted by tests)

```text
fetched == valid + rejected
valid   == inserted + duplicates        (when storage_ok)
count(videos A) and count(videos B) are always disjoint
same comment_id fetched twice ⇒ exactly one row, first_seen_at unchanged
COUNT(videos WHERE is_active = 1) ≤ 1          (Sprint 4.2, §6)
switch A → B ⇒ rows(A) = rows(comments A) = rows(ingestion_runs A) = ∅
stale generation write ⇒ rejected (no row written, no run recorded)
```

## What is intentionally absent

- No emotion/topic/aspect columns (later sprints — the model must not
  pretend they exist). Sprint 4 adds **only** the sentiment columns above.
- No fabricated fields: only YouTube-provided or legitimately derived values
  (duration stays `NULL` rather than guessed from `publishedAt`).
- No translation columns (multilingual text is stored as-is; only a
  `language` label is derived; unsupported languages are recorded as
  `UNSUPPORTED_LANGUAGE` verdicts, never re-labeled).
- No second database / no sentiment table: one comment row carries exactly
  one analysis result (see `sentiment-analysis.md`).

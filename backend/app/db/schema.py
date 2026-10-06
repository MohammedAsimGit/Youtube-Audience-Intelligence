"""Database schema (idempotent DDL) - tables, constraints, and indexes.

Design notes (rationale lives in docs/architecture/database.md):
- Timestamps are stored as ISO-8601 UTC TEXT ("YYYY-MM-DDTHH:MM:SS+00:00"):
  lexicographic order == chronological order, so MIN/MAX and range predicates
  work without SQLite date functions and the format survives a Postgres move.
- `comments.comment_id` is the PRIMARY KEY: the platform's stable id is the
  uniqueness constraint (Sprint 3 dedup rule). `video_id` is a required
  column + foreign key so every comment stays bound to exactly one video
  (video-level dataset isolation).
- Raw vs processed separation: `raw_text` preserves exactly what YouTube
  returned; `normalized_text` holds the cleaned pipeline output. Neither is
  ever derived destructively from the other at read time.
- Sprint 4 sentiment columns live on `comments` (no second database, no
  sentiment table): one comment row == one analysis result. `sentiment_label`
  is NULL until analyzed (or after a content reset), so "has this row been
  analyzed" and "what did it resolve to" are one query, not a join.
- Sprint 5 audience intelligence extends the SAME row (smallest clean
  extension, no parallel table): `emotion_label`/`emotion_score`/
  `emotion_model` hold the NRC emotion verdict and `sentiment_intensity`
  holds the derived LOW/MEDIUM/HIGH band. Invariant after migration: a row
  with a polarity verdict also carries emotion + intensity (all three are
  written by one outcome in one pass); UNSUPPORTED_LANGUAGE rows keep all
  three NULL (never pretend-analyzed).
- Sprint 4.2 active-video lifecycle columns on `videos`: `is_active` is the
  explicit marker for the ONE video that owns the working dataset (never
  inferred from timestamps), and `dataset_generation` is a monotonic token
  bumped on every activation so in-flight acquisitions/analysis runs from a
  superseded dataset can be rejected at write time (race safety, §12/§13).
"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS videos (
    video_id           TEXT PRIMARY KEY,
    title              TEXT,
    description        TEXT,
    channel_id         TEXT,
    channel_title      TEXT,
    published_at       TEXT,
    category_id        TEXT,
    duration           TEXT,
    view_count         INTEGER,
    like_count         INTEGER,
    comment_count      INTEGER,
    source             TEXT    NOT NULL DEFAULT 'youtube',
    comments_status    TEXT    NOT NULL DEFAULT 'ok',
    has_more           INTEGER NOT NULL DEFAULT 0,
    is_active          INTEGER NOT NULL DEFAULT 0,
    dataset_generation INTEGER NOT NULL DEFAULT 0,
    first_acquired_at  TEXT    NOT NULL,
    last_acquired_at   TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS comments (
    comment_id          TEXT PRIMARY KEY,
    video_id            TEXT NOT NULL,
    parent_comment_id   TEXT,
    author              TEXT,
    raw_text            TEXT    NOT NULL,
    normalized_text     TEXT    NOT NULL,
    published_at        TEXT,
    updated_at          TEXT,
    like_count          INTEGER NOT NULL DEFAULT 0,
    is_reply            INTEGER NOT NULL DEFAULT 0,
    source              TEXT    NOT NULL DEFAULT 'youtube',
    language            TEXT    NOT NULL DEFAULT 'unknown',
    processing_status   TEXT    NOT NULL DEFAULT 'READY_FOR_ANALYSIS',
    sentiment_label         TEXT,
    sentiment_score         REAL,
    sentiment_confidence    REAL,
    sentiment_model         TEXT,
    sentiment_processed_at  TEXT,
    sentiment_intensity     TEXT,
    emotion_label           TEXT,
    emotion_score           REAL,
    emotion_model           TEXT,
    first_seen_at       TEXT    NOT NULL,
    last_seen_at        TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,
    FOREIGN KEY (video_id) REFERENCES videos (video_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id        TEXT    NOT NULL,
    fetched_count   INTEGER NOT NULL,
    valid_count     INTEGER NOT NULL,
    rejected_count  INTEGER NOT NULL,
    duplicate_count INTEGER NOT NULL,
    inserted_count  INTEGER NOT NULL,
    updated_count   INTEGER NOT NULL,
    storage_ok      INTEGER NOT NULL,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT    NOT NULL,
    duration_ms     INTEGER NOT NULL
);

-- Index rationale (docs/architecture/database.md):
-- 1. Dominant access pattern: the per-video dataset ordered in time
--    (oldest/newest queries, range scans, per-video listing).
CREATE INDEX IF NOT EXISTS idx_comments_video_published
    ON comments (video_id, published_at);
-- 2. "What is ready for Sprint 4 / what needs reprocessing" per video.
CREATE INDEX IF NOT EXISTS idx_comments_video_status
    ON comments (video_id, processing_status);
-- 3. Freshness lookups are by primary key (video_id); no scan over
--    last_acquired_at exists, so no index is added there (never index
--    blindly). ingestion_runs is append-only and tiny -> no index beyond
--    the implicit rowid; the latest run per video is fetched by
--    ORDER BY run_id DESC LIMIT 1 (covered by the table scan order).
-- 4. (Sprint 4) Sentiment aggregates scan one video's rows grouped by label
--    (`SELECT ... WHERE video_id = ? GROUP BY sentiment_label`).
--    idx_comments_video_status does not cover sentiment_label, so a dedicated
--    (video_id, sentiment_label) index keeps that GROUP BY off full-table
--    scans on large datasets. SQLite indexes NULLs, so the index also serves
--    the "count analyzed vs pending" probes.
CREATE INDEX IF NOT EXISTS idx_comments_video_sentiment
    ON comments (video_id, sentiment_label);

-- Sprint 4.3: background analysis job lifecycle (§8). A small dedicated
-- table because job state (status/phase/progress/error + timestamps) does
-- NOT fit the existing tables: `videos` is the dataset row,
-- `ingestion_runs` is append-only acquisition quality history,
-- `comments` is per-comment.
-- Persisting here is what makes browser-refresh recovery and post-restart
-- detection possible (§8/§32). Deliberately NO foreign key to `videos`:
-- the active-video switch deletes video rows, but the job record must
-- survive as the truthful history of what ran (a cancelled job reports
-- CANCELLED instead of vanishing from the UI). Single-active-video policy
-- means at most one non-terminal row can exist (switch_active_video
-- cancels every other in-flight job inside the same transaction).
CREATE TABLE IF NOT EXISTS analysis_jobs (
    job_id            TEXT PRIMARY KEY,
    video_id          TEXT NOT NULL,
    dataset_generation INTEGER NOT NULL,
    status            TEXT NOT NULL DEFAULT 'QUEUED',
    phase             TEXT NOT NULL DEFAULT 'NONE',
    collected          INTEGER NOT NULL DEFAULT 0,
    has_more           INTEGER NOT NULL DEFAULT 0,
    error_code         TEXT,
    error_message      TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    finished_at        TEXT
);
-- Status lookups are per-video, newest first ("what is happening with this
-- video?"); the table is tiny (one active video's job history), so this
-- index also serves the startup sweep over non-terminal rows.
CREATE INDEX IF NOT EXISTS idx_analysis_jobs_video
    ON analysis_jobs (video_id, created_at);
"""

# Sprint 4 columns added to a database created before this sprint, and
# Sprint 4.2 lifecycle columns for databases created before THAT sprint.
# SQLite has no `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, so each column is
# added only when `PRAGMA table_info` says it is missing (idempotent, safe to
# run on every startup, no data rewrite). `migrate(conn)` runs right after
# the DDL above, inside the same connection bootstrap.
_MIGRATIONS = {
    "comments": (
        "ALTER TABLE comments ADD COLUMN sentiment_label TEXT",
        "ALTER TABLE comments ADD COLUMN sentiment_score REAL",
        "ALTER TABLE comments ADD COLUMN sentiment_confidence REAL",
        "ALTER TABLE comments ADD COLUMN sentiment_model TEXT",
        "ALTER TABLE comments ADD COLUMN sentiment_processed_at TEXT",
        # Sprint 5: emotion verdict + derived intensity band (§8: the
        # smallest clean extension of the existing per-comment result).
        "ALTER TABLE comments ADD COLUMN sentiment_intensity TEXT",
        "ALTER TABLE comments ADD COLUMN emotion_label TEXT",
        "ALTER TABLE comments ADD COLUMN emotion_score REAL",
        "ALTER TABLE comments ADD COLUMN emotion_model TEXT",
    ),
    "videos": (
        # Pre-4.2 rows default to "not active": the first ensure_active()
        # call adopts the requested video's row (preserving fresh data) and
        # removes every other working dataset.
        "ALTER TABLE videos ADD COLUMN is_active INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE videos ADD COLUMN dataset_generation INTEGER NOT NULL DEFAULT 0",
    ),
}


def migrate(conn) -> None:
    """Apply additive schema migrations that `CREATE IF NOT EXISTS` cannot.

    Runs BEFORE the DDL script: an existing pre-Sprint-4/pre-4.2 database
    must gain the referenced columns before any index DDL that uses them
    runs. Fresh databases already contain the columns (the CREATE statements
    above), the table does not exist yet, so this is a no-op there; on
    existing databases the PRAGMA check adds only what is missing (no data
    rewrite, existing rows keep NULL/defaults).

    Sprint 5 one-time reopen (§9: existing data stays safe): when the
    emotion columns are added for the first time, rows already marked
    PROCESSED predate emotion/intensity and would forever break the
    "PROCESSED carries the full intelligence set" invariant (empty emotion
    distributions, wrong denominators). Those rows take the SAME reopen the
    content-reset performs - PROCESSED -> READY_FOR_ANALYSIS with the
    derived columns cleared - so the next run (sync GET or background job)
    regenerates verdicts deterministically (VADER is pure; NRCLex is
    lexicon lookup). Raw text is untouched. The guard (`emotion_label`
    missing) makes this fire exactly once: never on later startups, and
    never on fresh databases.
    """
    reopen_legacy = False
    for table, statements in _MIGRATIONS.items():
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if exists is None:
            continue  # fresh database: SCHEMA_SQL creates the columns directly
        existing = {
            row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if table == "comments" and "emotion_label" not in existing:
            reopen_legacy = True
        for statement in statements:
            column = statement.split("ADD COLUMN ", 1)[1].split(" ", 1)[0]
            if column not in existing:
                # `table` only ever comes from the fixed _MIGRATIONS dict
                # above (PRAGMA/ALTER take identifiers, not parameters).
                conn.execute(statement)
    if reopen_legacy:
        # One-time Sprint 5 requeue (see docstring). Mirrors the column set
        # the content-reset clears, so no stale verdict survives while the
        # row is pending again.
        conn.execute(
            "UPDATE comments SET processing_status = 'READY_FOR_ANALYSIS', "
            "sentiment_label = NULL, sentiment_score = NULL, "
            "sentiment_confidence = NULL, sentiment_model = NULL, "
            "sentiment_processed_at = NULL "
            "WHERE processing_status = 'PROCESSED'"
        )


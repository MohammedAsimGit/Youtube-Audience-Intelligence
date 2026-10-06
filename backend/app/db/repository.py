"""Repository / data-access layer - the ONLY module that runs dataset SQL.

API routes never touch this directly; services (ingestion/dataset/sentiment)
mediate, keeping the layering from docs/architecture/data-pipeline.md:

    API Route -> Service Layer -> Repository -> Database

All statements are parameterized (no string-built SQL from user input; the
only interpolated values are fixed runs of '?' placeholders), so API
parameters can never become arbitrary queries (security review: no
arbitrary database queries are exposed through API parameters).

Dedup contract (Sprint 3): uniqueness is `comment_id` (the platform's stable
id). A re-fetch of the same comment updates metadata + `last_seen_at` and
never inserts a second row; `first_seen_at`/`created_at` are preserved.

Active-video lifecycle (Sprint 4.2): `get_active_video()`/`
switch_active_video()` own the single-working-dataset transition, and every
write method accepts an optional `generation` token so a superseded
acquisition/analysis can never repopulate the database after a switch
(§12/§13: the guard and the write happen inside ONE lock section).
"""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from app.db.connection import Database
from app.models.job import ACTIVE_JOB_STATUSES, JobPhase, JobStatus
from app.models.processing import ProcessingStatus, assert_transition, can_transition, parse_status

# Placeholder per id per chunk; kept well under SQLite's variable limit
# (32766 on modern builds, 999 historically).
_ID_CHUNK = 500

# Sprint 4.2: activation placeholder marks a video as "selected, not yet
# acquired" - the epoch guarantees the row is never FRESH, so the normal
# freshness policy still forces a real acquisition (§18 first load).
_EPOCH = "1970-01-01T00:00:00+00:00"

_COMMENT_COLUMNS = [
    "comment_id", "video_id", "parent_comment_id", "author", "raw_text",
    "normalized_text", "published_at", "updated_at", "like_count", "is_reply",
    "source", "language", "processing_status", "first_seen_at", "last_seen_at",
    "created_at",
]
# Content fields whose change forces the row back to READY_FOR_ANALYSIS (the
# row's text was edited upstream, so Sprint 4 must reprocess it). `like_count`
# deliberately does NOT reset status: engagement is metadata, not content.
_CONTENT_CHANGED = (
    "comments.raw_text IS NOT excluded.raw_text "
    "OR comments.normalized_text IS NOT excluded.normalized_text "
    "OR comments.updated_at IS NOT excluded.updated_at"
)
# Sprint 4: a content reset must also invalidate the stored analysis - a
# sentiment result describes the text that produced it. Without this, an
# aggregate over sentiment_label could count a stale verdict for text that no
# longer exists. All five columns reset together (label first, bookkeeping
# last) exactly when processing_status resets.
_RESET_SENTIMENT_SQL = "\n        ".join(
    f"{column} = CASE WHEN {_CONTENT_CHANGED} THEN NULL ELSE comments.{column} END,"
    for column in (
        "sentiment_label",
        "sentiment_score",
        "sentiment_confidence",
        "sentiment_model",
        "sentiment_processed_at",
        # Sprint 5: a content change invalidates emotion + intensity too -
        # they are derived from the same text, so they reset together.
        "sentiment_intensity",
        "emotion_label",
        "emotion_score",
        "emotion_model",
    )
)
_UPSERT_COMMENT_SQL = f"""
    INSERT INTO comments ({', '.join(_COMMENT_COLUMNS)})
    VALUES ({', '.join('?' for _ in _COMMENT_COLUMNS)})
    ON CONFLICT(comment_id) DO UPDATE SET
        parent_comment_id = excluded.parent_comment_id,
        author = excluded.author,
        raw_text = excluded.raw_text,
        normalized_text = excluded.normalized_text,
        published_at = excluded.published_at,
        updated_at = excluded.updated_at,
        like_count = excluded.like_count,
        is_reply = excluded.is_reply,
        language = excluded.language,
        processing_status = CASE
            WHEN {_CONTENT_CHANGED}
            THEN 'READY_FOR_ANALYSIS'
            ELSE comments.processing_status
        END,
        {_RESET_SENTIMENT_SQL}
        last_seen_at = excluded.last_seen_at
"""

_IN_CHUNK = 500


@dataclass(frozen=True)
class SentimentOutcome:
    """One claimed comment's analysis verdict, ready to persist.

    Produced by the sentiment service, written only by this repository.
    `label` is None on failure (target_status FAILED); `target_status` is
    PROCESSED or FAILED and is validated against the state machine here, so
    the service never writes processing_status directly.

    Sprint 5 fields (all defaulted -> callers/tests from Sprint 4 stay
    valid): `emotion_label`/`emotion_score` carry the NRC emotion verdict
    (None when the language gate skipped the row or inference failed) and
    `intensity` the derived LOW/MEDIUM/HIGH band. `emotion_model` records
    which engine produced the emotion (provenance, like `model`).
    """

    comment_id: str
    label: Optional[str]
    score: Optional[float]
    confidence: Optional[float]
    model: Optional[str]
    processed_at: Optional[str]
    target_status: str
    intensity: Optional[str] = None
    emotion_label: Optional[str] = None
    emotion_score: Optional[float] = None
    emotion_model: Optional[str] = None


@dataclass(frozen=True)
class UpsertStats:
    """Real counts from this upsert run (never estimated).

    Invariant: for a batch of valid comments,
    `valid = inserted + duplicates` and `valid = inserted + updated + in_batch`.
    """

    inserted: int
    updated: int
    duplicates: int  # occurrences that created no new row (in-batch + known ids)


def _placeholders(count: int) -> str:
    return ",".join("?" for _ in range(count))


class DatasetRepository:
    """All dataset queries: videos, comments, stats, ingestion runs."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------- helpers
    def _execute(self, sql: str, params: Sequence[object]) -> sqlite3.Cursor:
        conn = self._db.connection()
        with self._db.lock:
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor

    def _query(self, sql: str, params: Sequence[object] = ()) -> List[sqlite3.Row]:
        conn = self._db.connection()
        with self._db.lock:
            return conn.execute(sql, params).fetchall()

    @staticmethod
    def _active_generation_locked(conn: sqlite3.Connection) -> Optional[int]:
        """Generation of the active dataset, or None when nothing is active.

        Caller must hold the database lock.
        """
        row = conn.execute(
            "SELECT dataset_generation FROM videos WHERE is_active = 1 LIMIT 1"
        ).fetchone()
        return int(row["dataset_generation"]) if row is not None else None

    def _guard_active_locked(
        self, conn: sqlite3.Connection, video_id: str, generation: Optional[int]
    ) -> bool:
        """True when writes for (video_id, generation) are still allowed.

        `generation is None` means "no lifecycle guard" (direct service/unit
        test paths); production acquisition always passes the generation it
        captured from `ensure_active`. The check runs INSIDE the same lock
        as the write it protects, so a switch can never interleave between
        "is this still active?" and the write itself (§13).
        """
        if generation is None:
            return True
        row = conn.execute(
            "SELECT 1 FROM videos WHERE video_id = ? AND is_active = 1 "
            "AND dataset_generation = ?",
            (video_id, generation),
        ).fetchone()
        return row is not None

    def _existing_ids_locked(
        self, conn: sqlite3.Connection, ids: Sequence[str]
    ) -> Set[str]:
        found: Set[str] = set()
        for start in range(0, len(ids), _ID_CHUNK):
            chunk = ids[start:start + _ID_CHUNK]
            rows = conn.execute(
                f"SELECT comment_id FROM comments WHERE comment_id IN ({_placeholders(len(chunk))})",
                tuple(chunk),
            ).fetchall()
            found.update(row["comment_id"] for row in rows)
        return found

    # ------------------------------------------------------------ lifecycle
    def get_active_video(self) -> Optional[sqlite3.Row]:
        """The explicit active-video row (Sprint 4.2), or None.

        This is the single source of truth for which video owns the working
        dataset - never inferred from timestamps or insertion order (§6).
        """
        rows = self._query("SELECT * FROM videos WHERE is_active = 1 LIMIT 1")
        return rows[0] if rows else None

    def switch_active_video(self, new_video_id: str) -> int:
        """Atomically make `new_video_id` the single active working dataset.

        One transaction (single lock, single commit, rollback on error - §14):

            generation = MAX(dataset_generation) + 1   # monotonic (§12)
            DELETE FROM ingestion_runs                 # every working run
            DELETE FROM videos                         # cascades comments
                                                        # (+ sentiment columns)
            keep/insert `new_video_id` as is_active=1 with that generation

        If a row for `new_video_id` already exists (adopting a pre-4.2
        database, or a legacy stray), that row's dataset is PRESERVED -
        freshness still governs its reuse - and only every OTHER dataset is
        removed. `ingestion_runs` has no FK, so it is cleaned explicitly
        (no orphaned run rows, §10). Returns the new generation; already-
        active input is an idempotent no-op returning the current one.
        """
        conn = self._db.connection()
        with self._db.lock:
            try:
                current = conn.execute(
                    "SELECT dataset_generation FROM videos "
                    "WHERE is_active = 1 LIMIT 1"
                ).fetchone()
                if current is not None and conn.execute(
                    "SELECT 1 FROM videos WHERE video_id = ? AND is_active = 1",
                    (new_video_id,),
                ).fetchone() is not None:
                    return int(current["dataset_generation"])  # already active

                row = conn.execute(
                    "SELECT COALESCE(MAX(dataset_generation), 0) AS g FROM videos"
                ).fetchone()
                generation = int(row["g"]) + 1

                conn.execute(
                    "DELETE FROM ingestion_runs WHERE video_id != ?",
                    (new_video_id,),
                )
                # Sprint 4.3 §5/§19: the switch IS the cancellation signal
                # for in-flight analysis jobs - no second cancellation
                # mechanism. Every non-terminal job now belongs to a dead
                # generation, so it is cancelled inside the SAME
                # transaction: after this commit the database can never
                # show an ACQUIRING/ANALYZING job for a video that is not
                # the active dataset.
                now_cancelled = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "UPDATE analysis_jobs SET status = ?, error_code = ?, "
                    "error_message = ?, updated_at = ?, finished_at = ? "
                    f"WHERE status IN ({_placeholders(len(ACTIVE_JOB_STATUSES))})",
                    (
                        JobStatus.CANCELLED.value,
                        "job_cancelled",
                        "Superseded by a different active video.",
                        now_cancelled,
                        now_cancelled,
                        *ACTIVE_JOB_STATUSES,
                    ),
                )
                # Removing the video rows cascades every comment (FK ON
                # DELETE CASCADE) including its sentiment columns.
                conn.execute(
                    "DELETE FROM videos WHERE video_id != ?", (new_video_id,)
                )

                now = datetime.now(timezone.utc).isoformat()
                preserved = conn.execute(
                    "SELECT 1 FROM videos WHERE video_id = ?", (new_video_id,)
                ).fetchone()
                if preserved is not None:
                    conn.execute(
                        "UPDATE videos SET is_active = 1, dataset_generation = ?, "
                        "updated_at = ? WHERE video_id = ?",
                        (generation, now, new_video_id),
                    )
                else:
                    # Activation placeholder: epoch `last_acquired_at` makes
                    # the row deterministically STALE, so acquisition still
                    # runs even when this activation precedes any fetch.
                    conn.execute(
                        "INSERT INTO videos (video_id, is_active, "
                        "dataset_generation, first_acquired_at, "
                        "last_acquired_at, updated_at) VALUES (?, 1, ?, ?, ?, ?)",
                        (new_video_id, generation, now, _EPOCH, now),
                    )
                conn.commit()
            except sqlite3.Error:
                conn.rollback()  # never leave a half-switched state (§32)
                raise
            return generation

    # -------------------------------------------------------------- videos
    def get_video(self, video_id: str) -> Optional[sqlite3.Row]:
        rows = self._query("SELECT * FROM videos WHERE video_id = ?", (video_id,))
        return rows[0] if rows else None

    def upsert_video(
        self,
        video_id: str,
        metadata: Dict[str, object],
        comments_status: str,
        has_more: bool,
        acquired_at: str,
        generation: Optional[int] = None,
    ) -> bool:
        """Insert or refresh the video row; `first_acquired_at` is preserved.

        With `generation` set (Sprint 4.2 acquisition paths), the write is
        rejected - returning False - when another dataset became active
        after this acquisition started, so a superseded run can never
        resurrect a deleted video row (§13). `generation=None` keeps the
        unguarded behavior for direct service/unit-test paths.
        """
        sql = """
        INSERT INTO videos (
            video_id, title, description, channel_id, channel_title, published_at,
            category_id, duration, view_count, like_count, comment_count,
            source, comments_status, has_more,
            first_acquired_at, last_acquired_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(video_id) DO UPDATE SET
            title = excluded.title,
            description = excluded.description,
            channel_id = excluded.channel_id,
            channel_title = excluded.channel_title,
            published_at = excluded.published_at,
            category_id = excluded.category_id,
            duration = excluded.duration,
            view_count = excluded.view_count,
            like_count = excluded.like_count,
            comment_count = excluded.comment_count,
            comments_status = excluded.comments_status,
            has_more = excluded.has_more,
            last_acquired_at = excluded.last_acquired_at,
            updated_at = excluded.updated_at
        """
        params = (
            video_id,
            metadata.get("title"),
            metadata.get("description"),
            metadata.get("channel_id"),
            metadata.get("channel_title"),
            metadata.get("published_at"),
            metadata.get("category_id"),
            metadata.get("duration"),
            metadata.get("view_count"),
            metadata.get("like_count"),
            metadata.get("comment_count"),
            "youtube",
            comments_status,
            1 if has_more else 0,
            acquired_at,  # first_acquired_at (insert-only via ON CONFLICT)
            acquired_at,  # last_acquired_at (always refreshed)
            acquired_at,  # updated_at
        )
        conn = self._db.connection()
        with self._db.lock:
            if not self._guard_active_locked(conn, video_id, generation):
                return False
            conn.execute(sql, params)
            conn.commit()
            return True

    # ------------------------------------------------------------ comments
    def existing_comment_ids(self, comment_ids: Iterable[str]) -> Set[str]:
        """Which of `comment_ids` already exist (chunked IN lookups)."""
        wanted = list(dict.fromkeys(comment_ids))
        if not wanted:
            return set()
        conn = self._db.connection()
        with self._db.lock:
            return self._existing_ids_locked(conn, wanted)

    def upsert_comments(
        self, rows: Sequence[Dict[str, object]], batch_size: int,
        generation: Optional[int] = None,
    ) -> Optional[UpsertStats]:
        """Batch upsert with in-batch dedup + real insert/update counts.

        The existing-id probe and the writes run inside ONE lock, so
        concurrent acquisitions cannot double-count. When `generation` is
        given (Sprint 4.2), the whole batch is rejected with `None` if
        another dataset is now active - the guard and every write share one
        lock section, so a switch can never interleave between them (§13).
        """
        if not rows:
            return UpsertStats(inserted=0, updated=0, duplicates=0)

        # In-batch dedup: the same comment id twice in one fetch keeps the
        # last occurrence (most recent metadata) and counts as a duplicate.
        unique: Dict[str, Dict[str, object]] = {}
        in_batch_duplicates = 0
        for row in rows:
            key = str(row["comment_id"])
            if key in unique:
                in_batch_duplicates += 1
            unique[key] = row

        conn = self._db.connection()
        with self._db.lock:
            if not self._guard_active_locked(
                conn, str(next(iter(unique.values()))["video_id"]), generation
            ):
                return None
            existing = self._existing_ids_locked(conn, list(unique))
            prepared = [
                tuple(row[col] for col in _COMMENT_COLUMNS)
                for row in unique.values()
            ]
            for start in range(0, len(prepared), batch_size):
                conn.executemany(
                    _UPSERT_COMMENT_SQL, prepared[start:start + batch_size]
                )
            conn.commit()

        updated = len(set(unique) & existing)
        inserted = len(unique) - updated
        return UpsertStats(
            inserted=inserted,
            updated=updated,
            duplicates=in_batch_duplicates + updated,
        )

    def get_comments(self, video_id: str) -> List[sqlite3.Row]:
        return self._query(
            "SELECT * FROM comments WHERE video_id = ? ORDER BY published_at DESC",
            (video_id,),
        )

    def iter_comments(
        self, video_id: str, batch_size: int, status: Optional[str] = None
    ) -> Iterator[sqlite3.Row]:
        """Keyset-paginated reader (no OFFSET; bounded memory per batch).

        Used for dataset reads so a 10k+ row dataset is never pulled through
        one unbounded cursor; callers can stop iterating at any time.
        """
        last_id = ""
        while True:
            if status is None:
                sql = (
                    "SELECT * FROM comments WHERE video_id = ? AND comment_id > ? "
                    "ORDER BY comment_id LIMIT ?"
                )
                params: Sequence[object] = (video_id, last_id, batch_size)
            else:
                sql = (
                    "SELECT * FROM comments WHERE video_id = ? "
                    "AND processing_status = ? AND comment_id > ? "
                    "ORDER BY comment_id LIMIT ?"
                )
                params = (video_id, status, last_id, batch_size)
            rows = self._query(sql, params)
            if not rows:
                return
            for row in rows:
                yield row
            last_id = rows[-1]["comment_id"]
            if len(rows) < batch_size:
                return

    def get_comments_by_status(self, video_id: str, status: str) -> List[sqlite3.Row]:
        return self._query(
            "SELECT * FROM comments WHERE video_id = ? AND processing_status = ? "
            "ORDER BY published_at DESC",
            (video_id, status),
        )

    def claim_for_processing(
        self, video_id: str, comment_ids: Sequence[str]
    ) -> Set[str]:
        """Batch-claim READY_FOR_ANALYSIS rows -> PROCESSING (Sprint 4).

        One lock + one commit for the whole batch: the SELECT and the UPDATE
        cannot interleave with another writer, and the transition is checked
        against the state machine before anything is written. Ids that are
        not currently READY (unknown, already claimed, since reset) are
        silently skipped - a claim is optimistic, never forced. Returns the
        ids actually claimed.
        """
        wanted = list(dict.fromkeys(comment_ids))
        if not wanted:
            return set()
        target = ProcessingStatus.PROCESSING
        assert_transition(ProcessingStatus.READY_FOR_ANALYSIS, target)
        claimed: Set[str] = set()
        conn = self._db.connection()
        with self._db.lock:
            for start in range(0, len(wanted), _ID_CHUNK):
                chunk = wanted[start:start + _ID_CHUNK]
                rows = conn.execute(
                    "SELECT comment_id FROM comments "
                    f"WHERE video_id = ? AND processing_status = ? "
                    f"AND comment_id IN ({_placeholders(len(chunk))})",
                    (video_id, ProcessingStatus.READY_FOR_ANALYSIS.value, *chunk),
                ).fetchall()
                claimed.update(row["comment_id"] for row in rows)
            if claimed:
                conn.executemany(
                    "UPDATE comments SET processing_status = ? "
                    "WHERE comment_id = ? AND video_id = ? AND processing_status = ?",
                    [
                        (target.value, comment_id, video_id,
                         ProcessingStatus.READY_FOR_ANALYSIS.value)
                        for comment_id in sorted(claimed)
                    ],
                )
                conn.commit()
        return claimed

    def save_sentiment_results(
        self, video_id: str, results: Sequence[SentimentOutcome], batch_size: int
    ) -> int:
        """Persist sentiment verdicts + PROCESSING -> PROCESSED/FAILED.

        The only write path for sentiment columns (repository stays the sole
        persistence abstraction). Each row's transition is validated against
        the state machine from its CURRENT status; rows that moved on since
        the claim (e.g. a concurrent content reset) are skipped, never
        forced - so a stale verdict can never overwrite a fresh reset. All
        chunks are written under one lock and committed once. Returns the
        number of rows updated.
        """
        if not results:
            return 0
        updated = 0
        conn = self._db.connection()
        with self._db.lock:
            for start in range(0, len(results), batch_size):
                chunk = results[start:start + batch_size]
                ids = [outcome.comment_id for outcome in chunk]
                rows = conn.execute(
                    "SELECT comment_id, processing_status FROM comments "
                    f"WHERE video_id = ? AND comment_id IN ({_placeholders(len(ids))})",
                    (video_id, *ids),
                ).fetchall()
                current_by_id = {
                    row["comment_id"]: parse_status(row["processing_status"])
                    for row in rows
                }
                writable: List[SentimentOutcome] = []
                for outcome in chunk:
                    current = current_by_id.get(outcome.comment_id)
                    if current is None:
                        continue  # row gone (cannot happen for a bound FK, defensive)
                    target = parse_status(outcome.target_status)
                    if not can_transition(current, target):
                        continue  # moved on since claim -> skip, never force
                    writable.append(outcome)
                if not writable:
                    continue
                conn.executemany(
                    "UPDATE comments SET sentiment_label = ?, sentiment_score = ?, "
                    "sentiment_confidence = ?, sentiment_model = ?, "
                    "sentiment_processed_at = ?, sentiment_intensity = ?, "
                    "emotion_label = ?, emotion_score = ?, emotion_model = ?, "
                    "processing_status = ? "
                    "WHERE comment_id = ? AND video_id = ?",
                    [
                        (
                            outcome.label,
                            outcome.score,
                            outcome.confidence,
                            outcome.model,
                            outcome.processed_at,
                            outcome.intensity,
                            outcome.emotion_label,
                            outcome.emotion_score,
                            outcome.emotion_model,
                            parse_status(outcome.target_status).value,
                            outcome.comment_id,
                            video_id,
                        )
                        for outcome in writable
                    ],
                )
                updated += len(writable)
            conn.commit()
        return updated

    def update_processing_status(
        self, video_id: str, comment_id: str, target_status: str
    ) -> int:
        """Transition one comment's status (validates the state machine).

        Reads the current status and writes the target atomically under the
        database lock; an illegal transition raises and changes nothing.
        Returns the number of rows changed (0 = comment not found).
        """
        target = parse_status(target_status)  # raises on unknown state
        conn = self._db.connection()
        with self._db.lock:
            row = conn.execute(
                "SELECT processing_status FROM comments "
                "WHERE comment_id = ? AND video_id = ?",
                (comment_id, video_id),
            ).fetchone()
            if row is None:
                return 0
            current = parse_status(row["processing_status"])
            assert_transition(current, target)
            cursor = conn.execute(
                "UPDATE comments SET processing_status = ? "
                "WHERE comment_id = ? AND video_id = ?",
                (target.value, comment_id, video_id),
            )
            conn.commit()
            return cursor.rowcount

    # --------------------------------------------------------------- stats
    def get_comment_stats(self, video_id: str) -> Dict[str, object]:
        """SQL-side aggregates (never loads rows into Python for counting)."""
        row = self._query(
            "SELECT COUNT(*) AS total, COUNT(DISTINCT comment_id) AS unique_total, "
            "COALESCE(SUM(is_reply), 0) AS replies, "
            "MIN(published_at) AS oldest, MAX(published_at) AS newest "
            "FROM comments WHERE video_id = ?",
            (video_id,),
        )[0]
        return {
            "total": int(row["total"]),
            "unique": int(row["unique_total"]),
            "replies": int(row["replies"]),
            "oldest": row["oldest"],
            "newest": row["newest"],
        }

    def get_status_counts(self, video_id: str) -> Dict[str, int]:
        rows = self._query(
            "SELECT processing_status, COUNT(*) AS n FROM comments "
            "WHERE video_id = ? GROUP BY processing_status",
            (video_id,),
        )
        return {row["processing_status"]: int(row["n"]) for row in rows}

    def get_sentiment_counts(self, video_id: str) -> Dict[str, int]:
        """Sentiment aggregation: label -> row count (Sprint 4, SQL-side).

        Only rows carrying a label are counted, so pending/failed rows (NULL
        label) can never pollute the distribution. UNSUPPORTED_LANGUAGE rows
        are counted under their own key and reported as `skipped` - they are
        analyzed rows without a polarity verdict, never folded into neutral.
        """
        rows = self._query(
            "SELECT sentiment_label, COUNT(*) AS n FROM comments "
            "WHERE video_id = ? AND sentiment_label IS NOT NULL "
            "GROUP BY sentiment_label",
            (video_id,),
        )
        return {row["sentiment_label"]: int(row["n"]) for row in rows}

    def get_emotion_counts(self, video_id: str) -> Dict[str, int]:
        """Sprint 5 emotion aggregation: label -> row count (SQL-side).

        Rows without an emotion verdict (pending, failed, or the language
        gate) carry NULL and are excluded - the denominator is exactly the
        emotion-analyzed set (== polarity-analyzed rows by the write-path
        invariant documented in schema.py).
        """
        rows = self._query(
            "SELECT emotion_label, COUNT(*) AS n FROM comments "
            "WHERE video_id = ? AND emotion_label IS NOT NULL "
            "GROUP BY emotion_label",
            (video_id,),
        )
        return {row["emotion_label"]: int(row["n"]) for row in rows}

    def get_intensity_counts(self, video_id: str) -> Dict[str, int]:
        """Sprint 5 intensity aggregation: LOW/MEDIUM/HIGH -> row count."""
        rows = self._query(
            "SELECT sentiment_intensity, COUNT(*) AS n FROM comments "
            "WHERE video_id = ? AND sentiment_intensity IS NOT NULL "
            "GROUP BY sentiment_intensity",
            (video_id,),
        )
        return {row["sentiment_intensity"]: int(row["n"]) for row in rows}

    def get_average_sentiment_confidence(self, video_id: str) -> Optional[float]:
        """Sprint 5: AVG decision-margin over polarity-analyzed rows.

        NULL exactly when nothing has been analyzed (no rows to average) -
        the API reports `confidence.average = null`, never a fabricated 0.
        UNSUPPORTED_LANGUAGE rows hold NULL confidence and drop out of the
        average naturally.
        """
        row = self._query(
            "SELECT AVG(sentiment_confidence) AS avg_conf FROM comments "
            "WHERE video_id = ? AND sentiment_label IN (?, ?, ?)",
            (
                video_id,
                "POSITIVE",
                "NEUTRAL",
                "NEGATIVE",
            ),
        )[0]
        value = row["avg_conf"]
        return None if value is None else float(value)

    def get_analysis_fingerprint(self, video_id: str) -> Tuple[int, Optional[str]]:
        """Sprint 6: (analyzed count, latest verdict timestamp) in one read.

        The topic service memoizes its result against this fingerprint:
        any change to the analyzed set - new verdicts, a content reset
        clearing them, or a re-analysis writing fresh timestamps - changes
        the fingerprint, so a memoized topic result can never describe
        data that no longer exists. Returns (0, None) when nothing has been
        analyzed (empty / pending / all rows skipped).
        """
        row = self._query(
            "SELECT COUNT(*) AS n, MAX(sentiment_processed_at) AS latest "
            "FROM comments WHERE video_id = ? "
            "AND sentiment_label IN (?, ?, ?)",
            (video_id, "POSITIVE", "NEUTRAL", "NEGATIVE"),
        )[0]
        return int(row["n"]), row["latest"]

    def count_recent_comments(self, video_id: str, since_iso: str) -> int:
        """Sprint 8 (§19): comments PUBLISHED at/after `since_iso`.

        The audience-activity signal is computed from real `published_at`
        timestamps (never from fetch times or invented motion). ISO-8601
        UTC text compares lexicographically == chronologically, so this is
        a plain range probe served by idx_comments_video_published -
        no new index needed (§37: never index blindly).
        """
        row = self._query(
            "SELECT COUNT(*) AS n FROM comments "
            "WHERE video_id = ? AND published_at >= ?",
            (video_id, since_iso),
        )[0]
        return int(row["n"])

    # ------------------------------------------------------ ingestion runs
    def record_ingestion_run(
        self,
        video_id: str,
        fetched: int,
        valid: int,
        rejected: int,
        duplicates: int,
        inserted: int,
        updated: int,
        storage_ok: bool,
        started_at: str,
        finished_at: str,
        duration_ms: int,
        generation: Optional[int] = None,
    ) -> bool:
        """Persist one acquisition's aggregated quality run.

        Returns False (writing nothing) when the `generation` guard sees a
        different active dataset - a superseded acquisition never leaves an
        orphaned run row behind (§10/§13)."""
        sql = (
            "INSERT INTO ingestion_runs ("
            "video_id, fetched_count, valid_count, rejected_count, duplicate_count, "
            "inserted_count, updated_count, storage_ok, started_at, finished_at, "
            "duration_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
        )
        params = (
            video_id, fetched, valid, rejected, duplicates,
            inserted, updated, 1 if storage_ok else 0,
            started_at, finished_at, duration_ms,
        )
        conn = self._db.connection()
        with self._db.lock:
            if not self._guard_active_locked(conn, video_id, generation):
                return False
            conn.execute(sql, params)
            conn.commit()
            return True

    def get_latest_ingestion_run(self, video_id: str) -> Optional[sqlite3.Row]:
        rows = self._query(
            "SELECT * FROM ingestion_runs WHERE video_id = ? "
            "ORDER BY run_id DESC LIMIT 1",
            (video_id,),
        )
        return rows[0] if rows else None

    # ------------------------------------------------------- analysis jobs
    # Sprint 4.3: persistent background-job lifecycle (docs/13). All
    # transitions are guarded so a worker thread can never resurrect a job
    # that the active-video switch already cancelled, and a terminal job
    # can never be overwritten (stale writes become no-ops).

    def create_job(self, job_id: str, video_id: str, generation: int) -> None:
        """Insert a QUEUED job for the (already) active video."""
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            "INSERT INTO analysis_jobs (job_id, video_id, dataset_generation, "
            "status, phase, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (job_id, video_id, generation, JobStatus.QUEUED.value,
             JobPhase.NONE.value, now, now),
        )

    def get_latest_job(self, video_id: str) -> Optional[sqlite3.Row]:
        """Newest job row for one video ("what is happening with this
        video?"), or None when no job ever ran.
        """
        rows = self._query(
            "SELECT * FROM analysis_jobs WHERE video_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (video_id,),
        )
        return rows[0] if rows else None

    def get_job(self, job_id: str) -> Optional[sqlite3.Row]:
        rows = self._query("SELECT * FROM analysis_jobs WHERE job_id = ?", (job_id,))
        return rows[0] if rows else None

    def update_job_progress(self, job_id: str, collected: int, has_more: bool) -> bool:
        """Per-page progress write; only while the job is non-terminal.

        One short transaction per page (never held across YouTube calls,
        §30). Returns False when the job was already finished/cancelled -
        the stale worker's numbers are dropped, not persisted.
        """
        now = datetime.now(timezone.utc).isoformat()
        conn = self._db.connection()
        with self._db.lock:
            cursor = conn.execute(
                "UPDATE analysis_jobs SET collected = ?, has_more = ?, updated_at = ? "
                f"WHERE job_id = ? AND status IN ({_placeholders(len(ACTIVE_JOB_STATUSES))})",
                (collected, 1 if has_more else 0, now, job_id, *ACTIVE_JOB_STATUSES),
            )
            conn.commit()
            return cursor.rowcount > 0

    def begin_job_phase(self, job_id: str, status: str, phase: str) -> bool:
        """QUEUED -> ACQUIRING -> ANALYZING progression (active jobs only)."""
        now = datetime.now(timezone.utc).isoformat()
        conn = self._db.connection()
        with self._db.lock:
            cursor = conn.execute(
                "UPDATE analysis_jobs SET status = ?, phase = ?, updated_at = ? "
                f"WHERE job_id = ? AND status IN ({_placeholders(len(ACTIVE_JOB_STATUSES))})",
                (status, phase, now, job_id, *ACTIVE_JOB_STATUSES),
            )
            conn.commit()
            return cursor.rowcount > 0

    def finish_job(
        self,
        job_id: str,
        status: str,
        phase: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        collected: Optional[int] = None,
        has_more: Optional[bool] = None,
    ) -> bool:
        """Terminal transition (COMPLETED/FAILED/CANCELLED/STALE).

        Guarded to fire exactly once: if the active-video switch already
        cancelled this job, the worker's late finish is a no-op (rowcount 0)
        and the switch's truthful CANCELLED state stands. `phase=None`
        keeps the phase the job died in (honest for failure reports).
        """
        now = datetime.now(timezone.utc).isoformat()
        conn = self._db.connection()
        with self._db.lock:
            sets = ["status = ?", "error_code = ?",
                    "error_message = ?", "updated_at = ?", "finished_at = ?"]
            params: List[object] = [status, error_code, error_message, now, now]
            if phase is not None:
                sets.insert(1, "phase = ?")
                params.insert(1, phase)
            if collected is not None:
                sets.append("collected = ?")
                params.append(collected)
            if has_more is not None:
                sets.append("has_more = ?")
                params.append(1 if has_more else 0)
            params.append(job_id)
            params.extend(ACTIVE_JOB_STATUSES)
            cursor = conn.execute(
                f"UPDATE analysis_jobs SET {', '.join(sets)} "
                f"WHERE job_id = ? AND status IN ({_placeholders(len(ACTIVE_JOB_STATUSES))})",
                params,
            )
            conn.commit()
            return cursor.rowcount > 0

    def sweep_interrupted_jobs(self) -> int:
        """Startup recovery (§32): non-terminal jobs with no worker become
        STALE so the UI can never stay pinned at ACQUIRING forever after a
        server restart. Returns the number of rows recovered.
        """
        now = datetime.now(timezone.utc).isoformat()
        conn = self._db.connection()
        with self._db.lock:
            cursor = conn.execute(
                "UPDATE analysis_jobs SET status = ?, error_code = ?, "
                "error_message = ?, updated_at = ?, finished_at = ? "
                f"WHERE status IN ({_placeholders(len(ACTIVE_JOB_STATUSES))})",
                (
                    JobStatus.STALE.value,
                    "job_interrupted",
                    "Analysis was interrupted by a server restart. Please retry.",
                    now,
                    now,
                    *ACTIVE_JOB_STATUSES,
                ),
            )
            conn.commit()
            return cursor.rowcount

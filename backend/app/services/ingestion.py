"""Ingestion service: validation -> normalization/cleaning -> dedup -> persist.

This is the middle of the Sprint 3 pipeline (docs/architecture/data-pipeline.md):

    YouTube acquisition -> [THIS] -> repository -> SQLite -> Sprint 4 input

Guarantees:
- Invalid records never reach the database (they become counted, logged
  rejections with structured reasons - never silent drops, never raw text).
- One malformed comment never destroys the valid batch.
- Dedup key is `comment_id`: in-batch duplicates collapse to the latest
  occurrence; already-stored ids are refreshed (`last_seen_at`), not
  re-inserted. Cross-page duplicates (the same comment on two YouTube pages)
  are handled by the same upsert - they count as refreshed rows, never as
  new records.
- Persistence failures degrade to "serve the acquired data anyway" with a
  PERSISTENCE_FAILED event - a storage hiccup must not 500 a valid
  acquisition (the failure is still observable in logs + ingestion_runs).
- Batching: rows are written in `COMMENT_BATCH_SIZE` chunks so large
  datasets never form one giant statement.

Sprint 4.1 (incremental persistence):
- `begin()` opens an `IngestionSession`: one YouTube page at a time is
  validated -> normalized -> language-detected -> deduped -> written to
  SQLite BEFORE the next page is fetched (page -> batch -> persist ->
  next page). The full comment dataset is never held before writing.
- `finish()` finalizes the parent video row (truthful `comments_status` +
  `has_more`) and records exactly ONE `ingestion_runs` row with the
  ACQUISITION-aggregated quality numbers, so the §27 invariants
  (fetched == valid + rejected, valid == inserted + duplicates) hold for
  the whole run - not just per page.
- The single-batch `ingest()` remains as a thin wrapper over the session,
  so existing callers and tests keep their exact contract.
"""
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.repository import DatasetRepository
from app.models.internal import Comment, VideoMetadata
from app.services.language import detect_language
from app.services.text_pipeline import normalize_comment_text
from app.services.validation import ValidationIssue, validate_comments

logger = get_logger("ingestion")


def _iso(value: Optional[datetime]) -> Optional[str]:
    """Aware UTC datetime -> stable ISO-8601 text (the storage format)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


@dataclass
class IngestionReport:
    """Real data-quality numbers from one pipeline run (never fabricated).

    For an incremental session this is the ACQUISITION-aggregated report:
    every counter sums the pages actually ingested.

    Invariants (asserted by tests):
        fetched == valid + rejected
        valid   == inserted + duplicates   (when storage_ok is True)
    """

    video_id: str
    fetched: int
    valid: int
    rejected: int
    duplicates: int  # in-batch extras + ids already stored (refreshed)
    inserted: int
    updated: int
    storage_ok: bool
    duration_ms: int
    rejected_issues: List[ValidationIssue] = field(default_factory=list)


def _video_metadata_fields(metadata: VideoMetadata) -> Dict[str, object]:
    """Normalized metadata -> the repository's `upsert_video` dictionary."""
    return {
        "title": metadata.title,
        "description": metadata.description,
        "channel_id": metadata.channel_id,
        "channel_title": metadata.channel_title,
        "published_at": _iso(metadata.published_at),
        "category_id": metadata.category_id,
        "duration": metadata.duration,
        "view_count": metadata.statistics.view_count,
        "like_count": metadata.statistics.like_count,
        "comment_count": metadata.statistics.comment_count,
    }


class IngestionSession:
    """Incremental ingestion for ONE acquisition run (Sprint 4.1).

    Flow per page (spec §11/§12):

        YouTube page -> validate -> normalize -> language detection
                     -> dedup -> batched SQLite write -> next page

    Lifecycle:
        session = service.begin(video_id, metadata, acquired_at, generation)
        session.add_page(page_comments, page_has_more=token_present)  # × N
        processed, report = session.finish(comments_status, has_more)

    Rules:
    - The parent video row is upserted before each page's comment writes
      (foreign key + truthful mid-run flags); `finish()` writes the final
      `comments_status`/`has_more` and records ONE aggregated run row.
    - If a page fails to persist, `storage_ok` latches False for the whole
      session: no run row claims success for a partially written acquisition.
    - If the acquisition never produced a page (quota/disabled on the first
      call), `finish()` still finalizes the video row (e.g. `disabled`),
      exactly like the original single-shot behavior.
    - Race safety (Sprint 4.2 §13): when a `generation` is provided, every
      write carries it into the repository's in-lock active check - if
      another video becomes active mid-run the session latches
      `superseded`, `add_page` drops the batch (returns None) so the loop
      stops fetching, and `finish()` writes nothing.
    """

    def __init__(
        self,
        repository: DatasetRepository,
        settings: Settings,
        video_id: str,
        metadata: VideoMetadata,
        acquired_at: datetime,
        generation: Optional[int] = None,
    ) -> None:
        self._repo = repository
        self._settings = settings
        self._video_id = video_id
        self._metadata = metadata
        self._acquired_at = acquired_at
        self._generation = generation
        self._superseded = False
        self._started = time.monotonic()
        self._started_at = datetime.now(timezone.utc)
        # Acquisition-aggregated quality counters (summed over pages).
        self._fetched = 0
        self._valid = 0
        self._rejected = 0
        self._duplicates = 0
        self._inserted = 0
        self._updated = 0
        self._storage_ok = True
        self._issues: List[ValidationIssue] = []
        # Response items, deduped across pages (first appearance order,
        # latest occurrence's metadata wins - same rule as single-shot).
        self._processed: Dict[str, Comment] = {}
        self._pages = 0
        self._finished = False

    # ------------------------------------------------------------- state
    @property
    def pages_ingested(self) -> int:
        return self._pages

    @property
    def fetched(self) -> int:
        """Comments counted by this session so far (post-trim).

        The authoritative cap accounting when the fetch loop and the ingest
        worker run on different threads (Sprint 5.1 §6): the worker trims
        every page to `COMMENT_ACQUISITION_MAX_COMMENTS - fetched` before
        writing, so a stored dataset can never exceed the configured limit
        no matter how far ahead the fetch loop runs.
        """
        return self._fetched

    @property
    def superseded(self) -> bool:
        """True once a guarded write saw another video become active."""
        return self._superseded

    def _mark_superseded(self) -> None:
        if not self._superseded:
            self._superseded = True
            logger.warning(
                "ACQUISITION_SUPERSEDED",
                extra={"video_id": self._video_id, "pages": self._pages},
            )

    def _ensure_open(self) -> None:
        if self._finished:
            raise RuntimeError("ingestion session already finished")

    # ------------------------------------------------------------- pages
    def add_page(
        self, comments: Sequence[Comment], page_has_more: bool
    ) -> Optional[IngestionReport]:
        """Validate + normalize + dedup + persist ONE page.

        Returns the page-level report, or None when the session was
        superseded (another video is now active) - in that case nothing was
        written and the caller must stop acquiring (§13).
        """
        self._ensure_open()
        if self._superseded:
            return None  # short-circuit: never touch the new working dataset
        video_id = self._video_id
        now_iso = _iso(self._started_at)

        # 1. VALIDATION ------------------------------------------------------
        valid, rejected = validate_comments(comments, video_id)
        issues: List[ValidationIssue] = [
            issue for _, issue_list in rejected for issue in issue_list
        ]
        for comment, issue_list in rejected:
            # Structured reasons only - raw comment text is never logged.
            logger.warning(
                "VALIDATION_REJECTED",
                extra={
                    "video_id": video_id,
                    "comment_id": comment.comment_id,
                    "issues": [f"{i.field}:{i.reason}" for i in issue_list],
                },
            )
        logger.info(
            "VALIDATION_COMPLETED",
            extra={"video_id": video_id, "valid": len(valid), "rejected": len(rejected)},
        )

        # 2. NORMALIZATION + CLEANING ----------------------------------------
        processed: List[Comment] = []
        rows: List[Dict[str, object]] = []
        normalize_rejects = 0
        for comment in valid:
            normalized = normalize_comment_text(comment.text)
            if not normalized:
                # e.g. a comment made only of invisible characters - reject,
                # never store an empty "content" row.
                normalize_rejects += 1
                issues.append(ValidationIssue("text", "empty_after_normalization"))
                continue
            language = detect_language(normalized)
            processed.append(comment.model_copy(update={"text_normalized": normalized}))
            rows.append(
                {
                    "comment_id": comment.comment_id,
                    "video_id": video_id,
                    "parent_comment_id": comment.parent_id,
                    "author": comment.author,
                    "raw_text": comment.text,  # raw preserved verbatim
                    "normalized_text": normalized,
                    "published_at": _iso(comment.published_at),
                    "updated_at": _iso(comment.updated_at),
                    "like_count": comment.like_count,
                    "is_reply": 1 if comment.is_reply else 0,
                    "source": "youtube",
                    "language": language,
                    "processing_status": "READY_FOR_ANALYSIS",
                    "first_seen_at": now_iso,
                    "last_seen_at": now_iso,
                    "created_at": now_iso,
                }
            )
        logger.info(
            "NORMALIZATION_COMPLETED",
            extra={"video_id": video_id, "normalized": len(rows)},
        )

        # 3. DEDUPLICATION (in-page) -----------------------------------------
        unique: Dict[str, Dict[str, object]] = {}
        in_batch_duplicates = 0
        for row in rows:
            key = str(row["comment_id"])
            if key in unique:
                in_batch_duplicates += 1
            unique[key] = row
        # Response order follows first appearance; last occurrence's metadata
        # wins for storage.
        for comment in processed:
            self._processed[comment.comment_id] = comment

        # 4. PERSISTENCE (batched + active-generation guarded) ---------------
        batch = self._settings.comment_batch_size
        inserted = 0
        updated = 0
        page_storage_ok = True
        row_list = list(unique.values())
        try:
            # Parent row first (FK), carrying this page's truthful has_more.
            # The generation guard runs inside the repository's lock: if a
            # switch happened, this returns False and NOTHING is written.
            if not self._repo.upsert_video(
                video_id=video_id,
                metadata=_video_metadata_fields(self._metadata),
                comments_status="ok",
                has_more=page_has_more,
                acquired_at=now_iso or "",
                generation=self._generation,
            ):
                self._mark_superseded()
                return None
            for start in range(0, len(row_list), batch):
                chunk = row_list[start:start + batch]
                stats = self._repo.upsert_comments(
                    chunk, batch, generation=self._generation
                )
                if stats is None:
                    # Superseded between the video write and this chunk: the
                    # switch (if any) already removed those rows - the new
                    # dataset is never touched.
                    self._mark_superseded()
                    return None
                inserted += stats.inserted
                updated += stats.updated
        except sqlite3.Error as exc:
            page_storage_ok = False
            self._storage_ok = False
            logger.error(
                "PERSISTENCE_FAILED",
                extra={"video_id": video_id, "error": type(exc).__name__},
            )
        logger.info(
            "DEDUPLICATION_COMPLETED",
            extra={
                "video_id": video_id,
                "in_batch_duplicates": in_batch_duplicates,
                "already_stored": updated,
            },
        )

        page_fetched = len(comments)
        page_rejected = len(rejected) + normalize_rejects
        page_valid = page_fetched - page_rejected
        page_duplicates = in_batch_duplicates + updated
        duration_ms = int((time.monotonic() - self._started) * 1000)

        # Accumulate the acquisition-aggregated counters.
        self._fetched += page_fetched
        self._valid += page_valid
        self._rejected += page_rejected
        self._duplicates += page_duplicates
        self._inserted += inserted
        self._updated += updated
        self._issues.extend(issues)
        self._pages += 1

        if page_storage_ok:
            logger.info(
                "PERSISTENCE_COMPLETED",
                extra={
                    "video_id": video_id,
                    "inserted": inserted,
                    "updated": updated,
                    "batches": (len(row_list) + batch - 1) // batch,
                    "duration_ms": duration_ms,
                },
            )

        return IngestionReport(
            video_id=video_id,
            fetched=page_fetched,
            valid=page_valid,
            rejected=page_rejected,
            duplicates=page_duplicates,
            inserted=inserted,
            updated=updated,
            storage_ok=page_storage_ok,
            duration_ms=duration_ms,
            rejected_issues=issues,
        )

    # ------------------------------------------------------------- finish
    def _aggregate_report(
        self, finished_at: datetime, duration_ms: int
    ) -> IngestionReport:
        return IngestionReport(
            video_id=self._video_id,
            fetched=self._fetched,
            valid=self._valid,
            rejected=self._rejected,
            duplicates=self._duplicates,
            inserted=self._inserted,
            updated=self._updated,
            storage_ok=self._storage_ok,
            duration_ms=duration_ms,
            rejected_issues=self._issues,
        )

    def finish(
        self, comments_status: str, has_more: bool
    ) -> Tuple[List[Comment], IngestionReport]:
        """Finalize the video row + record ONE aggregated ingestion run.

        Returns (processed comments for the API response, aggregated report).
        The returned comments always reflect the pipeline output even when
        persistence fails - acquisition results are still served. When the
        session was superseded, NOTHING is written: the switch already owns
        the database and no video/run row may outlive it (§14).
        """
        self._ensure_open()
        self._finished = True
        video_id = self._video_id
        now_iso = _iso(self._started_at)

        if self._superseded:
            return list(self._processed.values()), self._aggregate_report(
                datetime.now(timezone.utc),
                int((time.monotonic() - self._started) * 1000),
            )

        # Final parent-row flags: the authoritative comments_status +
        # overall has_more (per-page values are overwritten by this write).
        try:
            if not self._repo.upsert_video(
                video_id=video_id,
                metadata=_video_metadata_fields(self._metadata),
                comments_status=comments_status,
                has_more=has_more,
                acquired_at=now_iso or "",
                generation=self._generation,
            ):
                self._mark_superseded()  # switched away after the last page
        except sqlite3.Error as exc:
            self._storage_ok = False
            logger.error(
                "PERSISTENCE_FAILED",
                extra={"video_id": video_id, "error": type(exc).__name__},
            )

        finished_at = datetime.now(timezone.utc)
        duration_ms = int((time.monotonic() - self._started) * 1000)
        report = self._aggregate_report(finished_at, duration_ms)

        if self._superseded:
            return list(self._processed.values()), report  # no run row

        # Quality run + readiness event ---------------------------------------
        if report.storage_ok:
            try:
                recorded = self._repo.record_ingestion_run(
                    video_id=video_id,
                    fetched=report.fetched,
                    valid=report.valid,
                    rejected=report.rejected,
                    duplicates=report.duplicates,
                    inserted=report.inserted,
                    updated=report.updated,
                    storage_ok=True,
                    started_at=_iso(self._started_at) or "",
                    finished_at=_iso(finished_at) or "",
                    duration_ms=duration_ms,
                    generation=self._generation,
                )
                if not recorded:
                    self._mark_superseded()  # switched away mid-finalize
                else:
                    ready = self._repo.get_status_counts(video_id).get(
                        "READY_FOR_ANALYSIS", 0
                    )
                    logger.info(
                        "PROCESSING_READY",
                        extra={"video_id": video_id, "ready": ready},
                    )
            except sqlite3.Error as exc:
                logger.error(
                    "PERSISTENCE_FAILED",
                    extra={"video_id": video_id, "error": type(exc).__name__},
                )

        return list(self._processed.values()), report


class IngestionService:
    def __init__(self, repository: DatasetRepository, settings: Settings) -> None:
        self._repo = repository
        self._settings = settings

    # ------------------------------------------------------------------ api
    def begin(
        self,
        video_id: str,
        metadata: VideoMetadata,
        acquired_at: datetime,
        generation: Optional[int] = None,
    ) -> IngestionSession:
        """Open an incremental session (no database writes until the first
        page arrives - a failed fetch before page 1 leaves nothing behind).

        `generation` is the active-dataset token captured by
        `DatasetService.ensure_active` (Sprint 4.2): pass it in production
        so a video switch mid-acquisition rejects every later write;
        `None` keeps the unguarded legacy/unit-test behavior.
        """
        return IngestionSession(
            repository=self._repo,
            settings=self._settings,
            video_id=video_id,
            metadata=metadata,
            acquired_at=acquired_at,
            generation=generation,
        )

    def ingest(
        self,
        video_id: str,
        metadata: VideoMetadata,
        comments: Sequence[Comment],
        comments_status: str,
        has_more: bool,
        acquired_at: datetime,
    ) -> Tuple[List[Comment], IngestionReport]:
        """Run the full pipeline for one acquisition in a single batch.

        Thin wrapper over the incremental session (begin -> one page ->
        finish) so the original single-shot contract - including the
        `disabled`/empty paths and the ingestion-run invariants - is
        byte-for-byte preserved for existing callers.
        """
        session = self.begin(video_id, metadata, acquired_at)
        if comments:
            session.add_page(comments, page_has_more=has_more)
        return session.finish(comments_status, has_more)

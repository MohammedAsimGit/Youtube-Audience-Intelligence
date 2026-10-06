"""Dataset service - the read path over stored datasets.

Responsibilities:
- Freshness policy (single source of truth for §25): a stored dataset is
  served without touching the YouTube API while its age is strictly below
  `DATASET_CACHE_TTL_SECONDS`. Nothing else in the codebase may hardcode
  freshness.
- Rebuild the stable `VideoDataResponse` contract from stored rows so the
  Chrome extension cannot tell (and does not need to care) whether data came
  from YouTube just now or from the dataset store - only `source.cached`
  differs.
- Dataset statistics via SQL aggregates (batch-friendly; rows are never
  loaded into Python just to count them).
"""
from datetime import datetime, timezone
from typing import List, NamedTuple, Optional

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.repository import DatasetRepository
from app.models.internal import (
    Comment,
    CommentCollection,
    DatasetStats,
    IngestionSummary,
    SourceInfo,
    VideoDataResponse,
    VideoMetadata,
    VideoStatistics,
)
from app.utils.normalize import parse_dt

logger = get_logger("dataset")

# Reads pull rows through the repository's keyset reader in these chunks -
# bounded memory regardless of dataset size (§16 batch-oriented design).
_READ_BATCH = 500


class Activation(NamedTuple):
    """Result of `DatasetService.ensure_active` (Sprint 4.2).

    generation - the active dataset's monotonic token; acquisition sessions
                 carry it into every guarded write (race safety).
    changed    - the active video identity changed (previous dataset
                 replaced) - callers must clear their in-memory caches so
                 L1 follows the same single-active-dataset policy as L2.
    """

    generation: int
    changed: bool


class DatasetInfo(NamedTuple):
    """Everything the Sprint 8 incremental fetch needs from ONE stored row.

    Returned by `DatasetService.get_dataset_info` so an incremental poll can
    run WITHOUT a videos.list round-trip (0 quota): metadata for the
    ingestion session, the flags `finish()` must truthfully preserve, and
    the stored total for the dataset-cap guard.
    """

    metadata: VideoMetadata
    comments_status: str
    has_more: bool
    total_comments: int


def _row_to_comment(row) -> Comment:
    return Comment(
        comment_id=row["comment_id"],
        video_id=row["video_id"],
        author=row["author"],
        text=row["raw_text"],
        text_normalized=row["normalized_text"],
        published_at=parse_dt(row["published_at"]),
        updated_at=parse_dt(row["updated_at"]),
        like_count=int(row["like_count"]),
        is_reply=bool(row["is_reply"]),
        parent_id=row["parent_comment_id"],
    )


def _row_to_video(row) -> VideoMetadata:
    return VideoMetadata(
        video_id=row["video_id"],
        title=row["title"],
        description=row["description"],
        channel_id=row["channel_id"],
        channel_title=row["channel_title"],
        published_at=parse_dt(row["published_at"]),
        category_id=row["category_id"],
        duration=row["duration"],
        statistics=VideoStatistics(
            view_count=row["view_count"],
            like_count=row["like_count"],
            comment_count=row["comment_count"],
        ),
    )


class DatasetService:
    def __init__(self, repository: DatasetRepository, settings: Settings) -> None:
        self._repo = repository
        self._settings = settings

    # ---------------------------------------------------------- lifecycle
    def ensure_active(self, video_id: str) -> Activation:
        """Make `video_id` the single active working dataset (Sprint 4.2).

        This is the ONLY lifecycle transition in the system and lives here
        (dataset lifecycle service, Option B of §7) while the state itself
        lives on the `videos` row (Option A: `is_active` +
        `dataset_generation`):

        - Same video already active -> no destructive cleanup (§19/§20);
          L1/L2 freshness alone decides whether to re-acquire.
        - Different video (or nothing active yet) -> atomic repository
          switch: remove every other working dataset, activate the new one
          with a fresh generation. A pre-4.2 row for the requested video is
          adopted instead of discarded, so upgrading never throws away a
          still-fresh dataset.

        Read-only endpoints (stats/sentiment) never call this - they answer
        strictly for their video id and 404 when it is not the working
        dataset (§40).
        """
        active = self._repo.get_active_video()
        if active is not None and active["video_id"] == video_id:
            return Activation(
                generation=int(active["dataset_generation"]), changed=False
            )
        generation = self._repo.switch_active_video(video_id)
        logger.info(
            "ACTIVE_VIDEO_SWITCHED",
            extra={
                "video_id": video_id,
                "previous_video_id": (
                    active["video_id"] if active is not None else None
                ),
                "generation": generation,
            },
        )
        return Activation(generation=generation, changed=True)

    # ------------------------------------------------------------ freshness
    def _age_seconds(self, row, now: Optional[datetime] = None) -> float:
        """Dataset age from `last_acquired_at` (None when unparseable)."""
        acquired_at = parse_dt(row["last_acquired_at"])
        if acquired_at is None:
            return float("inf")
        current = now or datetime.now(timezone.utc)
        return (current - acquired_at).total_seconds()

    def _is_fresh_row(self, row, now: Optional[datetime] = None) -> bool:
        """Central freshness rule (§24/§25): age < DATASET_CACHE_TTL_SECONDS."""
        return self._age_seconds(row, now) < float(
            self._settings.dataset_cache_ttl_seconds
        )

    def is_fresh(self, video_id: str, now: Optional[datetime] = None) -> bool:
        row = self._repo.get_video(video_id)
        if row is None:
            return False
        return self._is_fresh_row(row, now)

    def needs_acquisition(self, video_id: str, now: Optional[datetime] = None) -> bool:
        """True when the stored dataset cannot answer without YouTube
        (Sprint 4.3 §22/§40).

        Freshness alone is NOT enough: a job retry after a PARTIAL
        acquisition must CONTINUE collecting (the incremental pages keep
        `last_acquired_at` young, so the row looks fresh while `has_more`
        is still set and the configured limit was not reached). Conversely
        a fresh dataset is served without quota when it is complete,
        comment-free, or comments-disabled - the §25 quota rule.
        """
        row = self._repo.get_video(video_id)
        if row is None or not self._is_fresh_row(row, now):
            return True
        if row["comments_status"] != "ok":
            return False  # 'none' / 'disabled': nothing further to collect
        if not bool(row["has_more"]):
            return False  # complete dataset (all available comments stored)
        # Interrupted run: more comments were indicated but the configured
        # cap was never reached -> a retry continues the acquisition.
        stored = int(self._repo.get_comment_stats(video_id)["total"])
        return stored < self._settings.comment_acquisition_max_comments

    # ------------------------------------------------- Sprint 8: incremental
    def get_dataset_info(self, video_id: str) -> Optional[DatasetInfo]:
        """One stored row -> metadata + flags + total for an incremental poll.

        Sprint 8 (§17): the realtime monitor refreshes comments WITHOUT a
        videos.list call - the metadata captured at first acquisition is
        reused, so a poll costs only commentThreads.list quota (1 unit).
        None when the video was never acquired (nothing to increment).
        """
        row = self._repo.get_video(video_id)
        if row is None:
            return None
        return DatasetInfo(
            metadata=_row_to_video(row),
            comments_status=str(row["comments_status"]),
            has_more=bool(row["has_more"]),
            total_comments=int(self._repo.get_comment_stats(video_id)["total"]),
        )

    def existing_comment_ids(self, comment_ids) -> set:
        """Which of `comment_ids` are already stored (Sprint 8 dedup probe).

        The incremental fetch stops at the first already-known comment -
        newest-first ordering means everything below it is older and was
        acquired by a previous run, so the probe bounds quota to the truly
        new head of the thread list.
        """
        return self._repo.existing_comment_ids(comment_ids)

    def get_fresh_response(self, video_id: str) -> Optional[VideoDataResponse]:
        """Stored dataset -> stable contract, or None when absent/stale."""
        row = self._repo.get_video(video_id)
        if row is None or not self._is_fresh_row(row):
            return None
        age_seconds = self._age_seconds(row)

        comments: List[Comment] = []
        for comment_row in self._repo.iter_comments(video_id, _READ_BATCH):
            comments.append(_row_to_comment(comment_row))

        acquired_at = parse_dt(row["last_acquired_at"])
        assert acquired_at is not None  # guaranteed by _is_fresh_row above
        response = VideoDataResponse(
            video=_row_to_video(row),
            comments=CommentCollection(
                items=comments,
                count=len(comments),
                has_more=bool(row["has_more"]),
                status=row["comments_status"],
            ),
            source=SourceInfo(
                provider="youtube",
                retrieved_at=acquired_at,
                cached=True,
            ),
        )
        logger.info(
            "DATASET_SERVED",
            extra={
                "video_id": video_id,
                "comments": response.comments.count,
                "age_seconds": int(age_seconds),
            },
        )
        return response

    # --------------------------------------------------------------- stats
    def get_stats(self, video_id: str) -> Optional[DatasetStats]:
        """Aggregates for one video's dataset (SQL-side; None if untracked)."""
        video_row = self._repo.get_video(video_id)
        if video_row is None:
            return None
        aggregates = self._repo.get_comment_stats(video_id)
        status_counts = self._repo.get_status_counts(video_id)
        run_row = self._repo.get_latest_ingestion_run(video_id)

        last_ingest: Optional[IngestionSummary] = None
        if run_row is not None:
            last_ingest = IngestionSummary(
                fetched=int(run_row["fetched_count"]),
                valid=int(run_row["valid_count"]),
                rejected=int(run_row["rejected_count"]),
                duplicates=int(run_row["duplicate_count"]),
                inserted=int(run_row["inserted_count"]),
                updated=int(run_row["updated_count"]),
                storage_ok=bool(run_row["storage_ok"]),
                duration_ms=int(run_row["duration_ms"]),
                finished_at=parse_dt(run_row["finished_at"]),
            )

        return DatasetStats(
            video_id=video_id,
            total_comments=int(aggregates["total"]),
            unique_comments=int(aggregates["unique"]),
            reply_count=int(aggregates["replies"]),
            top_level_comment_count=int(aggregates["total"]) - int(aggregates["replies"]),
            oldest_comment_timestamp=parse_dt(aggregates["oldest"]),
            newest_comment_timestamp=parse_dt(aggregates["newest"]),
            last_acquired_at=parse_dt(video_row["last_acquired_at"]),
            comments_status=video_row["comments_status"],
            processing_status=status_counts,
            last_ingest=last_ingest,
        )

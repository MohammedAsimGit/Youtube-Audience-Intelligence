"""Video data acquisition service - the COLLECT/SERVE entry point.

Pipeline position (Sprint 3 + 4.1):
    COLLECT -> INGEST(ingestion service, one page at a time) -> VALIDATE
            -> CLEAN -> NORMALIZE -> DEDUPLICATE -> PERSIST -> SERVE
Sprint 4.1: pages are persisted incrementally (page -> batch -> SQLite ->
next page); the dataset is never fully buffered before writing
(docs/architecture/data-pipeline.md).

Sprint 5.1 (§6 overlap, measured bottleneck): acquisition is a bounded
producer/consumer pipeline on plain threads (no broker, no Redis/Celery):

    FETCH loop (this service)             INGEST worker (child thread)
    -----------------------               ---------------------------
    commentThreads.list (network I/O) ->  validate -> normalize ->
    bounded queue (maxsize=2 pages)       language detect -> dedup ->
    nextPageToken guards, cap guard       SQLite persist -> OPTIONAL
                                          interim sentiment drain (§9)

Rationale (benchmark, backend/benchmarks/bench_pipeline.py): language
detection was 89% of pipeline time (~5.9 ms/comment) while a YouTube page
fetch is pure I/O - overlapping them hides CPU behind network waits instead
of serializing them, and the first READY batch reaches the sentiment engine
after page 1 instead of after the whole dataset. The queue is BOUNDED
(2 pages) so memory stays flat regardless of dataset size (§5); SQLite's
single locked connection is the durable buffer between ingest and analysis.

Quota model (verified): videos.list = 1 unit, commentThreads.list = 1 unit
per page. Three cache layers protect quota:
    L1 in-memory TTL (hot) -> L2 stored dataset (freshness TTL) -> L3 YouTube
so repeated Analyze clicks cost 0 units within the freshness window.

Pagination (Sprint 4.1, spec §9):
- PAGE SIZE is `max_comments_per_request` (<= 100, YouTube's page cap);
  the DATASET LIMIT is `comment_acquisition_max_comments` (per video,
  per acquisition run). They are different settings - never conflated.
- Walk `nextPageToken` until YouTube runs out, the dataset limit is hit,
  the `max_api_pages` safety bound is hit, or a token repeats (loop guard).
- `has_more` is only True while YouTube actually handed us a next token;
  availability is never inferred when the API does not indicate it.
"""
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional, Set

from app.core.config import Settings
from app.core.errors import (
    AcquisitionError,
    CommentsDisabled,
    MissingApiKey,
    VideoIdInvalid,
    VideoNotFound,
)
from app.core.logging import get_logger
from app.models.internal import (
    Comment,
    CommentCollection,
    SourceInfo,
    VideoDataResponse,
    VideoMetadata,
    VideoStatistics,
)
from app.services.cache import TTLCache
from app.services.dataset import DatasetService
from app.services.ingestion import IngestionService
from app.utils.normalize import (
    normalize_thread_comments,
    parse_dt,
    parse_int,
)

logger = get_logger("acquisition")

# §5/§6: bounded hand-off between the fetch loop and the ingest worker.
# Two pages in flight keeps the pipeline busy without ever holding more
# than a couple of raw pages in memory, independent of dataset size.
_INGEST_QUEUE_MAX_PAGES = 2

VIDEO_ID_LENGTH = 11
_VALID_ID_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)


def validate_video_id(video_id: str) -> str:
    """Path-level validation before any upstream request (422 on failure).

    YouTube video ids are exactly 11 characters from [A-Za-z0-9_-] - the
    same rule the Chrome extension's URL parser enforces.
    """
    if (
        len(video_id) != VIDEO_ID_LENGTH
        or any(ch not in _VALID_ID_CHARS for ch in video_id)
    ):
        raise VideoIdInvalid("video id failed validation")
    return video_id


@dataclass(frozen=True)
class AcquisitionOutcome:
    """Result of one paginated acquisition run (Sprint 4.1, internal).

    comments    - validated/deduped items for the API response
    status      - ok | none | disabled (the stored comments_status)
    has_more    - YouTube handed a next page token when we stopped
                  (True => more comments may exist beyond this run)
    pages_fetched - commentThreads.list pages actually requested
    fetched     - comments returned by YouTube this run (pre-validation)
    storage_ok  - whether persistence succeeded for the whole run
    superseded  - another video became active mid-run (Sprint 4.2): every
                  guarded write was dropped, the result must not be cached
    """

    comments: List[Comment]
    status: str
    has_more: bool
    pages_fetched: int
    fetched: int
    storage_ok: bool
    superseded: bool = False


@dataclass(frozen=True)
class AcquisitionRun:
    """Everything the sync contract builder needs from one L3 acquisition.

    Shared by the original `GET /api/videos/{id}` flow and the Sprint 4.3
    background job (the job discards the response and keeps only the
    stored dataset + outcome).
    """

    metadata: VideoMetadata
    retrieved_at: datetime
    outcome: AcquisitionOutcome


@dataclass(frozen=True)
class IncrementalOutcome:
    """Result of one Sprint 8 incremental (newest-first) poll.

    pages_fetched - commentThreads.list pages requested this poll
    fetched       - comments YouTube returned this poll (pre-validation,
                    including already-known ones on scanned pages)
    new_comments  - normalized comments that were NOT yet stored (probe)
    inserted      - rows actually inserted (post-validation; <= new_comments)
    updated       - rows refreshed (probe/write race overlap; normally 0)
    status        - comments_status implied by this poll's finalization
    has_more      - truthful final flag (preserved from the stored row unless
                    this poll OBSERVED YouTube's end of list or stopped on
                    quota grounds - availability is never inferred)
    storage_ok    - every write this poll persisted successfully
    superseded    - another video became active mid-poll (all writes dropped)
    stop_reason   - why the scan stopped: boundary (first page whose ids are
                    all stored = steady state, 1 unit of quota), exhausted
                    (YouTube handed no next token), cap / pages_bound /
                    cancelled / superseded / disabled / token_loop / error
    """

    pages_fetched: int
    fetched: int
    new_comments: int
    inserted: int
    updated: int
    status: str
    has_more: bool
    storage_ok: bool
    superseded: bool
    stop_reason: str


@dataclass(frozen=True)
class _PageMessage:
    """One fetched page handed from the producer (fetch loop) to the
    ingest worker. `page_has_more` is computed by the producer at fetch
    time so each persisted page carries its truthful mid-run flag (§7)."""

    page: object  # YtCommentThreadsResponse (client-typed; duck-typed here)
    page_has_more: bool


@dataclass(frozen=True)
class _FinishMessage:
    """Terminal signal for the ingest worker - how the fetch loop ended.

    kind: "done"     - normal completion (incl. cap/pages/token/cancel stop)
          "disabled" - commentsDisabled surfaced from the fetch side
          "error"    - a fetch-side exception; the worker finalizes the
                       session (pages already persisted stay stored) and
                       the producer re-raises `error` after the join.
    """

    kind: str
    has_more: bool
    error: Optional[BaseException] = None


def _count_raw_comments(page) -> int:
    """Comments present in one raw response (top-level threads + replies).

    Producer-side cap accounting ONLY: walks already-parsed schema lists
    (no normalization, no Python model construction) so the fetch loop can
    stop at COMMENT_ACQUISITION_MAX_COMMENTS without waiting for the ingest
    worker. The worker re-enforces the cap exactly on the normalized count
    (same trim rule as Sprint 4.1), so a stored dataset can never exceed
    the configured maximum.
    """
    total = 0
    for thread in page.items:
        total += 1
        replies = getattr(thread, "replies", None)
        if replies is not None and replies.comments:
            total += len(replies.comments)
    return total


class VideoDataService:
    """Orchestrates cache -> dataset store -> YouTube -> ingestion -> contract."""

    def __init__(
        self,
        client: object,
        settings: Settings,
        cache: TTLCache,
        ingestion: IngestionService,
        dataset: DatasetService,
    ) -> None:
        self._client = client
        self._settings = settings
        self._cache = cache
        self._ingestion = ingestion
        self._dataset = dataset

    # ------------------------------------------------------------------ cache
    @staticmethod
    def _cache_key(video_id: str) -> str:
        return f"video:{video_id}"

    # ----------------------------------------------------------------- acquire
    def get_video_data(self, video_id: str) -> VideoDataResponse:
        validate_video_id(video_id)
        logger.debug("video id validated", extra={"video_id": video_id})

        # Sprint 4.2 §9: THIS acquisition flow is the extension's explicit
        # active-video transition - read endpoints and mere navigation
        # never activate (and therefore never delete) a dataset. Validation
        # runs first so an invalid id can never trigger a destructive
        # switch (422 before any lifecycle change).
        activation = self._dataset.ensure_active(video_id)
        if activation.changed:
            # L1 mirrors the single-active-dataset policy (§30): entries
            # for the replaced video must not outlive it.
            self._cache.clear()

        # L1: hot in-memory cache (0 quota, sub-millisecond).
        key = self._cache_key(video_id)
        cached = self._cache.get(key)
        if cached is not None:
            logger.info("cache hit", extra={"video_id": video_id})
            dumped = cached.model_dump()
            response = VideoDataResponse.model_validate(dumped)
            response.source.cached = True
            return response

        # L2: fresh stored dataset (0 quota) - central freshness policy.
        stored = self._dataset.get_fresh_response(video_id)
        if stored is not None:
            self._cache.set(key, stored.model_copy(deep=True))
            return stored

        # L3: real acquisition (official YouTube Data API only).
        run = self.acquire_dataset(video_id, activation.generation)

        response = VideoDataResponse(
            video=run.metadata,
            comments=CommentCollection(
                items=run.outcome.comments,
                count=len(run.outcome.comments),
                has_more=run.outcome.has_more,
                status=run.outcome.status,
            ),
            source=SourceInfo(
                provider="youtube",
                retrieved_at=run.retrieved_at,
                cached=False,
            ),
        )
        if not run.outcome.superseded:
            # Superseded results are never cached: L1 holds only the active
            # video's dataset (§30).
            self._cache.set(key, response.model_copy(deep=True))
        return response

    # --------------------------------------------------------------- L3 run
    def acquire_dataset(
        self,
        video_id: str,
        generation: int,
        should_cancel: Optional[Callable[[], bool]] = None,
        on_page: Optional[Callable[..., None]] = None,
        after_page: Optional[Callable[[], None]] = None,
    ) -> AcquisitionRun:
        """Metadata + paginated comment acquisition with persistence.

        The single L3 code path behind BOTH the synchronous GET contract
        and the Sprint 4.3 background job. No cache interaction here (L1/
        L2 are owned by the callers). `should_cancel` is polled between
        pages so a cancelled job stops fetching without spending quota;
        `on_page(pages, collected, has_more)` reports REAL progress after
        each persisted page (never before - progress is only claimed for
        data that is actually in the store). Runs the fetch loop and the
        ingest worker as a bounded producer/consumer pair and returns only
        after the worker has fully drained (callers see identical
        post-return state as the old serial loop).

        `after_page()` (Sprint 5.1 §6/§9) is invoked by the INGEST worker
        after every persisted page - the job uses it to drain ready rows
        while fetching continues, so first insight arrives after page 1.
        """
        started = time.monotonic()
        logger.info("ACQUISITION_STARTED", extra={"video_id": video_id})
        if not self._settings.youtube_api_key:
            raise MissingApiKey("YOUTUBE_API_KEY is not configured")

        retrieved_at = datetime.now(timezone.utc)
        metadata = self._fetch_metadata(video_id)
        outcome = self._acquire_comments(
            video_id,
            metadata,
            retrieved_at,
            generation,
            should_cancel=should_cancel,
            on_page=on_page,
            after_page=after_page,
        )
        logger.info(
            "ACQUISITION_COMPLETED",
            extra={
                "video_id": video_id,
                "comments": len(outcome.comments),
                "fetched": outcome.fetched,
                "pages": outcome.pages_fetched,
                "has_more": outcome.has_more,
                "status": outcome.status,
                "storage_ok": outcome.storage_ok,
                "superseded": outcome.superseded,
                "generation": generation,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            },
        )
        return AcquisitionRun(metadata=metadata, retrieved_at=retrieved_at, outcome=outcome)

    # -------------------------------------------------------- Sprint 8: incremental
    def acquire_incremental(
        self,
        video_id: str,
        generation: int,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> IncrementalOutcome:
        """Newest-first incremental refresh for the realtime monitor (§16).

        Fetches commentThreads.list pages in `order=time` (the client's
        fixed ordering - newest first) and stops at the FIRST page whose
        comment ids are all already stored: everything below that boundary
        is older than anything a previous run missed, so a steady-state
        poll costs exactly ONE unit of quota (§17 "no unnecessary API
        requests"). Only comments that pass the probe are handed to the
        EXISTING ingestion session (validate -> normalize -> language detect
        -> dedup -> generation-guarded upsert), so already-PROCESSED rows
        are never rewritten, never re-queued, and never re-analyzed
        (§14/§18 - no second pipeline, no full re-analysis).

        Guarantees:
        - No videos.list call: the STORED row's metadata feeds the session
          (0 quota for metadata; only commentThreads.list is spent).
        - Idempotent: a comment that races in between probe and write is
          absorbed by the comment_id upsert (counts as updated, never a
          duplicate row - §11).
        - Cap-safe: inserts are trimmed so the stored total can never
          exceed COMMENT_ACQUISITION_MAX_COMMENTS.
        - Generation-guarded: a video switch mid-poll drops every write
          (outcome.superseded) exactly like the full acquisition (§13).
        - Bounded: max_api_pages + the dataset cap bound a burst backlog;
          a backlog poll pages forward, the next poll re-enters at the top.
        - `has_more` is preserved from the stored row unless this poll
          OBSERVED YouTube's end of list (False) or stopped early while a
          next token was in hand (token presence) - availability is never
          inferred (§9 of the original spec).

        Categorized upstream errors are re-raised AFTER finalizing any
        pages already persisted (§31: partial progress is kept);
        `commentsDisabled` is a valid terminal status, not an error.
        """
        started = time.monotonic()
        validate_video_id(video_id)
        if not self._settings.youtube_api_key:
            raise MissingApiKey("YOUTUBE_API_KEY is not configured")

        info = self._dataset.get_dataset_info(video_id)
        if info is None:
            raise VideoNotFound("no stored dataset to increment")

        cap = self._settings.comment_acquisition_max_comments
        page_size = self._settings.max_comments_per_request
        room = cap - info.total_comments
        if room <= 0:
            # Dataset at the configured limit: never fetch what we could
            # not store (the monitor logs this and backs off to its next
            # interval instead of spending quota).
            return IncrementalOutcome(
                pages_fetched=0,
                fetched=0,
                new_comments=0,
                inserted=0,
                updated=0,
                status=info.comments_status,
                has_more=info.has_more,
                storage_ok=True,
                superseded=False,
                stop_reason="cap",
            )

        retrieved_at = datetime.now(timezone.utc)
        session = self._ingestion.begin(
            video_id, info.metadata, retrieved_at, generation=generation
        )

        pages = 0
        fetched = 0
        new_seen = 0
        inserted = 0
        updated = 0
        storage_ok = True
        disabled = False
        stop_reason = "boundary"
        # Default: preserve the stored flag. Only an OBSERVED end of list
        # (no next token) or an early stop with a token in hand rewrites it.
        has_more = info.has_more
        observed_token: Optional[str] = None
        page_token: Optional[str] = None
        seen_tokens: Set[str] = set()
        fetch_error: Optional[BaseException] = None

        try:
            while True:
                if session.superseded:
                    stop_reason = "superseded"
                    break
                if should_cancel is not None and should_cancel():
                    # Cancelled BETWEEN pages: no quota after the request.
                    stop_reason = "cancelled"
                    break
                if pages >= self._settings.max_api_pages:
                    stop_reason = "pages_bound"
                    has_more = observed_token is not None
                    break
                if room <= 0:
                    stop_reason = "cap"
                    has_more = observed_token is not None
                    break

                page = self._client.get_comment_threads(  # type: ignore[attr-defined]
                    video_id,
                    max_results=min(page_size, room),
                    page_token=page_token,
                )
                pages += 1
                observed_token = page.next_page_token

                # Probe FIRST: `order=time` puts unknown ids at the top, so
                # a page with zero new ids marks the boundary with stored
                # data - stop without writing anything (read-only poll).
                comments = normalize_thread_comments(page, video_id)
                fetched += len(comments)
                known = self._dataset.existing_comment_ids(
                    comment.comment_id for comment in comments
                )
                fresh = [c for c in comments if c.comment_id not in known]
                if not fresh:
                    stop_reason = "boundary"
                    has_more = info.has_more  # stored truth stands
                    break

                new_seen += len(fresh)
                if len(fresh) > room:
                    fresh = fresh[:room]  # cap-safe (§5: bounded storage)
                report = session.add_page(
                    fresh, page_has_more=observed_token is not None
                )
                if report is None:
                    # Superseded (ACQUISITION_SUPERSEDED logged): the switch
                    # owns the database now; every write was dropped.
                    stop_reason = "superseded"
                    break
                inserted += report.inserted
                updated += report.updated
                storage_ok = storage_ok and report.storage_ok
                room -= report.inserted

                if observed_token is None:
                    stop_reason = "exhausted"  # saw YouTube's whole list
                    has_more = False
                    break
                if observed_token in seen_tokens:
                    logger.warning(
                        "INCREMENTAL_PAGINATION_TOKEN_LOOP",
                        extra={"video_id": video_id, "page": pages},
                    )
                    stop_reason = "token_loop"
                    has_more = False
                    break
                seen_tokens.add(observed_token)
                page_token = observed_token
        except CommentsDisabled:
            # Valid terminal state, not an error - mirrors the full path:
            # persist `disabled` (even with nothing new) and stop.
            disabled = True
            stop_reason = "disabled"
            has_more = False
        except AcquisitionError as exc:
            fetch_error = exc
            stop_reason = "error"
        except sqlite3.Error as exc:
            # Storage failed mid-scan: degrade exactly like the full path
            # (persisted pages stay, storage_ok latches False, §31).
            fetch_error = exc
            stop_reason = "error"
            storage_ok = False
            logger.error(
                "INCREMENTAL_STORE_FAILURE",
                extra={"video_id": video_id, "error": type(exc).__name__},
            )

        # ------------------------------------------------ finalization
        status = info.comments_status
        if disabled and not session.superseded:
            session.finish("disabled", has_more=False)
            status = "disabled"
        elif session.pages_ingested > 0 and not session.superseded:
            _, report = session.finish(
                "ok" if session.fetched else "none", has_more
            )
            storage_ok = storage_ok and report.storage_ok
            status = "ok" if report.fetched else "none"
        # else: nothing persisted (pure probe poll) -> stored flags stand,
        # no zero-count ingestion_runs row, last_acquired_at untouched.

        outcome = IncrementalOutcome(
            pages_fetched=pages,
            fetched=fetched,
            new_comments=new_seen,
            inserted=inserted,
            updated=updated,
            status=status,
            has_more=has_more,
            storage_ok=storage_ok,
            superseded=session.superseded,
            stop_reason=stop_reason,
        )
        logger.info(
            "INCREMENTAL_ACQUISITION_COMPLETED",
            extra={
                "video_id": video_id,
                "pages": pages,
                "fetched": fetched,
                "new": new_seen,
                "inserted": inserted,
                "updated": updated,
                "stop_reason": stop_reason,
                "has_more": has_more,
                "storage_ok": storage_ok,
                "superseded": session.superseded,
                "generation": generation,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            },
        )
        if fetch_error is not None:
            # Pages already persisted stay stored (worker-error ordering of
            # the full path); the categorized error reaches the monitor,
            # which logs it and retries on the next interval.
            raise fetch_error
        return outcome

    # -------------------------------------------------------------- metadata
    def _fetch_metadata(self, video_id: str) -> VideoMetadata:
        page = self._client.get_video(video_id)  # type: ignore[attr-defined]
        if not page.items:
            raise VideoNotFound("video not found or has no accessible metadata")
        item = page.items[0]
        snippet = item.snippet
        stats = item.statistics
        return VideoMetadata(
            video_id=item.id,
            title=snippet.title if snippet else None,
            description=snippet.description if snippet else None,
            channel_id=snippet.channel_id if snippet else None,
            channel_title=snippet.channel_title if snippet else None,
            published_at=parse_dt(snippet.published_at if snippet else None),
            category_id=snippet.category_id if snippet else None,
            duration=None,  # videos.list snippet does not provide duration
            statistics=VideoStatistics(
                view_count=parse_int(stats.view_count) if stats else None,
                like_count=parse_int(stats.like_count) if stats else None,
                comment_count=parse_int(stats.comment_count) if stats else None,
            ),
        )

    # --------------------------------------------------------------- comments
    def _acquire_comments(
        self,
        video_id: str,
        metadata: VideoMetadata,
        retrieved_at: datetime,
        generation: int,
        should_cancel: Optional[Callable[[], bool]] = None,
        on_page: Optional[Callable[..., None]] = None,
        after_page: Optional[Callable[[], None]] = None,
    ) -> AcquisitionOutcome:
        """Paginated comment acquisition - bounded producer/consumer (§6).

        Producer (this thread) walks YouTube's pages until one of (spec §9):
            - YouTube returns no nextPageToken (all available comments),
            - `comment_acquisition_max_comments` is reached (per run),
            - `max_api_pages` safety bound is reached,
            - a page token repeats (pagination-loop guard),
            - the session is SUPERSEDED (Sprint 4.2 §11/§13) or the job was
              cancelled (Sprint 4.3 §5) - fetching stops, no quota is spent
              on a dead dataset, and guarded writes were already dropped
              inside the repository's lock.
        Each raw page is handed to the INGEST worker through a BOUNDED queue
        (`_INGEST_QUEUE_MAX_PAGES`); the worker validates, normalizes,
        language-detects, deduplicates and persists ONE page at a time, so a
        mid-run failure keeps everything persisted so far (§31) and memory
        stays flat regardless of dataset size (§5).

        The worker reports REAL progress via `on_page` only AFTER the page
        is persisted, then invokes `after_page()` (the job's interim
        sentiment drain - §6/§9 first insight). This method returns only
        after the worker has fully drained and finalized the session, so
        callers observe the exact post-conditions of the old serial loop.

        `commentsDisabled` is a valid outcome (status="disabled"), not an
        error; quota/timeout/network failures propagate as categorized
        errors after any completed pages were persisted.
        """
        cap = self._settings.comment_acquisition_max_comments
        page_size = self._settings.max_comments_per_request
        session = self._ingestion.begin(
            video_id, metadata, retrieved_at, generation=generation
        )

        pages = 0
        raw_seen = 0  # producer-side cap accounting (threads + replies seen)
        has_more = False
        page_token: Optional[str] = None
        seen_tokens: Set[str] = set()

        # ----------------------------------------------------- ingest worker
        # Worker-owned result slots (written by the worker, read by the
        # producer after join() - the join is the memory barrier).
        worker_error: Optional[BaseException] = None
        worker_processed: List[Comment] = []
        worker_report = None
        consumer_broken = threading.Event()
        handoff: "queue.Queue" = queue.Queue(maxsize=_INGEST_QUEUE_MAX_PAGES)

        def _ingest_worker() -> None:
            nonlocal worker_error, worker_processed, worker_report
            pages_done = 0
            try:
                while True:
                    message = handoff.get()
                    if isinstance(message, _FinishMessage):
                        # Terminal signal: finalize exactly once, mirroring
                        # the serial loop's outcome paths.
                        if message.kind == "disabled":
                            worker_processed, worker_report = session.finish(
                                "disabled", has_more=False
                            )
                        elif message.kind == "error":
                            # Categorized fetch error: pages already
                            # persisted stay in the dataset and the run is
                            # finalized so stats/hasMore reflect reality
                            # (§31). The producer re-raises `message.error`.
                            if session.pages_ingested > 0:
                                logger.warning(
                                    "ACQUISITION_INTERRUPTED",
                                    extra={
                                        "video_id": video_id,
                                        "pages": pages_done,
                                        "fetched": session.fetched,
                                        "error": (
                                            type(message.error).__name__
                                            if message.error is not None
                                            else "Unknown"
                                        ),
                                    },
                                )
                                session.finish(
                                    "ok" if session.fetched else "none",
                                    message.has_more,
                                )
                        else:  # "done": normal, cap/pages/token/cancel stop
                            status = "ok" if session.fetched > 0 else "none"
                            worker_processed, worker_report = session.finish(
                                status, message.has_more
                            )
                        return

                    # _PageMessage ------------------------------------------------
                    if session.superseded:
                        # Short-circuit: never touch the new working dataset;
                        # keep draining so the producer can always enqueue its
                        # finish message (no deadlock on the bounded queue).
                        continue
                    normalized = normalize_thread_comments(
                        message.page, video_id  # type: ignore[arg-type]
                    )
                    remaining = cap - session.fetched
                    if len(normalized) > remaining:
                        # Safety: never exceed the configured dataset limit
                        # even if an upstream response over-delivers.
                        normalized = normalized[:remaining]
                    pages_done += 1
                    page_report = session.add_page(
                        normalized, page_has_more=message.page_has_more
                    )
                    if page_report is None:
                        # Superseded (ACQUISITION_SUPERSEDED logged): drop
                        # this page and every queued page after it.
                        continue
                    logger.info(
                        "comment page retrieved",
                        extra={
                            "video_id": video_id,
                            "page": pages_done,
                            "comments": len(normalized),
                            "units": 1,
                            "storage_ok": page_report.storage_ok,
                        },
                    )
                    if on_page is not None:
                        # REAL progress, reported only AFTER the page was
                        # persisted (§7: never fabricate progress).
                        on_page(
                            pages=pages_done,
                            collected=session.fetched,
                            has_more=message.page_has_more,
                        )
                    if after_page is not None:
                        # §6/§9: opportunistic sentiment drain while the
                        # producer keeps fetching (never blocks ingestion -
                        # callbacks own their own error handling).
                        after_page()
            except Exception as exc:  # noqa: BLE001 - surfaced after join
                worker_error = exc
                consumer_broken.set()
                logger.error(
                    "INGEST_WORKER_FAILED",
                    extra={
                        "video_id": video_id,
                        "pages": pages_done,
                        "error": type(exc).__name__,
                    },
                )
                # Drain to the finish message so the producer never
                # deadlocks on the bounded queue, finalizing best-effort.
                while True:
                    message = handoff.get()
                    if isinstance(message, _FinishMessage):
                        if session.pages_ingested > 0:
                            try:
                                session.finish(
                                    "ok" if session.fetched else "none",
                                    message.has_more,
                                )
                            except Exception:  # noqa: BLE001 - store broken
                                pass
                        return

        worker = threading.Thread(
            target=_ingest_worker,
            name=f"ingest-{video_id[:8]}",
            daemon=True,
        )
        worker.start()

        # ----------------------------------------------------------- producer
        fetch_error: Optional[BaseException] = None
        fetch_disabled = False
        try:
            while (
                raw_seen < cap
                and pages < self._settings.max_api_pages
                and not session.superseded
                and not consumer_broken.is_set()
                # Sprint 4.3 §5: a cancelled/superseded job stops fetching
                # BETWEEN pages (the generation guard inside each write is
                # the hard barrier; this just avoids spending quota on a
                # dead dataset).
                and not (should_cancel is not None and should_cancel())
            ):
                remaining = cap - raw_seen
                page = self._client.get_comment_threads(  # type: ignore[attr-defined]
                    video_id,
                    max_results=min(page_size, remaining),
                    page_token=page_token,
                )
                pages += 1
                raw_seen += _count_raw_comments(page)
                next_token = page.next_page_token
                # Bounded hand-off: this put BLOCKS while the worker catches
                # up - that is the intended backpressure (§5), never an
                # unbounded buffer.
                handoff.put(
                    _PageMessage(
                        page=page, page_has_more=next_token is not None
                    )
                )

                if not next_token:
                    has_more = False  # YouTube: no further comments
                    break
                if next_token in seen_tokens:
                    # Loop guard: a repeated token yields the same page again.
                    # Availability is not confirmed, so we do not claim more.
                    logger.warning(
                        "PAGINATION_TOKEN_LOOP",
                        extra={"video_id": video_id, "page": pages},
                    )
                    has_more = False
                    break
                seen_tokens.add(next_token)
                page_token = next_token
                has_more = True
                logger.debug(
                    "pagination detected", extra={"video_id": video_id, "page": pages}
                )
        except CommentsDisabled:
            logger.info("comments disabled for video", extra={"video_id": video_id})
            fetch_disabled = True
            has_more = False
            handoff.put(_FinishMessage(kind="disabled", has_more=False))
        except Exception as exc:  # noqa: BLE001 - re-raised below, unchanged
            fetch_error = exc
            handoff.put(
                _FinishMessage(kind="error", has_more=has_more, error=exc)
            )
        else:
            handoff.put(_FinishMessage(kind="done", has_more=has_more))

        worker.join()

        # Root-cause ordering: a worker failure trips `consumer_broken`
        # (stopping the fetch loop first), so it surfaces ahead of any
        # fetch-side error, exactly as a serial loop would have raised it.
        if worker_error is not None:
            raise worker_error
        if fetch_error is not None:
            raise fetch_error

        if fetch_disabled:
            assert worker_report is not None  # set by finish("disabled")
            return AcquisitionOutcome(
                comments=worker_processed,
                status="disabled",
                has_more=False,
                pages_fetched=pages,
                fetched=session.fetched,
                storage_ok=worker_report.storage_ok,
                superseded=session.superseded,
            )

        assert worker_report is not None  # set by finish("done")
        status = "ok" if session.fetched > 0 else "none"
        return AcquisitionOutcome(
            comments=worker_processed,
            status=status,
            has_more=has_more,
            pages_fetched=pages,
            fetched=session.fetched,
            storage_ok=worker_report.storage_ok,
            superseded=session.superseded,
        )

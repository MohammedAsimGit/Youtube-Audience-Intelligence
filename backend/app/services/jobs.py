"""Background analysis jobs (Sprint 4.3) - trigger, worker, status.

The timeout fix (§41): the HTTP request NEVER runs the pipeline. Starting
an analysis persists a QUEUED `analysis_jobs` row, spawns one daemon
worker thread, and returns immediately; the worker performs the exact same
batched work the synchronous routes used to do inline:

    POST /analysis ──► job row (QUEUED) ──► 202 Accepted
                            │
                    worker thread (this module)
                            │
        ACQUISITION   page → validate → normalize → dedup → SQLite →
                      progress write → next page        (§11 still batched)
                            │
        ANALYZING     READY batch → classify → persist → next batch (§14)
                            │
        TOPIC          topic discovery warm-up on this thread (Sprint 6,
                       §25 - never inside a request; failure never fails
                       the analysis, §39)
                            │
        INSIGHT        evidence-based insight warm-up (Sprint 7, §25 -
                       primes the memo; failure never fails the analysis)
                            │
        COMPLETED / FAILED / CANCELLED / STALE

Design (smallest mechanism compatible with FastAPI + SQLite, §3):
- ONE daemon thread per job; no broker, no second database, no Celery.
- Job state lives in SQLite (`analysis_jobs`) so a browser refresh can
  always ask "what is happening with this video?" (§8) and a server
  restart is detected at startup instead of leaving the UI stuck
  (§32 `sweep_interrupted`).
- Race safety reuses Sprint 4.2 wholesale: the POST trigger performs the
  active-video switch, every dataset write carries the generation guard,
  and the switch ITSELF cancels non-terminal jobs inside its transaction
  (§5 - no second cancellation mechanism). `cancel` events only make the
  wind-down faster; the generation guard is the hard barrier.
- The event loop is never blocked: all work runs on the worker thread
  (CPU-bound sentiment included, §15) while FastAPI keeps serving.
"""
import sqlite3
import threading
import time
from typing import Dict, NamedTuple, Optional
from uuid import uuid4

from app.core.config import Settings
from app.core.errors import AcquisitionError, MissingApiKey
from app.core.logging import get_logger
from app.db.repository import DatasetRepository
from app.models.internal import AnalysisJobStatusResponse
from app.models.job import ACTIVE_JOB_STATUSES, JobPhase, JobStatus
from app.models.processing import ProcessingStatus
from app.services.acquisition import VideoDataService
from app.services.cache import TTLCache
from app.services.dataset import DatasetService
from app.services.sentiment import UNSUPPORTED_LANGUAGE, SentimentService
from app.services.topics import TopicService
from app.services.insights import InsightService

logger = get_logger("jobs")

_TERMINAL_ERROR_FALLBACK = "acquisition_failed"
_TERMINAL_MESSAGE_FALLBACK = "Analysis failed. Please try again."
_CANCELLED_MESSAGE = "Analysis was cancelled before completion. Please retry."

# Sentiment labels that count as ANALYZED (the §23 % denominator).
_ANALYZED_LABELS = ("POSITIVE", "NEUTRAL", "NEGATIVE")


class JobStart(NamedTuple):
    """Result of an idempotent job trigger (§9/§21)."""

    job_id: str
    video_id: str
    status: str
    created: bool


class AnalysisJobService:
    """Owns the job lifecycle: trigger → worker → terminal state."""

    def __init__(
        self,
        repository: DatasetRepository,
        dataset: DatasetService,
        acquisition: VideoDataService,
        sentiment: SentimentService,
        settings: Settings,
        cache: Optional[TTLCache] = None,
        topics: Optional[TopicService] = None,
        insight: Optional[InsightService] = None,
    ) -> None:
        self._repo = repository
        self._dataset = dataset
        self._acquisition = acquisition
        self._sentiment = sentiment
        self._settings = settings
        self._cache = cache
        # Sprint 6 §25: optional topic warm-up - discovery runs on THIS
        # worker thread between sentiment and COMPLETE (never inside a
        # request), priming the compute-on-read memo so the extension's
        # GET /topics is a fast read. Absent in unit tests that only
        # exercise acquisition/sentiment phases.
        self._topics = topics
        # Sprint 7 §25: optional insight warm-up after topics - evidence
        # construction + phrasing run on THIS worker thread, priming the
        # memo so the extension's GET /insight is a fast read. Failures
        # are logged and never fail the job (same rule as topics).
        self._insight = insight
        # Serializes the start() decision (dedupe vs create+spawn) so two
        # rapid Analyze clicks can never produce two workers (§21).
        self._state_lock = threading.Lock()
        self._threads: Dict[str, threading.Thread] = {}
        self._cancels: Dict[str, threading.Event] = {}
        # Sprint 5.2 §9/§10: videos whose FIRST interim drain for the
        # current job has been dispatched. First drain = immediate (fast
        # time-to-first-insight); subsequent drains wait for
        # ANALYSIS_PROGRESS_UPDATE_INTERVAL pending rows. Reset per job
        # in _execute so a re-run of the same video drains its first new
        # page immediately again.
        self._interim_bootstrapped: set = set()

    # ---------------------------------------------------------- start/stop
    def sweep_interrupted(self) -> int:
        """Startup recovery (§32): non-terminal rows with no worker become
        STALE (retryable) instead of leaving the UI at ACQUIRING forever.
        """
        recovered = self._repo.sweep_interrupted_jobs()
        if recovered:
            logger.warning(
                "ANALYSIS_JOBS_SWEPT",
                extra={"recovered": recovered},
            )
        return recovered

    def shutdown(self, timeout: float = 5.0) -> None:
        """Signal every worker and wait briefly (graceful app shutdown).

        Rows left non-terminal by a killed process are handled by the next
        startup's `sweep_interrupted` (§32) - never left stuck.
        """
        with self._state_lock:
            events = list(self._cancels.values())
            threads = list(self._threads.values())
        for event in events:
            event.set()
        deadline = time.monotonic() + timeout
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

    def start(self, video_id: str) -> JobStart:
        """Idempotently trigger an analysis job for `video_id` (§9/§21).

        Order matters:
        1. Activation FIRST (§9.2) - the POST is the extension's explicit
           active-video transition, exactly like the legacy GET. The switch
           cancels every other video's in-flight job in its transaction.
        2. Dedupe - an already-running job for this video is returned as-is
           (Analyze × 3 = one job).
        3. Fail fast on missing API key only when a real acquisition is
           required (a fresh dataset needs no YouTube access).
        4. Create the QUEUED row + spawn the worker inside one lock, so
           concurrent triggers serialize deterministically.
        """
        with self._state_lock:
            activation = self._dataset.ensure_active(video_id)
            if activation.changed and self._cache is not None:
                # L1 mirrors the single-active-dataset policy (same rule as
                # the sync GET path, §30).
                self._cache.clear()

            latest = self._repo.get_latest_job(video_id)
            if latest is not None and str(latest["status"]) in ACTIVE_JOB_STATUSES:
                logger.info(
                    "ANALYSIS_JOB_REUSED",
                    extra={
                        "video_id": video_id,
                        "job_id": latest["job_id"],
                        "status": latest["status"],
                    },
                )
                return JobStart(
                    job_id=str(latest["job_id"]),
                    video_id=video_id,
                    status=str(latest["status"]),
                    created=False,
                )

            if (
                not self._settings.youtube_api_key
                and self._dataset.needs_acquisition(video_id)
            ):
                # 503 before anything starts - the extension shows the
                # friendly "not configured" copy immediately (§31).
                raise MissingApiKey("YOUTUBE_API_KEY is not configured")

            # Any other tracked worker now belongs to a dead generation:
            # its row was cancelled by the switch above (or it already
            # reached a terminal state) - the event just stops it sooner.
            for event in self._cancels.values():
                event.set()

            job_id = uuid4().hex
            self._repo.create_job(job_id, video_id, activation.generation)
            cancel = threading.Event()
            thread = threading.Thread(
                target=self._run_job,
                args=(job_id, video_id, activation.generation, cancel),
                name=f"analysis-job-{job_id[:8]}",
                daemon=True,
            )
            self._cancels[job_id] = cancel
            self._threads[job_id] = thread
            thread.start()
            logger.info(
                "ANALYSIS_JOB_STARTED",
                extra={
                    "video_id": video_id,
                    "job_id": job_id,
                    "generation": activation.generation,
                },
            )
            return JobStart(
                job_id=job_id,
                video_id=video_id,
                status=JobStatus.QUEUED.value,
                created=True,
            )

    # ------------------------------------------------------------- status
    def get_status(self, video_id: str) -> Optional[AnalysisJobStatusResponse]:
        """Latest job for one video + REAL live counts (§7/§10), or None.

        Counts are computed from the dataset tables at read time, so they
        can never drift from what the store actually holds; only
        `collected`/`has_more` come from the job row (page progress).
        """
        job = self._repo.get_latest_job(video_id)
        if job is None:
            return None

        status_counts = self._repo.get_status_counts(video_id)
        sentiment_counts = self._repo.get_sentiment_counts(video_id)
        stored = sum(status_counts.values())
        analyzed = sum(sentiment_counts.get(label, 0) for label in _ANALYZED_LABELS)
        skipped = sentiment_counts.get(UNSUPPORTED_LANGUAGE, 0)
        failed = status_counts.get(ProcessingStatus.FAILED.value, 0)
        pending = stored - analyzed - skipped - failed

        return AnalysisJobStatusResponse(
            job_id=str(job["job_id"]),
            video_id=video_id,
            status=job["status"],
            phase=job["phase"],
            collected=int(job["collected"]),
            stored=stored,
            analyzable=stored,
            analyzed=analyzed,
            skipped=skipped,
            failed=failed,
            pending=max(0, pending),
            has_more=bool(job["has_more"]),
            error_code=job["error_code"],
            error_message=job["error_message"],
            created_at=job["created_at"],
            updated_at=job["updated_at"],
            finished_at=job["finished_at"],
        )

    # ------------------------------------------------------------- worker
    def _run_job(
        self,
        job_id: str,
        video_id: str,
        generation: int,
        cancel: threading.Event,
    ) -> None:
        """Worker entry: never raises, always terminates in a state the UI
        can render and retry from (§22/§31 - categorized, no stack traces)."""
        try:
            self._execute(job_id, video_id, generation, cancel)
        except AcquisitionError as exc:
            # Categorized upstream/config failure with curated copy (§31).
            logger.warning(
                "ANALYSIS_JOB_FAILED",
                extra={
                    "video_id": video_id,
                    "job_id": job_id,
                    "phase_exception": type(exc).__name__,
                    "code": exc.code,
                },
            )
            self._finish_failed(job_id, exc.code, exc.message)
        except sqlite3.Error as exc:
            logger.error(
                "ANALYSIS_JOB_STORE_FAILURE",
                extra={
                    "video_id": video_id,
                    "job_id": job_id,
                    "error": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            self._finish_failed(
                job_id,
                "storage_unavailable",
                "The dataset store is unavailable right now. Please try again later.",
            )
        except Exception as exc:  # noqa: BLE001 - worker must die statefully
            logger.error(
                "ANALYSIS_JOB_INTERNAL_ERROR",
                extra={
                    "video_id": video_id,
                    "job_id": job_id,
                    "error": type(exc).__name__,
                },
            )
            self._finish_failed(job_id, _TERMINAL_ERROR_FALLBACK, _TERMINAL_MESSAGE_FALLBACK)
        finally:
            with self._state_lock:
                self._threads.pop(job_id, None)
                self._cancels.pop(job_id, None)

    def _execute(
        self,
        job_id: str,
        video_id: str,
        generation: int,
        cancel: threading.Event,
    ) -> None:
        """ACQUISITION → ANALYZING → COMPLETED on the worker thread."""
        # ------------------------------------------------------- acquiring
        if not self._repo.begin_job_phase(
            job_id, JobStatus.ACQUIRING.value, JobPhase.ACQUISITION.value
        ):
            return  # already cancelled/swept - never resurrect a dead job

        if self._dataset.needs_acquisition(video_id):
            # Sprint 5.2: re-arm the immediate first interim drain for
            # this job (fresh comments are expected during acquisition).
            self._interim_bootstrapped.discard(video_id)
            run = self._acquisition.acquire_dataset(
                video_id,
                generation,
                should_cancel=cancel.is_set,
                # REAL per-page progress: written only after the page was
                # persisted (§7 - never fabricate progress).
                on_page=lambda pages, collected, has_more: self._repo.update_job_progress(
                    job_id, collected, has_more
                ),
                # Sprint 5.1 §6/§9: after EVERY persisted page the ingest
                # worker drains ready rows on this same callback, so the
                # first real aggregate exists seconds after page 1 while
                # fetching continues in parallel.
                after_page=lambda: self._interim_analysis(
                    video_id, generation, cancel
                ),
            )
            if run.outcome.superseded or cancel.is_set():
                self._finish_cancelled(job_id, JobPhase.ACQUISITION)
                return
            self._repo.update_job_progress(
                job_id, run.outcome.fetched, run.outcome.has_more
            )
            if self._cache is not None:
                # A re-acquisition outdates every L1 entry (§30): the next
                # GET must serve the freshly stored dataset, not old memory.
                self._cache.clear()
        else:
            # Fresh, complete dataset: 0 quota - collect the stored count
            # as this job's truthful "collected" (§7 collected == stored).
            stored_rows = self._repo.get_comment_stats(video_id)["total"]
            video_row = self._repo.get_video(video_id)
            self._repo.update_job_progress(
                job_id,
                int(stored_rows),
                bool(video_row["has_more"]) if video_row is not None else False,
            )
            if cancel.is_set():
                self._finish_cancelled(job_id, JobPhase.ACQUISITION)
                return

        # ------------------------------------------------------- analyzing
        if not self._repo.begin_job_phase(
            job_id, JobStatus.ANALYZING.value, JobPhase.SENTIMENT.value
        ):
            return  # cancelled between phases (switch happened)

        summary = self._sentiment.process_pending(
            video_id,
            generation,
            should_cancel=cancel.is_set,
            wait=True,  # briefly wait out an in-flight sync GET run
        )
        if summary is None or summary.stopped:
            # Superseded (generation guard) or cancelled: the switch has
            # already written (or will write) the truthful CANCELLED row;
            # this guarded write is a no-op if so.
            self._finish_cancelled(job_id, JobPhase.SENTIMENT)
            return

        # --------------------------------------------------- Sprint 6
        # TOPIC phase (§25): discovery warm-up on the worker thread after
        # sentiment. Topic failure NEVER fails the analysis (§39): the
        # sentiment results are already persisted, the phase is logged,
        # and the job still completes - the API answers honestly on read.
        if not self._repo.begin_job_phase(
            job_id, JobStatus.ANALYZING.value, JobPhase.TOPIC.value
        ):
            return  # cancelled between phases (switch happened)
        if cancel.is_set():
            self._finish_cancelled(job_id, JobPhase.TOPIC)
            return
        if self._topics is not None:
            try:
                self._topics.get_analysis(video_id)
            except Exception as exc:  # noqa: BLE001 - §39: never fail sentiment
                logger.error(
                    "TOPIC_WARM_FAILED",
                    extra={
                        "video_id": video_id,
                        "job_id": job_id,
                        "error": type(exc).__name__,
                    },
                )
        if cancel.is_set():
            self._finish_cancelled(job_id, JobPhase.TOPIC)
            return

        # --------------------------------------------------- Sprint 7
        # INSIGHT phase (§25): evidence-based insight warm-up on the
        # worker thread after topics. Same rule as TOPIC (§39): a failed
        # warm-up never fails the analysis - sentiment/topic results are
        # already persisted and the API answers honestly on read.
        if not self._repo.begin_job_phase(
            job_id, JobStatus.ANALYZING.value, JobPhase.INSIGHT.value
        ):
            return  # cancelled between phases (switch happened)
        if cancel.is_set():
            self._finish_cancelled(job_id, JobPhase.INSIGHT)
            return
        if self._insight is not None:
            try:
                self._insight.get_insight(video_id)
            except Exception as exc:  # noqa: BLE001 - §39: never fail the job
                logger.error(
                    "INSIGHT_WARM_FAILED",
                    extra={
                        "video_id": video_id,
                        "job_id": job_id,
                        "error": type(exc).__name__,
                    },
                )
        if cancel.is_set():
            self._finish_cancelled(job_id, JobPhase.INSIGHT)
            return

        finished = self._repo.finish_job(
            job_id, JobStatus.COMPLETED.value, JobPhase.COMPLETE.value
        )
        if finished:
            logger.info(
                "ANALYSIS_JOB_COMPLETED",
                extra={
                    "video_id": video_id,
                    "job_id": job_id,
                    "processed": summary.processed,
                    "failed": summary.failed,
                    "skipped": summary.skipped,
                    "requeued": summary.requeued,
                },
            )

    # ------------------------------------------------------ interim analysis
    def _interim_analysis(
        self, video_id: str, generation: int, cancel: threading.Event
    ) -> None:
        """Drain READY rows WHILE acquisition is still running (§6/§9).

        Called by the acquisition ingest worker after every persisted page:
        page 1's rows reach the sentiment engine immediately, so
        time-to-first-insight is one page + one batch instead of the whole
        dataset (measured baseline: first insight == full acquisition).

        Sprint 5.2 §9/§10 - cadence gate (inference decoupled from page
        cadence): the FIRST drain of a job always runs (fast first real
        aggregate), then further interim drains only re-arm once
        ANALYSIS_PROGRESS_UPDATE_INTERVAL rows are pending, so inference
        happens in interval-sized batches instead of one small drain per
        ~100-comment YouTube page, and the UI's `analyzed` counter grows
        in truthful interval-sized steps. The final ANALYZING drain is
        unconditional - gated rows are always finished there.

        Best-effort by design:
        - `wait=False` - never block ingestion behind an in-flight sync GET
          run (that run owns the lock and is doing the same work).
        - Errors are logged and LEFT for the final ANALYZING drain - the
          authoritative, unchanged analysis step - so a transient failure
          never fails a healthy acquisition and never discards persisted
          pages (§22: successful batches are kept).
        - Cancellation/supersession is handled inside process_pending via
          the generation guard and the cancel event; nothing to do here.
        """
        if cancel.is_set():
            return
        try:
            if video_id in self._interim_bootstrapped:
                interval = self._settings.analysis_progress_update_interval
                pending = self._repo.get_status_counts(video_id).get(
                    ProcessingStatus.READY_FOR_ANALYSIS.value, 0
                )
                if pending < interval:
                    return  # not enough new work yet - final drain covers it
            self._interim_bootstrapped.add(video_id)
            self._sentiment.process_pending(
                video_id,
                generation,
                should_cancel=cancel.is_set,
                wait=False,
            )
        except Exception as exc:  # noqa: BLE001 - final drain re-raises
            logger.error(
                "INTERIM_ANALYSIS_FAILED",
                extra={"video_id": video_id, "error": type(exc).__name__},
            )

    # ------------------------------------------------------ terminal writes
    def _finish_failed(self, job_id: str, code: str, message: str) -> None:
        self._repo.finish_job(
            job_id,
            JobStatus.FAILED.value,
            error_code=code or _TERMINAL_ERROR_FALLBACK,
            error_message=message or _TERMINAL_MESSAGE_FALLBACK,
        )

    def _finish_cancelled(self, job_id: str, phase: JobPhase) -> None:
        self._repo.finish_job(
            job_id,
            JobStatus.CANCELLED.value,
            phase=phase.value,
            error_code="job_cancelled",
            error_message=_CANCELLED_MESSAGE,
        )

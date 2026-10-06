"""Realtime audience intelligence - the active-video monitor (Sprint 8).

The system moves from "analyze the current audience" to "keep understanding
how the audience changes while the user stays on the video" WITHOUT a second
pipeline, a second scheduler, or new infrastructure:

    extension polls GET /realtime  (touch)
              │
              ▼
    RealtimeService.ensure_monitor  ──► one daemon thread per active video
              │                          (interval = clamped poll setting)
              ▼  every interval
    active + generation guard  ── superseded? retire (stale-video §12)
              │
    analysis job active?  ────── yes? skip this cycle (job dedupe §13)
              │ no
              ▼
    acquire_incremental  ─────── newest-first probe page, 1 quota unit in
              │                  steady state (§17 no unnecessary requests)
              │                  dedup by comment_id (idempotent §11)
              ▼
    sentiment.process_pending ── READY rows only: PROCESSED rows are never
              │                  re-claimed, so no full re-analysis (§18)
              ▼
    snapshot: trend / activity / counts ── insight warm at threshold (§8)
              │
              ▼
    overlay notices `version` flipped → silent refetch (§11 auto UI)

Key decisions (documented per §54.25):

- ONE monitor thread per active video, keyed by video_id under a state lock
  (job-dedupe pattern of AnalysisJobService, §21 "avoid duplicate checks").
  It starts on a /realtime touch while the video is active and enabled, and
  retires when: the active video/generation changes (§12), the extension
  stops polling for > max(3×interval, 60s) (lifecycle cleanup, §8), the API
  key is missing, comments become disabled, or the app shuts down.
- Polling cadence is REALTIME_POLL_INTERVAL_SECONDS clamped into
  [MIN, MAX] - never sub-second, never uncontrolled (§16).
- Trend lives in monitor MEMORY only: the previous (analyzed, percentages)
  snapshot is compared with the current one after each cycle; movement of
  the net balance (positive% − negative%) below REALTIME_TREND_MIN_CHANGE
  percentage points reports STABLE (noise floor). Restarting the backend
  resets the baseline to the current distribution - a documented §21
  limitation (no new table was added, per brief "do not automatically add
  this table").
- Audience activity is computed LIVE at read time from published_at inside
  a trailing window of max(2 × interval, 60) seconds - real rows only,
  never fabricated motion (§4). Bands: ≤1 comment/min LOW, ≤5/min MODERATE,
  >5/min HIGH (documented, tested).
- Insight regenerates in the background only after
  REALTIME_INSIGHT_MIN_NEW_ANALYZED newly analyzed verdicts since the last
  warm (cost gate for optional LLM providers); the memo itself still
  invalidates on the analysis fingerprint, so a GET /insight between warms
  is always correct - the warm just primes it off the request path (§8).
  Topic intelligence stays compute-on-read: its fingerprint memo
  invalidates automatically when analyzed rows change (Sprint 6 §25), and
  the overlay's silent refetch triggers the rebuild.
- L1 is cleared after any successful insert (single-active cache policy of
  §30, same as the analysis job does after acquisition).

No raw comment text is ever logged (§12 of the brief): events carry counts,
ids of the video, codes and timings only.
"""
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

from app.core.config import Settings
from app.core.errors import AcquisitionError, MissingApiKey
from app.core.logging import get_logger
from app.db.repository import DatasetRepository
from app.models.internal import (
    RealtimeActivity,
    RealtimeSentimentBreakdown,
    RealtimeStatusResponse,
    RealtimeTrend,
)
from app.models.job import ACTIVE_JOB_STATUSES
from app.models.processing import ProcessingStatus
from app.services.acquisition import VideoDataService
from app.services.cache import TTLCache
from app.services.dataset import DatasetInfo, DatasetService
from app.services.insights import InsightService
from app.services.sentiment import (
    EMOTION_PRIORITY,
    UNSUPPORTED_LANGUAGE,
    SentimentService,
    distribution_percentages,
    dominant_label,
)

logger = get_logger("realtime")

# Sentiment labels that count as ANALYZED (denominator of every share).
_ANALYZED_LABELS = ("POSITIVE", "NEUTRAL", "NEGATIVE")

# Audience-activity bands on comments-per-minute over the measurement
# window (documented; tested). A steady video accrues under 1/min, an
# engaged one a few per minute, a viral surge well past 5/min.
ACTIVITY_LOW_MAX_PER_MINUTE = 1.0
ACTIVITY_MODERATE_MAX_PER_MINUTE = 5.0
# Window floor so short poll intervals cannot produce a 0-minute division
# or a spuriously hot rate from a handful of seconds.
ACTIVITY_WINDOW_MIN_SECONDS = 60.0
# Idle stop: the extension touches /realtime on its own (faster) cadence;
# missing this many timeouts means the overlay is gone (§8 lifecycle).
MONITOR_IDLE_MIN_SECONDS = 60.0
MONITOR_IDLE_INTERVALS = 3


def clamp_poll_interval(settings: Settings) -> float:
    """Effective poll interval: setting clamped into [min, max] (§16)."""
    return min(
        max(
            float(settings.realtime_poll_interval_seconds),
            float(settings.realtime_min_poll_interval_seconds),
        ),
        float(settings.realtime_max_poll_interval_seconds),
    )


@dataclass
class _MonitorState:
    """Per-video monitor: handle fields + snapshot fields (lock-guarded)."""

    video_id: str
    generation: int
    interval: float
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None
    last_touch: float = field(default_factory=time.monotonic)
    stop_reason: Optional[str] = None
    retired: bool = False
    # --- snapshot (written by the monitor thread, read by status builds) ---
    last_checked_at: Optional[datetime] = None
    last_updated_at: Optional[datetime] = None
    new_comments: int = 0
    pending_inserts: int = 0
    # Trend baseline: distribution at the previous analyzed-count change.
    prev_analyzed: int = 0
    prev_positive: float = 0.0
    prev_neutral: float = 0.0
    prev_negative: float = 0.0
    trend_state: str = "STABLE"
    trend_change_pp: float = 0.0
    trend_positive_pp: float = 0.0
    trend_neutral_pp: float = 0.0
    trend_negative_pp: float = 0.0
    # Analyzed count at the last successful insight warm (cost gate).
    insight_watermark: int = 0


class RealtimeService:
    """Owns monitor lifecycle + one polling cycle for the active video."""

    def __init__(
        self,
        repository: DatasetRepository,
        dataset: DatasetService,
        acquisition: VideoDataService,
        sentiment: SentimentService,
        insight: Optional[InsightService],
        settings: Settings,
        cache: Optional[TTLCache] = None,
    ) -> None:
        self._repo = repository
        self._dataset = dataset
        self._acquisition = acquisition
        self._sentiment = sentiment
        self._insight = insight
        self._settings = settings
        self._cache = cache
        self._lock = threading.Lock()
        self._monitors: Dict[str, _MonitorState] = {}
        self._shutting_down = False

    # ------------------------------------------------------------------ public
    def observe(self, video_id: str) -> Optional[RealtimeStatusResponse]:
        """Touch + status in one call (the route's whole contract).

        Returns None when the video has no stored dataset (the route turns
        that into 404). Otherwise: refreshes the monitor's idle timer,
        starts a monitor when the video is the ACTIVE one and realtime is
        enabled (read endpoints never activate, §40), stops a monitor that
        belongs to a now-inactive video, and answers with live aggregates +
        the monitor's snapshot.
        """
        info = self._dataset.get_dataset_info(video_id)
        if info is None:
            return None

        active = self._repo.get_active_video()
        is_active = active is not None and str(active["video_id"]) == video_id
        if (
            is_active
            and self._settings.realtime_enabled
            # No key -> polling can never succeed; do not start (or restart
            # after a tick retires it) a monitor that would only spin.
            and bool(self._settings.youtube_api_key)
            and not self._shutting_down
        ):
            self._ensure_monitor(video_id, int(active["dataset_generation"]))
        else:
            if not is_active:
                # Stale-video protection (§12): a monitor for a video that
                # is no longer the working dataset must not keep polling.
                with self._lock:
                    stale = self._monitors.get(video_id)
                if stale is not None:
                    self._retire(stale, "not_active")
            # disabled: never monitor; the endpoint still answers honestly
            # with monitoring=false (config contract).
        return self._build_status(video_id, info)

    def poll_once(self, video_id: str) -> bool:
        """Run ONE monitoring cycle synchronously; False when no monitor.

        The monitor thread invokes this on its interval. It is public so
        tests (and operators) can drive a deterministic cycle without
        waiting on wall-clock sleeps.
        """
        with self._lock:
            state = self._monitors.get(video_id)
        if state is None or state.retired:
            return False
        self._tick(state)
        return True

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop every monitor and briefly join their threads (§32 parity
        with AnalysisJobService.shutdown - daemon threads are never left
        polling YouTube past app teardown)."""
        self._shutting_down = True
        with self._lock:
            states = list(self._monitors.values())
        for state in states:
            state.stop_reason = state.stop_reason or "shutdown"
            state.stop_event.set()
        deadline = time.monotonic() + timeout
        for state in states:
            thread = state.thread
            if thread is None or thread is threading.current_thread():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        with self._lock:
            self._monitors.clear()

    # --------------------------------------------------------------- monitor
    def _ensure_monitor(self, video_id: str, generation: int) -> None:
        """Idempotent start: one live monitor per video (§21 dedupe)."""
        now = time.monotonic()
        interval = clamp_poll_interval(self._settings)
        with self._lock:
            existing = self._monitors.get(video_id)
            if existing is not None:
                same_generation = existing.generation == generation
                alive = (
                    existing.thread is not None
                    and existing.thread.is_alive()
                    and not existing.stop_event.is_set()
                )
                if same_generation and alive:
                    existing.last_touch = now  # keep-alive touch
                    return
                # Dying thread or generation changed (video switched away
                # and back): retire the old handle, start a fresh one.
                existing.stop_reason = existing.stop_reason or "replaced"
                existing.stop_event.set()
            if self._shutting_down:
                return
            state = _MonitorState(
                video_id=video_id,
                generation=generation,
                interval=interval,
                last_touch=now,
            )
            # Baseline: distribution + analyzed count NOW, so the first
            # movement is measured from what the job just produced and the
            # insight warm does not re-run over already-warmed verdicts.
            counts = self._repo.get_sentiment_counts(video_id)
            positive = counts.get("POSITIVE", 0)
            neutral = counts.get("NEUTRAL", 0)
            negative = counts.get("NEGATIVE", 0)
            analyzed = positive + neutral + negative
            pos_pct, neu_pct, neg_pct = distribution_percentages(
                positive, neutral, negative
            )
            state.prev_analyzed = analyzed
            state.prev_positive = pos_pct
            state.prev_neutral = neu_pct
            state.prev_negative = neg_pct
            state.insight_watermark = analyzed
            thread = threading.Thread(
                target=self._monitor_loop,
                args=(state,),
                name=f"realtime-{video_id[:8]}",
                daemon=True,
            )
            state.thread = thread
            self._monitors[video_id] = state
            thread.start()
        logger.info(
            "REALTIME_MONITOR_STARTED",
            extra={
                "video_id": video_id,
                "generation": generation,
                "interval_seconds": interval,
            },
        )

    def _monitor_loop(self, state: _MonitorState) -> None:
        """Sleep → idle-guard → one cycle. Retires itself on any stop."""
        try:
            while True:
                if state.stop_event.wait(state.interval):
                    break
                idle = time.monotonic() - state.last_touch
                if idle > max(
                    MONITOR_IDLE_INTERVALS * state.interval,
                    MONITOR_IDLE_MIN_SECONDS,
                ):
                    # Extension stopped polling (overlay closed / navigation
                    # without a switch): stop spending quota (§8 cleanup).
                    self._retire(state, "disconnected")
                    break
                try:
                    self._tick(state)
                except Exception as exc:  # noqa: BLE001 - monitor never dies
                    logger.error(
                        "REALTIME_TICK_FAILED",
                        extra={
                            "video_id": state.video_id,
                            "error": type(exc).__name__,
                        },
                    )
        finally:
            self._retire(
                state, state.stop_reason or "stopped"
            )  # idempotent

    def _retire(self, state: _MonitorState, reason: str) -> None:
        """Stop one monitor exactly once (dict owner check, single log)."""
        with self._lock:
            if state.retired:
                return
            state.retired = True
            state.stop_reason = reason
            state.stop_event.set()
            if self._monitors.get(state.video_id) is state:
                del self._monitors[state.video_id]
        logger.info(
            "REALTIME_MONITOR_STOPPED",
            extra={"video_id": state.video_id, "reason": reason},
        )

    # ------------------------------------------------------------------ cycle
    def _tick(self, state: _MonitorState) -> None:
        """One full polling cycle (guarded, incremental, best-effort)."""
        video_id = state.video_id

        # 1. Stale-video protection (§12): active identity AND generation.
        active = self._repo.get_active_video()
        if (
            active is None
            or str(active["video_id"]) != video_id
            or int(active["dataset_generation"]) != state.generation
        ):
            self._retire(state, "superseded")
            return

        # 2. Job dedupe (§13): while the analysis job owns this dataset it
        #    acquires/analyzes; polling would only double-spend quota.
        #    last_checked_at is NOT advanced - no YouTube check happened.
        job = self._repo.get_latest_job(video_id)
        if job is not None and str(job["status"]) in ACTIVE_JOB_STATUSES:
            logger.debug(
                "REALTIME_POLL_SKIPPED",
                extra={"video_id": video_id, "job_status": str(job["status"])},
            )
            return

        # 3. Incremental acquisition (§16/§17): newest-first probe; 1 unit
        #    of quota in steady state; categorized errors surface here.
        try:
            outcome = self._acquisition.acquire_incremental(
                video_id,
                state.generation,
                should_cancel=state.stop_event.is_set,
            )
        except MissingApiKey:
            # Misconfiguration: polling can never succeed - retire instead
            # of hammering; /realtime keeps answering monitoring=false.
            self._retire(state, "not_configured")
            return
        except AcquisitionError as exc:
            logger.warning(
                "REALTIME_POLL_FAILED",
                extra={
                    "video_id": video_id,
                    "code": exc.code,
                    # message may echo upstream text - keep logs to codes.
                },
            )
            return  # transient upstream: retry on the next interval (§15)
        except sqlite3.Error as exc:
            logger.error(
                "REALTIME_POLL_STORE_FAILURE",
                extra={"video_id": video_id, "error": type(exc).__name__},
            )
            return  # store hiccup: retry next interval (§15 error recovery)

        state.last_checked_at = datetime.now(timezone.utc)
        if outcome.superseded:
            self._retire(state, "superseded")
            return
        if outcome.status == "disabled":
            logger.info(
                "REALTIME_COMMENTS_DISABLED",
                extra={"video_id": video_id},
            )
            self._retire(state, "comments_disabled")
            return
        if not outcome.storage_ok:
            logger.warning(
                "REALTIME_POLL_STORAGE_DEGRADED",
                extra={"video_id": video_id, "stop_reason": outcome.stop_reason},
            )
        if outcome.inserted > 0 or outcome.updated > 0:
            logger.info(
                "REALTIME_INCREMENTAL_ACQUIRED",
                extra={
                    "video_id": video_id,
                    "new": outcome.inserted,
                    "updated": outcome.updated,
                    "pages": outcome.pages_fetched,
                    "stop_reason": outcome.stop_reason,
                },
            )
            if self._cache is not None:
                # L1 mirrors the store (§30): drop the pre-update contract.
                self._cache.clear()

        # 4. Incremental analysis (§18): READY rows only - the claim state
        #    machine never re-queues PROCESSED verdicts, so old comments
        #    are not re-analyzed. wait=True on this dedicated thread; the
        #    cancel event winds the run down on shutdown/supersede.
        summary = self._sentiment.process_pending(
            video_id,
            state.generation,
            should_cancel=state.stop_event.is_set,
            wait=True,
        )
        # summary is None only if a concurrent run held the lock across the
        # wait (defensive): the next cycle picks the rows up.

        # 5. Snapshot: trend + activity + insight gate (all real data).
        counts = self._repo.get_sentiment_counts(video_id)
        positive = counts.get("POSITIVE", 0)
        neutral = counts.get("NEUTRAL", 0)
        negative = counts.get("NEGATIVE", 0)
        analyzed = positive + neutral + negative
        pos_pct, neu_pct, neg_pct = distribution_percentages(
            positive, neutral, negative
        )
        now = datetime.now(timezone.utc)

        if outcome.inserted > 0:
            state.pending_inserts += outcome.inserted
            state.last_updated_at = now  # stored data changed right now
        if analyzed != state.prev_analyzed:
            # Analysis moved: report the movement vs the previous analyzed
            # snapshot, then rebase. Net = positive% − negative%; below the
            # noise floor stays STABLE (§9 trend calculation).
            delta_positive = round(pos_pct - state.prev_positive, 1)
            delta_neutral = round(neu_pct - state.prev_neutral, 1)
            delta_negative = round(neg_pct - state.prev_negative, 1)
            net_change = round(
                (pos_pct - neg_pct) - (state.prev_positive - state.prev_negative),
                1,
            )
            floor = float(self._settings.realtime_trend_min_change)
            if net_change >= floor:
                trend_state = "RISING"
            elif net_change <= -floor:
                trend_state = "FALLING"
            else:
                trend_state = "STABLE"
            if trend_state != state.trend_state:
                logger.info(
                    "REALTIME_TREND_CHANGED",
                    extra={
                        "video_id": video_id,
                        "trend": trend_state,
                        "change_pp": net_change,
                        "analyzed": analyzed,
                    },
                )
            state.trend_state = trend_state
            state.trend_change_pp = net_change
            state.trend_positive_pp = delta_positive
            state.trend_neutral_pp = delta_neutral
            state.trend_negative_pp = delta_negative
            state.prev_analyzed = analyzed
            state.prev_positive = pos_pct
            state.prev_neutral = neu_pct
            state.prev_negative = neg_pct
            # The comments behind this movement become the reported batch.
            state.new_comments = state.pending_inserts
            state.pending_inserts = 0
            state.last_updated_at = now
        elif state.pending_inserts > 0:
            # Stored rows changed without new verdicts yet (e.g. an edit
            # reset a row to READY but analysis did not land this cycle):
            # surface the batch honestly and let the next cycle finish it.
            state.new_comments = state.pending_inserts
            state.pending_inserts = 0

        # 6. Conditional insight warm (§8): only after enough NEW verdicts
        #    to be worth it; failures log and retry next cycle - they never
        #    fail the poll (Sprint 7 §39 rule).
        if self._insight is not None:
            new_analyzed = analyzed - state.insight_watermark
            if new_analyzed >= self._settings.realtime_insight_min_new_analyzed:
                try:
                    self._insight.get_insight(video_id)
                    state.insight_watermark = analyzed
                    logger.info(
                        "REALTIME_INSIGHT_REGENERATED",
                        extra={
                            "video_id": video_id,
                            "analyzed": analyzed,
                            "new_analyzed": new_analyzed,
                        },
                    )
                except Exception as exc:  # noqa: BLE001 - warm is optional
                    logger.error(
                        "REALTIME_INSIGHT_WARM_FAILED",
                        extra={
                            "video_id": video_id,
                            "error": type(exc).__name__,
                        },
                    )

        logger.debug(
            "REALTIME_POLL_COMPLETED",
            extra={
                "video_id": video_id,
                "stop_reason": outcome.stop_reason,
                "inserted": outcome.inserted,
                "analyzed": analyzed,
                "trend": state.trend_state,
            },
        )

    # ------------------------------------------------------------------ read
    def _build_status(
        self, video_id: str, info: DatasetInfo
    ) -> RealtimeStatusResponse:
        """Live aggregates (store) + snapshot (monitor) → response.

        Counts are recomputed at read time exactly like the job-status
        endpoint (§7 of Sprint 4.3): the response can never drift from what
        SQLite holds, regardless of what any thread is doing.
        """
        status_counts = self._repo.get_status_counts(video_id)
        sentiment_counts = self._repo.get_sentiment_counts(video_id)
        positive = sentiment_counts.get("POSITIVE", 0)
        neutral = sentiment_counts.get("NEUTRAL", 0)
        negative = sentiment_counts.get("NEGATIVE", 0)
        analyzed = positive + neutral + negative
        skipped = sentiment_counts.get(UNSUPPORTED_LANGUAGE, 0)
        failed = status_counts.get(ProcessingStatus.FAILED.value, 0)
        stored = sum(status_counts.values())
        pending = max(0, stored - analyzed - skipped - failed)
        pos_pct, neu_pct, neg_pct = distribution_percentages(
            positive, neutral, negative
        )
        dominant_emotion = dominant_label(
            self._repo.get_emotion_counts(video_id), EMOTION_PRIORITY
        )

        # Activity (§9 audience activity): trailing window of REAL
        # published_at timestamps; computed live so it is correct even
        # between monitor cycles (and when monitoring is off).
        interval = clamp_poll_interval(self._settings)
        window_seconds = max(2 * interval, ACTIVITY_WINDOW_MIN_SECONDS)
        window_minutes = round(window_seconds / 60.0, 3)
        since = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat()
        new_recent = self._repo.count_recent_comments(video_id, since)
        rate = round(new_recent / window_minutes, 2) if window_minutes > 0 else 0.0
        if rate <= ACTIVITY_LOW_MAX_PER_MINUTE:
            level = "LOW"
        elif rate <= ACTIVITY_MODERATE_MAX_PER_MINUTE:
            level = "MODERATE"
        else:
            level = "HIGH"

        with self._lock:
            state = self._monitors.get(video_id)
            monitoring = (
                state is not None
                and not state.retired
                and state.thread is not None
                and state.thread.is_alive()
                and not state.stop_event.is_set()
            )
            if state is not None:
                last_checked_at = state.last_checked_at
                last_updated_at = state.last_updated_at
                new_comments = state.new_comments
                trend = RealtimeTrend(
                    state=state.trend_state,  # type: ignore[arg-type]
                    change_pp=state.trend_change_pp,
                    positive_pp=state.trend_positive_pp,
                    neutral_pp=state.trend_neutral_pp,
                    negative_pp=state.trend_negative_pp,
                )
            else:
                last_checked_at = None
                last_updated_at = None
                new_comments = 0
                trend = RealtimeTrend()  # no monitor -> no movement claimed

        return RealtimeStatusResponse(
            video_id=video_id,
            enabled=bool(self._settings.realtime_enabled),
            monitoring=monitoring,
            poll_interval_seconds=interval,
            last_checked_at=last_checked_at,
            last_updated_at=last_updated_at,
            new_comments=new_comments,
            total_comments=stored,
            analyzed=analyzed,
            pending=pending,
            skipped=skipped,
            failed=failed,
            sentiment=RealtimeSentimentBreakdown(
                positive=pos_pct, neutral=neu_pct, negative=neg_pct
            ),
            trend=trend,
            activity=RealtimeActivity(
                level=level,  # type: ignore[arg-type]
                new_recent=new_recent,
                window_minutes=window_minutes,
                rate_per_minute=rate,
            ),
            dominant_emotion=dominant_emotion,
            # Cheap flip marker for the overlay's silent refetch: changes
            # iff stored rows or verdicts changed (both are live reads).
            version=f"{stored}:{analyzed}",
        )

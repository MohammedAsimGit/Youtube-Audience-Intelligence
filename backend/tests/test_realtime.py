"""Sprint 8 - realtime audience intelligence (regression + new coverage).

Covers:
- incremental acquisition: boundary stop, idempotent inserts, dataset cap,
  missing key / missing dataset, generation write-guard (superseded),
  no reprocessing of PROCESSED rows (§18);
- monitor lifecycle: one thread per video, shutdown, stale-video retire,
  job-active skip (§13), transient-error recovery (§15);
- trend math: RISING / FALLING / STABLE noise floor, baseline, persistence;
- audience activity: LOW / MODERATE / HIGH bands over real published_at;
- insight warm gated by REALTIME_INSIGHT_MIN_NEW_ANALYZED;
- GET /realtime contract: camelCase fields, 404/422, enabled=false honesty,
  read-never-activates (§40);
- end-to-end: new comments flow into totals/version WITHOUT any Analyze
  action (the Sprint 8 definition of done).

All fixture text is long, plain English so language detection resolves to
`en` and the rows are analyzable (short strings would be skipped honestly).
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.errors import MissingApiKey, UpstreamUnavailable, VideoNotFound
from app.db.connection import Database
from app.db.repository import DatasetRepository
from app.schemas.youtube import YtVideoListResponse
from app.services.acquisition import VideoDataService
from app.services.cache import TTLCache
from app.services.dataset import DatasetService
from app.services.ingestion import IngestionService
from app.services.insights import InsightService
from app.services.realtime import RealtimeService
from app.services.sentiment import SentimentService
from app.services.topics import TopicService

from tests.conftest import (
    FakeYouTubeClient,
    make_settings,
    seed_video,
    thread_payload,
    video_payload,
)

VIDEO = "dQw4w9WgXcQ"
OTHER = "othervideo1"

POS_TEXT = (
    "I absolutely love this fantastic wonderful excellent amazing great "
    "video that I enjoy every single time I watch it again today"
)
NEG_TEXT = (
    "I absolutely hate this terrible awful disappointing horrible boring "
    "video that I regret wasting my evening on every single time honestly"
)
NEU_TEXT = (
    "The video is about an ordinary everyday subject that happened "
    "yesterday somewhere in the neighborhood according to the report"
)


def _thread(cid: str, text: str, when: datetime | None = None) -> dict:
    payload = thread_payload(cid, text)
    if when is not None:
        payload["snippet"]["topLevelComment"]["snippet"]["publishedAt"] = (
            when.isoformat()
        )
    return payload


def make_stack(client: FakeYouTubeClient | None = None, **overrides):
    """Direct service stack (same wiring as create_app, isolated DB)."""
    # 15s = the configured minimum: the monitor thread never fires inside a
    # test (cycles are driven by poll_once), and the activity window is a
    # deterministic 60 seconds.
    overrides.setdefault("realtime_poll_interval_seconds", 15)
    settings = make_settings(**overrides)
    if client is None:
        client = FakeYouTubeClient(
            video=YtVideoListResponse.model_validate(video_payload())
        )
    database = Database(settings.database_url)
    repo = DatasetRepository(database)
    ingestion = IngestionService(repo, settings)
    dataset = DatasetService(repo, settings)
    cache = TTLCache(
        ttl_seconds=settings.cache_ttl_seconds,
        max_entries=settings.cache_max_entries,
    )
    acquisition = VideoDataService(
        client=client,
        settings=settings,
        cache=cache,
        ingestion=ingestion,
        dataset=dataset,
    )
    sentiment = SentimentService(repo, settings)
    topics = TopicService(repo, settings)
    insight = InsightService(repo, sentiment, topics, settings)
    realtime = RealtimeService(
        repository=repo,
        dataset=dataset,
        acquisition=acquisition,
        sentiment=sentiment,
        insight=insight,
        settings=settings,
        cache=cache,
    )
    return SimpleNamespace(
        settings=settings,
        client=client,
        database=database,
        repo=repo,
        ingestion=ingestion,
        dataset=dataset,
        cache=cache,
        acquisition=acquisition,
        sentiment=sentiment,
        topics=topics,
        insight=insight,
        realtime=realtime,
    )


def seed(stack, threads) -> int:
    """Full acquisition through the real L3 path (activates VIDEO)."""
    stack.client.pages = {None: (list(threads), None)}
    stack.acquisition.get_video_data(VIDEO)
    return int(stack.repo.get_active_video()["dataset_generation"])


def generation(stack) -> int:
    return int(stack.repo.get_active_video()["dataset_generation"])


def analyze(stack) -> None:
    """Inline analysis pass (same path the extension's GET uses)."""
    stack.sentiment.get_analysis(VIDEO)


def processed_map(stack) -> dict:
    return {
        row["comment_id"]: row["sentiment_processed_at"]
        for row in stack.repo.get_comments(VIDEO)
    }


def status_of(stack, video_id: str = VIDEO):
    return stack.realtime.observe(video_id)


# ---------------------------------------------------------------------------
# Incremental acquisition
# ---------------------------------------------------------------------------


def test_incremental_boundary_stop_writes_nothing():
    stack = make_stack()
    seed(stack, [_thread("c1", POS_TEXT), _thread("c2", NEG_TEXT)])
    run_before = stack.repo.get_latest_ingestion_run(VIDEO)
    assert run_before is not None  # the seed acquisition recorded its run

    outcome = stack.acquisition.acquire_incremental(VIDEO, generation(stack))

    assert outcome.stop_reason == "boundary"
    assert outcome.pages_fetched == 1
    assert outcome.new_comments == 0
    assert outcome.inserted == 0
    assert outcome.storage_ok is True
    assert outcome.superseded is False
    # Exactly one PROBE page beyond the initial acquisition (1 quota unit);
    # a pure probe records no NEW ingestion run (latest row unchanged).
    assert len(stack.client.comment_calls) == 2
    run_after = stack.repo.get_latest_ingestion_run(VIDEO)
    assert {k: run_after[k] for k in run_after.keys()} == {
        k: run_before[k] for k in run_before.keys()
    }


def test_incremental_inserts_only_new_comments_idempotently():
    stack = make_stack()
    seed(stack, [_thread("c1", POS_TEXT), _thread("c2", NEG_TEXT)])

    # Newest-first: two unknown comments above one known.
    stack.client.pages = {None: ([_thread("c4", POS_TEXT), _thread("c3", POS_TEXT), _thread("c1", POS_TEXT)], None)}
    first = stack.acquisition.acquire_incremental(VIDEO, generation(stack))
    assert first.new_comments == 2
    assert first.inserted == 2
    assert first.stop_reason == "exhausted"  # no next token -> whole list seen
    assert first.has_more is False
    assert stack.repo.get_comment_stats(VIDEO)["total"] == 4

    # Same page again: nothing new -> boundary, no duplicates, no run row.
    second = stack.acquisition.acquire_incremental(VIDEO, generation(stack))
    assert second.new_comments == 0
    assert second.inserted == 0
    assert second.stop_reason == "boundary"
    assert stack.repo.get_comment_stats(VIDEO)["total"] == 4
    # 1 seed fetch + one probe per poll: bounded, never a full re-walk.
    assert len(stack.client.comment_calls) == 3


def test_incremental_stops_at_known_boundary_page():
    stack = make_stack()
    seed(stack, [_thread("c1", POS_TEXT), _thread("c2", NEG_TEXT)])

    # Page 1 has one new comment; page 2 is fully stored -> stop there.
    stack.client.pages = {
        None: ([_thread("c3", POS_TEXT)], "p1"),
        "p1": ([_thread("c2", NEG_TEXT), _thread("c1", POS_TEXT)], None),
    }
    outcome = stack.acquisition.acquire_incremental(VIDEO, generation(stack))
    assert outcome.inserted == 1
    assert outcome.stop_reason == "boundary"
    assert outcome.pages_fetched == 2
    assert outcome.has_more is False  # preserved stored truth (was complete)
    assert stack.repo.get_comment_stats(VIDEO)["total"] == 3


def test_incremental_never_reprocesses_processed_rows():
    stack = make_stack()
    seed(
        stack,
        [_thread("c1", POS_TEXT), _thread("c2", NEG_TEXT), _thread("c3", NEU_TEXT)],
    )
    analyze(stack)
    before = processed_map(stack)
    assert all(ts is not None for ts in before.values())

    stack.client.pages = {None: ([_thread("c5", POS_TEXT), _thread("c4", POS_TEXT), _thread("c1", POS_TEXT)], None)}
    stack.realtime.observe(VIDEO)
    assert stack.realtime.poll_once(VIDEO) is True
    stack.realtime.shutdown()

    after = processed_map(stack)
    # Old verdicts untouched: same processed_at, never re-run (§18).
    for cid, ts in before.items():
        assert after[cid] == ts
    # New rows analyzed in the same cycle.
    assert after["c4"] is not None and after["c5"] is not None
    assert stack.repo.get_comment_stats(VIDEO)["total"] == 5


def test_incremental_respects_dataset_cap():
    stack = make_stack(comment_acquisition_max_comments=2)
    seed(stack, [_thread("c1", POS_TEXT), _thread("c2", NEG_TEXT)])
    calls_before = len(stack.client.comment_calls)

    stack.client.pages = {None: ([_thread("c3", POS_TEXT)], None)}
    outcome = stack.acquisition.acquire_incremental(VIDEO, generation(stack))

    assert outcome.stop_reason == "cap"
    assert outcome.pages_fetched == 0
    assert len(stack.client.comment_calls) == calls_before  # 0 quota
    assert stack.repo.get_comment_stats(VIDEO)["total"] == 2


def test_incremental_requires_api_key_then_dataset():
    stack = make_stack()
    seed(stack, [_thread("c1", POS_TEXT)])

    stack.settings.youtube_api_key = ""
    with pytest.raises(MissingApiKey):
        stack.acquisition.acquire_incremental(VIDEO, generation(stack))
    stack.settings.youtube_api_key = "test-key-never-production"

    fresh = make_stack()
    with pytest.raises(VideoNotFound):
        fresh.acquisition.acquire_incremental(VIDEO, 1)


def test_incremental_write_guard_drops_pages_after_switch():
    stack = make_stack()
    seed(stack, [_thread("c1", POS_TEXT)])
    # A new comment exists, but the active video flips mid-fetch: every
    # guarded write must be dropped (§13 generation guard).
    stack.client.pages = {None: ([_thread("c9", POS_TEXT), _thread("c1", POS_TEXT)], None)}

    def hook(_n: int) -> None:
        stack.dataset.ensure_active(OTHER)

    stack.client.page_hook = hook
    outcome = stack.acquisition.acquire_incremental(VIDEO, generation(stack))
    stack.client.page_hook = None

    assert outcome.superseded is True
    assert outcome.stop_reason == "superseded"
    assert outcome.inserted == 0
    # The switch removed the old working dataset entirely.
    assert stack.repo.get_video(VIDEO) is None


# ---------------------------------------------------------------------------
# Monitor lifecycle
# ---------------------------------------------------------------------------


def test_monitor_single_instance_and_shutdown():
    stack = make_stack()
    seed(stack, [_thread("c1", POS_TEXT), _thread("c2", NEG_TEXT)])
    analyze(stack)

    first = status_of(stack)
    second = status_of(stack)
    assert first is not None and second is not None
    assert first.monitoring is True and second.monitoring is True
    assert len(stack.realtime._monitors) == 1  # §21 no duplicate monitors
    assert first.version == second.version == "2:2"
    assert first.trend.state == "STABLE"
    assert first.last_checked_at is None  # first cycle not run yet

    stack.realtime.shutdown()
    assert stack.realtime.poll_once(VIDEO) is False
    assert not stack.realtime._monitors
    # Shutting down: honest answer, no new monitor (§32 restart safety).
    assert status_of(stack).monitoring is False


def test_monitor_retires_when_video_no_longer_active():
    stack = make_stack()
    seed(stack, [_thread("c1", POS_TEXT)])
    analyze(stack)
    assert status_of(stack).monitoring is True
    calls_before = len(stack.client.comment_calls)

    stack.dataset.ensure_active(OTHER)
    assert stack.realtime.poll_once(VIDEO) is True  # cycle ran, then retired
    assert VIDEO not in stack.realtime._monitors
    # No quota was spent on the dead dataset.
    assert len(stack.client.comment_calls) == calls_before
    # Its rows were removed by the switch -> status honestly 404s (None).
    assert status_of(stack) is None
    stack.realtime.shutdown()


def test_monitor_never_activates_a_stray_video():
    stack = make_stack()
    seed(stack, [_thread("c1", POS_TEXT)])
    analyze(stack)
    # Store a second video row WITHOUT activating it (legacy stray, §40).
    seed_video(stack.repo, OTHER)

    stray = status_of(stack, OTHER)
    assert stray is not None
    assert stray.monitoring is False
    assert not stack.realtime._monitors
    # Read endpoints never activate: A is still the working dataset.
    assert str(stack.repo.get_active_video()["video_id"]) == VIDEO
    stack.realtime.shutdown()


def test_tick_skips_while_analysis_job_active():
    stack = make_stack()
    seed(stack, [_thread("c1", POS_TEXT)])
    analyze(stack)
    status_of(stack)
    calls_before = len(stack.client.comment_calls)

    stack.repo.create_job("job-1", VIDEO, generation(stack))
    assert stack.realtime.poll_once(VIDEO) is True
    # Job owns the pipeline: no YouTube poll, no "checked" claim (§13).
    assert len(stack.client.comment_calls) == calls_before
    assert status_of(stack).last_checked_at is None

    stack.repo.finish_job("job-1", "COMPLETED", "COMPLETE")
    assert stack.realtime.poll_once(VIDEO) is True
    assert len(stack.client.comment_calls) == calls_before + 1
    assert status_of(stack).last_checked_at is not None
    stack.realtime.shutdown()


def test_transient_upstream_error_recovers_next_cycle():
    stack = make_stack()
    seed(stack, [_thread("c1", POS_TEXT)])
    analyze(stack)
    status_of(stack)

    stack.client.comment_error = UpstreamUnavailable("boom")
    assert stack.realtime.poll_once(VIDEO) is True
    # Failed attempt: still monitoring, "checked" not advanced (§15).
    live = status_of(stack)
    assert live.monitoring is True
    assert live.last_checked_at is None

    stack.client.comment_error = None
    assert stack.realtime.poll_once(VIDEO) is True
    assert status_of(stack).last_checked_at is not None
    stack.realtime.shutdown()


def test_monitor_retires_when_api_key_missing():
    stack = make_stack()
    seed(stack, [_thread("c1", POS_TEXT)])
    analyze(stack)
    status_of(stack)

    stack.settings.youtube_api_key = ""
    stack.realtime.poll_once(VIDEO)
    assert not stack.realtime._monitors
    assert status_of(stack).monitoring is False
    stack.realtime.shutdown()


def test_monitor_stops_when_comments_become_disabled():
    stack = make_stack()
    seed(stack, [_thread("c1", POS_TEXT)])
    analyze(stack)
    status_of(stack)

    from app.core.errors import CommentsDisabled

    stack.client.comment_error = CommentsDisabled("comments are off")
    stack.realtime.poll_once(VIDEO)
    assert not stack.realtime._monitors
    row = stack.repo.get_video(VIDEO)
    assert row["comments_status"] == "disabled"  # persisted truth
    stack.realtime.shutdown()


# ---------------------------------------------------------------------------
# Trend
# ---------------------------------------------------------------------------


def test_trend_falling_then_persists_until_next_movement():
    stack = make_stack()
    seed(
        stack,
        [_thread(f"p{i}", POS_TEXT) for i in range(5)]
        + [_thread(f"n{i}", NEG_TEXT) for i in range(5)],
    )
    analyze(stack)  # baseline 50 / 0 / 50, net 0
    status_of(stack)

    stack.client.pages = {
        None: ([_thread(f"x{i}", NEG_TEXT) for i in range(10)] + [_thread("p0", POS_TEXT)], None)
    }
    assert stack.realtime.poll_once(VIDEO) is True
    live = status_of(stack)
    assert live.trend.state == "FALLING"
    assert live.trend.change_pp == -50.0  # (25 - 75) - (50 - 50)
    assert live.trend.positive_pp == -25.0
    assert live.new_comments == 10
    assert live.last_updated_at is not None

    # No new data next cycle: trend and batch are STABLE snapshots (§4 -
    # numbers never move without new data behind them).
    checked_before = live.last_checked_at
    assert stack.realtime.poll_once(VIDEO) is True
    same = status_of(stack)
    assert same.trend.state == "FALLING"
    assert same.trend.change_pp == -50.0
    assert same.new_comments == 10
    assert same.last_updated_at == live.last_updated_at
    assert same.last_checked_at > checked_before
    stack.realtime.shutdown()


def test_trend_rising():
    stack = make_stack()
    seed(
        stack,
        [_thread(f"p{i}", POS_TEXT) for i in range(5)]
        + [_thread(f"n{i}", NEG_TEXT) for i in range(5)],
    )
    analyze(stack)
    status_of(stack)

    stack.client.pages = {
        None: ([_thread(f"y{i}", POS_TEXT) for i in range(10)] + [_thread("n0", NEG_TEXT)], None)
    }
    stack.realtime.poll_once(VIDEO)
    live = status_of(stack)
    assert live.trend.state == "RISING"
    assert live.trend.change_pp == 50.0
    assert live.trend.negative_pp == -25.0
    assert live.analyzed == 20
    stack.realtime.shutdown()


def test_trend_stable_below_noise_floor():
    stack = make_stack(realtime_trend_min_change=50.0)
    seed(
        stack,
        [_thread(f"p{i}", POS_TEXT) for i in range(5)]
        + [_thread(f"n{i}", NEG_TEXT) for i in range(5)],
    )
    analyze(stack)
    status_of(stack)

    stack.client.pages = {None: ([_thread("p5", POS_TEXT), _thread("p0", POS_TEXT)], None)}
    stack.realtime.poll_once(VIDEO)
    live = status_of(stack)
    # 6/11 vs 5/10: net moves +9.0pp - real movement, below the floor.
    assert live.trend.state == "STABLE"
    assert live.trend.change_pp == 9.0
    assert live.trend.positive_pp == 4.5
    stack.realtime.shutdown()


def test_trend_baseline_starts_stable_with_zero_change():
    stack = make_stack()
    seed(stack, [_thread("c1", POS_TEXT), _thread("c2", NEG_TEXT)])
    analyze(stack)
    live = status_of(stack)
    assert live.trend.state == "STABLE"
    assert live.trend.change_pp == 0.0
    stack.realtime.poll_once(VIDEO)  # no new data -> baseline untouched
    assert status_of(stack).trend.change_pp == 0.0
    stack.realtime.shutdown()


def test_trend_resets_baseline_after_restart():
    """Sprint 8 documented limitation: trend snapshot is in-memory only."""
    stack = make_stack()
    seed(stack, [_thread("c1", POS_TEXT)])
    analyze(stack)
    status_of(stack)
    stack.realtime.shutdown()

    stack.realtime = RealtimeService(
        repository=stack.repo,
        dataset=stack.dataset,
        acquisition=stack.acquisition,
        sentiment=stack.sentiment,
        insight=stack.insight,
        settings=stack.settings,
        cache=stack.cache,
    )
    fresh = status_of(stack)
    assert fresh.monitoring is True  # monitor restarts on the next touch
    assert fresh.trend.state == "STABLE"
    assert fresh.trend.change_pp == 0.0
    assert fresh.last_checked_at is None
    stack.realtime.shutdown()


# ---------------------------------------------------------------------------
# Audience activity
# ---------------------------------------------------------------------------


def test_activity_low_when_no_recent_comments():
    stack = make_stack()
    seed(stack, [_thread("c1", POS_TEXT), _thread("c2", NEG_TEXT)])
    live = status_of(stack)
    assert live.activity.level == "LOW"
    assert live.activity.new_recent == 0
    assert live.activity.window_minutes == 1.0  # max(2*15s, 60s)
    assert live.activity.rate_per_minute == 0.0
    stack.realtime.shutdown()


def test_activity_bands_from_real_published_at():
    now = datetime.now(timezone.utc)

    moderate = make_stack()
    moderate.client.pages = {
        None: (
            [_thread(f"m{i}", POS_TEXT, now - timedelta(seconds=3)) for i in range(3)],
            None,
        )
    }
    moderate.acquisition.get_video_data(VIDEO)
    live = status_of(moderate)
    assert live.activity.level == "MODERATE"
    assert live.activity.new_recent == 3
    assert live.activity.rate_per_minute == 3.0
    moderate.realtime.shutdown()

    high = make_stack()
    high.client.pages = {
        None: (
            [_thread(f"h{i}", POS_TEXT, now - timedelta(seconds=5)) for i in range(6)],
            None,
        )
    }
    high.acquisition.get_video_data(VIDEO)
    live = status_of(high)
    assert live.activity.level == "HIGH"
    assert live.activity.new_recent == 6
    assert live.activity.rate_per_minute == 6.0
    high.realtime.shutdown()


def test_count_recent_comments_boundary():
    stack = make_stack()
    now = datetime.now(timezone.utc)
    stack.client.pages = {
        None: ([_thread("c1", POS_TEXT, now - timedelta(seconds=10))], None)
    }
    stack.acquisition.get_video_data(VIDEO)
    repo = stack.repo

    assert repo.count_recent_comments(VIDEO, (now - timedelta(seconds=60)).isoformat()) == 1
    assert repo.count_recent_comments(VIDEO, (now + timedelta(seconds=10)).isoformat()) == 0


# ---------------------------------------------------------------------------
# Insight warm gate
# ---------------------------------------------------------------------------


class SpyInsight:
    def __init__(self) -> None:
        self.calls = 0

    def get_insight(self, video_id: str):
        self.calls += 1
        return None


def test_insight_warm_gated_by_new_analyzed_threshold():
    stack = make_stack(realtime_insight_min_new_analyzed=2)
    seed(stack, [_thread("c1", POS_TEXT)])
    analyze(stack)  # analyzed = 1
    spy = SpyInsight()
    stack.realtime = RealtimeService(
        repository=stack.repo,
        dataset=stack.dataset,
        acquisition=stack.acquisition,
        sentiment=stack.sentiment,
        insight=spy,
        settings=stack.settings,
        cache=stack.cache,
    )
    status_of(stack)  # baseline watermark = 1

    stack.client.pages = {None: ([_thread("c2", POS_TEXT), _thread("c1", POS_TEXT)], None)}
    stack.realtime.poll_once(VIDEO)
    assert stack.repo.get_sentiment_counts(VIDEO)["POSITIVE"] == 2
    assert spy.calls == 0  # 1 new analyzed < threshold

    stack.client.pages = {None: ([_thread("c3", POS_TEXT), _thread("c2", POS_TEXT), _thread("c1", POS_TEXT)], None)}
    stack.realtime.poll_once(VIDEO)
    assert spy.calls == 1  # 2 new analyzed >= threshold -> warm

    stack.client.pages = {None: ([_thread("c4", POS_TEXT), _thread("c3", POS_TEXT), _thread("c2", POS_TEXT), _thread("c1", POS_TEXT)], None)}
    stack.realtime.poll_once(VIDEO)
    assert spy.calls == 1  # watermark advanced -> gated again
    stack.realtime.shutdown()


# ---------------------------------------------------------------------------
# API contract (TestClient)
# ---------------------------------------------------------------------------


def _api_client(app_factory, **overrides):
    client = FakeYouTubeClient(
        video=YtVideoListResponse.model_validate(video_payload()),
        pages={None: ([_thread("c1", POS_TEXT), _thread("c2", NEG_TEXT)], None)},
    )
    return app_factory(client, **overrides)


def test_realtime_endpoint_contract_and_touch_dedupe(app_factory):
    tc = _api_client(app_factory)
    try:
        assert tc.get(f"/api/videos/{VIDEO}").status_code == 200
        assert tc.get(f"/api/videos/{VIDEO}/sentiment").status_code == 200

        response = tc.get(f"/api/videos/{VIDEO}/realtime")
        assert response.status_code == 200
        data = response.json()

        # camelCase contract (ApiModel serialization aliases).
        expected_keys = {
            "videoId", "enabled", "monitoring", "pollIntervalSeconds",
            "lastCheckedAt", "lastUpdatedAt", "newComments",
            "totalComments", "analyzed", "pending", "skipped", "failed",
            "sentiment", "trend", "activity", "dominantEmotion", "version",
        }
        assert expected_keys <= set(data)
        assert set(data["sentiment"]) >= {"positive", "neutral", "negative"}
        assert set(data["trend"]) >= {
            "state", "changePp", "positivePp", "neutralPp", "negativePp",
        }
        assert set(data["activity"]) >= {
            "level", "newRecent", "windowMinutes", "ratePerMinute",
        }

        assert data["videoId"] == VIDEO
        assert data["enabled"] is True
        assert data["monitoring"] is True  # active video -> monitor started
        assert data["totalComments"] == 2
        assert data["analyzed"] == 2
        assert data["version"] == "2:2"
        assert data["trend"]["state"] == "STABLE"
        assert data["activity"]["level"] in {"LOW", "MODERATE", "HIGH"}

        # Touch keeps ONE monitor alive (no duplicate threads, §21).
        second = tc.get(f"/api/videos/{VIDEO}/realtime")
        assert second.status_code == 200
        assert second.json()["monitoring"] is True
        assert len(tc.app.state.realtime_service._monitors) == 1
    finally:
        tc.app.state.realtime_service.shutdown()


def test_realtime_endpoint_404_and_422(app_factory):
    tc = _api_client(app_factory)
    try:
        # 11 valid chars, never acquired -> 404 (no dataset to report on).
        assert tc.get("/api/videos/AAAAAAAAAAA/realtime").status_code == 404
        # Fails video-id validation BEFORE any store access -> 422.
        assert tc.get("/api/videos/bad/realtime").status_code == 422
    finally:
        tc.app.state.realtime_service.shutdown()


def test_realtime_endpoint_disabled_answers_honestly(app_factory):
    tc = _api_client(app_factory, realtime_enabled=False)
    try:
        assert tc.get(f"/api/videos/{VIDEO}").status_code == 200
        response = tc.get(f"/api/videos/{VIDEO}/realtime")
        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is False
        assert data["monitoring"] is False  # configured off -> no monitor
        assert not tc.app.state.realtime_service._monitors
        # Data still answers (stored aggregates are real regardless).
        assert data["totalComments"] == 2
    finally:
        tc.app.state.realtime_service.shutdown()


def test_realtime_endpoint_never_activates_stray_video(app_factory):
    tc = _api_client(app_factory)
    try:
        assert tc.get(f"/api/videos/{VIDEO}").status_code == 200  # A active
        repo = tc.app.state.dataset_service._repo
        seed_video(repo, OTHER)  # stored, never activated

        response = tc.get(f"/api/videos/{OTHER}/realtime")
        assert response.status_code == 200
        data = response.json()
        assert data["monitoring"] is False  # §40: read never activates
        assert str(repo.get_active_video()["video_id"]) == VIDEO
        assert not tc.app.state.realtime_service._monitors
    finally:
        tc.app.state.realtime_service.shutdown()


def test_end_to_end_realtime_flow_without_analyze_button(app_factory):
    """The Sprint 8 definition of done, end to end through the API:
    new comments flow into totals + version while the overlay just polls
    /realtime - no Analyze, no Refresh."""
    tc = _api_client(app_factory)
    try:
        assert tc.get(f"/api/videos/{VIDEO}").status_code == 200
        assert tc.get(f"/api/videos/{VIDEO}/sentiment").status_code == 200
        before = tc.get(f"/api/videos/{VIDEO}/realtime").json()
        assert before["totalComments"] == 2
        assert before["analyzed"] == 2
        assert before["newComments"] == 0
        assert before["lastUpdatedAt"] is None

        # "Two minutes later": two genuinely new audience comments exist.
        tc.app.state.video_data_service._client.pages = {
            None: (
                [_thread("c4", POS_TEXT), _thread("c3", POS_TEXT), _thread("c1", POS_TEXT)],
                None,
            )
        }
        # One background cycle (driven synchronously instead of waiting 30s).
        assert tc.app.state.realtime_service.poll_once(VIDEO) is True

        after = tc.get(f"/api/videos/{VIDEO}/realtime").json()
        assert after["totalComments"] == 4
        assert after["analyzed"] == 4  # only the new rows were processed
        assert after["newComments"] == 2
        assert after["version"] != before["version"]  # silent-refetch marker
        assert after["lastUpdatedAt"] is not None
        assert after["lastCheckedAt"] is not None
        # The cached L1 contract was invalidated by the insert.
        assert tc.app.state.cache.__len__() == 0
    finally:
        tc.app.state.realtime_service.shutdown()

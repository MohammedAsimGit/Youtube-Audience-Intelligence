"""Sprint 4.3 tests: background analysis jobs (§34).

Covers, with fixtures only (mock-data policy):
- job creation: fast 202 (never waits for the pipeline), job id, dedupe of
  duplicate triggers, validation, missing-key fail-fast;
- acquisition through the job: paginated multi-page runs, fresh-dataset
  skip (0 quota), partial failure + retry continuation without duplicates;
- sentiment through the job: batch processing to COMPLETED with real
  status-endpoint counts;
- progress: QUEUED -> ACQUIRING -> ANALYZING -> COMPLETED (observed as a
  never-regressing subsequence) and failure paths;
- timeout: the POST returns while the pipeline is still running;
- races: A -> B, A -> B -> C -> D - stale jobs can never write into the
  active dataset;
- same-video behavior: A -> A reuses the running job;
- restart: non-terminal jobs are swept to STALE on startup (§32).
"""
import time

import pytest

from app.db.connection import Database
from app.db.repository import DatasetRepository
from app.schemas.youtube import YtVideoListResponse
from tests.conftest import (
    FakeYouTubeClient,
    thread_payload,
    video_payload,
)

VIDEO = "dQw4w9WgXcQ"
OTHER = "BBBBBBBBBBB"
THIRD = "CCCCCCCCCCC"
FOURTH = "DDDDDDDDDDD"

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "STALE"}
# Legal lifecycle order for subsequence assertions (intermediate states may
# be skipped under fast execution, but a status may never go backwards).
ORDER = {"QUEUED": 0, "ACQUIRING": 1, "ANALYZING": 2, "COMPLETED": 3}


def _pages(count: int, prefix: str = "c"):
    """Chain `count` fake comment pages, each with one comment."""
    pages = {}
    token = None
    for index in range(count):
        next_token = f"t{index + 1}" if index + 1 < count else None
        pages[token] = ([thread_payload(f"{prefix}{index}", "hello world")], next_token)
        token = next_token
    return pages


def _fake(pages=None, video_id=VIDEO, **kwargs):
    return FakeYouTubeClient(
        video=YtVideoListResponse.model_validate(video_payload(video_id)),
        pages=pages if pages is not None else {None: ([thread_payload("c1", "hi")], None)},
        **kwargs,
    )


def wait_for_terminal(client, video_id: str, timeout: float = 10.0) -> dict:
    """Poll the status endpoint until the job reaches a terminal state."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/videos/{video_id}/analysis/status")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in TERMINAL:
            return last
        time.sleep(0.02)
    raise AssertionError(f"job never reached a terminal state (last: {last})")


def observed_statuses(client, video_id: str, timeout: float = 10.0) -> list:
    """Poll until terminal, recording every distinct status in order."""
    deadline = time.monotonic() + timeout
    seen: list = []
    while time.monotonic() < deadline:
        response = client.get(f"/api/videos/{video_id}/analysis/status")
        assert response.status_code == 200, response.text
        status = response.json()["status"]
        if not seen or seen[-1] != status:
            seen.append(status)
        if status in TERMINAL:
            return seen
        time.sleep(0.01)
    raise AssertionError(f"job never reached a terminal state (seen: {seen})")


class TestJobCreation:
    def test_post_returns_202_without_waiting_for_the_pipeline(self, app_factory):
        """§34 timeout test: the HTTP request must NOT stay open until the
        pipeline finishes. The fake needs >= 0.6s of wall time; the POST
        must come back long before that, with the job still non-terminal."""
        fake = _fake(pages=_pages(3), page_hook=lambda n: time.sleep(0.25))
        client = app_factory(fake)

        started = time.monotonic()
        response = client.post(f"/api/videos/{VIDEO}/analysis")
        elapsed = time.monotonic() - started

        assert response.status_code == 202
        body = response.json()
        assert body["videoId"] == VIDEO
        assert body["status"] == "QUEUED"
        assert body["jobId"]
        assert elapsed < 0.6, f"POST blocked for {elapsed:.2f}s"

        # The request returned while the pipeline was still running.
        status = client.get(f"/api/videos/{VIDEO}/analysis/status").json()
        assert status["status"] not in TERMINAL

        final = wait_for_terminal(client, VIDEO)
        assert final["status"] == "COMPLETED"
        assert final["phase"] == "COMPLETE"
        assert final["collected"] == 3
        assert final["stored"] == 3

    def test_duplicate_trigger_returns_the_running_job(self, app_factory):
        """§21: Analyze × 3 while running -> one job, one acquisition."""
        fake = _fake(pages=_pages(3), page_hook=lambda n: time.sleep(0.2))
        client = app_factory(fake)

        first = client.post(f"/api/videos/{VIDEO}/analysis").json()
        second = client.post(f"/api/videos/{VIDEO}/analysis").json()
        third = client.post(f"/api/videos/{VIDEO}/analysis").json()

        assert first["jobId"] == second["jobId"] == third["jobId"]
        assert {first["status"], second["status"], third["status"]} <= {
            "QUEUED",
            "ACQUIRING",
            "ANALYZING",
        }
        wait_for_terminal(client, VIDEO)
        # One videos.list + exactly the pages of ONE run (no triple work).
        assert fake.video_calls == 1
        assert len(fake.comment_calls) == 3

    def test_invalid_video_id_is_422_before_any_lifecycle_change(self, app_factory):
        client = app_factory(_fake())
        response = client.post("/api/videos/bad/analysis")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_video_id"
        assert client.get("/api/videos/bad/analysis/status").status_code == 422

    def test_missing_api_key_fails_fast_with_503(self, app_factory):
        client = app_factory(_fake(), youtube_api_key="")
        response = client.post(f"/api/videos/{VIDEO}/analysis")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "server_not_configured"

    def test_status_is_404_before_any_job_exists(self, app_factory):
        client = app_factory(_fake())
        response = client.get(f"/api/videos/{VIDEO}/analysis/status")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "video_not_found"


class TestAcquisitionThroughJob:
    def test_paginated_acquisition_completes_with_real_progress(self, app_factory):
        fake = _fake(pages=_pages(4))
        client = app_factory(fake)

        client.post(f"/api/videos/{VIDEO}/analysis")
        final = wait_for_terminal(client, VIDEO)

        assert final["status"] == "COMPLETED"
        assert final["collected"] == 4
        assert final["stored"] == 4
        assert final["analyzable"] == 4
        assert final["hasMore"] is False
        assert final["errorCode"] is None
        assert len(fake.comment_calls) == 4  # one call per page, no ceiling at 1

        stats = client.get(f"/api/videos/{VIDEO}/stats").json()
        assert stats["totalComments"] == 4

    def test_fresh_dataset_job_skips_youtube(self, app_factory):
        """§40 quota rule: a fresh, complete dataset costs 0 units."""
        fake = _fake()
        client = app_factory(fake)

        assert client.get(f"/api/videos/{VIDEO}").status_code == 200
        assert fake.video_calls == 1

        client.post(f"/api/videos/{VIDEO}/analysis")
        final = wait_for_terminal(client, VIDEO)

        assert final["status"] == "COMPLETED"
        assert final["collected"] == final["stored"] == 1  # truthful counts
        assert fake.video_calls == 1  # no re-acquisition
        assert len(fake.comment_calls) == 1

    def test_partial_failure_preserves_data_and_retry_continues(self, app_factory):
        """§22: failure after N comments keeps them; retry continues the
        acquisition without duplicating comments or reprocessing verdicts."""
        from app.core.errors import UpstreamTimeout

        fake = _fake(
            pages=_pages(4),
            comment_error=UpstreamTimeout("youtube request timed out"),
            comment_error_after=2,  # pages 1-2 succeed, page 3 fails
        )
        client = app_factory(fake)

        client.post(f"/api/videos/{VIDEO}/analysis")
        failed = wait_for_terminal(client, VIDEO)

        assert failed["status"] == "FAILED"
        assert failed["errorCode"] == "upstream_timeout"
        assert failed["errorMessage"]
        assert failed["collected"] == 2  # "failed after 2 comments"
        assert failed["stored"] == 2  # partial data preserved
        stats = client.get(f"/api/videos/{VIDEO}/stats")
        assert stats.status_code == 200
        assert stats.json()["totalComments"] == 2

        # Retry: the transient error is gone -> acquisition CONTINUES.
        fake.comment_error = None
        client.post(f"/api/videos/{VIDEO}/analysis")
        retried = wait_for_terminal(client, VIDEO)

        assert retried["status"] == "COMPLETED"
        assert retried["collected"] == 4  # full run fetched all 4 pages
        assert retried["stored"] == 4  # dedup: exactly 4 rows, no doubles
        assert retried["jobId"] != failed["jobId"]  # retry = new job
        stats = client.get(f"/api/videos/{VIDEO}/stats").json()
        assert stats["totalComments"] == 4


class TestSentimentThroughJob:
    def test_job_runs_sentiment_to_completion(self, app_factory):
        positive = (
            "I really love this video, it is fantastic and genuinely helpful"
        )
        fake = _fake(pages={None: ([thread_payload("c1", positive)], None)})
        client = app_factory(fake)

        client.post(f"/api/videos/{VIDEO}/analysis")
        final = wait_for_terminal(client, VIDEO)

        assert final["status"] == "COMPLETED"
        assert final["phase"] == "COMPLETE"
        assert final["analyzed"] == 1
        assert final["skipped"] == 0
        assert final["failed"] == 0
        assert final["pending"] == 0

        sentiment = client.get(f"/api/videos/{VIDEO}/sentiment").json()
        assert sentiment["status"] == "PROCESSED"
        assert sentiment["stats"]["analyzed"] == 1


class TestProgressLifecycle:
    def test_status_sequence_never_goes_backwards(self, app_factory):
        fake = _fake(pages=_pages(3), page_hook=lambda n: time.sleep(0.15))
        client = app_factory(fake)

        client.post(f"/api/videos/{VIDEO}/analysis")
        seen = observed_statuses(client, VIDEO)

        assert seen[0] in {"QUEUED", "ACQUIRING"}
        assert seen[-1] == "COMPLETED"
        ranks = [ORDER[status] for status in seen]  # KeyError = illegal state
        assert ranks == sorted(ranks), f"lifecycle regressed: {seen}"
        # The slow pages make ACQUIRING observable in practice; ANALYZING
        # may be skipped by a fast poll - both are legal (§7 states).
        assert set(seen) <= set(ORDER)

    def test_failure_path_reports_failed_with_counts(self, app_factory):
        from app.core.errors import UpstreamTimeout

        fake = _fake(
            pages=_pages(2),
            comment_error=UpstreamTimeout("youtube request timed out"),
        )
        client = app_factory(fake)

        client.post(f"/api/videos/{VIDEO}/analysis")
        seen = observed_statuses(client, VIDEO)
        assert seen[-1] == "FAILED"

        status = client.get(f"/api/videos/{VIDEO}/analysis/status").json()
        assert status["errorCode"] == "upstream_timeout"
        assert status["finishedAt"] is not None
        assert status["collected"] == 0  # failed on the first page


class TestVideoSwitchInvalidation:
    def test_switch_cancels_job_and_blocks_stale_writes(self, app_factory):
        """§19: A running -> B activated -> A can never write again."""
        fake = _fake(pages=_pages(5), page_hook=lambda n: time.sleep(0.15))
        client = app_factory(fake)

        client.post(f"/api/videos/{VIDEO}/analysis")
        # Wait until A actually stored its first page (job is live).
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = client.get(f"/api/videos/{VIDEO}/analysis/status").json()
            if status["stored"] >= 1:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("job never stored a page")

        # User switches to B (legacy GET path performs the activation).
        fake.video = YtVideoListResponse.model_validate(video_payload(OTHER))
        fake.pages = {None: ([thread_payload("b1", "video b")], None)}
        assert client.get(f"/api/videos/{OTHER}").status_code == 200

        cancelled = wait_for_terminal(client, VIDEO)
        assert cancelled["status"] == "CANCELLED"
        assert cancelled["errorCode"] == "job_cancelled"

        # Only B's dataset exists; A's partial rows are gone.
        assert client.get(f"/api/videos/{VIDEO}/stats").status_code == 404
        stats_b = client.get(f"/api/videos/{OTHER}/stats").json()
        assert stats_b["totalComments"] == 1
        assert stats_b["videoId"] == OTHER

    def test_rapid_switch_a_b_c_d_leaves_only_d(self, app_factory):
        """§20: A -> B -> C -> D without stale data corruption."""
        fake = _fake(pages=_pages(4), page_hook=lambda n: time.sleep(0.1))
        client = app_factory(fake)

        client.post(f"/api/videos/{VIDEO}/analysis")
        time.sleep(0.05)  # let A get going before the storm

        jobs = {}
        for video_id in (OTHER, THIRD, FOURTH):
            response = client.post(f"/api/videos/{video_id}/analysis")
            assert response.status_code == 202
            jobs[video_id] = response.json()["jobId"]
            time.sleep(0.03)

        final = wait_for_terminal(client, FOURTH)
        assert final["status"] == "COMPLETED"

        # Every superseded job is terminal-CANCELLED, never stuck.
        for video_id in (VIDEO, OTHER, THIRD):
            status = client.get(f"/api/videos/{video_id}/analysis/status").json()
            assert status["status"] == "CANCELLED", (
                video_id,
                status["status"],
            )

        # Only D owns the working dataset (ids are shared across the fake
        # pages, so a surviving stale row would surface as extra rows).
        active = client.get(f"/api/videos/{FOURTH}/stats")
        assert active.status_code == 200
        assert active.json()["totalComments"] == 4
        for video_id in (VIDEO, OTHER, THIRD):
            assert client.get(f"/api/videos/{video_id}/stats").status_code == 404

    def test_same_video_rerequest_reuses_running_job(self, app_factory):
        """§21 A -> A: no duplicate job, no duplicate dataset work."""
        fake = _fake(pages=_pages(3), page_hook=lambda n: time.sleep(0.2))
        client = app_factory(fake)

        first = client.post(f"/api/videos/{VIDEO}/analysis").json()
        again = client.post(f"/api/videos/{VIDEO}/analysis").json()
        assert first["jobId"] == again["jobId"]

        wait_for_terminal(client, VIDEO)
        stats = client.get(f"/api/videos/{VIDEO}/stats").json()
        assert stats["totalComments"] == 3  # one dataset, not two


class TestRestartRecovery:
    def test_startup_marks_running_jobs_stale(self, app_factory, tmp_path):
        """§32: a job orphaned by a restart becomes STALE (retryable) -
        the UI can never stay pinned at ACQUIRING forever."""
        db_url = f"sqlite:///{tmp_path}/restart.db"

        # Simulate the pre-restart process: schema + a non-terminal job.
        db = Database(db_url)
        repo = DatasetRepository(db)
        repo.create_job("job-orphaned", VIDEO, 1)
        assert repo.get_job("job-orphaned")["status"] == "QUEUED"
        db.close()

        client = app_factory(_fake(), database_url=db_url)
        with client:  # lifespan runs the startup sweep
            status = client.get(f"/api/videos/{VIDEO}/analysis/status")
            assert status.status_code == 200
            body = status.json()
            assert body["status"] == "STALE"
            assert body["errorCode"] == "job_interrupted"
            assert body["finishedAt"] is not None

        # And a fresh job can start afterwards (retry is possible).
        db2 = Database(db_url)
        row = DatasetRepository(db2).get_job("job-orphaned")
        assert row["status"] == "STALE"
        db2.close()


class TestNoRawLeakage:
    def test_status_payload_contains_no_comment_text_or_secrets(self, app_factory):
        """§10/§37: progress API exposes counts only."""
        secret_text = "super secret audience comment body"
        fake = _fake(pages={None: ([thread_payload("c1", secret_text)], None)})
        client = app_factory(fake)

        client.post(f"/api/videos/{VIDEO}/analysis")
        final = wait_for_terminal(client, VIDEO)

        import json

        payload = json.dumps(final)
        assert secret_text not in payload
        assert "test-key-never-production" not in payload
        assert "AIza" not in payload  # no API key material in statuses


# ---------------------------------------------------------------------------
# Sprint 5.1 (§6/§9): acquisition and analysis overlap on one job
# ---------------------------------------------------------------------------


class TestOverlappedPipeline:
    def test_interim_analysis_persists_before_acquisition_finishes(self, app_factory):
        """§6/§9: while pages are STILL being fetched, earlier pages are
        already analyzed - first insight no longer waits for the last page.

        The page hook observes the dataset from the fetch side: every hook
        call means acquisition is in progress, so a PROCESSED count > 0 at
        that moment proves the interim drain overlapped the fetch loop.
        """
        total = 6
        text = "This video is absolutely amazing, great work! Thanks for sharing."
        pages = {}
        token = None
        for index in range(total):
            next_token = f"t{index + 1}" if index + 1 < total else None
            pages[token] = ([thread_payload(f"c{index}", text)], next_token)
            token = next_token

        processed_during_fetch: list = []
        box: dict = {}

        def hook(_n: int) -> None:
            # Generous window for the ingest worker + interim drain (same
            # sleep pattern as the existing race tests).
            time.sleep(0.2)
            repo = DatasetRepository(box["client"].app.state.database)
            counts = repo.get_status_counts(VIDEO)
            processed_during_fetch.append(counts.get("PROCESSED", 0))

        fake = _fake(pages=pages, page_hook=hook)
        client = app_factory(fake)
        box["client"] = client

        assert client.post(f"/api/videos/{VIDEO}/analysis").status_code == 202
        final = wait_for_terminal(client, VIDEO)

        assert final["status"] == "COMPLETED"
        assert final["collected"] == total
        # Every row was RUN (analyzed or language-skipped), none failed.
        assert final["analyzed"] + final["skipped"] == total
        assert final["failed"] == 0
        # Overlap proof: at least one page fetch observed rows already
        # moved to PROCESSED by the interim drain.
        assert processed_during_fetch, "page hook never ran"
        assert max(processed_during_fetch) > 0, (
            "no analysis happened while acquisition was still running - "
            "the interim drain did not overlap the fetch loop (§6)"
        )


class TestInterimProgressCadence:
    """Sprint 5.2 §9/§10: interim drains are decoupled from page cadence.

    The FIRST interim drain of a job runs immediately (time-to-first-
    insight), then further drains re-arm only once
    ANALYSIS_PROGRESS_UPDATE_INTERVAL rows are pending - so inference
    happens in interval-sized batches instead of one small drain per
    YouTube page, and `analyzed` advances in truthful interval-sized
    steps. The final ANALYZING drain always finishes everything.
    """

    @staticmethod
    def _pages(total: int, text: str) -> dict:
        pages = {}
        token = None
        for index in range(total):
            next_token = f"t{index + 1}" if index + 1 < total else None
            pages[token] = ([thread_payload(f"c{index}", text)], next_token)
            token = next_token
        return pages

    def test_bootstrap_drains_first_page_then_gate_holds(self, app_factory):
        """interval far above the dataset: exactly ONE interim drain (the
        bootstrap) happens mid-fetch; everything else waits for the
        authoritative final drain."""
        total = 6
        text = "This video is absolutely amazing, great work! Thanks for sharing."
        processed_during_fetch: list = []
        box: dict = {}

        def hook(_n: int) -> None:
            time.sleep(0.2)  # generous window (same pattern as §6 test)
            repo = DatasetRepository(box["client"].app.state.database)
            processed_during_fetch.append(
                repo.get_status_counts(VIDEO).get("PROCESSED", 0)
            )

        fake = _fake(pages=self._pages(total, text), page_hook=hook)
        client = app_factory(fake, analysis_progress_update_interval=1000)
        box["client"] = client

        assert client.post(f"/api/videos/{VIDEO}/analysis").status_code == 202
        final = wait_for_terminal(client, VIDEO)

        assert final["status"] == "COMPLETED"
        assert final["collected"] == total
        # Gate proof: with the old per-page drains the hook would observe
        # PROCESSED growing every page (up to total-1); now it must never
        # exceed the single bootstrap page (1 comment per page here).
        assert processed_during_fetch, "page hook never ran"
        assert max(processed_during_fetch) > 0, "bootstrap interim drain missing (§9)"
        assert max(processed_during_fetch) <= 1, (
            "interim drain ran despite pending < ANALYSIS_PROGRESS_UPDATE_INTERVAL (§10)"
        )
        # No comment was sacrificed: the final drain analyzed everything.
        assert final["analyzed"] + final["skipped"] == total
        assert final["failed"] == 0

    def test_gate_rearms_at_interval_during_fetch(self, app_factory):
        """interval=3 over six 1-comment pages: bootstrap drains page 1,
        then the gate re-arms mid-fetch once 3 rows are pending (observed
        PROCESSED grows from 1 to 4 in one truthful step), and the final
        drain completes the rest."""
        total = 6
        text = "This video is absolutely amazing, great work! Thanks for sharing."
        processed_during_fetch: list = []
        box: dict = {}

        def hook(_n: int) -> None:
            time.sleep(0.2)
            repo = DatasetRepository(box["client"].app.state.database)
            processed_during_fetch.append(
                repo.get_status_counts(VIDEO).get("PROCESSED", 0)
            )

        fake = _fake(pages=self._pages(total, text), page_hook=hook)
        client = app_factory(fake, analysis_progress_update_interval=3)
        box["client"] = client

        assert client.post(f"/api/videos/{VIDEO}/analysis").status_code == 202
        final = wait_for_terminal(client, VIDEO)

        assert final["status"] == "COMPLETED"
        assert processed_during_fetch, "page hook never ran"
        # Re-armed drain observed mid-fetch (1 -> 4), never finished early.
        assert max(processed_during_fetch) >= 4, "gate never re-armed at the interval (§10)"
        assert max(processed_during_fetch) < total, "everything drained mid-fetch?"
        assert final["analyzed"] + final["skipped"] == total
        assert final["failed"] == 0

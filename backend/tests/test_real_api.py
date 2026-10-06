"""Real YouTube Data API integration test - SEPARATE + opt-in (§34).

Never part of the deterministic suite: it needs network access and a real
`YOUTUBE_API_KEY` (from the git-ignored backend/.env or the environment).

Run it explicitly:
    RUN_REAL_API_TESTS=1 python -m pytest tests/test_real_api.py

Credentials are never committed, never asserted against, and never logged.
"""
import os

import pytest

from app.core.config import Settings

_RUN = os.environ.get("RUN_REAL_API_TESTS") == "1"
_KEY_PRESENT = bool(Settings().youtube_api_key)

pytestmark = pytest.mark.skipif(
    not (_RUN and _KEY_PRESENT),
    reason="set RUN_REAL_API_TESTS=1 with YOUTUBE_API_KEY configured (real network)",
)


def test_real_acquisition_persists_analysis_ready_dataset():
    """Extension-shaped request against the real API, then verify storage."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    # Fresh settings: real key, both caches off so we truly hit YouTube + DB.
    settings = Settings(
        database_url="sqlite:///:memory:",
        cache_ttl_seconds=0,
        dataset_cache_ttl_seconds=0,
        log_level="WARNING",
    )
    app = create_app(settings=settings)
    client = TestClient(app)

    # A stable, long-lived public video with active comments.
    video_id = "dQw4w9WgXcQ"

    response = client.get(f"/api/videos/{video_id}")
    assert response.status_code == 200, response.text
    body = response.json()

    # Contract shape the Chrome extension consumes.
    assert body["video"]["videoId"] == video_id
    assert isinstance(body["video"]["title"], str) and body["video"]["title"]
    assert body["comments"]["status"] == "ok"
    assert body["comments"]["count"] >= 1
    assert body["source"]["provider"] == "youtube"
    assert body["source"]["cached"] is False
    first = body["comments"]["items"][0]
    assert first["commentId"] and first["text"]

    # The same acquisition is now queryable as an analysis-ready dataset.
    stats_response = client.get(f"/api/videos/{video_id}/stats")
    assert stats_response.status_code == 200, stats_response.text
    stats = stats_response.json()
    assert stats["totalComments"] == body["comments"]["count"]
    assert stats["processingStatus"]["READY_FOR_ANALYSIS"] == body["comments"]["count"]
    assert stats["lastIngest"]["storageOk"] is True
    assert stats["lastIngest"]["inserted"] >= 1

    client.app.state.database.close()


def test_real_acquisition_paginates_multiple_youtube_pages():
    """Sprint 4.1 proof: the acquisition walks REAL YouTube pages.

    Page size 100, dataset limit 150: collecting more than one page's worth
    proves nextPageToken was followed (a single page can never exceed 100),
    and the dataset metrics must reconcile exactly with what was stored.
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = Settings(
        database_url="sqlite:///:memory:",
        cache_ttl_seconds=0,
        dataset_cache_ttl_seconds=0,
        max_comments_per_request=100,   # YouTube page size
        comment_acquisition_max_comments=150,  # dataset limit for this run
        log_level="WARNING",
    )
    app = create_app(settings=settings)
    client = TestClient(app)
    video_id = "dQw4w9WgXcQ"

    response = client.get(f"/api/videos/{video_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    count = body["comments"]["count"]

    # Multi-page proof: more than ONE page's maximum was collected.
    assert count > 100, f"expected >100 comments from >=2 pages, got {count}"
    assert count <= 150  # dataset limit respected
    has_more = body["comments"]["hasMore"]

    # Ingestion documented the whole run (aggregated, not per-page).
    stats = client.get(f"/api/videos/{video_id}/stats").json()
    assert stats["totalComments"] == count
    assert stats["lastIngest"]["fetched"] >= count
    assert stats["lastIngest"]["storageOk"] is True

    # Sentiment dataset metrics reconcile with the acquisition (§22/§41).
    sentiment = client.get(f"/api/videos/{video_id}/sentiment")
    assert sentiment.status_code == 200, sentiment.text
    dataset = sentiment.json()["dataset"]
    assert dataset["stored"] == count
    assert dataset["collected"] == count
    assert dataset["analyzed"] + dataset["skipped"] == dataset["stored"]
    assert dataset["hasMore"] is has_more
    # limitReached is only true when more comments were indicated while the
    # stored dataset sat at/above the configured maximum - exactly (§14).
    assert dataset["limitReached"] is (has_more and count >= 150)

    client.app.state.database.close()


def test_real_sentiment_processing_persists_and_aggregates():
    """Sprint 4 end-to-end on real data: acquire -> analyze -> verify.

    Runs only under RUN_REAL_API_TESTS=1 with a real key (one acquisition of
    the same bounded dataset; no credentials are ever asserted or logged).
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = Settings(
        database_url="sqlite:///:memory:",
        cache_ttl_seconds=0,
        dataset_cache_ttl_seconds=0,
        log_level="WARNING",
    )
    app = create_app(settings=settings)
    client = TestClient(app)
    video_id = "dQw4w9WgXcQ"

    # 1. Acquire + persist the dataset through the normal contract.
    acquisition = client.get(f"/api/videos/{video_id}")
    assert acquisition.status_code == 200, acquisition.text
    assert acquisition.json()["comments"]["count"] >= 1

    # 2. Backend-owned analysis: this GET triggers processing inline.
    sentiment = client.get(f"/api/videos/{video_id}/sentiment")
    assert sentiment.status_code == 200, sentiment.text
    body = sentiment.json()
    assert body["videoId"] == video_id
    assert body["status"] == "PROCESSED"

    # 3. Aggregates are internally consistent (no fabricated numbers).
    stats = body["stats"]
    total = stats["totalComments"]
    assert total >= 1
    analyzed = stats["positive"] + stats["neutral"] + stats["negative"]
    assert stats["analyzed"] == analyzed
    assert analyzed + stats["skipped"] == total  # every row accounted for
    if analyzed:
        # Exact contract: every percentage is a multiple of 0.1 and the
        # integer TENTHS sum to 1000 (IEEE-754 addition of 0.1-multiples
        # can land a hair off 100.0, so never assert float equality here).
        assert sum(
            int(round(stats[key] * 10))
            for key in ("positivePercent", "neutralPercent", "negativePercent")
        ) == 1000
        assert body["dominantSentiment"] in ("POSITIVE", "NEUTRAL", "NEGATIVE")
    else:
        assert body["dominantSentiment"] is None  # never a label without data

    # 4. Persisted through the repository: verdicts live on the rows.
    repository = client.app.state.sentiment_service._repo
    processed = repository.get_comments_by_status(video_id, "PROCESSED")
    assert len(processed) == analyzed + stats["skipped"]
    for row in processed:
        label = row["sentiment_label"]
        assert label in ("POSITIVE", "NEUTRAL", "NEGATIVE", "UNSUPPORTED_LANGUAGE")
        if label != "UNSUPPORTED_LANGUAGE":
            assert row["sentiment_score"] is not None
            assert -1.0 <= row["sentiment_score"] <= 1.0
            assert row["sentiment_confidence"] is not None
            assert 0.0 <= row["sentiment_confidence"] <= 1.0
        assert row["sentiment_model"]
        assert row["sentiment_processed_at"]

    # 5. Idempotent: the second GET recomputes nothing and agrees exactly.
    rerun = client.get(f"/api/videos/{video_id}/sentiment")
    assert rerun.status_code == 200
    assert rerun.json() == body

    # 6. The stats endpoint now reports the analysis states (additive only).
    stats_after = client.get(f"/api/videos/{video_id}/stats").json()
    statuses = stats_after["processingStatus"]
    assert statuses.get("PROCESSED", 0) == analyzed + stats["skipped"]
    assert statuses.get("READY_FOR_ANALYSIS", 0) == 0

    client.app.state.database.close()


def test_real_video_switch_replaces_the_working_dataset():
    """Sprint 4.2 (§50): acquire video A, then video B against the real API.

    After B activates: A's comments/runs are gone, A's stats+sentiment 404,
    and B's dataset is the only one stored. Bounded dataset (50) to keep the
    real-network cost low. No credentials asserted, no comment text logged.
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = Settings(
        database_url="sqlite:///:memory:",
        cache_ttl_seconds=0,
        dataset_cache_ttl_seconds=0,
        max_comments_per_request=50,
        comment_acquisition_max_comments=50,
        log_level="WARNING",
    )
    app = create_app(settings=settings)
    client = TestClient(app)
    repo = client.app.state.sentiment_service._repo

    video_a = "dQw4w9WgXcQ"  # long-lived, active comments
    video_b = "jNQXAC9IVRw"  # "Me at the zoo" - the first YouTube video

    # Video A: acquire its dataset.
    first = client.get(f"/api/videos/{video_a}")
    assert first.status_code == 200, first.text
    count_a = first.json()["comments"]["count"]
    assert count_a >= 1
    assert repo.get_active_video()["video_id"] == video_a
    assert repo.get_comment_stats(video_a)["total"] == count_a

    # Video B: the switch deletes A's dataset in the same transaction.
    second = client.get(f"/api/videos/{video_b}")
    assert second.status_code == 200, second.text
    count_b = second.json()["comments"]["count"]
    assert count_b >= 1

    # §25/§45: only B's rows remain - not A + B.
    assert repo.get_active_video()["video_id"] == video_b
    assert repo.get_video(video_a) is None
    assert repo.get_comments(video_a) == []
    assert repo.get_latest_ingestion_run(video_a) is None
    assert repo.get_comment_stats(video_b)["total"] == count_b

    # §40: the previous video's read endpoints answer 404 - no blind
    # activation, no stale numbers.
    assert client.get(f"/api/videos/{video_a}/stats").status_code == 404
    assert client.get(f"/api/videos/{video_a}/sentiment").status_code == 404
    # The active video's endpoints work normally.
    assert client.get(f"/api/videos/{video_b}/stats").status_code == 200
    assert client.get(f"/api/videos/{video_b}/sentiment").status_code == 200

    client.app.state.database.close()

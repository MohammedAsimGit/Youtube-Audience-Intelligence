"""Sprint 4.1 tests: pagination, configurable limits, incremental
persistence, truthful hasMore/limit metadata, idempotent re-acquisition.

Fixtures only (mock-data policy): the FakeYouTubeClient stands in for
YouTube; every count below comes from real pipeline execution.
"""
from typing import Dict, List, Optional, Tuple

from app.core.errors import QuotaExceeded
from app.schemas.youtube import YtVideoListResponse
from tests.conftest import (
    FakeYouTubeClient,
    TestClient,
    thread_payload,
    video_payload,
)

VIDEO = "dQw4w9WgXcQ"


def build_pages(
    sizes: List[int], last_token: Optional[str] = None
) -> Dict[Optional[str], Tuple[list, Optional[str]]]:
    """Pages keyed by request token: None -> P2 -> P3 ... -> (last_token).

    Each page `i` holds `sizes[i]` distinct top-level comments (no replies),
    so the expected dataset size is exactly sum(sizes).
    """
    pages: Dict[Optional[str], Tuple[list, Optional[str]]] = {}
    token: Optional[str] = None
    for index, size in enumerate(sizes):
        next_token = f"P{index + 2}" if index + 1 < len(sizes) else last_token
        items = [
            thread_payload(
                f"c{index}-{i}",
                f"I really love page {index} comment {i}, this video is great",
            )
            for i in range(size)
        ]
        pages[token] = (items, next_token)
        token = next_token
    return pages


def _fake(pages, **client_kwargs) -> FakeYouTubeClient:
    return FakeYouTubeClient(
        video=YtVideoListResponse.model_validate(video_payload()),
        pages=pages,
        **client_kwargs,
    )


class TestPagination:
    def test_walks_every_page_until_youtube_runs_out(self, app_factory):
        """3 full pages, no cap pressure -> exactly 3 requests, hasMore False."""
        fake = _fake(build_pages([2, 2, 2]))
        client = app_factory(fake)
        response = client.get(f"/api/videos/{VIDEO}")
        assert response.status_code == 200
        body = response.json()
        assert body["comments"]["count"] == 6
        assert body["comments"]["hasMore"] is False
        # Multi-page proof: token chain None -> P2 -> P3 requested in order.
        assert len(fake.comment_calls) == 3
        assert [call[1] for call in fake.comment_calls] == [None, "P2", "P3"]

    def test_page_size_and_dataset_limit_are_distinct_settings(self, app_factory):
        # Page size 2 (per request), dataset limit 5 -> three requests with
        # max_results 2, 2, 1 (the last request honors the remaining budget).
        fake = _fake(build_pages([2, 2, 2]))
        client = app_factory(
            fake,
            max_comments_per_request=2,
            comment_acquisition_max_comments=5,
        )
        body = client.get(f"/api/videos/{VIDEO}").json()
        assert body["comments"]["count"] == 5
        assert body["comments"]["hasMore"] is False  # page 3 carried no token
        assert [call[0] for call in fake.comment_calls] == [2, 2, 1]

    def test_limit_larger_than_one_page_fetches_multiple_pages(self, app_factory):
        # Limit 150 > one API page (100): page 2 is requested with the
        # remaining budget (50) and the response is clamped to it.
        fake = _fake(build_pages([100, 100], last_token="P3"))
        client = app_factory(
            fake,
            max_comments_per_request=100,
            comment_acquisition_max_comments=150,
        )
        body = client.get(f"/api/videos/{VIDEO}").json()
        assert body["comments"]["count"] == 150
        assert body["comments"]["hasMore"] is True  # P3 still offered
        assert len(fake.comment_calls) == 2
        assert fake.comment_calls[1] == (50, "P2")

    def test_limit_below_one_page_clamps_without_claiming_more(self, app_factory):
        # Limit 1 with a single 5-comment page and NO token: the cap holds
        # and hasMore stays False (no more pages were ever indicated).
        fake = _fake(build_pages([5]))
        client = app_factory(fake, comment_acquisition_max_comments=1)
        body = client.get(f"/api/videos/{VIDEO}").json()
        assert body["comments"]["count"] == 1
        assert body["comments"]["hasMore"] is False

    def test_empty_page_ends_naturally(self, app_factory):
        fake = _fake(build_pages([3, 0]))
        client = app_factory(fake)
        body = client.get(f"/api/videos/{VIDEO}").json()
        assert body["comments"]["count"] == 3
        assert body["comments"]["hasMore"] is False
        assert len(fake.comment_calls) == 2  # the empty page was requested

    def test_repeated_page_token_stops_safely(self, app_factory):
        # Pathological upstream: the same nextPageToken is returned forever.
        pages = {
            None: ([thread_payload("c1", "one")], "LOOP"),
            "LOOP": ([thread_payload("c2", "two")], "LOOP"),
        }
        fake = _fake(pages)
        client = app_factory(fake)
        body = client.get(f"/api/videos/{VIDEO}").json()
        # The guard stops after seeing the token twice - no infinite loop,
        # and availability is NOT claimed for a token that yields no new data.
        assert body["comments"]["count"] == 2
        assert body["comments"]["hasMore"] is False
        assert len(fake.comment_calls) == 2


class TestIncrementalPersistence:
    def test_api_failure_after_partial_acquisition_keeps_fetched_pages(
        self, app_factory
    ):
        """Page 1 is persisted BEFORE page 2 is requested: when the second
        call fails with a categorized error, page 1 remains queryable and
        the aggregated ingestion run is recorded (spec §31/§38)."""
        fake = _fake(
            build_pages([5, 5], last_token="P3"),
            comment_error=QuotaExceeded("quota"),
            comment_error_after=1,
        )
        client = app_factory(fake, cache_ttl_seconds=0)

        # 1. The categorized error still surfaces unchanged.
        first = client.get(f"/api/videos/{VIDEO}")
        assert first.status_code == 429
        assert first.json()["error"]["code"] == "quota_exceeded"

        # 2. Page 1 survived the failure: the dataset is already fresh, so
        #    the retry is served from storage with ZERO new YouTube calls.
        second = client.get(f"/api/videos/{VIDEO}")
        assert second.status_code == 200
        body = second.json()
        assert body["comments"]["count"] == 5
        assert body["comments"]["hasMore"] is True  # P3 was still indicated
        assert body["source"]["cached"] is True
        assert len(fake.comment_calls) == 2  # 1 success + 1 failed call

        # 3. The interrupted run is documented through the stats contract.
        stats = client.get(f"/api/videos/{VIDEO}/stats").json()
        assert stats["totalComments"] == 5
        assert stats["lastIngest"]["fetched"] == 5
        assert stats["lastIngest"]["storageOk"] is True

        # 4. Truthful metadata flows into the sentiment dataset block: more
        #    comments exist, but the configured limit was NOT reached.
        sentiment = client.get(f"/api/videos/{VIDEO}/sentiment").json()
        assert sentiment["dataset"]["hasMore"] is True
        assert sentiment["dataset"]["limitReached"] is False

    def test_multi_page_acquisition_records_one_aggregated_run(self, app_factory):
        fake = _fake(build_pages([4, 4, 4]))
        client = app_factory(fake)
        assert client.get(f"/api/videos/{VIDEO}").status_code == 200

        stats = client.get(f"/api/videos/{VIDEO}/stats").json()
        assert stats["totalComments"] == 12
        last_ingest = stats["lastIngest"]
        # ONE run for the whole acquisition (not one row per page) with
        # the Sprint 3 invariants intact across pages:
        assert last_ingest["fetched"] == 12
        assert last_ingest["valid"] == 12
        assert last_ingest["rejected"] == 0
        assert last_ingest["inserted"] == 12
        assert last_ingest["duplicates"] == 0
        assert last_ingest["fetched"] == last_ingest["valid"] + last_ingest["rejected"]
        assert last_ingest["valid"] == last_ingest["inserted"] + last_ingest["duplicates"]
        assert stats["processingStatus"] == {"READY_FOR_ANALYSIS": 12}

        # Cross-page proof: rows arrived incrementally yet deduped exactly.
        body = client.get(f"/api/videos/{VIDEO}").json()
        ids = [c["commentId"] for c in body["comments"]["items"]]
        assert len(ids) == len(set(ids)) == 12

    def test_reacquisition_is_idempotent_no_duplicates_no_sentiment_clobber(
        self, app_factory
    ):
        """Run the same acquisition twice (stale dataset -> re-fetch):
        rows refresh in place, none are inserted twice, and already-computed
        sentiment survives unchanged when the text did not change."""
        fake = _fake(build_pages([3, 3]))
        client = app_factory(fake, cache_ttl_seconds=0, dataset_cache_ttl_seconds=0)

        assert client.get(f"/api/videos/{VIDEO}").status_code == 200
        sentiment_first = client.get(f"/api/videos/{VIDEO}/sentiment").json()
        assert sentiment_first["status"] == "PROCESSED"
        assert sentiment_first["stats"]["analyzed"] == 6

        # Second acquisition: fresh YouTube fetch, same underlying comments.
        second = client.get(f"/api/videos/{VIDEO}")
        assert second.status_code == 200
        assert second.json()["comments"]["count"] == 6  # still exactly 6 rows

        stats = client.get(f"/api/videos/{VIDEO}/stats").json()
        assert stats["totalComments"] == 6
        run = stats["lastIngest"]
        assert run["fetched"] == 6
        assert run["inserted"] == 0          # no new records
        assert run["duplicates"] == 6        # every row refreshed, not copied
        assert run["valid"] == run["inserted"] + run["duplicates"]

        # Sentiment idempotency: aggregates identical after re-acquisition.
        sentiment_second = client.get(f"/api/videos/{VIDEO}/sentiment").json()
        assert sentiment_second == sentiment_first

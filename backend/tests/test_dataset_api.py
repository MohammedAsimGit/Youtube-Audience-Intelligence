"""API tests: dataset freshness (L2 cache), isolation, stats endpoint (§22/§24)."""
from app.schemas.youtube import YtVideoListResponse
from tests.conftest import FakeYouTubeClient, thread_payload, video_payload

VIDEO = "dQw4w9WgXcQ"


def _fake_with_comments():
    return FakeYouTubeClient(
        video=YtVideoListResponse.model_validate(video_payload()),
        pages={None: ([thread_payload("c1", "hello")], None)},
    )


class TestFreshDatasetServing:
    def test_fresh_dataset_served_without_youtube(self, app_factory):
        """Re-analyze inside the freshness window costs 0 YouTube quota."""
        fake = _fake_with_comments()
        client = app_factory(fake, cache_ttl_seconds=0, dataset_cache_ttl_seconds=3600)

        first = client.get(f"/api/videos/{VIDEO}")
        assert first.status_code == 200
        assert first.json()["source"]["cached"] is False
        assert fake.video_calls == 1

        second = client.get(f"/api/videos/{VIDEO}")
        assert second.status_code == 200
        body = second.json()
        # L1 memory cache disabled (ttl=0) -> this came from the dataset store.
        assert body["source"]["cached"] is True
        assert fake.video_calls == 1  # no new videos.list call
        assert len(fake.comment_calls) == 1  # no new commentThreads.list call

        # Extension contract shape is identical on the stored-data path.
        first_body = first.json()
        assert body["video"]["videoId"] == VIDEO
        assert body["comments"]["count"] == first_body["comments"]["count"] == 1
        assert body["comments"]["status"] == "ok"
        item = body["comments"]["items"][0]
        assert item["commentId"] == "c1"
        assert item["text"] == "hello"
        assert item["textNormalized"] == "hello"
        assert body["source"]["provider"] == "youtube"

    def test_stale_dataset_reacquires(self, app_factory):
        """TTL elapsed -> freshness policy re-acquires (never stale forever)."""
        fake = _fake_with_comments()
        client = app_factory(fake, cache_ttl_seconds=0, dataset_cache_ttl_seconds=0)

        client.get(f"/api/videos/{VIDEO}")
        second = client.get(f"/api/videos/{VIDEO}")
        assert fake.video_calls == 2
        assert second.json()["source"]["cached"] is False


class TestVideoIsolationThroughApi:
    def test_switch_replaces_the_working_dataset(self, app_factory):
        """Sprint 4.2 §26: the store keeps ONLY the active video's dataset.

        Acquiring video B activates it and atomically removes video A's
        working dataset (comments + runs + video row) - no cross-video
        rows can coexist, and A's stats end with 404 (not stale numbers).
        """
        fake = _fake_with_comments()
        client = app_factory(fake, cache_ttl_seconds=0)
        first = client.get(f"/api/videos/{VIDEO}")
        assert first.status_code == 200
        assert first.json()["comments"]["count"] == 1  # A has 1 comment

        # Second video: different metadata + different comment ids.
        fake.video = YtVideoListResponse.model_validate(video_payload("BBBBBBBBBBB"))
        fake.pages = {None: ([thread_payload("b1", "second video")], None)}
        other = client.get("/api/videos/BBBBBBBBBBB")
        assert other.status_code == 200
        assert other.json()["video"]["videoId"] == "BBBBBBBBBBB"
        assert other.json()["comments"]["count"] == 1  # B has 1 comment

        stats_b = client.get("/api/videos/BBBBBBBBBBB/stats").json()
        assert stats_b["totalComments"] == 1
        # Datasets never mix: each comment id lives under its own video,
        # and only the active one exists at all.
        assert stats_b["videoId"] == "BBBBBBBBBBB"
        gone_a = client.get(f"/api/videos/{VIDEO}/stats")
        assert gone_a.status_code == 404
        assert gone_a.json()["error"]["code"] == "video_not_found"


class TestStatsEndpoint:
    def _acquire(self, app_factory):
        pages = {
            None: (
                [
                    thread_payload("c1", "first comment"),
                    thread_payload(
                        "c2",
                        "second with reply",
                        replies=[
                            {
                                "id": "r1",
                                "parentId": "c2",
                                "snippet": {
                                    "authorDisplayName": "Replier",
                                    "textOriginal": "reply text",
                                    "publishedAt": "2024-03-03T09:00:00Z",
                                    "likeCount": 1,
                                },
                            }
                        ],
                    ),
                ],
                None,
            )
        }
        fake = FakeYouTubeClient(
            video=YtVideoListResponse.model_validate(video_payload()),
            pages=pages,
        )
        client = app_factory(fake)
        response = client.get(f"/api/videos/{VIDEO}")
        assert response.status_code == 200
        return client

    def test_stats_reflect_real_pipeline_execution(self, app_factory):
        client = self._acquire(app_factory)
        response = client.get(f"/api/videos/{VIDEO}/stats")
        assert response.status_code == 200
        body = response.json()

        assert body["videoId"] == VIDEO
        assert body["totalComments"] == 3  # 2 top-level + 1 reply
        assert body["uniqueComments"] == 3
        assert body["replyCount"] == 1
        assert body["topLevelCommentCount"] == 2
        assert body["commentsStatus"] == "ok"
        assert body["processingStatus"] == {"READY_FOR_ANALYSIS": 3}
        assert body["oldestCommentTimestamp"] is not None
        assert body["newestCommentTimestamp"] is not None
        assert body["lastAcquiredAt"] is not None

        # Data-quality numbers came from the real ingestion run (§27).
        last_ingest = body["lastIngest"]
        assert last_ingest["fetched"] == 3
        assert last_ingest["valid"] == 3
        assert last_ingest["rejected"] == 0
        assert last_ingest["duplicates"] == 0
        assert last_ingest["inserted"] == 3
        assert last_ingest["storageOk"] is True
        assert last_ingest["durationMs"] >= 0

    def test_stats_for_unknown_video_404(self, app_factory):
        client = app_factory(_fake_with_comments())
        response = client.get("/api/videos/aaaaaaaaaaa/stats")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "video_not_found"

    def test_stats_invalid_id_422(self, app_factory):
        client = app_factory(_fake_with_comments())
        response = client.get("/api/videos/bad/stats")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_video_id"

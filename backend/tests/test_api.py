"""Endpoint tests: health, validation, success, pagination, failures, cache."""
import json
from urllib.parse import quote

from app.clients.youtube import (
    parse_comment_threads_payload,
    parse_video_payload,
)
from app.core.errors import (
    CommentsDisabled,
    QuotaExceeded,
    UpstreamTimeout,
    UpstreamUnstable,
)
from app.schemas.youtube import YtVideoListResponse
from tests.conftest import (
    FakeYouTubeClient,
    TestClient,
    thread_payload,
    video_payload,
)


class TestHealth:
    def test_health_ok(self, client: TestClient):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestValidation:
    def test_invalid_video_ids_rejected(self, app_factory):
        client = app_factory(FakeYouTubeClient())
        for bad in ["bad", "wrongid!", "dQw4w9WgXcQ-extra", "spaces here"]:
            response = client.get(f"/api/videos/{quote(bad, safe='')}")
            assert response.status_code == 422, bad
            assert response.json()["error"]["code"] == "invalid_video_id"

    def test_video_not_found_when_youtube_returns_empty(self, app_factory):
        client = app_factory(FakeYouTubeClient(video=YtVideoListResponse(items=[])))
        response = client.get("/api/videos/aaaaaaaaaaa")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "video_not_found"


class TestSuccess:
    def test_metadata_normalized_to_contract(self, app_factory):
        fake = FakeYouTubeClient(
            video=YtVideoListResponse.model_validate(video_payload()),
            pages={None: ([], None)},
        )
        client = app_factory(fake)
        response = client.get("/api/videos/dQw4w9WgXcQ")
        assert response.status_code == 200
        body = response.json()
        # camelCase contract keys
        assert body["video"]["videoId"] == "dQw4w9WgXcQ"
        assert body["video"]["channelTitle"] == "Fixture Channel"
        # statistics arrived as strings, normalized to ints
        assert body["video"]["statistics"]["viewCount"] == 123456
        assert body["source"]["provider"] == "youtube"
        assert body["source"]["cached"] is False
        assert body["comments"]["status"] == "none"

    def test_pagination_and_reply_normalization(self, app_factory):
        pages = {
            None: (
                [thread_payload("c1", " first comment "), thread_payload("c2", "second")],
                "PAGE2",
            ),
            "PAGE2": (
                [
                    thread_payload(
                        "c3",
                        "third with reply",
                        replies=[
                            {
                                "id": "r1",
                                "parentId": "c3",
                                "snippet": {
                                    "authorDisplayName": "Replier",
                                    "textOriginal": "  reply   text \n here ",
                                    "publishedAt": "2024-03-03T09:00:00Z",
                                    "likeCount": 1,
                                },
                            }
                        ],
                    )
                ],
                None,
            ),
        }
        fake = FakeYouTubeClient(
            video=YtVideoListResponse.model_validate(video_payload()),
            pages=pages,
        )
        client = app_factory(fake)
        response = client.get("/api/videos/dQw4w9WgXcQ")
        assert response.status_code == 200
        body = response.json()
        assert body["comments"]["count"] == 4  # 3 top-level + 1 reply
        assert body["comments"]["hasMore"] is False
        assert len(fake.comment_calls) == 2  # both pages fetched
        assert fake.comment_calls[1][1] == "PAGE2"  # page token forwarded
        comments = body["comments"]["items"]
        reply = next(c for c in comments if c["commentId"] == "r1")
        assert reply["isReply"] is True
        assert reply["parentId"] == "c3"
        # whitespace collapsed (ingestion-safe normalization only)
        assert reply["textNormalized"] == "reply text here"

    def test_cap_respected_and_has_more_flagged(self, app_factory):
        pages = {
            None: ([thread_payload("c1", "a"), thread_payload("c2", "b")], "P2"),
            "P2": ([thread_payload("c3", "c"), thread_payload("c4", "d")], "P3"),
        }
        fake = FakeYouTubeClient(
            video=YtVideoListResponse.model_validate(video_payload()),
            pages=pages,
        )
        client = app_factory(fake, comment_acquisition_max_comments=3)
        response = client.get("/api/videos/dQw4w9WgXcQ")
        body = response.json()
        # Page 1 returns 2, page 2 is requested with max_results=min(page,
        # remaining)=1 and the response is clamped to the dataset limit.
        assert body["comments"]["count"] == 3  # cap enforced
        assert body["comments"]["hasMore"] is True  # token remained
        assert fake.comment_calls[1][0] == 1  # page size honored remaining=1


class TestCommentsUnavailable:
    def test_comments_disabled_is_explicit_not_error(self, app_factory):
        fake = FakeYouTubeClient(
            video=YtVideoListResponse.model_validate(video_payload()),
            comment_error=CommentsDisabled("disabled"),
        )
        client = app_factory(fake)
        response = client.get("/api/videos/dQw4w9WgXcQ")
        assert response.status_code == 200  # metadata still served
        body = response.json()
        assert body["comments"]["status"] == "disabled"
        assert body["comments"]["count"] == 0
        assert body["video"]["title"] == "Fixture Video Title"


class TestFailures:
    def test_quota_exceeded_maps_to_429_with_clean_message(self, app_factory):
        fake = FakeYouTubeClient(
            video=YtVideoListResponse.model_validate(video_payload()),
            comment_error=QuotaExceeded("quota"),
        )
        client = app_factory(fake)
        response = client.get("/api/videos/dQw4w9WgXcQ")
        assert response.status_code == 429
        body = response.json()
        assert body["error"]["code"] == "quota_exceeded"
        assert "quota" in body["error"]["message"].lower()
        # no internal leakage
        raw = json.dumps(body)
        assert "Traceback" not in raw
        assert "test-key" not in raw

    def test_timeout_maps_to_504(self, app_factory):
        fake = FakeYouTubeClient(
            video=YtVideoListResponse.model_validate(video_payload()),
            comment_error=UpstreamTimeout("slow"),
        )
        client = app_factory(fake)
        response = client.get("/api/videos/dQw4w9WgXcQ")
        assert response.status_code == 504
        assert response.json()["error"]["code"] == "upstream_timeout"


class TestMalformedUpstream:
    def test_video_list_junk_raises_categorized_error(self):
        for junk, status in [("not-a-dict", 200), ({"items": "junk"}, 200), (None, 200)]:
            try:
                parse_video_payload(junk, status)
            except UpstreamUnstable:
                pass
            else:  # pragma: no cover - failure path
                raise AssertionError(f"expected UpstreamUnstable for {junk!r}")

    def test_error_reason_mapping_verified(self):
        # 403 commentsDisabled (official error reference) -> CommentsDisabled
        payload = {
            "error": {
                "code": 403,
                "message": "The video identified by ...",
                "errors": [{"reason": "commentsDisabled", "domain": "youtube.comment"}],
            }
        }
        try:
            parse_comment_threads_payload(payload, 403)
        except CommentsDisabled:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected CommentsDisabled")

    def test_threads_missing_snippet_tolerated(self):
        page = parse_comment_threads_payload({"items": [{"id": "x"}]}, 200)
        assert len(page.items) == 1
        assert page.items[0].snippet is None


class TestCaching:
    def test_second_request_served_from_cache(self, app_factory):
        fake = FakeYouTubeClient(
            video=YtVideoListResponse.model_validate(video_payload()),
            pages={None: ([thread_payload("c1", "hello")], None)},
        )
        client = app_factory(fake)
        first = client.get("/api/videos/dQw4w9WgXcQ").json()
        second = client.get("/api/videos/dQw4w9WgXcQ").json()
        assert fake.video_calls == 1
        assert len(fake.comment_calls) == 1  # quota saved
        assert first["source"]["cached"] is False
        assert second["source"]["cached"] is True


class TestSecurity:
    def test_api_key_never_appears_in_any_response(self, app_factory):
        from tests.conftest import TEST_KEY

        fake = FakeYouTubeClient(
            video=YtVideoListResponse.model_validate(video_payload()),
            pages={None: ([thread_payload("c1", "hi")], None)},
        )
        client = app_factory(fake)
        bodies = [
            client.get("/health").text,
            client.get("/api/videos/dQw4w9WgXcQ").text,
            client.get("/api/videos/bad").text,
        ]
        for body in bodies:
            assert TEST_KEY not in body

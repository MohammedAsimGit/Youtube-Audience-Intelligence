"""Sprint 4.3 §17 tests: bounded YouTube retries with exponential backoff.

All requests go through httpx.MockTransport - no network, no quota, no
credentials. The assertions pin the exact policy:
- TRANSIENT failures (5xx, 429, network errors, timeouts) are retried up
  to `max_attempts` total, then surfaced with the categorized error;
- PERMANENT outcomes (quota, commentsDisabled, 404, malformed payload)
  are NEVER retried - retrying would only burn quota/time (§17).
"""
import httpx
import pytest

from app.clients.youtube import YouTubeClient
from app.core.errors import (
    CommentsDisabled,
    QuotaExceeded,
    UpstreamTimeout,
    UpstreamUnavailable,
    UpstreamUnstable,
)

VALID_VIDEO_PAYLOAD = {
    "items": [
        {
            "id": "dQw4w9WgXcQ",
            "snippet": {"title": "T", "description": "D"},
            "statistics": {},
        }
    ]
}

QUOTA_PAYLOAD = {
    "error": {
        "code": 403,
        "errors": [{"reason": "quotaExceeded", "message": "quota"}],
    }
}


def make_client(handler, attempts=3, backoff=0.0):
    return YouTubeClient(
        api_key="test-key",
        timeout_seconds=1.0,
        max_attempts=attempts,
        backoff_seconds=backoff,
        transport=httpx.MockTransport(handler),
    )


class TestTransientRetries:
    def test_retries_5xx_then_succeeds(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if len(calls) < 3:
                return httpx.Response(503, json={"error": {"errors": []}})
            return httpx.Response(200, json=VALID_VIDEO_PAYLOAD)

        client = make_client(handler, attempts=3)
        page = client.get_video("dQw4w9WgXcQ")
        assert page.items[0].id == "dQw4w9WgXcQ"
        assert len(calls) == 3  # 2 transient failures + 1 success

    def test_gives_up_after_max_attempts_on_5xx(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(500, json={"error": {"errors": []}})

        client = make_client(handler, attempts=3)
        with pytest.raises(UpstreamUnavailable):
            client.get_video("dQw4w9WgXcQ")
        assert len(calls) == 3  # bounded - never loops forever (§17)

    def test_retries_rate_limit_429(self):
        calls = []

        def handler(request):
            calls.append(1)
            if len(calls) < 2:
                return httpx.Response(429, json={})
            return httpx.Response(200, json=VALID_VIDEO_PAYLOAD)

        client = make_client(handler, attempts=3)
        client.get_video("dQw4w9WgXcQ")
        assert len(calls) == 2

    def test_retries_network_errors(self):
        calls = []

        def handler(request):
            calls.append(1)
            if len(calls) < 3:
                raise httpx.ConnectError("connection refused")
            return httpx.Response(200, json=VALID_VIDEO_PAYLOAD)

        client = make_client(handler, attempts=3)
        client.get_video("dQw4w9WgXcQ")
        assert len(calls) == 3

    def test_timeout_maps_to_upstream_timeout_after_exhaustion(self):
        calls = []

        def handler(request):
            calls.append(1)
            raise httpx.ReadTimeout("read timed out")

        client = make_client(handler, attempts=2)
        with pytest.raises(UpstreamTimeout):
            client.get_comment_threads("dQw4w9WgXcQ", max_results=10)
        assert len(calls) == 2

    def test_single_attempt_mode_makes_exactly_one_request(self):
        calls = []

        def handler(request):
            calls.append(1)
            raise httpx.ConnectError("refused")

        client = make_client(handler, attempts=1)
        with pytest.raises(UpstreamUnavailable):
            client.get_video("dQw4w9WgXcQ")
        assert len(calls) == 1


class TestPermanentErrorsNeverRetried:
    def test_quota_exhaustion_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(403, json=QUOTA_PAYLOAD)

        client = make_client(handler, attempts=3)
        with pytest.raises(QuotaExceeded):
            client.get_video("dQw4w9WgXcQ")
        assert len(calls) == 1  # retrying quota only burns units (§17)

    def test_comments_disabled_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(
                403,
                json={
                    "error": {
                        "errors": [{"reason": "commentsDisabled"}]
                    }
                },
            )

        client = make_client(handler, attempts=3)
        with pytest.raises(CommentsDisabled):
            client.get_comment_threads("dQw4w9WgXcQ", max_results=10)
        assert len(calls) == 1

    def test_non_json_body_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(200, text="<html>not json</html>")

        client = make_client(handler, attempts=3)
        with pytest.raises(UpstreamUnstable):
            client.get_video("dQw4w9WgXcQ")
        assert len(calls) == 1

    def test_missing_key_never_touches_the_network(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(200, json=VALID_VIDEO_PAYLOAD)

        client = YouTubeClient(
            api_key="",
            max_attempts=3,
            transport=httpx.MockTransport(handler),
        )
        from app.core.errors import MissingApiKey

        with pytest.raises(MissingApiKey):
            client.get_video("dQw4w9WgXcQ")
        assert calls == []

"""YouTube Data API v3 client (the ONLY module that touches Google's API).

Responsibilities:
- hold the server-side API key (from env config - never logged, never
  serialized, never exposed to any client response),
- perform videos.list / commentThreads.list requests with a timeout,
- validate external payloads through the schemas layer,
- map documented YouTube error reasons to the internal error taxonomy.

No scraping, no unofficial endpoints - official API only.
"""
import time
from typing import Any, Dict, Optional

import httpx

from app.core.errors import (
    CommentsDisabled,
    MissingApiKey,
    QuotaExceeded,
    UpstreamTimeout,
    UpstreamUnavailable,
    UpstreamUnstable,
    VideoNotFound,
)
from app.core.logging import get_logger
from app.schemas.youtube import (
    YtCommentThreadsResponse,
    YtVideoListResponse,
)

logger = get_logger("youtube_client")

BASE_URL = "https://www.googleapis.com/youtube/v3"

# Reasons verified against developers.google.com/youtube/v3/docs/errors
_COMMENTS_DISABLED_REASONS = {"commentsDisabled"}
_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded"}
_KEY_REASONS = {"keyInvalid"}


def _reasons(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    error = payload.get("error")
    if not isinstance(error, dict):
        return []
    entries = error.get("errors")
    if not isinstance(entries, list):
        return []
    reasons: list[str] = []
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("reason"), str):
            reasons.append(entry["reason"])
    return reasons


def _raise_mapped(payload: Any, status_code: int) -> None:
    """Map a YouTube error payload to the internal taxonomy (never leaks raw)."""
    reasons = set(_reasons(payload))
    logger.warning(
        "youtube error response", extra={"status": status_code, "reasons": sorted(reasons)}
    )
    if reasons & _COMMENTS_DISABLED_REASONS:
        raise CommentsDisabled("comments are disabled for this video")
    if reasons & _QUOTA_REASONS:
        raise QuotaExceeded("youtube api quota exceeded")
    if reasons & _KEY_REASONS:
        raise MissingApiKey("youtube api key rejected by google")
    if status_code == 404:
        raise VideoNotFound("video not found")
    raise UpstreamUnavailable("youtube returned an error", status=status_code)


def parse_video_payload(payload: Any, status_code: int) -> YtVideoListResponse:
    """Validate a videos.list payload (pure - unit tested with junk input)."""
    if status_code >= 400 or (isinstance(payload, dict) and "error" in payload):
        _raise_mapped(payload, status_code)
    try:
        return YtVideoListResponse.model_validate(payload)
    except Exception as exc:  # pydantic ValidationError and shape errors
        raise UpstreamUnstable("malformed videos.list payload") from exc


def parse_comment_threads_payload(payload: Any, status_code: int) -> YtCommentThreadsResponse:
    """Validate a commentThreads.list payload (pure - unit tested with junk)."""
    if status_code >= 400 or (isinstance(payload, dict) and "error" in payload):
        _raise_mapped(payload, status_code)
    try:
        return YtCommentThreadsResponse.model_validate(payload)
    except Exception as exc:
        raise UpstreamUnstable("malformed commentThreads.list payload") from exc


class YouTubeClient:
    """Thin synchronous httpx wrapper around the two MVP endpoints.

    Retry policy (Sprint 4.3 §17): bounded exponential backoff for
    TRANSIENT failures only - network errors, timeouts, HTTP 429 and 5xx.
    Permanent outcomes (quota exhausted, commentsDisabled, 404, malformed
    payload) surface immediately without spending more quota or time. Every
    attempt logs the endpoint and status - never the API key.
    """

    def __init__(
        self,
        api_key: str,
        timeout_seconds: float = 10.0,
        max_attempts: int = 3,
        backoff_seconds: float = 0.5,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._api_key = api_key
        self._max_attempts = max(1, max_attempts)
        self._backoff_seconds = max(0.0, backoff_seconds)
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout_seconds,
            headers={"Accept": "application/json"},
            transport=transport,  # injected in tests (MockTransport)
        )

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _is_transient(status_code: int) -> bool:
        """Retryable HTTP statuses: rate limiting and server-side failures."""
        return status_code == 429 or status_code >= 500

    def _get(self, endpoint: str, params: Dict[str, Any]) -> Any:
        if not self._api_key:
            raise MissingApiKey("YOUTUBE_API_KEY is not configured")
        # `key` is part of the request params only - never logged.
        delay = self._backoff_seconds
        last_error: Optional[Exception] = None
        for attempt in range(1, self._max_attempts + 1):
            retry = False
            response: Optional[httpx.Response] = None
            try:
                response = self._client.get(
                    endpoint, params={**params, "key": self._api_key}
                )
            except httpx.TimeoutException:
                last_error = UpstreamTimeout("youtube request timed out")
                retry = True
            except httpx.HTTPError:
                last_error = UpstreamUnavailable("youtube unreachable")
                retry = True
            else:
                status = response.status_code
                if self._is_transient(status):
                    # Transient upstream status: retry with backoff; the
                    # FINAL attempt falls through to the mapped error below.
                    last_error = UpstreamUnavailable(
                        "youtube returned an error", status=status
                    )
                    retry = True
                else:
                    try:
                        return response.json(), status
                    except ValueError as exc:
                        if status >= 400:
                            # Documented YouTube error payloads map to the
                            # internal taxonomy (quota/disabled/404/...).
                            _raise_mapped({}, status)
                        raise UpstreamUnstable(
                            "youtube returned non-JSON body"
                        ) from exc
            if retry and attempt < self._max_attempts:
                logger.warning(
                    "youtube request retrying",
                    extra={
                        "endpoint": endpoint,
                        "attempt": attempt,
                        "max_attempts": self._max_attempts,
                        "delay_ms": int(delay * 1000),
                        "status": response.status_code if response is not None else None,
                    },
                )
                if delay > 0:
                    time.sleep(delay)
                delay *= 2
                continue
            break  # permanent error, or retries exhausted

        if response is not None and self._is_transient(response.status_code):
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            _raise_mapped(payload, response.status_code)
        assert last_error is not None  # set on every retry path above
        raise last_error

    def get_video(self, video_id: str) -> YtVideoListResponse:
        logger.info("youtube videos.list started", extra={"video_id": video_id})
        payload, status = self._get(
            "/videos",
            {"part": "snippet,statistics", "id": video_id},
        )
        return parse_video_payload(payload, status)

    def get_comment_threads(
        self,
        video_id: str,
        max_results: int,
        page_token: Optional[str] = None,
    ) -> YtCommentThreadsResponse:
        params: Dict[str, Any] = {
            "part": "snippet,replies",
            "videoId": video_id,
            "maxResults": max_results,
            "order": "time",
        }
        if page_token:
            params["pageToken"] = page_token
        payload, status = self._get("/commentThreads", params)
        return parse_comment_threads_payload(payload, status)

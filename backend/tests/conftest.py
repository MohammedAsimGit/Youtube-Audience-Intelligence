"""Shared test fixtures.

MOCK POLICY: the FakeYouTubeClient stands in for Google's API in unit tests
only. Fixtures are clearly test data and are NEVER presented as real YouTube
data (Sprint 2 mock-data policy).
"""
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.models.internal import Comment, VideoMetadata, VideoStatistics
from app.schemas.youtube import YtCommentThreadsResponse, YtVideoListResponse
from app.utils.normalize import collapse_whitespace

TEST_KEY = "test-key-never-production"


def make_settings(**overrides) -> Settings:
    base = dict(
        youtube_api_key=TEST_KEY,
        # Page size (max results per commentThreads.list call), NOT the
        # dataset limit - see COMMENT_ACQUISITION_MAX_COMMENTS for the cap.
        max_comments_per_request=50,
        comment_acquisition_max_comments=5000,
        youtube_request_timeout_seconds=5.0,
        cache_ttl_seconds=60,
        cache_max_entries=8,
        # Each test app gets its own isolated in-memory dataset store.
        database_url="sqlite:///:memory:",
        comment_batch_size=100,
        dataset_cache_ttl_seconds=3600,
        cors_origins=["chrome-extension://", "http://localhost:5173"],
        log_level="WARNING",
    )
    base.update(overrides)
    return Settings(**base)


class FakeYouTubeClient:
    """Deterministic stand-in implementing the client's public surface."""

    def __init__(
        self,
        video: Optional[YtVideoListResponse] = None,
        video_error: Optional[Exception] = None,
        pages: Optional[Dict[Optional[str], Tuple[list, Optional[str]]]] = None,
        comment_error: Optional[Exception] = None,
        comment_error_after: Optional[int] = None,
        page_hook: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.video = video or YtVideoListResponse(items=[])
        self.video_error = video_error
        self.pages: Dict[Optional[str], Tuple[list, Optional[str]]] = pages or {
            None: ([], None)
        }
        self.comment_error = comment_error
        # None -> fail on the first call (legacy behavior); N -> the first N
        # commentThreads calls succeed, call N+1 raises `comment_error`.
        self.comment_error_after = comment_error_after
        # Sprint 4.2 race tests: runs INSIDE the acquisition (after N-1
        # successful pages) so a switch can be injected mid-flight.
        self.page_hook = page_hook
        self.video_calls = 0
        self.comment_calls: List[Tuple[int, Optional[str]]] = []

    def get_video(self, video_id: str) -> YtVideoListResponse:
        self.video_calls += 1
        if self.video_error is not None:
            raise self.video_error
        return self.video

    def get_comment_threads(
        self, video_id: str, max_results: int, page_token: Optional[str] = None
    ) -> YtCommentThreadsResponse:
        self.comment_calls.append((max_results, page_token))
        if self.comment_error is not None and (
            self.comment_error_after is None
            or len(self.comment_calls) > self.comment_error_after
        ):
            raise self.comment_error
        if self.page_hook is not None:
            self.page_hook(len(self.comment_calls))
        items, next_token = self.pages.get(page_token, ([], None))
        return YtCommentThreadsResponse(items=items, next_page_token=next_token)

    def close(self) -> None:  # parity with httpx client for lifespan
        pass


def video_payload(video_id: str = "dQw4w9WgXcQ") -> dict:
    """TEST FIXTURE (not real YouTube data): videos.list shaped payload."""
    return {
        "items": [
            {
                "id": video_id,
                "snippet": {
                    "title": "Fixture Video Title",
                    "description": "Test description",
                    "channelId": "UCxxxxxxxxxxxxxxxxxxxxxx",
                    "channelTitle": "Fixture Channel",
                    "publishedAt": "2024-03-01T10:00:00Z",
                    "categoryId": "28",
                },
                "statistics": {
                    "viewCount": "123456",
                    "likeCount": "7890",
                    "commentCount": "321",
                },
            }
        ]
    }


def thread_payload(comment_id: str, text: str, replies: Optional[list] = None) -> dict:
    """TEST FIXTURE: commentThreads.list item shaped payload."""
    item = {
        "id": comment_id,
        "snippet": {
            "topLevelComment": {
                "id": comment_id,
                "snippet": {
                    "authorDisplayName": "TestUser",
                    "textOriginal": text,
                    "publishedAt": "2024-03-02T08:00:00Z",
                    "updatedAt": "2024-03-02T08:00:00Z",
                    "likeCount": 3,
                },
            }
        },
    }
    if replies:
        item["replies"] = {"comments": replies}
    return item


@pytest.fixture
def app_factory():
    """Build an app with an injected fake client + tuned settings."""

    def _factory(client: FakeYouTubeClient, **settings_overrides) -> TestClient:
        app = create_app(
            settings=make_settings(**settings_overrides),
            youtube_client=client,
        )
        return TestClient(app)

    return _factory


@pytest.fixture
def client(app_factory):
    return app_factory(FakeYouTubeClient(video=YtVideoListResponse.model_validate(video_payload())))


# ---------------------------------------------------------------------------
# Sprint 3 unit fixtures (internal contract shapes - deterministic test data)
# ---------------------------------------------------------------------------

def make_comment(
    comment_id: str = "c1",
    video_id: str = "dQw4w9WgXcQ",
    text: str = "hello world",
    **overrides,
) -> Comment:
    """TEST FIXTURE: internal Comment with sensible defaults."""
    defaults = dict(
        comment_id=comment_id,
        video_id=video_id,
        author="TestUser",
        text=text,
        text_normalized=collapse_whitespace(text),
        published_at=datetime(2024, 3, 2, 8, 0, tzinfo=timezone.utc),
        updated_at=None,
        like_count=3,
        is_reply=False,
        parent_id=None,
    )
    defaults.update(overrides)
    return Comment(**defaults)


def make_metadata(video_id: str = "dQw4w9WgXcQ") -> VideoMetadata:
    """TEST FIXTURE: normalized video metadata (mirrors video_payload)."""
    return VideoMetadata(
        video_id=video_id,
        title="Fixture Video Title",
        description="Test description",
        channel_id="UCxxxxxxxxxxxxxxxxxxxxxx",
        channel_title="Fixture Channel",
        published_at=datetime(2024, 3, 1, 10, 0, tzinfo=timezone.utc),
        category_id="28",
        duration=None,
        statistics=VideoStatistics(view_count=123456, like_count=7890, comment_count=321),
    )


def comment_row(
    comment_id: str,
    video_id: str = "dQw4w9WgXcQ",
    text: str = "hello world",
    **overrides,
) -> dict:
    """TEST FIXTURE: full repository row dict (all upsert columns present)."""
    stamp = "2024-03-04T00:00:00+00:00"
    row = dict(
        comment_id=comment_id,
        video_id=video_id,
        parent_comment_id=None,
        author="TestUser",
        raw_text=text,
        normalized_text=collapse_whitespace(text),
        published_at="2024-03-02T08:00:00+00:00",
        updated_at=None,
        like_count=3,
        is_reply=0,
        source="youtube",
        language="en",
        processing_status="READY_FOR_ANALYSIS",
        first_seen_at=stamp,
        last_seen_at=stamp,
        created_at=stamp,
    )
    row.update(overrides)
    return row


def seed_video(repository, video_id: str = "dQw4w9WgXcQ") -> None:
    """Insert the parent video row (FK requirement for comments)."""
    repository.upsert_video(
        video_id=video_id,
        metadata={
            "title": "Fixture Video Title",
            "description": "Test description",
            "channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx",
            "channel_title": "Fixture Channel",
            "published_at": "2024-03-01T10:00:00+00:00",
            "category_id": "28",
            "duration": None,
            "view_count": 123456,
            "like_count": 7890,
            "comment_count": 321,
        },
        comments_status="ok",
        has_more=False,
        acquired_at="2024-03-04T00:00:00+00:00",
    )

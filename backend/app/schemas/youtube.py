"""External YouTube Data API v3 response schemas (validation boundary).

Field names mirror the official JSON keys via camelCase aliases. Every model
is lenient (`extra="ignore"`) but still validated before entering the system:
a malformed payload can never crash a request - it maps to a categorized
UpstreamUnstable error instead.

Verified against Google's official docs/errors reference:
- commentThreads.list: 1 quota unit/call, `nextPageToken` pagination, max 100
  results/page; 403 reason `commentsDisabled`; 403 `quotaExceeded`.
- videos.list: 1 quota unit/call. Counters arrive as STRINGS - typed loosely
  here and coerced during normalization (never assumed perfect).
"""
from typing import List, Optional

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class ExternalModel(BaseModel):
    """camelCase validation aliases (parses Google's JSON keys like
    `topLevelComment`/`nextPageToken` onto snake_case fields)."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
    )


class YtErrorReason(ExternalModel):
    reason: Optional[str] = None
    message: Optional[str] = None
    domain: Optional[str] = None


class YtErrorEnvelope(ExternalModel):
    code: Optional[int] = None
    message: Optional[str] = None
    errors: List[YtErrorReason] = []


class YtErrorResponse(ExternalModel):
    error: Optional[YtErrorEnvelope] = None


class YtVideoSnippet(ExternalModel):
    title: Optional[str] = None
    description: Optional[str] = None
    channel_id: Optional[str] = None
    channel_title: Optional[str] = None
    published_at: Optional[str] = None
    category_id: Optional[str] = None


class YtVideoStatistics(ExternalModel):
    # YouTube returns numeric counters as JSON strings.
    view_count: Optional[str] = None
    like_count: Optional[str] = None
    comment_count: Optional[str] = None


class YtVideoItem(ExternalModel):
    id: str
    snippet: Optional[YtVideoSnippet] = None
    statistics: Optional[YtVideoStatistics] = None


class YtVideoListResponse(ExternalModel):
    items: List[YtVideoItem] = []
    error: Optional[YtErrorEnvelope] = None


class YtTopLevelComment(ExternalModel):
    id: Optional[str] = None
    snippet: Optional["YtCommentSnippet"] = None


class YtReplyComment(ExternalModel):
    id: Optional[str] = None
    parent_id: Optional[str] = None
    snippet: Optional["YtCommentSnippet"] = None


class YtCommentSnippet(ExternalModel):
    author_display_name: Optional[str] = None
    text_original: Optional[str] = None
    published_at: Optional[str] = None
    updated_at: Optional[str] = None
    like_count: Optional[int] = None


class YtThreadSnippet(ExternalModel):
    top_level_comment: Optional[YtTopLevelComment] = None
    total_reply_count: Optional[int] = None


class YtThreadReplies(ExternalModel):
    comments: List[YtReplyComment] = []


class YtCommentThreadItem(ExternalModel):
    id: Optional[str] = None
    snippet: Optional[YtThreadSnippet] = None
    replies: Optional[YtThreadReplies] = None


class YtCommentThreadsResponse(ExternalModel):
    items: List[YtCommentThreadItem] = []
    next_page_token: Optional[str] = None
    error: Optional[YtErrorEnvelope] = None


YtTopLevelComment.model_rebuild()
YtReplyComment.model_rebuild()

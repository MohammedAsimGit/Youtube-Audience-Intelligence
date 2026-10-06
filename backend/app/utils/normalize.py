"""Ingestion-safe normalization helpers (Sprint 2 scope ONLY).

Light foundation: whitespace/unicode handling, safe primitive coercion, and
comment normalization. The full NLP pipeline (tokenization, stemming,
sentiment, topics...) belongs to later sprints and must NOT be added here.
"""
import re
import unicodedata
from datetime import datetime, timezone
from typing import List, Optional

from app.models.internal import Comment
from app.schemas.youtube import YtCommentSnippet, YtCommentThreadsResponse

_WHITESPACE = re.compile(r"\s+", re.UNICODE)


def collapse_whitespace(text: str) -> str:
    """Unicode-safe trim + whitespace collapse (ingestion hygiene only)."""
    normalized = unicodedata.normalize("NFC", text)
    return _WHITESPACE.sub(" ", normalized).strip()


def parse_int(value: object) -> Optional[int]:
    """Safe counter coercion. YouTube sends strings; garbage becomes None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.lstrip("-").isdigit():
            return int(candidate)
    return None


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    """ISO-8601 (with/without trailing Z) -> aware UTC datetime, else None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _snippet_text(snippet: Optional[YtCommentSnippet]) -> Optional[str]:
    if snippet is None or snippet.text_original is None:
        return None
    return snippet.text_original


def normalize_thread_comments(
    page: YtCommentThreadsResponse, video_id: str
) -> List[Comment]:
    """Thread-level page -> flat list of individual normalized comments.

    - top-level thread comment -> Comment(is_reply=False)
    - each reply (replies.comments) -> Comment(is_reply=True, parent_id=thread)
    - records with missing/empty text are SKIPPED (never crash, never fabricate)
    """
    results: List[Comment] = []
    for thread in page.items:
        top = thread.snippet.top_level_comment if thread.snippet else None
        if top is None or not top.id:
            continue
        raw_text = _snippet_text(top.snippet)
        if raw_text is None:
            continue
        collapsed = collapse_whitespace(raw_text)
        if not collapsed:
            continue
        snippet = top.snippet
        results.append(
            Comment(
                comment_id=top.id,
                video_id=video_id,
                author=(snippet.author_display_name if snippet else None),
                text=raw_text,
                text_normalized=collapsed,
                published_at=parse_dt(snippet.published_at if snippet else None),
                updated_at=parse_dt(snippet.updated_at if snippet else None),
                like_count=parse_int(snippet.like_count if snippet else None) or 0,
                is_reply=False,
                parent_id=None,
            )
        )

        if thread.replies:
            for reply in thread.replies.comments:
                if not reply.id:
                    continue
                reply_raw = _snippet_text(reply.snippet)
                if reply_raw is None:
                    continue
                reply_collapsed = collapse_whitespace(reply_raw)
                if not reply_collapsed:
                    continue
                rs = reply.snippet
                results.append(
                    Comment(
                        comment_id=reply.id,
                        video_id=video_id,
                        author=(rs.author_display_name if rs else None),
                        text=reply_raw,
                        text_normalized=reply_collapsed,
                        published_at=parse_dt(rs.published_at if rs else None),
                        updated_at=parse_dt(rs.updated_at if rs else None),
                        like_count=parse_int(rs.like_count if rs else None) or 0,
                        is_reply=True,
                        parent_id=reply.parent_id or top.id,
                    )
                )
    return results

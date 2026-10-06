"""Comment validation - every record must pass before persistence (Sprint 3).

Rules (docs/architecture/data-pipeline.md):
- Structured issues (`field` + `reason`), never exceptions for bad records.
- ONE malformed comment never crashes the batch: `validate_comments` returns
  (valid, rejected) and the caller logs rejected issues + counts.
- Rejected records are counted in the data-quality report and never stored.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Sequence, Tuple

from app.models.internal import Comment

# "Maximum reasonable field lengths" - YouTube's real limits (comments cap at
# 10,000 characters) plus headroom guards for anything unexpected.
MAX_TEXT_LENGTH = 10_000
MAX_AUTHOR_LENGTH = 256
MAX_ID_LENGTH = 128
# Timestamps may legitimately be a few minutes ahead (clock skew).
_FUTURE_SKEW = timedelta(minutes=5)
_LIKE_COUNT_MAX = 10 ** 9


@dataclass(frozen=True)
class ValidationIssue:
    """Structured, loggable, secret-free description of one rejection."""

    field: str
    reason: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def validate_comment(comment: Comment, expected_video_id: str) -> List[ValidationIssue]:
    """All issues for one comment; empty list == valid."""
    issues: List[ValidationIssue] = []

    # --- identity -----------------------------------------------------------
    if not comment.comment_id or not comment.comment_id.strip():
        issues.append(ValidationIssue("commentId", "missing_comment_id"))
    elif len(comment.comment_id) > MAX_ID_LENGTH or comment.comment_id != comment.comment_id.strip():
        issues.append(ValidationIssue("commentId", "invalid_comment_id"))

    if not comment.video_id:
        issues.append(ValidationIssue("videoId", "missing_video_id"))
    elif comment.video_id != expected_video_id:
        # Video-level isolation: a record for another video can never enter
        # this video's dataset.
        issues.append(ValidationIssue("videoId", "video_mismatch"))

    # --- text ---------------------------------------------------------------
    if not comment.text or not comment.text.strip():
        issues.append(ValidationIssue("text", "missing_text"))
    elif len(comment.text) > MAX_TEXT_LENGTH:
        issues.append(ValidationIssue("text", "text_too_long"))

    if not comment.text_normalized or not comment.text_normalized.strip():
        issues.append(ValidationIssue("textNormalized", "missing_normalized_text"))

    # --- timestamps ---------------------------------------------------------
    if comment.published_at is None:
        issues.append(ValidationIssue("publishedAt", "missing_published_at"))
    elif comment.published_at.tzinfo is None:
        issues.append(ValidationIssue("publishedAt", "naive_timestamp"))
    elif comment.published_at > _now() + _FUTURE_SKEW:
        issues.append(ValidationIssue("publishedAt", "future_timestamp"))

    if comment.updated_at is not None:
        if comment.updated_at.tzinfo is None:
            issues.append(ValidationIssue("updatedAt", "naive_timestamp"))
        elif comment.updated_at > _now() + _FUTURE_SKEW:
            issues.append(ValidationIssue("updatedAt", "future_timestamp"))

    # --- counters / flags ---------------------------------------------------
    if not isinstance(comment.like_count, int) or isinstance(comment.like_count, bool):
        issues.append(ValidationIssue("likeCount", "invalid_like_count"))
    elif comment.like_count < 0:
        issues.append(ValidationIssue("likeCount", "negative_like_count"))
    elif comment.like_count > _LIKE_COUNT_MAX:
        issues.append(ValidationIssue("likeCount", "like_count_out_of_range"))

    if not isinstance(comment.is_reply, bool):
        issues.append(ValidationIssue("isReply", "invalid_reply_flag"))

    # --- thread linkage -----------------------------------------------------
    if comment.is_reply:
        if not comment.parent_id:
            issues.append(ValidationIssue("parentId", "missing_parent_id"))
        elif comment.parent_id == comment.comment_id:
            issues.append(ValidationIssue("parentId", "self_parent"))
    elif comment.parent_id:
        issues.append(ValidationIssue("parentId", "unexpected_parent_id"))

    # --- author -------------------------------------------------------------
    if comment.author is not None and len(comment.author) > MAX_AUTHOR_LENGTH:
        issues.append(ValidationIssue("author", "author_too_long"))

    return issues


def validate_comments(
    comments: Sequence[Comment], expected_video_id: str
) -> Tuple[List[Comment], List[Tuple[Comment, List[ValidationIssue]]]]:
    """Split a fetched batch into (valid, rejected-with-issues).

    Never raises on bad records - the batch survives one malformed comment.
    """
    valid: List[Comment] = []
    rejected: List[Tuple[Comment, List[ValidationIssue]]] = []
    for comment in comments:
        issues = validate_comment(comment, expected_video_id)
        if issues:
            rejected.append((comment, issues))
        else:
            valid.append(comment)
    return valid, rejected

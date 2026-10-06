"""Unit tests: comment validation rules (structured issues, batch survival)."""
from datetime import datetime, timedelta, timezone

from app.services.validation import (
    MAX_TEXT_LENGTH,
    validate_comment,
    validate_comments,
)
from tests.conftest import make_comment

VIDEO = "dQw4w9WgXcQ"
_FUTURE = datetime.now(timezone.utc) + timedelta(days=2)


def _reasons(issues) -> set:
    return {issue.reason for issue in issues}


class TestValidComment:
    def test_default_fixture_is_valid(self):
        assert validate_comment(make_comment(), VIDEO) == []

    def test_reply_with_parent_is_valid(self):
        comment = make_comment(comment_id="r1", is_reply=True, parent_id="c1")
        assert validate_comment(comment, VIDEO) == []


class TestRejections:
    def test_missing_comment_id(self):
        issues = validate_comment(make_comment(comment_id="   "), VIDEO)
        assert "missing_comment_id" in _reasons(issues)

    def test_empty_text_rejected(self):
        issues = validate_comment(make_comment(text=""), VIDEO)
        assert "missing_text" in _reasons(issues)

    def test_over_max_length_text_rejected(self):
        issues = validate_comment(make_comment(text="x" * (MAX_TEXT_LENGTH + 1)), VIDEO)
        assert "text_too_long" in _reasons(issues)

    def test_missing_published_at_rejected(self):
        issues = validate_comment(make_comment(published_at=None), VIDEO)
        assert "missing_published_at" in _reasons(issues)

    def test_naive_timestamp_rejected(self):
        naive = datetime(2024, 3, 2, 8, 0)  # no tzinfo
        issues = validate_comment(make_comment(published_at=naive), VIDEO)
        assert "naive_timestamp" in _reasons(issues)

    def test_future_timestamp_rejected(self):
        issues = validate_comment(make_comment(published_at=_FUTURE), VIDEO)
        assert "future_timestamp" in _reasons(issues)

    def test_negative_like_count_rejected(self):
        issues = validate_comment(make_comment(like_count=-1), VIDEO)
        assert "negative_like_count" in _reasons(issues)

    def test_reply_without_parent_rejected(self):
        comment = make_comment(is_reply=True, parent_id=None)
        issues = validate_comment(comment, VIDEO)
        assert "missing_parent_id" in _reasons(issues)

    def test_self_parent_rejected(self):
        comment = make_comment(comment_id="c1", is_reply=True, parent_id="c1")
        issues = validate_comment(comment, VIDEO)
        assert "self_parent" in _reasons(issues)

    def test_top_level_with_parent_rejected(self):
        comment = make_comment(is_reply=False, parent_id="other")
        issues = validate_comment(comment, VIDEO)
        assert "unexpected_parent_id" in _reasons(issues)

    def test_video_mismatch_enforces_isolation(self):
        """A record addressed to another video can never enter this dataset."""
        issues = validate_comment(make_comment(video_id="BBBBBBBBBBB"), VIDEO)
        assert "video_mismatch" in _reasons(issues)

    def test_author_too_long_rejected(self):
        issues = validate_comment(make_comment(author="a" * 257), VIDEO)
        assert "author_too_long" in _reasons(issues)

    def test_issues_are_structured_and_text_free(self):
        comment = make_comment(text="")
        issues = validate_comment(comment, VIDEO)
        assert issues, "expected structured issues"
        for issue in issues:
            assert issue.field and issue.reason
            # raw text never leaks into the issue representation
            assert "hello" not in issue.reason


class TestBatchSurvival:
    def test_one_bad_record_never_kills_the_batch(self):
        batch = [
            make_comment("good1"),
            make_comment("bad-empty", text=""),
            make_comment("bad-video", video_id="BBBBBBBBBBB"),
            make_comment("good2"),
        ]
        valid, rejected = validate_comments(batch, VIDEO)
        assert [c.comment_id for c in valid] == ["good1", "good2"]
        assert [c.comment_id for c, _ in rejected] == ["bad-empty", "bad-video"]
        # every rejection carries at least one structured issue
        assert all(issues for _, issues in rejected)

"""Database tests: insert / duplicate / update / isolation / status / stats.

Sprint 4: `TestSentimentPersistence` covers claim/persist/aggregation through
the repository (the only persistence abstraction) - state machine intact,
content reset clears verdicts, failures reprocessable, duplicates and video
isolation hold for sentiment rows too.
"""
import sqlite3

import pytest

from app.db.connection import Database
from app.db.repository import DatasetRepository, SentimentOutcome
from app.models.processing import IllegalTransition
from tests.conftest import comment_row, seed_video

VIDEO = "dQw4w9WgXcQ"
OTHER_VIDEO = "BBBBBBBBBBB"


@pytest.fixture
def repo():
    db = Database("sqlite:///:memory:")
    repository = DatasetRepository(db)
    seed_video(repository, VIDEO)  # parent row (FK) for comment inserts
    yield repository
    db.close()


class TestInsert:
    def test_new_comment_stored(self, repo):
        stats = repo.upsert_comments([comment_row("c1")], batch_size=100)
        assert stats.inserted == 1 and stats.updated == 0 and stats.duplicates == 0
        rows = repo.get_comments(VIDEO)
        assert len(rows) == 1
        assert rows[0]["comment_id"] == "c1"

    def test_comment_requires_parent_video(self, repo):
        # Foreign key enforces video-level organization at the storage layer.
        with pytest.raises(sqlite3.IntegrityError):
            repo.upsert_comments(
                [comment_row("orphan", video_id="NOVIDEOHERE")], batch_size=100
            )

    def test_unknown_video_returns_none(self, repo):
        assert repo.get_video("nope") is None


class TestDuplicate:
    def test_same_comment_id_never_inserted_twice(self, repo):
        repo.upsert_comments([comment_row("c1")], batch_size=100)
        stats = repo.upsert_comments([comment_row("c1")], batch_size=100)
        assert stats.inserted == 0 and stats.updated == 1 and stats.duplicates == 1
        assert len(repo.get_comments(VIDEO)) == 1  # Fetch #2 did NOT re-insert

    def test_first_seen_preserved_last_seen_refreshed(self, repo):
        first = comment_row("c1", first_seen_at="2024-03-04T00:00:00+00:00",
                            last_seen_at="2024-03-04T00:00:00+00:00",
                            created_at="2024-03-04T00:00:00+00:00")
        repo.upsert_comments([first], batch_size=100)
        second = comment_row("c1", first_seen_at="2024-03-05T00:00:00+00:00",
                             last_seen_at="2024-03-05T00:00:00+00:00",
                             created_at="2024-03-05T00:00:00+00:00")
        repo.upsert_comments([second], batch_size=100)
        row = repo.get_comments(VIDEO)[0]
        assert row["first_seen_at"] == "2024-03-04T00:00:00+00:00"  # preserved
        assert row["created_at"] == "2024-03-04T00:00:00+00:00"      # preserved
        assert row["last_seen_at"] == "2024-03-05T00:00:00+00:00"    # refreshed

    def test_metadata_updated_on_refetch(self, repo):
        repo.upsert_comments([comment_row("c1", like_count=3)], batch_size=100)
        repo.upsert_comments([comment_row("c1", like_count=99)], batch_size=100)
        assert repo.get_comments(VIDEO)[0]["like_count"] == 99

    def test_in_batch_duplicate_collapses_to_last_occurrence(self, repo):
        rows = [
            comment_row("c1", like_count=3),
            comment_row("c1", like_count=99),
        ]
        stats = repo.upsert_comments(rows, batch_size=100)
        assert stats.inserted == 1 and stats.updated == 0 and stats.duplicates == 1
        stored = repo.get_comments(VIDEO)
        assert len(stored) == 1
        assert stored[0]["like_count"] == 99  # most recent metadata wins


class TestVideoIsolation:
    def test_comments_never_mix_across_videos(self, repo):
        seed_video(repo, VIDEO)
        seed_video(repo, OTHER_VIDEO)
        repo.upsert_comments(
            [comment_row("a1"), comment_row("a2")], batch_size=100
        )
        repo.upsert_comments(
            [comment_row("b1", video_id=OTHER_VIDEO)], batch_size=100
        )
        ids_a = {r["comment_id"] for r in repo.get_comments(VIDEO)}
        ids_b = {r["comment_id"] for r in repo.get_comments(OTHER_VIDEO)}
        assert ids_a == {"a1", "a2"}
        assert ids_b == {"b1"}
        assert repo.get_comment_stats(VIDEO)["total"] == 2
        assert repo.get_comment_stats(OTHER_VIDEO)["total"] == 1


class TestStatusLifecycle:
    def test_full_transition_chain_in_db(self, repo):
        """ACQUIRED -> VALIDATED -> NORMALIZED -> READY_FOR_ANALYSIS (§33)."""
        repo.upsert_comments(
            [comment_row("c1", processing_status="ACQUIRED")], batch_size=100
        )
        for status in ("VALIDATED", "NORMALIZED", "READY_FOR_ANALYSIS"):
            changed = repo.update_processing_status(VIDEO, "c1", status)
            assert changed == 1
            assert repo.get_comments(VIDEO)[0]["processing_status"] == status

    def test_illegal_transition_raises_and_db_unchanged(self, repo):
        repo.upsert_comments([comment_row("c1")], batch_size=100)  # READY
        with pytest.raises(IllegalTransition):
            repo.update_processing_status(VIDEO, "c1", "ACQUIRED")
        assert repo.get_comments(VIDEO)[0]["processing_status"] == "READY_FOR_ANALYSIS"

    def test_unknown_status_raises(self, repo):
        repo.upsert_comments([comment_row("c1")], batch_size=100)
        with pytest.raises(ValueError):
            repo.update_processing_status(VIDEO, "c1", "NOT_A_STATUS")

    def test_missing_comment_returns_zero(self, repo):
        assert repo.update_processing_status(VIDEO, "ghost", "PROCESSING") == 0

    def test_get_comments_by_status_filters(self, repo):
        repo.upsert_comments(
            [comment_row("c1"), comment_row("c2"), comment_row("c3")],
            batch_size=100,
        )
        repo.update_processing_status(VIDEO, "c3", "FAILED")
        ready = repo.get_comments_by_status(VIDEO, "READY_FOR_ANALYSIS")
        failed = repo.get_comments_by_status(VIDEO, "FAILED")
        assert {r["comment_id"] for r in ready} == {"c1", "c2"}
        assert {r["comment_id"] for r in failed} == {"c3"}


class TestStatusResetOnContentChange:
    def test_unchanged_content_keeps_processed_status(self, repo):
        repo.upsert_comments([comment_row("c1")], batch_size=100)
        repo.update_processing_status(VIDEO, "c1", "PROCESSING")
        repo.update_processing_status(VIDEO, "c1", "PROCESSED")
        # Re-fetch of the same content must not clobber Sprint 4's work.
        repo.upsert_comments([comment_row("c1")], batch_size=100)
        assert repo.get_comments(VIDEO)[0]["processing_status"] == "PROCESSED"

    def test_edited_content_resets_to_ready(self, repo):
        repo.upsert_comments([comment_row("c1", text="original")], batch_size=100)
        repo.update_processing_status(VIDEO, "c1", "PROCESSING")
        repo.update_processing_status(VIDEO, "c1", "PROCESSED")
        repo.upsert_comments([comment_row("c1", text="edited text")], batch_size=100)
        assert repo.get_comments(VIDEO)[0]["processing_status"] == "READY_FOR_ANALYSIS"


class TestStats:
    def test_aggregates_are_sql_side_and_correct(self, repo):
        repo.upsert_comments(
            [
                comment_row("c1", published_at="2024-03-01T08:00:00+00:00"),
                comment_row("c2", published_at="2024-03-03T08:00:00+00:00"),
                comment_row("r1", is_reply=1, parent_comment_id="c1",
                            published_at="2024-03-02T08:00:00+00:00"),
            ],
            batch_size=100,
        )
        stats = repo.get_comment_stats(VIDEO)
        assert stats["total"] == 3
        assert stats["unique"] == 3
        assert stats["replies"] == 1
        assert stats["oldest"] == "2024-03-01T08:00:00+00:00"
        assert stats["newest"] == "2024-03-03T08:00:00+00:00"

    def test_status_counts_breakdown(self, repo):
        repo.upsert_comments(
            [comment_row("c1"), comment_row("c2")], batch_size=100
        )
        repo.update_processing_status(VIDEO, "c2", "FAILED")
        counts = repo.get_status_counts(VIDEO)
        assert counts == {"READY_FOR_ANALYSIS": 1, "FAILED": 1}


class TestBatchReader:
    def test_iter_comments_reads_in_bounded_batches(self, repo):
        rows = [comment_row(f"c{i:02d}") for i in range(25)]
        repo.upsert_comments(rows, batch_size=100)
        seen = [r["comment_id"] for r in repo.iter_comments(VIDEO, batch_size=10)]
        assert len(seen) == 25
        assert len(set(seen)) == 25  # keyset pagination never repeats rows

    def test_iter_comments_status_filter(self, repo):
        rows = [comment_row(f"c{i:02d}") for i in range(8)]
        repo.upsert_comments(rows, batch_size=100)
        for i in range(3):
            repo.update_processing_status(VIDEO, f"c{i:02d}", "FAILED")
        failed = [r["comment_id"] for r in repo.iter_comments(VIDEO, 4, status="FAILED")]
        assert len(failed) == 3

    def test_batch_size_matches_config_chunking(self, repo):
        # 5 rows through a batch_size of 2 -> 3 executemany chunks, all stored.
        rows = [comment_row(f"c{i}") for i in range(5)]
        stats = repo.upsert_comments(rows, batch_size=2)
        assert stats.inserted == 5
        assert len(repo.get_comments(VIDEO)) == 5


class TestVideoUpsert:
    def test_first_acquired_preserved_last_refreshed(self, repo):
        repo.upsert_video(VIDEO, {"title": "One"}, "ok", False, "2024-03-04T00:00:00+00:00")
        repo.upsert_video(VIDEO, {"title": "Two"}, "ok", True, "2024-03-05T00:00:00+00:00")
        row = repo.get_video(VIDEO)
        assert row["title"] == "Two"
        assert row["first_acquired_at"] == "2024-03-04T00:00:00+00:00"
        assert row["last_acquired_at"] == "2024-03-05T00:00:00+00:00"
        assert row["has_more"] == 1


class TestSentimentPersistence:
    """Sprint 4 columns: claim -> persist -> aggregate, all through here."""

    @staticmethod
    def _outcome(comment_id, label="POSITIVE", score=0.8, target="PROCESSED", confidence=0.75):
        return SentimentOutcome(
            comment_id=comment_id,
            label=label,
            score=score,
            confidence=confidence,
            model="vader-1.0",
            processed_at="2024-03-06T00:00:00+00:00",
            target_status=target,
        )

    @staticmethod
    def _analyze(repo, comment_id, label="POSITIVE", video_id=VIDEO, score=0.8):
        """Full claim + persist for one row (the service's happy path)."""
        assert repo.claim_for_processing(video_id, [comment_id]) == {comment_id}
        return repo.save_sentiment_results(
            video_id,
            [TestSentimentPersistence._outcome(comment_id, label=label, score=score)],
            100,
        )

    def test_claim_moves_only_ready_rows_to_processing(self, repo):
        repo.upsert_comments([comment_row("c1"), comment_row("c2")], batch_size=100)
        repo.update_processing_status(VIDEO, "c2", "FAILED")
        claimed = repo.claim_for_processing(VIDEO, ["c1", "c2", "ghost"])
        assert claimed == {"c1"}  # FAILED + unknown ids are never claimed
        statuses = {r["comment_id"]: r["processing_status"] for r in repo.get_comments(VIDEO)}
        assert statuses == {"c1": "PROCESSING", "c2": "FAILED"}
        # Second claim finds nothing READY -> idempotent.
        assert repo.claim_for_processing(VIDEO, ["c1"]) == set()

    def test_claim_is_video_scoped(self, repo):
        seed_video(repo, OTHER_VIDEO)
        repo.upsert_comments([comment_row("b1", video_id=OTHER_VIDEO)], batch_size=100)
        assert repo.claim_for_processing(VIDEO, ["b1"]) == set()

    def test_sentiment_fields_persist_and_row_becomes_queryable(self, repo):
        repo.upsert_comments([comment_row("c1")], batch_size=100)
        updated = self._analyze(repo, "c1")
        assert updated == 1
        row = repo.get_comments(VIDEO)[0]
        assert row["processing_status"] == "PROCESSED"
        assert row["sentiment_label"] == "POSITIVE"
        assert row["sentiment_score"] == 0.8
        assert row["sentiment_confidence"] == 0.75
        assert row["sentiment_model"] == "vader-1.0"
        assert row["sentiment_processed_at"] == "2024-03-06T00:00:00+00:00"
        # Queryable per video + label (SQL-side aggregation).
        assert repo.get_sentiment_counts(VIDEO) == {"POSITIVE": 1}

    def test_failure_outcome_records_failed_state(self, repo):
        repo.upsert_comments([comment_row("c1")], batch_size=100)
        repo.claim_for_processing(VIDEO, ["c1"])
        outcome = self._outcome("c1", label=None, score=None, target="FAILED")
        assert repo.save_sentiment_results(VIDEO, [outcome], 100) == 1
        row = repo.get_comments(VIDEO)[0]
        assert row["processing_status"] == "FAILED"
        assert row["sentiment_label"] is None

    def test_failed_comment_can_be_reprocessed(self, repo):
        # FAILED -> READY (existing requeue edge) -> claim -> PROCESSED.
        repo.upsert_comments([comment_row("c1")], batch_size=100)
        repo.claim_for_processing(VIDEO, ["c1"])
        failed = self._outcome("c1", label=None, score=None, target="FAILED")
        repo.save_sentiment_results(VIDEO, [failed], 100)
        assert repo.update_processing_status(VIDEO, "c1", "READY_FOR_ANALYSIS") == 1
        assert self._analyze(repo, "c1", label="NEGATIVE", score=-0.6) == 1
        row = repo.get_comments(VIDEO)[0]
        assert row["processing_status"] == "PROCESSED"
        assert row["sentiment_label"] == "NEGATIVE"

    def test_save_never_forces_illegal_target(self, repo):
        # PROCESSING -> READY_FOR_ANALYSIS is not an allowed edge; the row
        # must stay untouched instead of being forced.
        repo.upsert_comments([comment_row("c1")], batch_size=100)
        repo.claim_for_processing(VIDEO, ["c1"])
        outcome = self._outcome("c1", target="READY_FOR_ANALYSIS")
        assert repo.save_sentiment_results(VIDEO, [outcome], 100) == 0
        assert repo.get_comments(VIDEO)[0]["processing_status"] == "PROCESSING"
        assert repo.get_comments(VIDEO)[0]["sentiment_label"] is None

    def test_save_skips_rows_reset_since_claim(self, repo):
        # Content changed between claim and persist: row is back to READY with
        # cleared sentiment - the stale verdict must NOT overwrite the reset.
        repo.upsert_comments([comment_row("c1", text="original")], batch_size=100)
        repo.claim_for_processing(VIDEO, ["c1"])
        repo.upsert_comments([comment_row("c1", text="edited text")], batch_size=100)
        outcome = self._outcome("c1")
        assert repo.save_sentiment_results(VIDEO, [outcome], 100) == 0
        row = repo.get_comments(VIDEO)[0]
        assert row["processing_status"] == "READY_FOR_ANALYSIS"
        assert row["sentiment_label"] is None

    def test_content_change_clears_all_sentiment_fields(self, repo):
        repo.upsert_comments([comment_row("c1", text="original")], batch_size=100)
        self._analyze(repo, "c1")
        repo.upsert_comments([comment_row("c1", text="edited text")], batch_size=100)
        row = repo.get_comments(VIDEO)[0]
        assert row["processing_status"] == "READY_FOR_ANALYSIS"  # reprocess
        for column in (
            "sentiment_label",
            "sentiment_score",
            "sentiment_confidence",
            "sentiment_model",
            "sentiment_processed_at",
        ):
            assert row[column] is None, column

    def test_like_count_refresh_keeps_sentiment(self, repo):
        # Engagement metadata is not content: no reset, verdict preserved.
        repo.upsert_comments([comment_row("c1", like_count=3)], batch_size=100)
        self._analyze(repo, "c1")
        repo.upsert_comments([comment_row("c1", like_count=99)], batch_size=100)
        row = repo.get_comments(VIDEO)[0]
        assert row["processing_status"] == "PROCESSED"
        assert row["sentiment_label"] == "POSITIVE"
        assert row["like_count"] == 99

    def test_duplicate_upsert_never_duplicates_sentiment(self, repo):
        repo.upsert_comments([comment_row("c1")], batch_size=100)
        self._analyze(repo, "c1")
        repo.upsert_comments([comment_row("c1")], batch_size=100)  # re-fetch
        repo.upsert_comments([comment_row("c1")], batch_size=100)  # again
        assert len(repo.get_comments(VIDEO)) == 1
        assert repo.get_sentiment_counts(VIDEO) == {"POSITIVE": 1}

    def test_sentiment_counts_ignore_pending_and_stay_video_scoped(self, repo):
        seed_video(repo, OTHER_VIDEO)
        repo.upsert_comments(
            [
                comment_row("c1"),
                comment_row("c2"),
                comment_row("c3"),  # stays READY -> no label -> not counted
                comment_row("b1", video_id=OTHER_VIDEO),
            ],
            batch_size=100,
        )
        self._analyze(repo, "c1", label="POSITIVE")
        self._analyze(repo, "c2", label="NEGATIVE")
        self._analyze(repo, "b1", label="NEGATIVE", video_id=OTHER_VIDEO)
        assert repo.get_sentiment_counts(VIDEO) == {"POSITIVE": 1, "NEGATIVE": 1}
        assert repo.get_sentiment_counts(OTHER_VIDEO) == {"NEGATIVE": 1}

    def test_unsupported_language_label_persisted_separately(self, repo):
        repo.upsert_comments([comment_row("c1")], batch_size=100)
        repo.claim_for_processing(VIDEO, ["c1"])
        outcome = self._outcome("c1", label="UNSUPPORTED_LANGUAGE", score=None, confidence=None)
        repo.save_sentiment_results(VIDEO, [outcome], 100)
        assert repo.get_sentiment_counts(VIDEO) == {"UNSUPPORTED_LANGUAGE": 1}
        assert repo.get_comments(VIDEO)[0]["processing_status"] == "PROCESSED"

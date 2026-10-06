"""Integration tests: fetched batch -> validation -> normalization -> dedup ->
persistence, with real data-quality numbers (fixtures only, never real data)."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.db.connection import Database
from app.db.repository import DatasetRepository
from app.services.ingestion import IngestionService
from tests.conftest import make_comment, make_metadata, make_settings

VIDEO = "dQw4w9WgXcQ"


@pytest.fixture
def repo():
    db = Database("sqlite:///:memory:")
    repository = DatasetRepository(db)
    yield repository
    db.close()


def run_ingest(repo, comments, video_id=VIDEO, **collection):
    service = IngestionService(repo, make_settings())
    return service.ingest(
        video_id=video_id,
        metadata=make_metadata(),
        comments=comments,
        comments_status=collection.get("comments_status", "ok"),
        has_more=collection.get("has_more", False),
        acquired_at=datetime.now(timezone.utc),
    )


class TestPipelinePersistence:
    def test_raw_and_normalized_stored_separately(self, repo):
        raw = "  Hello &amp;  world \n\n nice "
        processed, report = run_ingest(repo, [make_comment("c1", text=raw)])

        assert report.storage_ok is True
        assert report.fetched == 1 and report.valid == 1 and report.rejected == 0
        assert report.inserted == 1 and report.duplicates == 0
        # §27 invariants from real execution:
        assert report.fetched == report.valid + report.rejected
        assert report.valid == report.inserted + report.duplicates

        row = repo.get_comments(VIDEO)[0]
        assert row["raw_text"] == raw                    # RAW preserved verbatim
        assert row["normalized_text"] == "Hello & world nice"  # processed copy
        assert processed[0].text == raw                   # contract keeps raw
        assert processed[0].text_normalized == "Hello & world nice"
        assert row["processing_status"] == "READY_FOR_ANALYSIS"
        assert row["language"]  # metadata field populated (value tested in test_language)

    def test_language_metadata_written(self, repo):
        english = "This video is absolutely amazing, great work! Thanks a lot."
        hindi = "यह वीडियो बहुत अच्छा है और मैंने इसे दोबारा देखा"
        run_ingest(
            repo,
            [make_comment("c1", text=english), make_comment("c2", text=hindi)],
        )
        languages = {r["comment_id"]: r["language"] for r in repo.get_comments(VIDEO)}
        assert languages["c1"] == "en"
        assert languages["c2"] == "hi"

    def test_video_row_stored_with_collection_meta(self, repo):
        _, report = run_ingest(
            repo, [], comments_status="disabled", has_more=True
        )
        assert report.storage_ok is True and report.fetched == 0
        row = repo.get_video(VIDEO)
        assert row is not None
        assert row["comments_status"] == "disabled"
        assert row["has_more"] == 1
        assert row["title"] == "Fixture Video Title"

    def test_ingestion_run_recorded_with_quality_numbers(self, repo):
        batch = [
            make_comment("good"),
            make_comment("bad", text=""),
            make_comment("wrong-video", video_id="BBBBBBBBBBB"),
        ]
        _, report = run_ingest(repo, batch)
        run = repo.get_latest_ingestion_run(VIDEO)
        assert run is not None
        assert run["fetched_count"] == report.fetched == 3
        assert run["valid_count"] == report.valid == 1
        assert run["rejected_count"] == report.rejected == 2
        assert run["inserted_count"] == report.inserted == 1
        assert run["storage_ok"] == 1
        assert run["duration_ms"] >= 0


class TestValidationInPipeline:
    def test_invalid_records_never_stored(self, repo):
        future = datetime.now(timezone.utc) + timedelta(days=3)
        batch = [
            make_comment("good"),
            make_comment("empty", text=""),
            make_comment("wrong-video", video_id="BBBBBBBBBBB"),
            make_comment("future", published_at=future),
        ]
        _, report = run_ingest(repo, batch)
        assert report.fetched == 4
        assert report.rejected == 3
        assert report.valid == 1
        stored = {r["comment_id"] for r in repo.get_comments(VIDEO)}
        assert stored == {"good"}
        # structured, text-free issues
        assert all(i.field and i.reason for i in report.rejected_issues)

    def test_one_bad_record_does_not_kill_the_batch(self, repo):
        batch = [make_comment("ok1"), make_comment("bad", text=""), make_comment("ok2")]
        processed, report = run_ingest(repo, batch)
        assert report.valid == 2
        assert {c.comment_id for c in processed} == {"ok1", "ok2"}
        assert len(repo.get_comments(VIDEO)) == 2

    def test_empty_after_normalization_rejected(self, repo):
        # LRM characters survive validation (not whitespace) but clean to ''.
        comments = [make_comment("x", text="\u200e\u200e")]
        _, report = run_ingest(repo, comments)
        assert report.fetched == 1
        assert report.rejected == 1
        assert report.valid == 0
        assert len(repo.get_comments(VIDEO)) == 0
        reasons = {(i.field, i.reason) for i in report.rejected_issues}
        assert ("text", "empty_after_normalization") in reasons


class TestDeduplication:
    def test_refetch_updates_instead_of_duplicating(self, repo):
        batch = [make_comment("c1"), make_comment("c2")]
        _, first = run_ingest(repo, batch)
        assert first.inserted == 2

        first_seen = {
            r["comment_id"]: r["first_seen_at"] for r in repo.get_comments(VIDEO)
        }

        _, second = run_ingest(repo, batch)
        assert second.inserted == 0
        assert second.updated == 2
        assert second.duplicates == 2
        assert second.valid == second.inserted + second.duplicates

        rows = repo.get_comments(VIDEO)
        assert len(rows) == 2  # still exactly two records
        for row in rows:
            assert row["first_seen_at"] == first_seen[row["comment_id"]]

    def test_in_batch_duplicates_collapse_to_one_record(self, repo):
        batch = [
            make_comment("dup", text="one"),
            make_comment("dup", text="two"),
        ]
        processed, report = run_ingest(repo, batch)
        assert report.fetched == 2
        assert report.valid == 2
        assert report.inserted == 1
        assert report.duplicates == 1  # in-batch extra counted as duplicate
        assert report.valid == report.inserted + report.duplicates
        # Response and storage agree: single record, latest content wins.
        assert len(processed) == 1
        rows = repo.get_comments(VIDEO)
        assert len(rows) == 1
        assert rows[0]["raw_text"] == "two"

    def test_two_videos_ingested_independently(self, repo):
        run_ingest(repo, [make_comment("a1")], video_id=VIDEO)
        run_ingest(
            repo,
            [make_comment("b1", video_id="BBBBBBBBBBB")],
            video_id="BBBBBBBBBBB",
        )
        assert {r["comment_id"] for r in repo.get_comments(VIDEO)} == {"a1"}
        assert {
            r["comment_id"] for r in repo.get_comments("BBBBBBBBBBB")
        } == {"b1"}


class TestFailureIsolation:
    def test_persistence_failure_still_serves_acquired_data(self, repo, monkeypatch):
        def boom(**_kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(repo, "upsert_video", boom)

        processed, report = run_ingest(repo, [make_comment("c1")])
        assert report.storage_ok is False
        assert report.inserted == 0
        # Acquisition results are still returned to the caller (best-effort
        # persistence) - the failure is observable via storage_ok/logs.
        assert len(processed) == 1
        assert repo.get_video(VIDEO) is None
        assert repo.get_latest_ingestion_run(VIDEO) is None


class TestIncrementalSession:
    """Sprint 4.1: page -> validate -> normalize -> dedup -> write -> next
    page, with ONE aggregated run per completed acquisition."""

    @staticmethod
    def _session(repo):
        service = IngestionService(repo, make_settings())
        return service.begin(VIDEO, make_metadata(), datetime.now(timezone.utc))

    def test_pages_persist_before_finish(self, repo):
        session = self._session(repo)
        session.add_page([make_comment("p1-c1"), make_comment("p1-c2")], page_has_more=True)

        # Page 1 is durable BEFORE the acquisition completed (no finish yet):
        # this is what makes a mid-acquisition failure keep fetched pages.
        stored = {r["comment_id"] for r in repo.get_comments(VIDEO)}
        assert stored == {"p1-c1", "p1-c2"}
        assert repo.get_latest_ingestion_run(VIDEO) is None  # run at finish

        session.add_page([make_comment("p2-c1")], page_has_more=False)
        processed, report = session.finish("ok", has_more=False)

        assert {c.comment_id for c in processed} == {"p1-c1", "p1-c2", "p2-c1"}
        run = repo.get_latest_ingestion_run(VIDEO)
        assert run is not None
        assert run["fetched_count"] == report.fetched == 3

    def test_multi_page_report_aggregates_with_invariants(self, repo):
        session = self._session(repo)
        page1 = [make_comment("c1"), make_comment("c2"), make_comment("c3")]
        page2 = [
            make_comment("c2"),                                   # cross-page duplicate
            make_comment("c4"),
            make_comment("bad", text=""),                          # rejected
        ]
        session.add_page(page1, page_has_more=True)
        session.add_page(page2, page_has_more=False)
        _, report = session.finish("ok", has_more=False)

        # Aggregated over both pages, never per-page only:
        assert report.fetched == 6
        assert report.valid == 5
        assert report.rejected == 1
        assert report.inserted == 4              # c1, c2, c3 (page 1) + c4
        assert report.updated == 1               # c2 refreshed across pages
        assert report.duplicates == 1            # the cross-page repeat
        # §33 invariants hold for the whole acquisition:
        assert report.fetched == report.valid + report.rejected
        assert report.valid == report.inserted + report.duplicates
        assert report.storage_ok is True
        assert len(repo.get_comments(VIDEO)) == 4  # one row per comment_id

        run = repo.get_latest_ingestion_run(VIDEO)
        assert run["fetched_count"] == 6 and run["valid_count"] == 5
        assert run["rejected_count"] == 1 and run["duplicate_count"] == 1

    def test_finish_overwrites_page_level_flags_with_final_truth(self, repo):
        session = self._session(repo)
        session.add_page([make_comment("c1")], page_has_more=True)
        session.finish("ok", has_more=False)  # YouTube ran out on the last page
        row = repo.get_video(VIDEO)
        assert row["has_more"] == 0  # final authoritative write wins
        assert row["comments_status"] == "ok"

"""Sprint 4.2 tests: active-video dataset lifecycle, cleanup, race safety.

Covers spec §44-§48 with fixtures only (mock-data policy):
- activation: first video, same-video no-op, switch, rapid A→B→C→D,
  switch-back, adoption of a pre-4.2 row, transactional rollback.
- data isolation: comments/ingestion_runs/sentiment of the previous video
  are removed; stats end with 404 (no stale numbers).
- race safety: generation-guarded repository writes, an ingestion session
  superseded mid-run, an end-to-end API race injected during acquisition,
  and a stale sentiment run that can never commit after A→B→A.
"""
import sqlite3
from datetime import datetime, timezone

import pytest

from app.db.connection import Database
from app.db.repository import DatasetRepository
from app.db.schema import migrate
from app.services.dataset import DatasetService
from app.services.ingestion import IngestionService
from app.services.sentiment import SentimentService
from app.services import sentiment as sentiment_module
from app.schemas.youtube import YtVideoListResponse
from tests.conftest import (
    FakeYouTubeClient,
    TestClient,
    comment_row,
    make_comment,
    make_metadata,
    make_settings,
    seed_video,
    thread_payload,
    video_payload,
)

VIDEO = "dQw4w9WgXcQ"
OTHER = "BBBBBBBBBBB"
THIRD = "CCCCCCCCCCC"
FOURTH = "DDDDDDDDDDD"

POSITIVE_TEXT = "I really love this video, it is fantastic and genuinely helpful"

# Minimal metadata dict for direct upsert_video calls (all fields optional).
META = {"title": "Fixture Video Title"}
NOW = "2026-09-26T00:00:00+00:00"


@pytest.fixture
def repo():
    db = Database("sqlite:///:memory:")
    repository = DatasetRepository(db)
    yield repository
    db.close()


@pytest.fixture
def dataset_service(repo):
    return DatasetService(repo, make_settings())


def _upsert_video(repo, video_id, acquired_at=None):
    # Fresh by default so adoption tests exercise the freshness policy,
    # not the wall clock (TTL = 3600s).
    acquired_at = acquired_at or datetime.now(timezone.utc).isoformat()
    repo.upsert_video(
        video_id=video_id,
        metadata=META,
        comments_status="ok",
        has_more=False,
        acquired_at=acquired_at,
    )


class TestActivation:
    def test_first_video_becomes_active_with_stale_placeholder(self, repo, dataset_service):
        activation = dataset_service.ensure_active(VIDEO)
        assert activation.changed is True
        assert activation.generation >= 1

        row = repo.get_active_video()
        assert row is not None
        assert row["video_id"] == VIDEO
        assert row["is_active"] == 1
        assert int(row["dataset_generation"]) == activation.generation
        # §18: the placeholder must never be fresh - acquisition still runs.
        assert dataset_service.get_fresh_response(VIDEO) is None

    def test_same_video_activation_is_not_destructive(self, repo, dataset_service):
        first = dataset_service.ensure_active(VIDEO)
        repo.upsert_comments([comment_row("c1"), comment_row("c2")], batch_size=10)

        second = dataset_service.ensure_active(VIDEO)
        assert second.changed is False
        assert second.generation == first.generation  # same activation
        # No deletion: the dataset is untouched for a same-video revisit.
        assert {r["comment_id"] for r in repo.get_comments(VIDEO)} == {"c1", "c2"}
        assert repo.get_active_video()["video_id"] == VIDEO

    def test_switch_removes_previous_dataset_including_runs(
        self, repo, dataset_service
    ):
        activation = dataset_service.ensure_active(VIDEO)
        repo.upsert_comments([comment_row("a1"), comment_row("a2")], batch_size=10)
        repo.record_ingestion_run(
            video_id=VIDEO,
            fetched=2, valid=2, rejected=0, duplicates=0,
            inserted=2, updated=0, storage_ok=True,
            started_at=NOW, finished_at=NOW, duration_ms=1,
        )

        switched = dataset_service.ensure_active(OTHER)
        assert switched.changed is True
        assert switched.generation > activation.generation

        # §10: video row + comments (cascade incl. sentiment columns) + runs.
        assert repo.get_video(VIDEO) is None
        assert repo.get_comments(VIDEO) == []
        assert repo.get_latest_ingestion_run(VIDEO) is None
        # New video owns an empty working dataset.
        row = repo.get_active_video()
        assert row["video_id"] == OTHER and row["is_active"] == 1
        assert repo.get_comments(OTHER) == []

    def test_rapid_switch_leaves_only_the_final_video(self, repo, dataset_service):
        generations = []
        for video_id in (VIDEO, OTHER, THIRD, FOURTH):
            generations.append(dataset_service.ensure_active(video_id).generation)
            repo.upsert_comments([comment_row(f"x-{video_id}", video_id=video_id)],
                                 batch_size=10)

        # §34: strictly increasing generations, final state is D only.
        assert generations == sorted(generations)
        assert len(set(generations)) == 4
        assert repo.get_active_video()["video_id"] == FOURTH
        for video_id in (VIDEO, OTHER, THIRD):
            assert repo.get_video(video_id) is None
            assert repo.get_comments(video_id) == []
        # Only D's own post-activation rows remain - never A/B/C's.
        assert {r["comment_id"] for r in repo.get_comments(FOURTH)} == {
            f"x-{FOURTH}"
        }

    def test_switch_back_treats_previous_video_as_fresh(self, repo, dataset_service):
        dataset_service.ensure_active(VIDEO)
        repo.upsert_comments([comment_row("a1")], batch_size=10)
        dataset_service.ensure_active(OTHER)

        back = dataset_service.ensure_active(VIDEO)
        assert back.changed is True
        assert back.generation > 1
        row = repo.get_video(VIDEO)
        assert row is not None and row["is_active"] == 1
        # §35: stale data never resurrects; B is gone; A must re-acquire.
        assert repo.get_comments(VIDEO) == []
        assert repo.get_video(OTHER) is None
        assert dataset_service.get_fresh_response(VIDEO) is None

    def test_adopts_existing_row_and_removes_strays(self, repo, dataset_service):
        # Pre-lifecycle database: rows exist but none is marked active.
        _upsert_video(repo, VIDEO)
        repo.upsert_comments([comment_row("a1")], batch_size=10)
        _upsert_video(repo, OTHER)
        repo.upsert_comments([comment_row("b1", video_id=OTHER)], batch_size=10)

        activation = dataset_service.ensure_active(VIDEO)
        assert activation.changed is True  # active identity was unset

        row = repo.get_active_video()
        assert row["video_id"] == VIDEO and row["is_active"] == 1
        # The requested video's fresh dataset is PRESERVED (upgrade-safe)…
        assert {r["comment_id"] for r in repo.get_comments(VIDEO)} == {"a1"}
        assert dataset_service.get_fresh_response(VIDEO) is not None
        # …while every other dataset is removed.
        assert repo.get_video(OTHER) is None
        assert repo.get_comments(OTHER) == []

    def test_failed_switch_rolls_back_to_consistent_state(self, repo, dataset_service):
        dataset_service.ensure_active(VIDEO)
        repo.upsert_comments([comment_row("a1")], batch_size=10)

        # Force the switch's INSERT to fail mid-transaction (§32).
        conn = repo._db.connection()
        with repo._db.lock:
            conn.execute(
                "CREATE TRIGGER reject_switch BEFORE INSERT ON videos "
                "WHEN NEW.video_id = 'BBBBBBBBBBB' "
                "BEGIN SELECT RAISE(ABORT, 'switch denied'); END"
            )
            conn.commit()

        with pytest.raises(sqlite3.Error):
            dataset_service.ensure_active(OTHER)

        # Never half-switched: A still active, data intact, B never claimed.
        row = repo.get_active_video()
        assert row is not None and row["video_id"] == VIDEO
        assert {r["comment_id"] for r in repo.get_comments(VIDEO)} == {"a1"}
        assert repo.get_video(OTHER) is None

    def test_pre_lifecycle_database_gains_columns_on_migration(self, repo, dataset_service):
        # Simulate an upgraded database: drop the Sprint 4.2 columns, insert
        # a pre-lifecycle row, then re-run the startup migration.
        conn = repo._db.connection()
        with repo._db.lock:
            conn.execute("ALTER TABLE videos DROP COLUMN is_active")
            conn.execute("ALTER TABLE videos DROP COLUMN dataset_generation")
            conn.execute(
                "INSERT INTO videos (video_id, first_acquired_at, last_acquired_at, "
                "updated_at) VALUES (?, ?, ?, ?)",
                (VIDEO, NOW, NOW, NOW),
            )
            conn.commit()
            migrate(conn)  # what connection bootstrap runs on startup

        row = repo.get_video(VIDEO)
        assert row["is_active"] == 0 and row["dataset_generation"] == 0

        activation = dataset_service.ensure_active(VIDEO)
        assert activation.generation >= 1
        assert repo.get_active_video()["video_id"] == VIDEO


class TestGenerationGuards:
    def test_stale_generation_cannot_write_anything(self, repo, dataset_service):
        activation = dataset_service.ensure_active(VIDEO)
        written = repo.upsert_video(
            video_id=VIDEO, metadata=META, comments_status="ok",
            has_more=False, acquired_at=NOW,
            generation=activation.generation,
        )
        assert written is True
        stats = repo.upsert_comments(
            [comment_row("a1")], batch_size=10,
            generation=activation.generation,
        )
        assert stats is not None and stats.inserted == 1

        # Switch away: the old generation is now stale everywhere (§13).
        dataset_service.ensure_active(OTHER)
        assert repo.upsert_video(
            video_id=VIDEO, metadata=META, comments_status="ok",
            has_more=False, acquired_at=NOW,
            generation=activation.generation,
        ) is False
        assert repo.upsert_comments(
            [comment_row("a2")], batch_size=10,
            generation=activation.generation,
        ) is None
        assert repo.record_ingestion_run(
            video_id=VIDEO, fetched=1, valid=1, rejected=0, duplicates=0,
            inserted=1, updated=0, storage_ok=True,
            started_at=NOW, finished_at=NOW, duration_ms=1,
            generation=activation.generation,
        ) is False

        # Nothing resurrected, no orphaned run row.
        assert repo.get_video(VIDEO) is None
        assert repo.get_comments(VIDEO) == []
        assert repo.get_latest_ingestion_run(VIDEO) is None

    def test_ingestion_session_superseded_mid_run_drops_pages(self, repo, dataset_service):
        activation = dataset_service.ensure_active(VIDEO)
        session = IngestionService(repo, make_settings()).begin(
            VIDEO, make_metadata(), datetime.now(timezone.utc),
            generation=activation.generation,
        )

        first = session.add_page(
            [make_comment("a1"), make_comment("a2")], page_has_more=True
        )
        assert first is not None and first.inserted == 2
        assert {r["comment_id"] for r in repo.get_comments(VIDEO)} == {"a1", "a2"}

        # User switches while page 2 is in flight (§11/§47).
        dataset_service.ensure_active(OTHER)

        second = session.add_page([make_comment("a3")], page_has_more=False)
        assert second is None
        assert session.superseded is True

        processed, report = session.finish("ok", has_more=False)
        # The superseded session leaves NO trace: no A rows, no video row,
        # no ingestion run, and B's dataset stays empty and untouched.
        assert repo.get_video(VIDEO) is None
        assert repo.get_comments(VIDEO) == []
        assert repo.get_latest_ingestion_run(VIDEO) is None
        assert repo.get_active_video()["video_id"] == OTHER
        assert repo.get_comments(OTHER) == []
        assert report.fetched >= 2  # honest counts still reported internally


def _fake_for(video_id: str, count: int, prefix: str) -> FakeYouTubeClient:
    items = [
        thread_payload(
            f"{prefix}{i}",
            f"I really love video {i}, this upload is genuinely great",
        )
        for i in range(count)
    ]
    return FakeYouTubeClient(
        video=YtVideoListResponse.model_validate(video_payload(video_id)),
        pages={None: (items, None)},
    )


class TestApiLifecycle:
    def test_database_keeps_only_current_video_dataset(self, app_factory):
        """§25/§26/§45: A(10) → B(20) leaves exactly 20 comment rows."""
        client = app_factory(_fake_for(VIDEO, 10, "a"), cache_ttl_seconds=0)
        first = client.get(f"/api/videos/{VIDEO}")
        assert first.status_code == 200
        assert first.json()["comments"]["count"] == 10

        fake = _fake_for(OTHER, 20, "b")
        client.app.state.video_data_service._client = fake  # switch fixture
        second = client.get(f"/api/videos/{OTHER}")
        assert second.status_code == 200
        assert second.json()["comments"]["count"] == 20

        repo = client.app.state.sentiment_service._repo
        # ~20 rows, not 30: only the active dataset remains.
        assert repo.get_comment_stats(OTHER)["total"] == 20
        assert repo.get_comments(VIDEO) == []
        assert repo.get_status_counts(VIDEO) == {}
        # Stats for the previous video end honestly with 404 (§28).
        gone = client.get(f"/api/videos/{VIDEO}/stats")
        assert gone.status_code == 404
        stats_b = client.get(f"/api/videos/{OTHER}/stats").json()
        assert stats_b["totalComments"] == 20

    def test_last_ingest_belongs_to_the_current_video(self, app_factory):
        """§28: video B must never report video A's ingestion run."""
        client = app_factory(_fake_for(VIDEO, 3, "a"), cache_ttl_seconds=0)
        client.get(f"/api/videos/{VIDEO}")
        client.app.state.video_data_service._client = _fake_for(OTHER, 2, "b")
        client.get(f"/api/videos/{OTHER}")

        stats = client.get(f"/api/videos/{OTHER}/stats").json()
        assert stats["lastIngest"]["fetched"] == 2
        assert stats["lastIngest"]["inserted"] == 2
        repo = client.app.state.sentiment_service._repo
        assert repo.get_latest_ingestion_run(VIDEO) is None  # cleaned with A

    def test_switch_back_reacquires_as_fresh_dataset(self, app_factory):
        """§35: A → B → A ends with A only, freshly acquirable."""
        client = app_factory(_fake_for(VIDEO, 4, "a"), cache_ttl_seconds=0)
        client.get(f"/api/videos/{VIDEO}")
        client.app.state.video_data_service._client = _fake_for(OTHER, 6, "b")
        client.get(f"/api/videos/{OTHER}")

        client.app.state.video_data_service._client = _fake_for(VIDEO, 4, "a")
        back = client.get(f"/api/videos/{VIDEO}")
        assert back.status_code == 200
        assert back.json()["comments"]["count"] == 4

        repo = client.app.state.sentiment_service._repo
        assert repo.get_active_video()["video_id"] == VIDEO
        assert repo.get_comments(OTHER) == []
        assert repo.get_comment_stats(VIDEO)["total"] == 4

    def test_race_switch_during_acquisition_never_persists_stale_data(
        self, app_factory
    ):
        """§47 end-to-end: video B activates while video A's page 2 is being
        fetched. A's result must leave no rows, no run, and no L1 entry."""
        pages = {
            None: (
                [thread_payload("a0", "I love this first comment a lot"),
                 thread_payload("a1", "I love this second comment a lot")],
                "P2",
            ),
            "P2": (
                [thread_payload("a2", "I love this third comment a lot"),
                 thread_payload("a3", "I love this fourth comment a lot")],
                None,
            ),
        }
        injected = {"done": False}

        fake = FakeYouTubeClient(
            video=YtVideoListResponse.model_validate(video_payload(VIDEO)),
            pages=pages,
        )
        client = app_factory(fake, cache_ttl_seconds=0)
        dataset = client.app.state.dataset_service

        def switch_on_second_page(call_number: int) -> None:
            if call_number == 2 and not injected["done"]:
                injected["done"] = True
                # User navigates to video B mid-acquisition.
                dataset.ensure_active(OTHER)

        fake.page_hook = switch_on_second_page

        response = client.get(f"/api/videos/{VIDEO}")
        assert response.status_code == 200  # stale consumer gets its answer
        assert injected["done"] is True

        repo = client.app.state.sentiment_service._repo
        # Video A's acquisition was superseded: nothing persisted (§13).
        assert repo.get_video(VIDEO) is None
        assert repo.get_comments(VIDEO) == []
        assert repo.get_latest_ingestion_run(VIDEO) is None
        assert repo.get_active_video()["video_id"] == OTHER
        # Superseded results are never cached into L1 (§30).
        assert client.app.state.cache.get(f"video:{VIDEO}") is None

        # The new working dataset acquires normally afterwards.
        fake.video = YtVideoListResponse.model_validate(video_payload(OTHER))
        fake.pages = {None: ([thread_payload("b0", "not my favorite honestly")], None)}
        second = client.get(f"/api/videos/{OTHER}")
        assert second.status_code == 200
        assert second.json()["comments"]["count"] == 1
        assert repo.get_comments(VIDEO) == []
        assert [r["comment_id"] for r in repo.get_comments(OTHER)] == ["b0"]

    def test_l1_cache_follows_single_active_dataset(self, app_factory):
        client = app_factory(_fake_for(VIDEO, 2, "a"), cache_ttl_seconds=60)
        client.get(f"/api/videos/{VIDEO}")
        cache = client.app.state.cache
        assert cache.get(f"video:{VIDEO}") is not None  # A cached while active

        client.app.state.video_data_service._client = _fake_for(OTHER, 2, "b")
        client.get(f"/api/videos/{OTHER}")
        # §30: A's entry is gone; only the active video is in memory.
        assert cache.get(f"video:{VIDEO}") is None
        assert cache.get(f"video:{OTHER}") is not None
        assert len(cache) == 1


class TestSentimentSupersession:
    def test_stale_sentiment_run_never_commits_after_reactivation(
        self, repo, dataset_service, monkeypatch, caplog
    ):
        """§39 + §35: batch 1 of video A's analysis triggers A→B→A; the run
        captured generation 1, so it can never write into the NEW A dataset."""
        activation = dataset_service.ensure_active(VIDEO)
        repo.upsert_comments(
            [comment_row(f"c{i}", text=POSITIVE_TEXT) for i in range(4)],
            batch_size=10,
        )
        service = SentimentService(repo, make_settings(comment_batch_size=2))

        switched = {"done": False}
        original = sentiment_module.classify

        def switch_mid_batch(text: str):
            verdict = original(text)
            if not switched["done"]:
                switched["done"] = True
                dataset_service.ensure_active(OTHER)      # user switches to B…
                dataset_service.ensure_active(VIDEO)      # …and back to A
                repo.upsert_comments(                      # new-generation rows
                    [comment_row(f"n{i}", text=POSITIVE_TEXT) for i in range(4)],
                    batch_size=10,
                )
            return verdict

        monkeypatch.setattr(sentiment_module, "classify", switch_mid_batch)
        with caplog.at_level("INFO"):
            analysis = service.get_analysis(VIDEO)

        # The run aborted on the generation check before batch 2.
        assert "SENTIMENT_RUN_SUPERSEDED" in caplog.text
        # The NEW A dataset was never claimed or committed by the stale run.
        rows = repo.get_comments(VIDEO)
        assert {r["comment_id"] for r in rows} == {f"n{i}" for i in range(4)}
        assert all(r["processing_status"] == "READY_FOR_ANALYSIS" for r in rows)
        assert all(r["sentiment_label"] is None for r in rows)
        # Response reflects the current generation's honest state.
        assert analysis.status == "NOT_ANALYZED"
        assert analysis.stats.analyzed == 0
        assert analysis.dataset.stored == 4
        # And the active row is the NEW activation (generation advanced).
        assert int(repo.get_active_video()["dataset_generation"]) > (
            activation.generation
        )

    def test_non_active_stray_row_is_never_processed(self, repo, dataset_service):
        """§40: reads of a non-active row report state, never trigger work."""
        dataset_service.ensure_active(OTHER)  # B owns the working dataset
        seed_video(repo, VIDEO)               # legacy stray row (pre-4.2)
        repo.upsert_comments(
            [comment_row("c1", text=POSITIVE_TEXT)], batch_size=10
        )

        service = SentimentService(repo, make_settings())
        analysis = service.get_analysis(VIDEO)
        assert analysis is not None
        # Pending row stays pending: no processing, no activation, no delete.
        row = repo.get_comments(VIDEO)[0]
        assert row["processing_status"] == "READY_FOR_ANALYSIS"
        assert row["sentiment_label"] is None
        assert analysis.status == "NOT_ANALYZED"
        assert repo.get_active_video()["video_id"] == OTHER

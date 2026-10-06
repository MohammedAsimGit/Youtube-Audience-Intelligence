"""Sprint 4 tests: engine classification, aggregation, orchestration, API.

Coverage map (spec §29-§31):
- Classification: POSITIVE / NEUTRAL / NEGATIVE, empty text, determinism,
  score & confidence bounds + documented boundary-distance semantics.
- Language policy: `en` analyzed, everything else UNSUPPORTED_LANGUAGE.
- Aggregation: percentages sum to exactly 100, dominant tie-break, empty.
- Service: batching through `comment_batch_size`, state-machine path,
  idempotency, per-row failure isolation, FAILED requeue, orphaned-claim
  recovery, video isolation, content-reset reprocessing.
- API: valid video, no dataset, not analyzed, processing, processed, failed,
  invalid id, empty dataset, aggregation correctness, video isolation.
- Security: no raw comment text in logs.

No test fabricates sentiment: every verdict comes from the real engine over
fixture text (the mock-data policy applies to text, not to model output).
"""
from typing import List

import pytest

from app.db.connection import Database
from app.db.repository import DatasetRepository, SentimentOutcome
from app.models.processing import ProcessingStatus
from app.services import sentiment as sentiment_module
from app.services.sentiment import (
    MODEL_ID,
    SentimentLabel,
    SentimentService,
    classify,
    derive_status,
    distribution_percentages,
    dominant_sentiment,
)
from app.schemas.youtube import YtVideoListResponse
from tests.conftest import (
    FakeYouTubeClient,
    TestClient,
    comment_row,
    make_settings,
    seed_video,
    thread_payload,
    video_payload,
)

VIDEO = "dQw4w9WgXcQ"
OTHER_VIDEO = "BBBBBBBBBBB"

POSITIVE_TEXT = "I really love this video, it is fantastic and genuinely helpful"
NEGATIVE_TEXT = "This is the worst video ever, a complete waste of my time"
NEUTRAL_TEXT = "The video was uploaded on Tuesday morning"


# ---------------------------------------------------------------------------
# §29: engine classification (pure text -> verdict)
# ---------------------------------------------------------------------------


class TestClassification:
    def test_positive_text_is_positive(self):
        verdict = classify(POSITIVE_TEXT)
        assert verdict.label is SentimentLabel.POSITIVE
        assert 0.05 <= verdict.score <= 1.0

    def test_negative_text_is_negative(self):
        verdict = classify(NEGATIVE_TEXT)
        assert verdict.label is SentimentLabel.NEGATIVE
        assert -1.0 <= verdict.score <= -0.05

    def test_neutral_text_is_neutral(self):
        verdict = classify(NEUTRAL_TEXT)
        assert verdict.label is SentimentLabel.NEUTRAL
        assert -0.05 < verdict.score < 0.05

    @pytest.mark.parametrize("text", ["", "   ", "\n\t"])
    def test_empty_text_handled_safely_without_fabrication(self, text):
        # No lexical evidence -> defined outcome with zero confidence, never
        # a made-up polarity.
        verdict = classify(text)
        assert verdict.label is SentimentLabel.NEUTRAL
        assert verdict.score == 0.0
        assert verdict.confidence == 0.0

    def test_deterministic_for_identical_input(self):
        samples = [POSITIVE_TEXT, NEGATIVE_TEXT, NEUTRAL_TEXT, "ok", "😍 great!"]
        first = [classify(text) for text in samples]
        second = [classify(text) for text in samples]
        assert first == second  # same input, same verdicts, run after run

    def test_score_always_within_documented_range(self):
        for text in [
            POSITIVE_TEXT,
            NEGATIVE_TEXT,
            NEUTRAL_TEXT,
            "AMAZING!!! best best best 😍😍",
            "bad bad terrible awful HATE",
            "ok",
        ]:
            verdict = classify(text)
            assert -1.0 <= verdict.score <= 1.0, text

    def test_label_matches_documented_thresholds(self):
        # Self-consistency with the published VADER thresholds: the returned
        # label must be exactly the threshold rule applied to the score.
        for text in [POSITIVE_TEXT, NEGATIVE_TEXT, NEUTRAL_TEXT, "ok", ""]:
            verdict = classify(text)
            if verdict.score >= 0.05:
                expected = SentimentLabel.POSITIVE
            elif verdict.score <= -0.05:
                expected = SentimentLabel.NEGATIVE
            else:
                expected = SentimentLabel.NEUTRAL
            assert verdict.label is expected

    def test_confidence_is_documented_boundary_distance(self):
        # confidence = normalized distance from the decision boundary:
        #   neutral:  (0.05 - |score|) / 0.05
        #   polar:    (|score| - 0.05) / (1 - 0.05)
        for text in [POSITIVE_TEXT, NEGATIVE_TEXT, NEUTRAL_TEXT, "ok", "great"]:
            verdict = classify(text)
            assert 0.0 <= verdict.confidence <= 1.0
            if verdict.label is SentimentLabel.NEUTRAL:
                expected = (0.05 - abs(verdict.score)) / 0.05
            else:
                expected = (abs(verdict.score) - 0.05) / 0.95
            assert verdict.confidence == pytest.approx(expected, abs=1e-9)

    def test_deep_neutral_scores_high_confidence(self):
        verdict = classify(NEUTRAL_TEXT)
        assert verdict.label is SentimentLabel.NEUTRAL
        assert verdict.confidence > 0.9  # far from both boundaries


# ---------------------------------------------------------------------------
# §29: aggregation (percentages + dominant)
# ---------------------------------------------------------------------------


class TestAggregation:
    @pytest.mark.parametrize(
        ("positive", "neutral", "negative", "expected"),
        [
            (10, 5, 5, (50.0, 25.0, 25.0)),       # spec example
            (1680, 480, 240, (70.0, 20.0, 10.0)),  # spec example
            (220, 130, 50, (55.0, 32.5, 12.5)),    # §19/§41 fractional example
            (1, 0, 0, (100.0, 0.0, 0.0)),
            (0, 0, 0, (0.0, 0.0, 0.0)),            # empty dataset
        ],
    )
    def test_known_percentages(self, positive, neutral, negative, expected):
        assert distribution_percentages(positive, neutral, negative) == expected

    def test_percentages_always_sum_to_exactly_100(self):
        for counts in [(1, 1, 1), (1, 1, 2), (7, 2, 1), (999, 1, 0), (3, 3, 3), (23, 41, 36)]:
            percentages = distribution_percentages(*counts)
            assert sum(percentages) == 100
            assert all(0 <= part <= 100 for part in percentages)
            # Deterministic rounding (§20): every value is an exact
            # multiple of 0.1 - verified in integer tenths so no float
            # summation error can hide a distribution bug.
            assert sum(int(round(part * 10)) for part in percentages) == 1000

    def test_fractional_tie_break_gives_point_to_priority_label(self):
        # 1/1/1 -> exact 33.33 each; the leftover tenth goes to POSITIVE
        # (documented priority POSITIVE > NEUTRAL > NEGATIVE), so the
        # shares remain one-decimal values that sum to exactly 100.
        assert distribution_percentages(1, 1, 1) == (33.4, 33.3, 33.3)

    @pytest.mark.parametrize(
        ("positive", "neutral", "negative", "expected"),
        [
            (10, 5, 5, "POSITIVE"),
            (0, 5, 5, "NEUTRAL"),      # tie: NEUTRAL outranks NEGATIVE
            (4, 4, 4, "POSITIVE"),     # full tie: POSITIVE wins by priority
            (0, 0, 3, "NEGATIVE"),
            (0, 0, 0, None),           # no analyzed data -> no dominant
        ],
    )
    def test_dominant_sentiment(self, positive, neutral, negative, expected):
        assert dominant_sentiment(positive, neutral, negative) == expected

    def test_dominant_ignores_skipped_only_dataset(self):
        # Skipped (unsupported language) rows are not analyzed -> no dominant.
        assert dominant_sentiment(0, 0, 0) is None


class TestStatusDerivation:
    @pytest.mark.parametrize(
        ("counts", "active", "expected"),
        [
            ({}, False, "NOT_ANALYZED"),                              # empty dataset
            ({"READY_FOR_ANALYSIS": 5}, False, "NOT_ANALYZED"),       # pending, idle
            ({"PROCESSING": 2}, False, "PROCESSING"),                 # rows claimed
            ({}, True, "PROCESSING"),                                 # run active in-process
            ({"FAILED": 3}, False, "FAILED"),                         # all failed
            ({"PROCESSED": 4}, False, "PROCESSED"),                   # complete
            ({"PROCESSED": 4, "FAILED": 1}, False, "PROCESSED"),      # partial -> results exist
            ({"PROCESSED": 4, "READY_FOR_ANALYSIS": 1}, False, "PROCESSED"),  # reset row waits
        ],
    )
    def test_public_lifecycle(self, counts, active, expected):
        assert derive_status(counts, active_for_video=active) == expected


class TestLanguagePolicy:
    def test_supported_set_is_explicit_and_english_only(self):
        # VADER's lexicon is English; the policy must not pretend otherwise.
        assert sentiment_module.SUPPORTED_LANGUAGES == frozenset({"en"})
        assert "unknown" not in sentiment_module.SUPPORTED_LANGUAGES
        assert sentiment_module.UNSUPPORTED_LANGUAGE not in sentiment_module.SUPPORTED_LANGUAGES


# ---------------------------------------------------------------------------
# §10-§13: orchestration over the repository
# ---------------------------------------------------------------------------


@pytest.fixture
def repo():
    db = Database("sqlite:///:memory:")
    repository = DatasetRepository(db)
    seed_video(repository, VIDEO)
    yield repository
    db.close()


@pytest.fixture
def service(repo):
    # Tiny batches force the multi-batch path in most tests.
    return SentimentService(repo, make_settings(comment_batch_size=2))


def _seed(repo: DatasetRepository, rows: List[dict]) -> None:
    repo.upsert_comments(rows, batch_size=100)


class TestServiceOrchestration:
    def test_ready_rows_processed_and_persisted(self, repo, service):
        _seed(repo, [
            comment_row("c1", text=POSITIVE_TEXT),
            comment_row("c2", text=NEGATIVE_TEXT),
            comment_row("c3", text=NEUTRAL_TEXT),
        ])
        analysis = service.get_analysis(VIDEO)
        assert analysis.status == "PROCESSED"
        assert analysis.stats.analyzed == 3
        assert analysis.dominant_sentiment is not None

        by_id = {r["comment_id"]: r for r in repo.get_comments(VIDEO)}
        assert by_id["c1"]["sentiment_label"] == "POSITIVE"
        assert by_id["c2"]["sentiment_label"] == "NEGATIVE"
        assert by_id["c3"]["sentiment_label"] == "NEUTRAL"
        for row in by_id.values():
            assert row["processing_status"] == "PROCESSED"
            assert row["sentiment_model"] == MODEL_ID
            assert row["sentiment_processed_at"] is not None
            assert row["sentiment_score"] is not None and -1.0 <= row["sentiment_score"] <= 1.0

    def test_state_machine_path_uses_claim_then_persist(self, repo, service, monkeypatch):
        _seed(repo, [comment_row("c1", text=POSITIVE_TEXT)])
        calls = []
        original_claim = repo.claim_for_processing
        original_save = repo.save_sentiment_results

        def claim_spy(video_id, comment_ids):
            claimed = original_claim(video_id, comment_ids)
            calls.append(("claim", tuple(comment_ids)))
            # Mid-run: the claim step really moved the row to PROCESSING.
            assert repo.get_comments(VIDEO)[0]["processing_status"] == "PROCESSING"
            return claimed

        def save_spy(video_id, results, batch_size):
            calls.append(("save", tuple(r.target_status for r in results)))
            return original_save(video_id, results, batch_size)

        monkeypatch.setattr(repo, "claim_for_processing", claim_spy)
        monkeypatch.setattr(repo, "save_sentiment_results", save_spy)
        service.get_analysis(VIDEO)

        # READY -> PROCESSING (claim) -> PROCESSED (persist), in that order.
        assert calls[0] == ("claim", ("c1",))
        assert calls[1] == ("save", ("PROCESSED",))
        assert repo.get_comments(VIDEO)[0]["processing_status"] == "PROCESSED"

    def test_idempotent_rerun_reuses_results(self, repo, service):
        _seed(repo, [comment_row("c1", text=POSITIVE_TEXT), comment_row("c2", text=NEGATIVE_TEXT)])
        first = service.get_analysis(VIDEO)
        stamp = repo.get_comments(VIDEO)[0]["sentiment_processed_at"]

        second = service.get_analysis(VIDEO)
        assert second.model_dump(by_alias=True) == first.model_dump(by_alias=True)
        # Nothing was reprocessed: same persisted timestamp, same statuses.
        assert repo.get_comments(VIDEO)[0]["sentiment_processed_at"] == stamp
        assert repo.get_status_counts(VIDEO) == {"PROCESSED": 2}

    def test_unsupported_language_recorded_not_mislabeled(self, repo, service):
        _seed(repo, [
            comment_row("c1", text=POSITIVE_TEXT),
            comment_row("c2", text="bahut achha video hai", language="hi"),
        ])
        analysis = service.get_analysis(VIDEO)
        assert analysis.status == "PROCESSED"
        assert analysis.stats.analyzed == 1
        assert analysis.stats.skipped == 1
        assert analysis.stats.neutral == 0  # never folded into NEUTRAL

        by_id = {r["comment_id"]: r for r in repo.get_comments(VIDEO)}
        assert by_id["c2"]["sentiment_label"] == "UNSUPPORTED_LANGUAGE"
        assert by_id["c2"]["sentiment_score"] is None
        assert by_id["c2"]["processing_status"] == "PROCESSED"

    def test_content_change_resets_and_reprocesses(self, repo, service):
        _seed(repo, [comment_row("c1", text=POSITIVE_TEXT)])
        service.get_analysis(VIDEO)
        assert repo.get_comments(VIDEO)[0]["sentiment_label"] == "POSITIVE"

        # Upstream edit: Sprint 3 resets status AND clears the stale verdict.
        _seed(repo, [comment_row("c1", text=NEGATIVE_TEXT)])
        row = repo.get_comments(VIDEO)[0]
        assert row["processing_status"] == "READY_FOR_ANALYSIS"
        assert row["sentiment_label"] is None

        analysis = service.get_analysis(VIDEO)
        assert analysis.status == "PROCESSED"
        assert repo.get_comments(VIDEO)[0]["sentiment_label"] == "NEGATIVE"

    def test_per_row_failure_does_not_destroy_run(self, repo, service, monkeypatch):
        _seed(repo, [
            comment_row("c1", text=POSITIVE_TEXT),
            comment_row("c2", text="this one explodes"),
            comment_row("c3", text=NEGATIVE_TEXT),
        ])
        original = sentiment_module.classify

        def flaky(text):
            if "explodes" in text:
                raise RuntimeError("engine boom")
            return original(text)

        monkeypatch.setattr(sentiment_module, "classify", flaky)
        analysis = service.get_analysis(VIDEO)
        # 2 of 3 survived: run completed, failure recorded on its own row.
        assert analysis.status == "PROCESSED"
        assert analysis.stats.analyzed == 2
        statuses = {r["comment_id"]: r["processing_status"] for r in repo.get_comments(VIDEO)}
        assert statuses["c2"] == "FAILED"
        assert statuses["c1"] == "PROCESSED" and statuses["c3"] == "PROCESSED"
        assert repo.get_comments(VIDEO)[1]["sentiment_label"] is None

    def test_failed_rows_requeued_and_reprocessed(self, repo, service, monkeypatch):
        _seed(repo, [comment_row("c1", text=POSITIVE_TEXT)])

        def down(_text):
            raise RuntimeError("engine offline")

        monkeypatch.setattr(sentiment_module, "classify", down)
        first = service.get_analysis(VIDEO)
        assert first.status == "FAILED"
        assert repo.get_status_counts(VIDEO) == {"FAILED": 1}

        # Engine recovers -> FAILED is requeued (FAILED -> READY) and retried.
        monkeypatch.undo()
        second = service.get_analysis(VIDEO)
        assert second.status == "PROCESSED"
        assert second.stats.analyzed == 1
        assert repo.get_status_counts(VIDEO) == {"PROCESSED": 1}

    def test_orphaned_processing_rows_recovered(self, repo, service):
        # A crashed run can leave claimed rows; a later run must reclaim them
        # through the legal path (PROCESSING -> FAILED -> READY).
        _seed(repo, [comment_row("c1", text=POSITIVE_TEXT), comment_row("c2", text=NEGATIVE_TEXT)])
        claimed = repo.claim_for_processing(VIDEO, ["c1", "c2"])
        assert claimed == {"c1", "c2"}

        analysis = service.get_analysis(VIDEO)
        assert analysis.status == "PROCESSED"
        assert analysis.stats.analyzed == 2

    def test_processing_reported_while_run_active(self, repo, service):
        _seed(repo, [comment_row("c1", text=POSITIVE_TEXT)])
        # Simulate the in-process run window: lock held + video marked active
        # (exactly the state get_analysis itself creates while processing).
        assert service._run_lock.acquire(blocking=False)
        try:
            service._active_video = VIDEO
            analysis = service.get_analysis(VIDEO)
        finally:
            service._active_video = None
            service._run_lock.release()
        assert analysis.status == "PROCESSING"
        assert analysis.stats.analyzed == 0
        assert analysis.dominant_sentiment is None

    def test_video_isolation(self, repo, service):
        seed_video(repo, OTHER_VIDEO)
        _seed(repo, [
            comment_row("a1", text=POSITIVE_TEXT),
            comment_row("b1", text=NEGATIVE_TEXT, video_id=OTHER_VIDEO),
        ])
        analysis_a = service.get_analysis(VIDEO)
        analysis_b = service.get_analysis(OTHER_VIDEO)
        assert analysis_a.stats.positive == 1 and analysis_a.stats.negative == 0
        assert analysis_b.stats.negative == 1 and analysis_b.stats.positive == 0
        assert analysis_a.stats.total_comments == 1
        assert analysis_b.stats.total_comments == 1

    def test_untracked_video_returns_none(self, repo, service):
        assert service.get_analysis("nope0000000") is None

    def test_empty_dataset_not_analyzed_with_null_dominant(self, repo, service):
        analysis = service.get_analysis(VIDEO)  # video exists, zero comments
        assert analysis.status == "NOT_ANALYZED"
        assert analysis.stats.total_comments == 0
        assert analysis.stats.analyzed == 0
        assert analysis.dominant_sentiment is None

    def test_processing_scales_through_configured_batches(self, repo, service, monkeypatch):
        # §34: reads/persists go through comment_batch_size chunks - spy on
        # the repository write path to prove no giant single write happens.
        rows = [
            comment_row(f"c{i}", text=POSITIVE_TEXT if i % 2 == 0 else NEGATIVE_TEXT)
            for i in range(25)
        ]
        _seed(repo, rows)

        sizes: List[int] = []
        original = repo.save_sentiment_results

        def spy(video_id, results, batch_size):
            sizes.append(len(results))
            return original(video_id, results, batch_size)

        monkeypatch.setattr(repo, "save_sentiment_results", spy)
        analysis = service.get_analysis(VIDEO)

        assert analysis.stats.analyzed == 25
        assert len(sizes) >= 13          # 25 rows in chunks of <= 2
        assert all(size <= 2 for size in sizes)
        assert sum(sizes) == 25          # every row persisted exactly once

    def test_run_logs_safe_structured_fields_only(self, repo, service, caplog):
        secret = "extremely rare opinion about the video"
        _seed(repo, [comment_row("c1", text=POSITIVE_TEXT), comment_row("c2", text=secret)])
        with caplog.at_level("INFO"):
            service.get_analysis(VIDEO)
        completed = [r for r in caplog.records if r.message == "SENTIMENT_RUN_COMPLETED"]
        assert completed, "run summary must be logged"
        record = completed[-1]
        assert record.elapsed_ms >= 0
        assert record.comments_per_second is not None  # measured, not fabricated
        assert record.comments_per_second > 0
        assert record.failed == 0
        # Security: no raw comment text ever reaches the logs.
        assert secret not in caplog.text
        assert POSITIVE_TEXT not in caplog.text


# ---------------------------------------------------------------------------
# §31: GET /api/videos/{video_id}/sentiment
# ---------------------------------------------------------------------------


def _fake_with_comments(
    comments: List[tuple], video_id: str = VIDEO
) -> FakeYouTubeClient:
    """Fake acquisition source for one video (fixture data, not real YouTube)."""
    return FakeYouTubeClient(
        video=YtVideoListResponse.model_validate(video_payload(video_id)),
        pages={None: ([thread_payload(cid, text) for cid, text in comments], None)},
    )


def _acquire(client: TestClient, video_id: str = VIDEO) -> None:
    response = client.get(f"/api/videos/{video_id}")
    assert response.status_code == 200


class TestSentimentEndpoint:
    def test_invalid_video_id_rejected(self, app_factory):
        client = app_factory(FakeYouTubeClient())
        response = client.get("/api/videos/bad")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_video_id"

    def test_no_dataset_returns_404(self, app_factory):
        client = app_factory(FakeYouTubeClient())
        response = client.get("/api/videos/aaaaaaaaaaa")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "video_not_found"

    def test_full_flow_processes_and_reports(self, app_factory):
        client = app_factory(
            _fake_with_comments([
                ("c1", POSITIVE_TEXT),
                ("c2", POSITIVE_TEXT),
                ("c3", POSITIVE_TEXT),
                ("c4", NEGATIVE_TEXT),
                ("c5", NEUTRAL_TEXT),
            ])
        )
        _acquire(client)
        response = client.get(f"/api/videos/{VIDEO}/sentiment")
        assert response.status_code == 200
        body = response.json()
        assert body["videoId"] == VIDEO
        assert body["status"] == "PROCESSED"
        stats = body["stats"]
        assert stats["totalComments"] == 5
        assert stats["analyzed"] == 5
        assert stats["positive"] == 3
        assert stats["negative"] == 1
        assert stats["neutral"] == 1
        assert stats["positivePercent"] + stats["neutralPercent"] + stats["negativePercent"] == 100
        assert stats["positivePercent"] == 60
        assert body["dominantSentiment"] == "POSITIVE"

    def test_existing_video_contract_untouched_alongside_sentiment(self, app_factory):
        client = app_factory(_fake_with_comments([("c1", POSITIVE_TEXT)]))
        _acquire(client)
        assert client.get(f"/api/videos/{VIDEO}/sentiment").status_code == 200
        # The original endpoint still answers with its original contract.
        video = client.get(f"/api/videos/{VIDEO}")
        assert video.status_code == 200
        assert video.json()["video"]["videoId"] == VIDEO
        assert "comments" in video.json() and "source" in video.json()
        assert "stats" not in video.json() and "dominantSentiment" not in video.json()

    def test_empty_dataset_state(self, app_factory):
        client = app_factory(_fake_with_comments([]))  # no accessible comments
        _acquire(client)
        response = client.get(f"/api/videos/{VIDEO}/sentiment")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "NOT_ANALYZED"
        assert body["stats"]["totalComments"] == 0
        assert body["stats"]["analyzed"] == 0
        assert body["dominantSentiment"] is None

    def test_processing_state_visible_while_run_active(self, app_factory):
        client = app_factory(_fake_with_comments([("c1", POSITIVE_TEXT)]))
        _acquire(client)
        service = client.app.state.sentiment_service
        assert service._run_lock.acquire(blocking=False)
        try:
            service._active_video = VIDEO
            body = client.get(f"/api/videos/{VIDEO}/sentiment").json()
        finally:
            service._active_video = None
            service._run_lock.release()
        assert body["status"] == "PROCESSING"
        assert body["stats"]["analyzed"] == 0
        assert body["dominantSentiment"] is None

    def test_failed_state_when_engine_unavailable(self, app_factory, monkeypatch):
        client = app_factory(_fake_with_comments([("c1", POSITIVE_TEXT)]))
        _acquire(client)

        def down(_text):
            raise RuntimeError("engine offline")

        monkeypatch.setattr(sentiment_module, "classify", down)
        body = client.get(f"/api/videos/{VIDEO}/sentiment").json()
        assert body["status"] == "FAILED"
        assert body["stats"]["analyzed"] == 0
        assert body["dominantSentiment"] is None
        # Failure path never leaks internals in stats (no fabricated counts).
        assert body["stats"]["positive"] == 0

    def test_not_analyzed_then_processed_on_retry(self, app_factory, monkeypatch):
        client = app_factory(_fake_with_comments([("c1", POSITIVE_TEXT)]))
        _acquire(client)

        def down(_text):
            raise RuntimeError("engine offline")

        monkeypatch.setattr(sentiment_module, "classify", down)
        assert client.get(f"/api/videos/{VIDEO}/sentiment").json()["status"] == "FAILED"

        monkeypatch.undo()  # engine recovers -> next GET requeues + processes
        body = client.get(f"/api/videos/{VIDEO}/sentiment").json()
        assert body["status"] == "PROCESSED"
        assert body["stats"]["analyzed"] == 1
        assert body["dominantSentiment"] == "POSITIVE"

    def test_video_isolation_through_api(self, app_factory):
        """Sprint 4.2 §27: after switching, ONLY the current video's
        sentiment exists - Video A's aggregates never influence B, and A's
        dataset (incl. verdicts) was removed with the switch."""
        fake = _fake_with_comments([("a1", POSITIVE_TEXT)], video_id=VIDEO)
        client = app_factory(fake)
        _acquire(client, VIDEO)
        stats_a = client.get(f"/api/videos/{VIDEO}/sentiment").json()["stats"]
        assert stats_a["positive"] == 1 and stats_a["negative"] == 0

        # Second video on the same app: different metadata + comment ids.
        fake.video = YtVideoListResponse.model_validate(video_payload(OTHER_VIDEO))
        fake.pages = {None: ([thread_payload("b1", NEGATIVE_TEXT)], None)}
        _acquire(client, OTHER_VIDEO)

        # Video A's working dataset is gone - no stale analysis to leak.
        gone_a = client.get(f"/api/videos/{VIDEO}/sentiment")
        assert gone_a.status_code == 404
        assert gone_a.json()["error"]["code"] == "video_not_found"

        # Video B reports exactly B's verdicts (positive from A = 0).
        body_b = client.get(f"/api/videos/{OTHER_VIDEO}/sentiment").json()
        stats_b = body_b["stats"]
        assert stats_b["negative"] == 1 and stats_b["positive"] == 0
        assert stats_b["totalComments"] == 1
        assert body_b["dataset"]["stored"] == 1
        assert body_b["dominantSentiment"] == "NEGATIVE"

    def test_rerun_returns_identical_aggregates(self, app_factory):
        client = app_factory(
            _fake_with_comments([("c1", POSITIVE_TEXT), ("c2", NEGATIVE_TEXT)])
        )
        _acquire(client)
        first = client.get(f"/api/videos/{VIDEO}/sentiment").json()
        second = client.get(f"/api/videos/{VIDEO}/sentiment").json()
        assert first == second  # idempotent: no recompute drift


# ---------------------------------------------------------------------------
# Sprint 4.1 §22: dataset metrics (collected/stored/analyzed/skipped/failed
# + truthful hasMore/limitReached) and denominator reconciliation
# ---------------------------------------------------------------------------

HINDI_TEXT = "यह वीडियो बहुत अच्छा है और मैंने इसे दोबारा देखा"


class TestDatasetMetrics:
    def test_dataset_block_reconciles_with_stats(self, app_factory):
        client = app_factory(
            _fake_with_comments(
                [
                    ("c1", POSITIVE_TEXT),
                    ("c2", NEGATIVE_TEXT),
                    ("c3", NEUTRAL_TEXT),
                    ("c4", HINDI_TEXT),  # unsupported language -> skipped
                ]
            )
        )
        _acquire(client)
        body = client.get(f"/api/videos/{VIDEO}/sentiment").json()

        dataset = body["dataset"]
        assert dataset == {
            "collected": 4,
            "stored": 4,
            "analyzed": 3,
            "skipped": 1,
            "failed": 0,
            "hasMore": False,       # YouTube ran out - nothing claimed
            "limitReached": False,  # configured limit never hit
        }
        # The reconciliation identity the UI must satisfy (§19/§41):
        assert dataset["analyzed"] + dataset["skipped"] == dataset["stored"]
        stats = body["stats"]
        assert stats["analyzed"] == dataset["analyzed"]
        assert stats["skipped"] == dataset["skipped"]
        assert stats["totalComments"] == dataset["stored"]
        # Denominator: percentages use ANALYZED (3), never collected (4).
        assert stats["positive"] + stats["neutral"] + stats["negative"] == 3

    def test_more_available_without_limit_claim(self, app_factory):
        # The API safety bound (MAX_API_PAGES) stops acquisition while a
        # next page is still offered and the configured limit (default
        # 5000) was NOT reached - `limitReached` must stay False (§14).
        fake = FakeYouTubeClient(
            video=YtVideoListResponse.model_validate(video_payload()),
            pages={None: ([thread_payload("c1", POSITIVE_TEXT)], "P2")},
        )
        client = app_factory(fake, max_api_pages=1)
        _acquire(client)
        video = client.get(f"/api/videos/{VIDEO}").json()
        assert video["comments"]["hasMore"] is True
        dataset = client.get(f"/api/videos/{VIDEO}/sentiment").json()["dataset"]
        assert dataset["hasMore"] is True
        assert dataset["limitReached"] is False

    def test_limit_reached_only_when_at_configured_maximum(self, app_factory):
        fake = FakeYouTubeClient(
            video=YtVideoListResponse.model_validate(video_payload()),
            pages={
                None: (
                    [
                        thread_payload("c1", POSITIVE_TEXT),
                        thread_payload("c2", NEGATIVE_TEXT),
                        thread_payload("c3", NEUTRAL_TEXT),
                    ],
                    "P2",
                )
            },
        )
        client = app_factory(fake, comment_acquisition_max_comments=3)
        _acquire(client)
        dataset = client.get(f"/api/videos/{VIDEO}/sentiment").json()["dataset"]
        assert dataset["stored"] == 3
        assert dataset["hasMore"] is True       # P2 still offered
        assert dataset["limitReached"] is True  # and we were at the limit

    def test_empty_dataset_metrics_are_all_zero(self, app_factory):
        client = app_factory(_fake_with_comments([]))
        _acquire(client)
        body = client.get(f"/api/videos/{VIDEO}/sentiment").json()
        assert body["dataset"] == {
            "collected": 0,
            "stored": 0,
            "analyzed": 0,
            "skipped": 0,
            "failed": 0,
            "hasMore": False,
            "limitReached": False,
        }

    def test_failed_rows_reported_as_failed_not_analyzed(self, app_factory, monkeypatch):
        client = app_factory(_fake_with_comments([("c1", POSITIVE_TEXT)]))
        _acquire(client)

        def down(_text):
            raise RuntimeError("engine offline")

        monkeypatch.setattr(sentiment_module, "classify", down)
        body = client.get(f"/api/videos/{VIDEO}/sentiment").json()
        assert body["status"] == "FAILED"
        dataset = body["dataset"]
        assert dataset["stored"] == 1
        assert dataset["analyzed"] == 0
        assert dataset["skipped"] == 0
        assert dataset["failed"] == 1
        # Every stored row is accounted for - nothing silently disappears.
        assert dataset["analyzed"] + dataset["skipped"] + dataset["failed"] == dataset["stored"]


# ---------------------------------------------------------------------------
# Sprint 5.1 (§3/§4/§9): model reuse, inference batching, first insight
# ---------------------------------------------------------------------------


class TestEmotionEngineReuse:
    """§3: the emotion engine is constructed exactly once (process-wide
    singleton) and never leaks one comment's evidence into the next."""

    def test_engine_is_constructed_once_across_many_calls(self, monkeypatch):
        constructions: list = []

        class CountingEngine:
            def __init__(self):
                constructions.append(1)
                self.tokens: list = []

            def load_token_list(self, token_list):
                self.tokens = list(token_list)  # full state reset, like NRCLex

            @property
            def raw_emotion_scores(self):
                return {}

        monkeypatch.setattr(sentiment_module, "NRCLex", CountingEngine)
        monkeypatch.setattr(sentiment_module, "_emotion_engine", None)

        for text in ("joyful happy day", "angry terrible mess", "joyful happy day"):
            sentiment_module.classify_emotion(text)

        # Loaded once, reused for every comment (NRCLex ctor ~267us/comment
        # in the baseline benchmark - that cost must be paid exactly once).
        assert len(constructions) == 1

    def test_reused_engine_does_not_leak_state_between_comments(self):
        first = sentiment_module.classify_emotion("so happy joyful and delighted")
        other = sentiment_module.classify_emotion("terrifying awful nightmare")
        again = sentiment_module.classify_emotion("so happy joyful and delighted")

        assert first == again  # deterministic: no carried-over evidence
        assert other.label != first.label  # different words -> different emotion


class TestInferenceBatching:
    """§4: SENTIMENT_INFERENCE_BATCH_SIZE controls the claim -> classify ->
    persist chunk; `0` (default) follows COMMENT_BATCH_SIZE."""

    @staticmethod
    def _spy_sizes(repo, monkeypatch) -> list:
        saved_sizes: list = []
        original = repo.save_sentiment_results

        def save_spy(video_id, results, batch_size):
            saved_sizes.append(len(results))
            return original(video_id, results, batch_size)

        monkeypatch.setattr(repo, "save_sentiment_results", save_spy)
        return saved_sizes

    def test_inference_batch_size_chunks_claim_and_persist(self, repo, monkeypatch):
        _seed(repo, [comment_row(f"c{i}", text=POSITIVE_TEXT) for i in range(7)])
        service = SentimentService(
            repo,
            make_settings(comment_batch_size=100, sentiment_inference_batch_size=3),
        )
        saved_sizes = self._spy_sizes(repo, monkeypatch)

        summary = service.process_pending(VIDEO, None)

        assert summary.processed == 7
        assert saved_sizes == [3, 3, 1]  # 7 rows in chunks of 3

    def test_default_zero_follows_comment_batch_size(self, repo, monkeypatch):
        _seed(repo, [comment_row(f"c{i}", text=POSITIVE_TEXT) for i in range(5)])
        service = SentimentService(repo, make_settings(comment_batch_size=2))
        saved_sizes = self._spy_sizes(repo, monkeypatch)

        summary = service.process_pending(VIDEO, None)

        assert summary.processed == 5
        assert saved_sizes == [2, 2, 1]  # inference batch falls back (0 -> 2)

    def test_negative_inference_batch_is_rejected(self):
        with pytest.raises(ValueError):
            make_settings(sentiment_inference_batch_size=-1)


class TestFirstInsightLog:
    """§9: time_to_first_insight is logged once, from the real clock, when
    the first polarity verdicts of a run are persisted - never estimated."""

    def test_logged_once_with_real_elapsed_time(self, repo, caplog):
        _seed(repo, [comment_row(f"c{i}", text=POSITIVE_TEXT) for i in range(5)])
        service = SentimentService(repo, make_settings(comment_batch_size=2))

        with caplog.at_level("INFO"):
            service.process_pending(VIDEO, None)

        events = [r for r in caplog.records if r.message == "SENTIMENT_FIRST_INSIGHT"]
        assert len(events) == 1  # exactly once per run, even with batches of 2
        assert events[0].analyzed > 0
        assert events[0].elapsed_ms >= 0
        assert events[0].video_id == VIDEO


class TestJobAwareFastRead:
    """Sprint 5.1 §8/§20: while a background job owns the dataset, GET
    /sentiment is a FAST READ - it never runs an inline drain (the job plus
    its interim per-page drain own the work) and it reports PROCESSING for
    the whole job lifetime, so partial aggregates can never read as final."""

    def test_active_job_blocks_inline_processing_and_reports_processing(self, repo):
        _seed(repo, [
            comment_row("c1", text=POSITIVE_TEXT),
            comment_row("c2", text=NEGATIVE_TEXT),
            comment_row("c3", text=NEUTRAL_TEXT),
        ])
        repo.create_job("job-active", VIDEO, generation=1)
        service = SentimentService(repo, make_settings(comment_batch_size=2))

        analysis = service.get_analysis(VIDEO)

        assert analysis.status == "PROCESSING"  # honest in-progress (§8)
        assert analysis.stats.analyzed == 0     # nothing drained inline
        # Read-only proof: every row is still READY for the job to process.
        assert repo.get_status_counts(VIDEO) == {"READY_FOR_ANALYSIS": 3}

    def test_read_only_gates_off_once_the_job_is_terminal(self, repo):
        _seed(repo, [
            comment_row("c1", text=POSITIVE_TEXT),
            comment_row("c2", text=NEGATIVE_TEXT),
            comment_row("c3", text=NEUTRAL_TEXT),
        ])
        repo.create_job("job-done", VIDEO, generation=1)
        repo.finish_job("job-done", "COMPLETED", phase="COMPLETE")
        service = SentimentService(repo, make_settings(comment_batch_size=2))

        analysis = service.get_analysis(VIDEO)

        # No active job -> the legacy backend-owned trigger still runs
        # inline on the GET (Sprint 4 contract preserved).
        assert analysis.status == "PROCESSED"
        assert analysis.stats.analyzed == 3


class TestEmotionDirectParity:
    """Sprint 5.2 §4/§15: direct emotion-lexicon counting must be
    byte-identical to the stock nrclex `load_token_list` path it replaces
    (same bundled lexicon, same per-label hit counts)."""

    TEXTS = [
        "so happy joyful and delighted, what a great day!",
        "terrifying awful nightmare, I fear the worst",
        "anticipation and trust built up beautifully",
        "sadness disgust anger all at once, ugh",
        "neutral words only about the topic here",
        "",  # zero-hit paths
        "!!! ??? 123",
        "joy joy joy sadness",  # duplicate + mixed labels
        "Este video es increible",  # foreign tokens simply miss the lexicon
    ]

    def test_direct_counts_match_stock_load_token_list(self):
        engine = sentiment_module._get_emotion_engine()
        lexicon = getattr(engine, "__lexicon__", None)
        assert lexicon is not None, "direct path feature detection failed"

        for text in self.TEXTS:
            tokens = sentiment_module._TOKEN_RE.findall(text.lower())
            if not tokens:
                continue
            direct = {}
            for token in tokens:
                labels = lexicon.get(token)
                if labels:
                    for label in labels:
                        direct[label] = direct.get(label, 0) + 1
            engine.load_token_list(tokens)
            assert direct == dict(engine.raw_emotion_scores), text

    def test_verdicts_identical_with_flag_on_and_off(self, monkeypatch):
        direct_on = [
            sentiment_module.classify_emotion(text) for text in self.TEXTS
        ]
        monkeypatch.setattr(sentiment_module, "_EMOTION_DIRECT", False)
        direct_off = [
            sentiment_module.classify_emotion(text) for text in self.TEXTS
        ]
        assert direct_on == direct_off

    def test_falls_back_to_stock_path_when_lexicon_attribute_missing(
        self, monkeypatch
    ):
        class StubEngine:
            def __init__(self):
                self.calls: List[list] = []

            def load_token_list(self, token_list):
                self.calls.append(list(token_list))

            @property
            def raw_emotion_scores(self):
                return {"joy": 1}

        stub = StubEngine()
        monkeypatch.setattr(sentiment_module, "_emotion_engine", stub)

        verdict = sentiment_module.classify_emotion("joyful happy day")

        assert stub.calls, "feature detection must fall back to load_token_list"
        assert verdict.label is sentiment_module.EmotionLabel.JOY


class TestProgressUpdateIntervalConfig:
    """§10: ANALYSIS_PROGRESS_UPDATE_INTERVAL is configurable through the
    project's existing Settings style and strictly validated."""

    def test_default_is_500(self):
        assert make_settings().analysis_progress_update_interval == 500

    def test_zero_is_rejected(self):
        with pytest.raises(ValueError):
            make_settings(analysis_progress_update_interval=0)

    def test_above_cap_is_rejected(self):
        with pytest.raises(ValueError):
            make_settings(analysis_progress_update_interval=10001)

    def test_one_drains_every_page(self):
        # interval=1 restores the pre-5.2 drain-after-every-page behavior.
        assert (
            make_settings(analysis_progress_update_interval=1)
            .analysis_progress_update_interval
            == 1
        )

"""Sprint 5 tests: audience intelligence (spec §25 + §29).

Coverage map (spec §25):
- Emotion: real NRC lexicon inference (joy/disgust/... from actual text),
  zero-hit -> NEUTRAL, polarity-only words are NOT emotions, tokenizer
  robustness, deterministic tie-breaks, invalid/empty input.
- Intensity: LOW/MEDIUM/HIGH + exact boundary conditions + negatives.
- Confidence: average over analyzed rows, unavailable (None) cases.
- Aggregation: correct denominators, percentages sum to 100 (9 buckets),
  dominant tie-breaks, empty + partial datasets, skipped/failed excluded.
- Audience mood: every documented rule branch + insufficient sample.
- Async/batching: emotion rides the existing batched run; per-row and
  model-level inference failures never destroy the run.
- Active video: emotion/intensity aggregates are per-video (no leakage).
- Migration: one-time reopen of pre-Sprint-5 PROCESSED rows, idempotent.
- API: new blocks with camelCase contract, old contract untouched.

No test fabricates intelligence: every emotion comes from the real NRC
lexicon over fixture text, every intensity/confidence/mood value from the
documented deterministic rules over those real outputs.
"""
import re
import sqlite3
from typing import List

import pytest

from app.db.connection import Database
from app.db.repository import DatasetRepository
from app.db.schema import migrate
from app.models.processing import ProcessingStatus
from app.services import sentiment as sentiment_module
from app.services.sentiment import (
    AUDIENCE_MOOD_MIN_SAMPLE,
    EMOTION_CATEGORIES,
    EMOTION_MODEL_ID,
    EMOTION_PRIORITY,
    NEUTRAL_EMOTION,
    SentimentService,
    audience_mood,
    classify,
    classify_emotion,
    dominant_label,
    intensity_for,
    label_distribution,
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

ALL_EMOTIONS = set(EMOTION_CATEGORIES) | {NEUTRAL_EMOTION}
INTENSITY_BANDS = {"LOW", "MEDIUM", "HIGH"}


# ---------------------------------------------------------------------------
# §25 Emotion: real model inference
# ---------------------------------------------------------------------------


class TestEmotionEngine:
    def test_positive_fixture_text_yields_real_joy(self):
        verdict = classify_emotion(POSITIVE_TEXT)
        assert verdict.label.value == "JOY"
        assert 0.0 < verdict.score <= 1.0

    def test_negative_fixture_text_yields_real_disgust(self):
        verdict = classify_emotion("I hate this garbage, awful and painful")
        assert verdict.label.value == "DISGUST"
        assert 0.0 < verdict.score <= 1.0

    def test_text_without_emotion_words_is_neutral_not_fabricated(self):
        # Real English with no lexicon emotion hit: an honest zero-hit
        # outcome, never an invented emotion.
        verdict = classify_emotion(NEUTRAL_TEXT)
        assert verdict.label.value == NEUTRAL_EMOTION
        assert verdict.score == 0.0

    def test_unmatchable_tokens_never_invent_emotion(self):
        verdict = classify_emotion("xq zvt 42 qqq")
        assert verdict.label.value == NEUTRAL_EMOTION
        assert verdict.score == 0.0

    def test_empty_and_none_text_safe(self):
        for text in ("", "   ", None):
            verdict = classify_emotion(text)  # type: ignore[arg-type]
            assert verdict.label.value == NEUTRAL_EMOTION
            assert verdict.score == 0.0

    def test_polarity_only_words_are_not_emotions(self):
        # 'ability' -> ['positive'] and 'aberrant' -> ['negative'] in the
        # lexicon: polarity lives on the sentiment axis, so the emotion
        # axis must report the honest zero-hit NEUTRAL.
        for word in ("ability", "aberrant"):
            verdict = classify_emotion(word)
            assert verdict.label.value == NEUTRAL_EMOTION, word
            assert verdict.score == 0.0

    def test_tokenizer_normalizes_case_and_punctuation(self):
        assert classify_emotion("LOVE!!!").label.value == classify_emotion(
            "love"
        ).label.value

    def test_single_evidence_word_scores_full_share(self):
        verdict = classify_emotion("I love this so much")
        assert verdict.label.value == "JOY"
        assert verdict.score == 1.0

    def test_tie_break_follows_canonical_emotion_priority(self):
        # 'happy' maps to anticipation/joy/positive/trust -> after the
        # polarity exclusion three categories tie at 1; the first label in
        # EMOTION_PRIORITY (lexicon canonical order) must win, every time.
        results = {classify_emotion("happy").label.value for _ in range(3)}
        assert results == {"ANTICIPATION"}
        tied = [c for c in EMOTION_CATEGORIES if c in ("ANTICIPATION", "JOY", "TRUST")]
        assert tied[0] == "ANTICIPATION"  # the documented order decides

    def test_deterministic_for_identical_input(self):
        first = classify_emotion(POSITIVE_TEXT)
        second = classify_emotion(POSITIVE_TEXT)
        assert first == second

    def test_model_identity_is_recorded(self):
        assert EMOTION_MODEL_ID.startswith("nrclex")


# ---------------------------------------------------------------------------
# §25 Intensity: bands + exact boundaries
# ---------------------------------------------------------------------------


class TestIntensity:
    @pytest.mark.parametrize(
        "score,expected",
        [
            (0.0, "LOW"),
            (0.04, "LOW"),       # VADER neutral zone -> LOW by construction
            (0.34, "LOW"),
            (0.35, "MEDIUM"),    # closed-low boundary lands UPPER
            (0.69, "MEDIUM"),
            (0.7, "HIGH"),       # closed-low boundary lands UPPER
            (1.0, "HIGH"),
            (-0.34, "LOW"),      # bands are on |compound|
            (-0.35, "MEDIUM"),
            (-0.7, "HIGH"),
            (-1.0, "HIGH"),
        ],
    )
    def test_boundary_conditions(self, score, expected):
        assert intensity_for(score).value == expected

    def test_intensity_follows_the_real_sentiment_score(self):
        positive = classify(POSITIVE_TEXT)
        neutral = classify(NEUTRAL_TEXT)
        assert intensity_for(positive.score).value == "HIGH"
        assert intensity_for(neutral.score).value == "LOW"

    def test_bands_are_exhaustive_for_the_documented_range(self):
        for i in range(-100, 101):
            assert intensity_for(i / 100.0).value in INTENSITY_BANDS


# ---------------------------------------------------------------------------
# §25 Aggregation: denominators, rounding, dominant, mood
# ---------------------------------------------------------------------------


class TestDistributionHelpers:
    def test_emotion_distribution_sums_to_exactly_100(self):
        counts = {label: count for label, count in zip(EMOTION_PRIORITY, [5, 3, 0, 0, 2, 0, 0, 0, 7])}
        percentages = label_distribution(counts, EMOTION_PRIORITY)
        assert list(percentages) == list(EMOTION_PRIORITY)  # stable order
        assert sum(percentages.values()) == pytest.approx(100.0)
        assert percentages["ANTICIPATION"] == 0.0  # absent label still present

    def test_empty_distribution_is_all_zero_not_nan(self):
        percentages = label_distribution({}, EMOTION_PRIORITY)
        assert set(percentages.values()) == {0.0}

    def test_single_bucket_takes_everything(self):
        percentages = label_distribution({"JOY": 9}, EMOTION_PRIORITY)
        assert percentages["JOY"] == 100.0

    def test_dominant_label_ties_break_by_priority(self):
        counts = {"JOY": 4, "FEAR": 4, "NEUTRAL": 2}
        assert dominant_label(counts, EMOTION_PRIORITY) == "FEAR"  # FEAR first

    def test_dominant_label_none_when_empty(self):
        assert dominant_label({}, EMOTION_PRIORITY) is None
        assert dominant_label({"JOY": 0, "NEUTRAL": 0}, EMOTION_PRIORITY) is None


class TestAudienceMoodRules:
    """Every branch of the documented audience_mood rules (§12)."""

    def _mood(self, analyzed, pos, neg, emotions, intensity):
        return audience_mood(analyzed, pos, neg, emotions, intensity)

    def test_insufficient_sample_never_claims_a_mood(self):
        assert (
            self._mood(AUDIENCE_MOOD_MIN_SAMPLE - 1, 90.0, 0.0,
                       {"JOY": 9}, {"HIGH": 9})
            is None
        )

    def test_excited_via_high_intensity(self):
        mood = self._mood(100, 80.0, 10.0, {"NEUTRAL": 60, "JOY": 40},
                          {"LOW": 30, "MEDIUM": 30, "HIGH": 40})
        assert mood == "EXCITED"

    def test_excited_via_dominant_joy(self):
        mood = self._mood(100, 70.0, 10.0,
                          {"JOY": 40, "SADNESS": 30, "NEUTRAL": 30},
                          {"LOW": 40, "MEDIUM": 50, "HIGH": 10})
        assert mood == "EXCITED"

    def test_calm_via_low_intensity(self):
        mood = self._mood(100, 60.0, 10.0, {"NEUTRAL": 70, "SADNESS": 20, "JOY": 10},
                          {"LOW": 60, "MEDIUM": 30, "HIGH": 10})
        assert mood == "CALM"

    def test_positive_fallback(self):
        mood = self._mood(100, 60.0, 10.0, {"NEUTRAL": 70, "SADNESS": 20, "JOY": 10},
                          {"LOW": 40, "MEDIUM": 50, "HIGH": 10})
        assert mood == "POSITIVE"

    def test_negative_via_high_intensity(self):
        mood = self._mood(100, 10.0, 60.0, {"NEUTRAL": 70, "ANGER": 30},
                          {"LOW": 30, "MEDIUM": 35, "HIGH": 35})
        assert mood == "NEGATIVE"

    def test_negative_via_dominant_anger(self):
        # ANGER must actually DOMINATE (be the max count): 60 > 40.
        mood = self._mood(100, 10.0, 60.0, {"ANGER": 60, "NEUTRAL": 40},
                          {"LOW": 55, "MEDIUM": 35, "HIGH": 10})
        assert mood == "NEGATIVE"

    def test_concerned_when_negativity_is_not_intense_or_angry(self):
        # FEAR-dominant negativity (share >= 25%) but no anger/disgust and
        # no high intensity -> concerned, not negative.
        mood = self._mood(100, 10.0, 40.0, {"FEAR": 60, "NEUTRAL": 40},
                          {"LOW": 55, "MEDIUM": 35, "HIGH": 10})
        assert mood == "CONCERNED"

    def test_mixed_when_balance_is_within_the_margin(self):
        mood = self._mood(100, 40.0, 30.0, {"NEUTRAL": 80, "JOY": 20},
                          {"LOW": 50, "MEDIUM": 40, "HIGH": 10})
        assert mood == "MIXED"

    def test_neutral_dominant_emotion_never_triggers_emotion_branches(self):
        # Strong positive balance, joy present but NOT dominant: falls
        # through emotion branches to the intensity/positivity rules.
        mood = self._mood(100, 80.0, 10.0, {"NEUTRAL": 90, "JOY": 10},
                          {"LOW": 60, "MEDIUM": 30, "HIGH": 10})
        assert mood == "CALM"


# ---------------------------------------------------------------------------
# §25 Service integration: batches persist real intelligence
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
    return SentimentService(repo, make_settings(comment_batch_size=2))


def _seed(repo: DatasetRepository, rows: List[dict]) -> None:
    repo.upsert_comments(rows, batch_size=100)


class TestServiceIntelligence:
    def test_run_persists_emotion_intensity_and_model(self, repo, service):
        _seed(repo, [
            comment_row("c1", text=POSITIVE_TEXT),
            comment_row("c2", text=NEUTRAL_TEXT),
        ])
        analysis = service.get_analysis(VIDEO)
        assert analysis.status == "PROCESSED"

        by_id = {r["comment_id"]: r for r in repo.get_comments(VIDEO)}
        joy_row = by_id["c1"]
        assert joy_row["emotion_label"] == "JOY"
        assert joy_row["emotion_score"] > 0.0
        assert joy_row["emotion_model"] == EMOTION_MODEL_ID
        assert joy_row["sentiment_intensity"] in INTENSITY_BANDS
        # Neutral fixture text: real zero-hit outcome + LOW band.
        neutral_row = by_id["c2"]
        assert neutral_row["emotion_label"] == NEUTRAL_EMOTION
        assert neutral_row["emotion_score"] == 0.0
        assert neutral_row["sentiment_intensity"] == "LOW"

    def test_unsupported_language_rows_get_no_intelligence(self, repo, service):
        _seed(repo, [
            comment_row("c1", text=POSITIVE_TEXT),
            comment_row("c2", text="buen video muy bueno", language="es"),
        ])
        analysis = service.get_analysis(VIDEO)
        assert analysis.stats.analyzed == 1
        assert analysis.stats.skipped == 1

        by_id = {r["comment_id"]: r for r in repo.get_comments(VIDEO)}
        skipped = by_id["c2"]
        assert skipped["sentiment_label"] == "UNSUPPORTED_LANGUAGE"
        assert skipped["emotion_label"] is None
        assert skipped["emotion_score"] is None
        assert skipped["emotion_model"] is None
        assert skipped["sentiment_intensity"] is None
        # Skipped rows are never folded into the emotion denominator.
        assert sum(analysis.emotion.distribution[l].count for l in EMOTION_PRIORITY) == 1
        assert sum(analysis.intensity.distribution[l].count for l in ("LOW", "MEDIUM", "HIGH")) == 1

    def test_response_blocks_reconcile_with_analyzed(self, repo, service):
        _seed(repo, [
            comment_row("c1", text=POSITIVE_TEXT),
            comment_row("c2", text=POSITIVE_TEXT),
            comment_row("c3", text=NEGATIVE_TEXT),
            comment_row("c4", text=NEUTRAL_TEXT),
        ])
        analysis = service.get_analysis(VIDEO)
        analyzed = analysis.stats.analyzed
        assert analyzed == 4

        emotion_total = sum(
            analysis.emotion.distribution[label].count for label in EMOTION_PRIORITY
        )
        intensity_total = sum(
            analysis.intensity.distribution[label].count
            for label in ("LOW", "MEDIUM", "HIGH")
        )
        assert emotion_total == analyzed  # write-path invariant, §11
        assert intensity_total == analyzed
        emotion_pct = sum(
            analysis.emotion.distribution[label].percent for label in EMOTION_PRIORITY
        )
        assert emotion_pct == pytest.approx(100.0)

        assert analysis.emotion.dominant in ALL_EMOTIONS
        assert analysis.emotion.dominant_percent == analysis.emotion.distribution[
            analysis.emotion.dominant
        ].percent
        assert analysis.intensity.overall in INTENSITY_BANDS
        assert analysis.confidence.average is not None
        assert 0.0 <= analysis.confidence.average <= 1.0

    def test_confidence_average_is_the_mean_of_real_margins(self, repo, service):
        _seed(repo, [comment_row("c1", text=POSITIVE_TEXT)])
        service.get_analysis(VIDEO)
        row = repo.get_comments(VIDEO)[0]
        analysis = service.get_analysis(VIDEO)
        assert analysis.confidence.average == pytest.approx(
            round(row["sentiment_confidence"], 3)
        )

    def test_confidence_unavailable_without_analyzed_rows(self, repo, service):
        analysis = service.get_analysis(VIDEO)  # no rows at all
        assert analysis.confidence.average is None  # never a fabricated 0
        assert analysis.emotion.dominant is None
        assert analysis.intensity.overall is None

    def test_mood_null_below_min_sample(self, repo, service):
        _seed(repo, [comment_row(f"c{i}", text=POSITIVE_TEXT) for i in range(3)])
        analysis = service.get_analysis(VIDEO)
        assert analysis.stats.analyzed == 3
        assert analysis.audience_mood is None  # too small to claim a mood

    def test_mood_derived_from_real_aggregates_at_scale(self, repo, service):
        _seed(repo, [comment_row(f"c{i}", text=POSITIVE_TEXT) for i in range(12)])
        analysis = service.get_analysis(VIDEO)
        assert analysis.stats.analyzed == 12
        # Strong positive balance + HIGH intensity on every row -> EXCITED.
        assert analysis.audience_mood == "EXCITED"

    def test_per_row_emotion_failure_fails_only_that_row(self, repo, service, monkeypatch):
        _seed(repo, [
            comment_row("c1", text=POSITIVE_TEXT),
            comment_row("c2", text=NEGATIVE_TEXT),
            comment_row("c3", text=NEUTRAL_TEXT),
        ])
        original = sentiment_module.classify_emotion

        def flaky(text):
            if "worst video" in text:
                raise RuntimeError("simulated inference failure")
            return original(text)

        monkeypatch.setattr(sentiment_module, "classify_emotion", flaky)
        summary = service.process_pending(VIDEO)
        assert summary is not None
        assert summary.processed == 2
        assert summary.failed == 1  # one bad comment never kills the run

        by_id = {r["comment_id"]: r for r in repo.get_comments(VIDEO)}
        assert by_id["c2"]["processing_status"] == ProcessingStatus.FAILED.value
        assert by_id["c2"]["emotion_label"] is None
        assert by_id["c1"]["emotion_label"] is not None
        assert by_id["c3"]["emotion_label"] == NEUTRAL_EMOTION

    def test_model_level_failure_fails_rows_without_crashing(self, repo, service, monkeypatch):
        _seed(repo, [comment_row("c1", text=POSITIVE_TEXT), comment_row("c2", text=NEGATIVE_TEXT)])

        def broken(text):
            raise RuntimeError("emotion engine unavailable")

        monkeypatch.setattr(sentiment_module, "classify_emotion", broken)
        summary = service.process_pending(VIDEO)
        assert summary is not None
        assert summary.processed == 0
        assert summary.failed == 2
        # Retryable state, honest public status - no partial fabrication.
        analysis = service.get_analysis(VIDEO)
        assert analysis.status == "FAILED"
        assert analysis.emotion.dominant is None
        assert analysis.confidence.average is None

    def test_requeue_after_failure_reprocesses_intelligence(self, repo, service, monkeypatch):
        _seed(repo, [comment_row("c1", text=POSITIVE_TEXT)])

        def broken(text):
            raise RuntimeError("transient engine failure")

        monkeypatch.setattr(sentiment_module, "classify_emotion", broken)
        summary = service.process_pending(VIDEO)
        assert summary.failed == 1
        # Engine recovers -> FAILED row is requeued and fully re-analyzed.
        monkeypatch.setattr(sentiment_module, "classify_emotion", classify_emotion)
        summary = service.process_pending(VIDEO)
        assert summary.processed == 1
        row = repo.get_comments(VIDEO)[0]
        assert row["emotion_label"] == "JOY"
        assert row["sentiment_intensity"] in INTENSITY_BANDS

    def test_content_change_resets_sprint5_columns(self, repo, service):
        _seed(repo, [comment_row("c1", text=POSITIVE_TEXT)])
        service.get_analysis(VIDEO)
        assert repo.get_comments(VIDEO)[0]["emotion_label"] is not None

        changed = comment_row("c1", text="Totally different text now")
        changed["raw_text"] = "Totally different text now"
        repo.upsert_comments([changed], batch_size=10)
        row = repo.get_comments(VIDEO)[0]
        # Content reset clears the derived intelligence together (§9).
        assert row["processing_status"] == ProcessingStatus.READY_FOR_ANALYSIS.value
        assert row["sentiment_label"] is None
        assert row["emotion_label"] is None
        assert row["sentiment_intensity"] is None

    def test_emotion_aggregates_never_leak_across_videos(self, repo, service):
        seed_video(repo, OTHER_VIDEO)
        _seed(repo, [
            comment_row("a1", video_id=VIDEO, text=POSITIVE_TEXT),
            comment_row("a2", video_id=VIDEO, text=POSITIVE_TEXT),
            comment_row("b1", video_id=OTHER_VIDEO, text=NEGATIVE_TEXT),
        ])
        analysis_a = service.get_analysis(VIDEO)
        analysis_b = service.get_analysis(OTHER_VIDEO)

        counts_a = repo.get_emotion_counts(VIDEO)
        counts_b = repo.get_emotion_counts(OTHER_VIDEO)
        assert sum(counts_a.values()) == 2
        assert sum(counts_b.values()) == 1
        # Each video reports only its own dominant emotion / mood evidence.
        assert analysis_a.emotion.dominant == "JOY"
        assert analysis_b.emotion.dominant in ALL_EMOTIONS
        assert analysis_a.emotion.dominant != "DISGUST"


# ---------------------------------------------------------------------------
# §25 Schema migration: one-time reopen of pre-Sprint-5 rows
# ---------------------------------------------------------------------------


class TestLegacyMigration:
    def _legacy_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE comments ("
            " comment_id TEXT PRIMARY KEY, video_id TEXT NOT NULL,"
            " processing_status TEXT NOT NULL DEFAULT 'READY_FOR_ANALYSIS',"
            " sentiment_label TEXT, sentiment_score REAL,"
            " sentiment_confidence REAL, sentiment_model TEXT,"
            " sentiment_processed_at TEXT)"
        )
        conn.execute(
            "INSERT INTO comments (comment_id, video_id, processing_status,"
            " sentiment_label, sentiment_score, sentiment_confidence,"
            " sentiment_model, sentiment_processed_at)"
            " VALUES ('c1', 'vid', 'PROCESSED', 'POSITIVE', 0.8, 0.75,"
            " 'vader-1.0', '2026-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO comments (comment_id, video_id, processing_status)"
            " VALUES ('c2', 'vid', 'READY_FOR_ANALYSIS')"
        )
        conn.commit()
        return conn

    def test_legacy_processed_rows_are_reopened_once(self):
        conn = self._legacy_conn()
        migrate(conn)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(comments)")}
        assert {"emotion_label", "emotion_score", "emotion_model",
                "sentiment_intensity"} <= columns

        row = conn.execute("SELECT * FROM comments WHERE comment_id = 'c1'").fetchone()
        # Reopened for reprocessing with stale verdicts cleared (never a
        # half-analyzed row claiming PROCESSED without emotion data).
        assert row["processing_status"] == "READY_FOR_ANALYSIS"
        assert row["sentiment_label"] is None
        assert row["sentiment_score"] is None
        assert row["sentiment_confidence"] is None
        assert row["sentiment_model"] is None
        assert row["emotion_label"] is None
        # Untouched pending row stays pending.
        assert conn.execute(
            "SELECT processing_status FROM comments WHERE comment_id = 'c2'"
        ).fetchone()[0] == "READY_FOR_ANALYSIS"

        # One-time: after reprocessing, later startups must NOT reopen.
        conn.execute(
            "UPDATE comments SET processing_status = 'PROCESSED',"
            " sentiment_label = 'POSITIVE', emotion_label = 'JOY'"
            " WHERE comment_id = 'c1'"
        )
        conn.commit()
        migrate(conn)
        assert conn.execute(
            "SELECT processing_status FROM comments WHERE comment_id = 'c1'"
        ).fetchone()[0] == "PROCESSED"

        # And a third run stays a no-op as well (idempotent).
        migrate(conn)
        assert conn.execute(
            "SELECT processing_status FROM comments WHERE comment_id = 'c1'"
        ).fetchone()[0] == "PROCESSED"
        conn.close()

    def test_fresh_database_is_untouched_by_reopen(self):
        db = Database("sqlite:///:memory:")
        repository = DatasetRepository(db)
        seed_video(repository, VIDEO)
        repository.upsert_comments(
            [comment_row("c1", text=POSITIVE_TEXT)], batch_size=10
        )
        service = SentimentService(repository, make_settings())
        service.get_analysis(VIDEO)
        row = repository.get_comments(VIDEO)[0]
        assert row["processing_status"] == "PROCESSED"  # stays processed
        assert row["emotion_label"] == "JOY"
        db.close()


# ---------------------------------------------------------------------------
# §25 API: new blocks, camelCase, old contract intact
# ---------------------------------------------------------------------------


def _fake_with_comments(comments, video_id: str = VIDEO) -> FakeYouTubeClient:
    return FakeYouTubeClient(
        video=YtVideoListResponse.model_validate(video_payload(video_id)),
        pages={None: ([thread_payload(cid, text) for cid, text in comments], None)},
    )


def _acquire(client: TestClient, video_id: str = VIDEO) -> None:
    response = client.get(f"/api/videos/{video_id}")
    assert response.status_code == 200


class TestIntelligenceEndpoint:
    def test_sentiment_endpoint_exposes_sprint5_blocks(self, app_factory):
        client = app_factory(
            _fake_with_comments([
                ("c1", POSITIVE_TEXT),
                ("c2", POSITIVE_TEXT),
                ("c3", NEGATIVE_TEXT),
                ("c4", NEUTRAL_TEXT),
                ("c5", POSITIVE_TEXT),
            ])
        )
        _acquire(client)
        body = client.get(f"/api/videos/{VIDEO}/sentiment").json()

        # Old contract untouched.
        assert body["videoId"] == VIDEO
        assert body["status"] == "PROCESSED"
        assert body["stats"]["analyzed"] == 5
        assert body["dominantSentiment"] == "POSITIVE"
        assert set(body["dataset"]) >= {"collected", "stored", "analyzed", "skipped", "failed"}

        # New blocks (camelCase, full vocabulary, correct denominators).
        assert set(body) >= {"emotion", "intensity", "confidence", "audienceMood"}
        emotion = body["emotion"]
        assert set(emotion["distribution"]) == set(EMOTION_PRIORITY)
        assert sum(v["count"] for v in emotion["distribution"].values()) == 5
        assert sum(v["percent"] for v in emotion["distribution"].values()) == pytest.approx(100.0)
        assert emotion["dominant"] in ALL_EMOTIONS

        intensity = body["intensity"]
        assert set(intensity["distribution"]) == {"LOW", "MEDIUM", "HIGH"}
        assert sum(v["count"] for v in intensity["distribution"].values()) == 5
        assert intensity["overall"] in INTENSITY_BANDS

        assert isinstance(body["confidence"]["average"], float)
        assert 0.0 <= body["confidence"]["average"] <= 1.0

        # 5 analyzed < min sample -> no mood claimed (never guessed).
        assert body["audienceMood"] is None

    def test_mood_appears_once_the_sample_is_large_enough(self, app_factory):
        comments = [(f"c{i}", POSITIVE_TEXT) for i in range(12)]
        client = app_factory(_fake_with_comments(comments))
        _acquire(client)
        body = client.get(f"/api/videos/{VIDEO}/sentiment").json()
        assert body["stats"]["analyzed"] == 12
        assert body["audienceMood"] == "EXCITED"

    def test_background_job_path_publishes_intelligence(self, app_factory):
        # Sprint 4.3 async path: emotion/intensity ride the SAME job pass.
        from tests.test_analysis_jobs import _fake, wait_for_terminal

        comments = [(f"c{i}", POSITIVE_TEXT) for i in range(4)]
        pages = {None: ([thread_payload(cid, text) for cid, text in comments], None)}
        client = app_factory(_fake(pages=pages))
        response = client.post(f"/api/videos/{VIDEO}/analysis")
        assert response.status_code == 202
        final = wait_for_terminal(client, VIDEO)
        assert final["status"] == "COMPLETED"
        assert final["analyzed"] == 4

        body = client.get(f"/api/videos/{VIDEO}/sentiment").json()
        assert body["status"] == "PROCESSED"
        assert sum(
            v["count"] for v in body["emotion"]["distribution"].values()
        ) == 4
        assert body["intensity"]["overall"] in INTENSITY_BANDS
        assert body["confidence"]["average"] is not None

    def test_untracked_video_still_404(self, app_factory):
        client = app_factory(_fake_with_comments([]))
        assert client.get("/api/videos/aaaaaaaaaaa/sentiment").status_code == 404

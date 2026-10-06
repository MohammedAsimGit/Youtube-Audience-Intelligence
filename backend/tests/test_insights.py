"""Sprint 7 tests: evidence-based audience insight engine.

Coverage map (brief §39):
- Evidence generation: sentiment/topics/sample copied correctly, missing
  blocks stay null, sample size uses the analyzed denominator.
- Insight selection: strongest supported positive theme, dominant
  discussion, meaningful pain point, overall-reaction gates, emotion
  signal threshold, and tiny/below-support samples ignored.
- Grounding: numbers must exist in the evidence, banned claim language
  rejected, length budgets enforced, malformed schema rejected.
- Deterministic generation: default provider output always validates,
  honest copy when a category has no evidence, cards carry real evidence
  lines with topic links, causal wording never used.
- Failure/fallback: provider timeout, unavailable provider, invalid
  output, bounded retries, honest `source` labels, retry action covered
  by the API error path.
- Active video: A -> B -> C never surfaces a stale insight; memo keyed
  by the analysis fingerprint.
- API: camelCase contract, insufficient/404/422 states, job completes
  through the INSIGHT phase, insight failure never fails the analysis.
"""
from datetime import datetime, timezone

import pytest

from app.core.config import Settings
from app.db.connection import Database
from app.db.repository import DatasetRepository
from app.services.insights import (
    DeterministicProvider,
    InsightService,
    ProviderError,
    build_cards,
    build_evidence,
    select_candidates,
    validate_draft,
)
from app.services.sentiment import SentimentService
from app.services.topics import TopicService
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
THIRD_VIDEO = "CCCCCCCCCCC"

MIN_SUPPORT = 8


# ---------------------------------------------------------------------------
# Pure evidence + candidate fixtures (deterministic test data)
# ---------------------------------------------------------------------------


def ref(topic_id, label, mentions, share, pos=0.0, neg=0.0):
    from app.services.insights import TopicRef

    return TopicRef(
        topic_id=topic_id,
        label=label,
        mentions=mentions,
        share_percent=share,
        positive_percent=pos,
        negative_percent=neg,
    )


def evidence_fixture(**overrides):
    base = dict(
        video_id=VIDEO,
        evidence_version="1211:2026-10-01T00:00:00",
        collected=2429,
        analyzed=1211,
        skipped=1218,
        positive_percent=68.3,
        neutral_percent=29.0,
        negative_percent=2.7,
        dominant_sentiment="POSITIVE",
        dominant_emotion="TRUST",
        dominant_emotion_percent=37.7,
        intensity_overall="LOW",
        confidence_average=0.71,
        audience_mood="POSITIVE",
        top_topic=ref("t1", "Learning roadmap", 312, 25.8, pos=55.0, neg=12.0),
        appreciated=[
            ref("t2", "Clear explanations", 284, 23.5, pos=91.0, neg=3.0),
            ref("t5", "Practical examples", 40, 3.3, pos=88.0, neg=2.0),
        ],
        pain_points=[ref("t3", "Learning difficulty", 96, 7.9, pos=8.0, neg=58.0)],
        mixed=[ref("t4", "React complexity", 184, 15.2, pos=42.9, neg=35.9)],
    )
    base.update(overrides)
    from app.services.insights import AudienceEvidence

    return AudienceEvidence(**base)


# ---------------------------------------------------------------------------
# Evidence generation (§4/§11)
# ---------------------------------------------------------------------------


class TestEvidenceGeneration:
    def test_build_evidence_copies_source_of_truth(self):
        """Evidence is a snapshot of the two responses - nothing more."""
        from app.models.internal import (
            EmotionBreakdown,
            LabelShare,
            SentimentAnalysisResponse,
            SentimentDatasetMetrics,
            SentimentStats,
            TopicAnalysisResponse,
            TopicItem,
        )

        sentiment = SentimentAnalysisResponse(
            video_id=VIDEO,
            status="PROCESSED",
            stats=SentimentStats(
                total_comments=100, analyzed=80, skipped=20,
                positive=60, neutral=15, negative=5,
                positive_percent=75.0, neutral_percent=18.7, negative_percent=6.3,
            ),
            dataset=SentimentDatasetMetrics(
                collected=100, stored=100, analyzed=80, skipped=20, failed=0,
                has_more=False, limit_reached=False,
            ),
            dominant_sentiment="POSITIVE",
            emotion=EmotionBreakdown(dominant="JOY", dominant_percent=44.0),
            audience_mood="POSITIVE",
        )

        def item(topic_id, label, mentions, pos, neg):
            return TopicItem(
                topic_id=topic_id, label=label, mentions=mentions,
                share_percent=10.0, category="MOST_DISCUSSED",
                confidence=0.5, evidence="factual",
                sentiment={
                    "POSITIVE": LabelShare(count=pos, percent=50.0),
                    "NEUTRAL": LabelShare(count=0, percent=0.0),
                    "NEGATIVE": LabelShare(count=neg, percent=50.0),
                },
                dominant_sentiment="POSITIVE",
            )

        topics = TopicAnalysisResponse(
            video_id=VIDEO,
            status="READY",
            analyzed_comments=80,
            topics=[item("t1", "Theme A", 30, 20, 5), item("t2", "Theme B", 25, 2, 15)],
            most_discussed=[item("t1", "Theme A", 30, 20, 5)],
            most_appreciated=[item("t1", "Theme A", 30, 20, 5)],
            pain_points=[item("t2", "Theme B", 25, 2, 15)],
        )
        evidence = build_evidence(VIDEO, sentiment, topics, "80:stamp")
        assert evidence.analyzed == 80
        assert evidence.collected == 100
        assert evidence.skipped == 20
        assert evidence.positive_percent == 75.0
        assert evidence.dominant_emotion == "JOY"
        assert evidence.top_topic.label == "Theme A"
        assert evidence.appreciated[0].positive_percent == 50.0
        assert evidence.pain_points[0].negative_percent == 50.0
        assert evidence.evidence_version == "80:stamp"

    def test_missing_blocks_stay_null(self):
        from app.models.internal import (
            SentimentAnalysisResponse,
            SentimentDatasetMetrics,
            SentimentStats,
            TopicAnalysisResponse,
        )

        sentiment = SentimentAnalysisResponse(
            video_id=VIDEO,
            status="PROCESSED",
            stats=SentimentStats(
                total_comments=5, analyzed=5, skipped=0,
                positive=2, neutral=2, negative=1,
                positive_percent=40.0, neutral_percent=40.0, negative_percent=20.0,
            ),
            dataset=SentimentDatasetMetrics(
                collected=5, stored=5, analyzed=5, skipped=0, failed=0,
                has_more=False, limit_reached=False,
            ),
        )
        topics = TopicAnalysisResponse(
            video_id=VIDEO, status="READY", analyzed_comments=5
        )
        evidence = build_evidence(VIDEO, sentiment, topics, "5:stamp")
        # Section 4: never invent missing values.
        assert evidence.dominant_emotion is None
        assert evidence.dominant_sentiment is None
        assert evidence.intensity_overall is None
        assert evidence.confidence_average is None
        assert evidence.audience_mood is None
        assert evidence.top_topic is None
        assert evidence.appreciated == []
        assert evidence.pain_points == []

    def test_sample_size_uses_analyzed_denominator(self, repo):
        """§11: conclusions rest on analyzed - skipped rows never count."""
        repo.upsert_comments(
            [comment_row(f"c{i}", text="clear learning roadmap") for i in range(30)]
            + [comment_row(f"e{i}", text="buen tutorial", language="es") for i in range(10)],
            batch_size=100,
        )
        SentimentService(repo, make_settings()).get_analysis(VIDEO)
        service = _service(repo)
        response = service.get_insight(VIDEO)
        assert response.sample.analyzed == 30
        assert response.sample.skipped == 10
        assert response.sample.collected == 40


# ---------------------------------------------------------------------------
# Insight selection (§6/§9/§10)
# ---------------------------------------------------------------------------


class TestInsightSelection:
    def test_strongly_positive_gate(self):
        candidates = select_candidates(evidence_fixture(), MIN_SUPPORT)
        assert candidates.overall.headline == "The audience response is strongly positive"
        assert "68.3% positive" in candidates.overall.detail

    def test_mostly_positive_and_negative_gates(self):
        mostly = select_candidates(
            evidence_fixture(positive_percent=55.0, negative_percent=10.0,
                             neutral_percent=35.0),
            MIN_SUPPORT,
        )
        assert mostly.overall.headline == "The audience response is mostly positive"
        negative = select_candidates(
            evidence_fixture(positive_percent=8.0, negative_percent=65.0,
                             neutral_percent=27.0, dominant_sentiment="NEGATIVE"),
            MIN_SUPPORT,
        )
        assert negative.overall.headline == "The audience response is strongly negative"

    def test_mixed_gate(self):
        mixed = select_candidates(
            evidence_fixture(positive_percent=41.0, negative_percent=37.0,
                             neutral_percent=22.0, dominant_sentiment="POSITIVE"),
            MIN_SUPPORT,
        )
        assert mixed.overall.headline == "The audience response is mixed"

    def test_strongest_supported_positive_theme_selected(self):
        # Section 9: the ranked first entry clears support and wins.
        candidates = select_candidates(evidence_fixture(), MIN_SUPPORT)
        assert candidates.appreciated.label == "Clear explanations"
        assert candidates.appreciated.positive_percent == 91.0

    def test_below_support_theme_never_selected(self):
        # Section 10: 5 mentions at 100% positive is not evidence.
        evidence = evidence_fixture(
            appreciated=[ref("t2", "Tiny theme", 5, 0.4, pos=100.0)],
            top_topic=ref("t1", "Small topic", 4, 0.3, pos=50.0),
            pain_points=[ref("t3", "Tiny pain", 3, 0.2, neg=100.0)],
            mixed=[],
        )
        candidates = select_candidates(evidence, MIN_SUPPORT)
        assert candidates.appreciated is None
        assert candidates.main_discussion is None
        assert candidates.pain_point is None

    def test_major_topic_and_pain_point_selected(self):
        candidates = select_candidates(evidence_fixture(), MIN_SUPPORT)
        assert candidates.main_discussion.label == "Learning roadmap"
        assert candidates.main_discussion.mentions == 312
        assert candidates.pain_point.label == "Learning difficulty"
        assert candidates.pain_point.negative_percent == 58.0
        assert candidates.mixed.label == "React complexity"

    def test_emotion_signal_threshold(self):
        strong = select_candidates(evidence_fixture(), MIN_SUPPORT)
        assert strong.emotion.label == "TRUST"
        assert strong.emotion.percent == 37.7
        weak = select_candidates(
            evidence_fixture(dominant_emotion_percent=15.0), MIN_SUPPORT
        )
        assert weak.emotion is None  # a 15% plurality is not a signal
        neutral = select_candidates(
            evidence_fixture(dominant_emotion="NEUTRAL"), MIN_SUPPORT
        )
        assert neutral.emotion is None

    def test_tiny_sample_never_builds_overall_claim(self):
        candidates = select_candidates(evidence_fixture(analyzed=0), MIN_SUPPORT)
        assert candidates.overall is None


# ---------------------------------------------------------------------------
# Grounding / validation (§7/§14/§15)
# ---------------------------------------------------------------------------


def _validated_payload(evidence=None):
    evidence = evidence or evidence_fixture()
    candidates = select_candidates(evidence, MIN_SUPPORT)
    return DeterministicProvider().generate(evidence, candidates), evidence


class TestGrounding:
    def test_deterministic_output_always_validates(self):
        payload, evidence = _validated_payload()
        draft = validate_draft(payload, evidence)
        assert draft.headline
        assert draft.summary
        assert draft.takeaway

    def test_deterministic_output_validates_across_shapes(self):
        # Sparse evidence: no topics, no emotion, low sample.
        evidence = evidence_fixture(
            analyzed=35, collected=40, skipped=5,
            positive_percent=50.0, neutral_percent=40.0, negative_percent=10.0,
            dominant_emotion=None, dominant_emotion_percent=0.0,
            intensity_overall=None, confidence_average=None, audience_mood=None,
            top_topic=None, appreciated=[], pain_points=[], mixed=[],
        )
        candidates = select_candidates(evidence, MIN_SUPPORT)
        payload = DeterministicProvider().generate(evidence, candidates)
        draft = validate_draft(payload, evidence)
        assert "Not enough repeated audience evidence" in draft.what_worked

    def test_ungrounded_number_rejected(self):
        payload, evidence = _validated_payload()
        payload["summary"] = "The audience is 97.4% positive overall."
        with pytest.raises(ProviderError, match="not present in evidence"):
            validate_draft(payload, evidence)

    def test_banned_claim_language_rejected(self):
        payload, evidence = _validated_payload()
        payload["summary"] = "Everyone loved the explanations."
        with pytest.raises(ProviderError, match="banned claim"):
            validate_draft(payload, evidence)
        payload["summary"] = "This is the best React roadmap on YouTube."
        with pytest.raises(ProviderError, match="banned claim"):
            validate_draft(payload, evidence)
        payload["summary"] = "Great content caused by hard work."
        with pytest.raises(ProviderError, match="banned claim"):
            validate_draft(payload, evidence)

    def test_length_budgets_enforced(self):
        payload, evidence = _validated_payload()
        payload["headline"] = " ".join(["word"] * 13)
        with pytest.raises(ProviderError, match="headline"):
            validate_draft(payload, evidence)
        payload["headline"] = "Mostly positive audience response"
        payload["summary"] = "x" * 701
        with pytest.raises(ProviderError, match="length budget"):
            validate_draft(payload, evidence)

    def test_malformed_schema_rejected(self):
        payload, evidence = _validated_payload()
        payload["unexpected"] = "key"
        with pytest.raises(ProviderError, match="schema"):
            validate_draft(payload, evidence)
        with pytest.raises(ProviderError, match="not an object"):
            validate_draft(["nope"], evidence)
        broken = dict(payload)
        broken["headline"] = ""
        with pytest.raises(ProviderError):
            validate_draft(broken, evidence)

    def test_grounded_number_forms_accepted(self):
        payload, evidence = _validated_payload()
        # Same numbers with/without % and thousands separators stay grounded.
        payload["summary"] = "68.3% positive across 1,211 analyzed comments."
        assert validate_draft(payload, evidence).summary.startswith("68.3%")


# ---------------------------------------------------------------------------
# Cards + deterministic phrasing (§8/§15/§17/§20)
# ---------------------------------------------------------------------------


class TestCardsAndPhrasing:
    def _cards(self, evidence=None):
        evidence = evidence or evidence_fixture()
        candidates = select_candidates(evidence, MIN_SUPPORT)
        payload = DeterministicProvider().generate(evidence, candidates)
        draft = validate_draft(payload, evidence)
        return evidence, candidates, build_cards(evidence, candidates, draft)

    def test_all_six_categories_present_with_rich_evidence(self):
        _, _, cards = self._cards()
        categories = [card.category for card in cards]
        assert categories == [
            "OVERALL_REACTION", "WHAT_WORKED", "MAIN_DISCUSSION",
            "PAIN_POINT", "EMOTIONAL_SIGNAL", "TAKEAWAY",
        ]

    def test_emotional_signal_omitted_without_signal(self):
        evidence = evidence_fixture(dominant_emotion="NEUTRAL")
        _, _, cards = self._cards(evidence)
        assert "EMOTIONAL_SIGNAL" not in [c.category for c in cards]

    def test_what_worked_honest_copy_without_preference(self):
        evidence = evidence_fixture(appreciated=[])
        _, _, cards = self._cards(evidence)
        worked = next(c for c in cards if c.category == "WHAT_WORKED")
        assert "Not enough repeated audience evidence" in worked.body

    def test_pain_card_honest_copy_without_pain(self):
        evidence = evidence_fixture(pain_points=[])
        _, _, cards = self._cards(evidence)
        pain = next(c for c in cards if c.category == "PAIN_POINT")
        assert "No recurring negative theme" in pain.body

    def test_evidence_lines_carry_numbers_and_topic_links(self):
        _, _, cards = self._cards()
        worked = next(c for c in cards if c.category == "WHAT_WORKED")
        topic_line = worked.evidence[0]
        assert topic_line.kind == "APPRECIATED"
        assert topic_line.label == "Clear explanations"
        assert topic_line.value == "284 mentions"
        assert topic_line.detail == "91.0% positive"
        assert topic_line.topic_id == "t2"  # §21 linking
        overall = cards[0]
        assert any(line.kind == "SAMPLE" for line in overall.evidence)
        assert any(line.kind == "SENTIMENT" for line in overall.evidence)

    def test_causal_language_never_used(self):
        _, _, cards = self._cards()
        for card in cards:
            assert "caused by" not in card.body.lower()
            assert "because of" not in card.body.lower()

    def test_summary_length_budget(self):
        import re

        _, _, cards = self._cards()
        overall = cards[0]
        # §17: 1-3 sentences, not an essay (decimals like 68.3 are not
        # sentence boundaries).
        assert len(overall.body) <= 700
        sentences = re.findall(r"(?<!\d)\.(?!\d)", overall.body)
        assert len(sentences) <= 4

    def test_emotional_signal_uses_associated_with(self):
        _, _, cards = self._cards()
        signal = next(c for c in cards if c.category == "EMOTIONAL_SIGNAL")
        assert "associated with" in signal.body
        assert "TRUST" in signal.body
        assert "37.7%" in signal.body


# ---------------------------------------------------------------------------
# Service: provider sources, fallback, memo, active video (§16/§23/§26/§28)
# ---------------------------------------------------------------------------


# Grounded, schema-valid LLM payload with NO numbers: passes validation
# against any evidence (numbers are what grounding checks reject).
VALID_LLM_PAYLOAD = {
    "headline": "Mostly positive audience response",
    "summary": "The analyzed audience shows a mostly positive pattern.",
    "what_worked": "Viewers repeatedly praised the clear structure.",
    "main_discussion": "The roadmap is the main discussion theme.",
    "pain_point": "Some viewers struggled with the learning pace.",
    "emotional_signal": (
        "Trust is the dominant detected emotion, associated with the "
        "positive sentiment."
    ),
    "takeaway": "Overall the response is positive with appreciation for structure.",
}


class FakeLLM:
    """Injected provider double - tests the abstraction, not the network."""

    def __init__(self, result, name="fake", model="fake-model"):
        self.name = name
        self.model = model
        self.result = result
        self.calls = 0

    def generate(self, evidence, candidates):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _service(repo, **settings_overrides) -> InsightService:
    settings = make_settings(**settings_overrides)
    return InsightService(
        repo,
        SentimentService(repo, settings),
        TopicService(repo, settings),
        settings,
    )


@pytest.fixture
def repo():
    db = Database("sqlite:///:memory:")
    repository = DatasetRepository(db)
    seed_video(repository, VIDEO)
    yield repository
    db.close()


def _analyzed(repo, count, text="clear learning roadmap for react", video_id=VIDEO):
    repo.upsert_comments(
        [comment_row(f"c{i}", video_id=video_id, text=text) for i in range(count)],
        batch_size=100,
    )
    SentimentService(repo, make_settings()).get_analysis(video_id)


class TestServiceAndFallback:
    def test_untracked_video_returns_none(self, repo):
        assert _service(repo).get_insight("aaaaaaaaaaa") is None

    def test_insufficient_below_min_comment_count(self, repo):
        _analyzed(repo, 5)
        response = _service(repo).get_insight(VIDEO)
        assert response.status == "INSUFFICIENT_DATA"
        assert response.message == (
            "Not enough analyzed audience evidence to generate a reliable insight."
        )
        assert response.sample.analyzed == 5
        assert response.headline == ""
        assert response.cards == []

    def test_ready_with_deterministic_source(self, repo):
        _analyzed(repo, 35)
        response = _service(repo).get_insight(VIDEO)
        assert response.status == "READY"
        assert response.source == "deterministic"
        assert response.provider.name == "deterministic"
        assert response.provider.model is None
        assert response.headline
        assert response.summary
        assert response.cards
        assert response.evidence_version.startswith("35:")
        assert response.generated_at is not None

    def test_memo_reused_until_fingerprint_changes(self, repo):
        _analyzed(repo, 35)
        service = _service(repo)
        first = service.get_insight(VIDEO)
        second = service.get_insight(VIDEO)
        assert first is second

        changed = comment_row("c0", text="a completely different new text")
        repo.upsert_comments([changed], batch_size=10)
        third = service.get_insight(VIDEO)
        assert third is not first  # re-analysis changed the fingerprint
        # The GET re-ran the sentiment engine on the pending row inline
        # (same contract as GET /sentiment), so the sample reflects the
        # refreshed analyzed set.
        assert third.sample.analyzed == 35

    def test_llm_success_reports_llm_source(self, repo):
        _analyzed(repo, 35)
        service = _service(repo, insight_provider="openai_compatible",
                           insight_api_key="test-key")
        fake = FakeLLM(dict(VALID_LLM_PAYLOAD))
        service._llm = fake
        response = service.get_insight(VIDEO)
        assert response.source == "llm"
        assert response.provider.name == "fake"
        assert response.provider.model == "fake-model"
        assert fake.calls == 1

    def test_llm_invalid_output_falls_back_after_bounded_retries(self, repo):
        _analyzed(repo, 35)
        service = _service(repo, insight_provider="openai_compatible",
                           insight_api_key="test-key", insight_retry_attempts=2)
        fake = FakeLLM({"headline": "Mostly positive audience response",
                        "summary": "The audience is 99.9% positive."})
        service._llm = fake
        response = service.get_insight(VIDEO)
        assert response.source == "fallback"  # §23: never claim AI here
        assert fake.calls == 2  # bounded retries, then fallback
        assert response.provider.name == "deterministic"
        assert response.cards  # §16: UI still gets useful intelligence
        assert "99.9" not in response.summary

    def test_llm_timeout_falls_back(self, repo):
        import httpx

        _analyzed(repo, 35)
        service = _service(repo, insight_provider="openai_compatible",
                           insight_api_key="test-key", insight_retry_attempts=1)
        service._llm = FakeLLM(httpx.TimeoutException("timed out"))
        response = service.get_insight(VIDEO)
        assert response.source == "fallback"
        assert response.status == "READY"

    def test_llm_provider_error_falls_back(self, repo):
        _analyzed(repo, 35)
        service = _service(repo, insight_provider="openai_compatible",
                           insight_api_key="test-key", insight_retry_attempts=3)
        fake = FakeLLM(
            ProviderError("provider rejected request (401)", permanent=True)
        )
        service._llm = fake
        response = service.get_insight(VIDEO)
        assert response.source == "fallback"
        assert fake.calls == 1  # permanent provider errors burn no retries


class TestActiveVideoSafety:
    def test_switch_never_surfaces_old_insight(self, repo):
        _analyzed(repo, 35, video_id=VIDEO)
        service = _service(repo)
        first = service.get_insight(VIDEO)
        assert first.status == "READY"

        repo.switch_active_video(OTHER_VIDEO)  # A's rows cascaded away
        assert service.get_insight(VIDEO) is None  # 404, never stale
        second = service.get_insight(OTHER_VIDEO)
        assert second.video_id == OTHER_VIDEO
        assert second.status == "INSUFFICIENT_DATA"  # B has no analyzed rows

    def test_rapid_switching_a_b_c(self, repo):
        seed_video(repo, OTHER_VIDEO)
        seed_video(repo, THIRD_VIDEO)
        service = _service(repo)

        _analyzed(repo, 35, video_id=VIDEO)
        assert service.get_insight(VIDEO).status == "READY"

        repo.switch_active_video(OTHER_VIDEO)
        _analyzed(repo, 35, video_id=OTHER_VIDEO, text="setup problems keep failing")
        b = service.get_insight(OTHER_VIDEO)
        assert b.video_id == OTHER_VIDEO
        assert b.status == "READY"
        # B's own evidence: sample and version belong to B.
        assert b.evidence_version.startswith("35:")
        assert service.get_insight(VIDEO) is None

        repo.switch_active_video(THIRD_VIDEO)
        c = service.get_insight(THIRD_VIDEO)
        assert c.video_id == THIRD_VIDEO
        assert c.status == "INSUFFICIENT_DATA"
        assert service.get_insight(OTHER_VIDEO) is None  # B never leaks to C


# ---------------------------------------------------------------------------
# API contract (§15/§23) + job integration (§25/§39)
# ---------------------------------------------------------------------------


def _fake_with_comments(comments, video_id: str = VIDEO) -> FakeYouTubeClient:
    return FakeYouTubeClient(
        video=video_payload_client(video_id),
        pages={None: ([thread_payload(cid, text) for cid, text in comments], None)},
    )


def video_payload_client(video_id: str):
    from app.schemas.youtube import YtVideoListResponse

    return YtVideoListResponse.model_validate(video_payload(video_id))


def _acquire(client: TestClient, video_id: str = VIDEO) -> None:
    assert client.get(f"/api/videos/{video_id}").status_code == 200


THEME_COMMENTS = [
    (f"c{i}", "this learning roadmap is really helpful for react") for i in range(35)
]


class TestInsightEndpoint:
    def test_camelcase_contract_over_real_analysis(self, app_factory):
        client = app_factory(_fake_with_comments(THEME_COMMENTS))
        _acquire(client)
        assert client.get(f"/api/videos/{VIDEO}/sentiment").status_code == 200

        response = client.get(f"/api/videos/{VIDEO}/insight")
        assert response.status_code == 200
        body = response.json()
        assert body["videoId"] == VIDEO
        assert body["status"] == "READY"
        assert body["source"] == "deterministic"
        assert body["provider"]["name"] == "deterministic"
        assert body["sample"]["analyzed"] == 35
        assert body["sample"]["collected"] == 35
        assert body["message"] is None
        assert body["headline"]
        assert body["evidenceVersion"]
        assert body["generatedAt"]
        categories = [card["category"] for card in body["cards"]]
        assert "OVERALL_REACTION" in categories
        overall = body["cards"][0]
        assert {"category", "title", "body", "evidence"} <= set(overall)
        line = overall["evidence"][0]
        assert {"kind", "label", "value"} <= set(line)
        # §20: real numbers behind the claim.
        assert any("positive" in e["value"] for e in overall["evidence"])

    def test_insufficient_data_state(self, app_factory):
        client = app_factory(_fake_with_comments([("c1", "nice video good stuff")]))
        _acquire(client)
        client.get(f"/api/videos/{VIDEO}/sentiment")
        body = client.get(f"/api/videos/{VIDEO}/insight").json()
        assert body["status"] == "INSUFFICIENT_DATA"
        assert body["cards"] == []
        assert "Not enough analyzed audience evidence" in body["message"]

    def test_untracked_video_404_and_invalid_id_422(self, app_factory):
        client = app_factory(_fake_with_comments([]))
        assert client.get("/api/videos/aaaaaaaaaaa/insight").status_code == 404
        assert client.get("/api/videos/xx/insight").status_code == 422

    def test_background_job_completes_through_insight_phase(self, app_factory):
        from tests.test_analysis_jobs import _fake, wait_for_terminal

        pages = {None: ([thread_payload(cid, text) for cid, text in THEME_COMMENTS], None)}
        client = app_factory(_fake(pages=pages))
        calls = []
        original = client.app.state.insight_service.get_insight
        client.app.state.insight_service.get_insight = lambda vid: (
            calls.append(vid), original(vid)
        )[1]

        assert client.post(f"/api/videos/{VIDEO}/analysis").status_code == 202
        final = wait_for_terminal(client, VIDEO)
        assert final["status"] == "COMPLETED"
        assert final["phase"] == "COMPLETE"
        assert calls == [VIDEO]  # §25 warm-up ran on the worker thread
        body = client.get(f"/api/videos/{VIDEO}/insight").json()
        assert body["status"] == "READY"

    def test_insight_failure_never_fails_the_analysis(self, app_factory, monkeypatch):
        from tests.test_analysis_jobs import _fake, wait_for_terminal

        pages = {None: ([thread_payload(cid, text) for cid, text in THEME_COMMENTS], None)}
        client = app_factory(_fake(pages=pages))

        def boom(*_args, **_kwargs):
            raise RuntimeError("phrasing exploded")

        monkeypatch.setattr(client.app.state.insight_service, "get_insight", boom)
        assert client.post(f"/api/videos/{VIDEO}/analysis").status_code == 202
        final = wait_for_terminal(client, VIDEO)
        assert final["status"] == "COMPLETED"  # §39: job survives
        sentiment = client.get(f"/api/videos/{VIDEO}/sentiment").json()
        assert sentiment["status"] == "PROCESSED"
        assert sentiment["stats"]["analyzed"] == 35

    def test_switching_videos_via_api_isolates_insight(self, app_factory):
        fake = _fake_with_comments(THEME_COMMENTS)
        client = app_factory(fake)
        _acquire(client, VIDEO)
        client.get(f"/api/videos/{VIDEO}/sentiment")
        first = client.get(f"/api/videos/{VIDEO}/insight").json()
        assert first["status"] == "READY"
        assert "roadmap" in (first["headline"] + first["summary"]).lower()

        # B serves DIFFERENT comments so its evidence is distinguishable.
        fake.pages = {
            None: (
                [
                    thread_payload(
                        f"b{i}",
                        "setup problems keep failing every time I run the project locally",
                    )
                    for i in range(35)
                ],
                None,
            )
        }
        _acquire(client, OTHER_VIDEO)  # active-video switch (Sprint 4.2)
        assert client.get(f"/api/videos/{VIDEO}/insight").status_code == 404
        second = client.get(f"/api/videos/{OTHER_VIDEO}/insight").json()
        assert second["videoId"] == OTHER_VIDEO
        # B's OWN evidence, never A's memoized roadmap insight.
        combined = " ".join(
            [second["headline"], second["summary"]]
            + [card["body"] for card in second["cards"]]
        ).lower()
        # B's own discovered vocabulary appears; A's roadmap theme never does.
        assert "roadmap" not in combined
        assert "failing" in combined


# ---------------------------------------------------------------------------
# Configuration bounds (§26)
# ---------------------------------------------------------------------------


class TestSettingsValidation:
    def test_unknown_provider_rejected(self):
        with pytest.raises(Exception):
            make_settings(insight_provider="some_vendor")

    def test_timeout_and_retry_bounds(self):
        with pytest.raises(Exception):
            make_settings(insight_timeout_seconds=0)
        with pytest.raises(Exception):
            make_settings(insight_timeout_seconds=999)
        with pytest.raises(Exception):
            make_settings(insight_retry_attempts=0)
        with pytest.raises(Exception):
            make_settings(insight_retry_attempts=99)

    def test_output_token_bounds(self):
        with pytest.raises(Exception):
            make_settings(insight_max_output_tokens=8)
        with pytest.raises(Exception):
            make_settings(insight_max_output_tokens=99999)


# ---------------------------------------------------------------------------
# Performance sanity (§36 - real numbers live in benchmarks/)
# ---------------------------------------------------------------------------


class TestPerformance:
    @pytest.mark.parametrize("scale", [100, 1000])
    def test_insight_generation_bounded(self, scale):
        db = Database("sqlite:///:memory:")
        repository = DatasetRepository(db)
        seed_video(repository, VIDEO)
        repo = repository
        themes = [
            "clear learning roadmap path",
            "setup problems npm install failing",
            "react projects portfolio ideas",
        ]
        repo.upsert_comments(
            [
                comment_row(f"c{i}", text=themes[i % len(themes)])
                for i in range(scale)
            ],
            batch_size=500,
        )
        SentimentService(repo, make_settings()).get_analysis(VIDEO)
        service = _service(repo)

        started = __import__("time").perf_counter()
        response = service.get_insight(VIDEO)
        elapsed = __import__("time").perf_counter() - started

        assert response.status == "READY"
        # Evidence + phrasing is O(topics), not O(comments); generous
        # CI-safe bound (measured locally well under 1s at 1000).
        assert elapsed < 10.0
        db.close()

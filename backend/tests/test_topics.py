"""Sprint 6 tests: topic / discussion / audience-preference intelligence.

Coverage map (brief §43):
- Extraction: meaningful themes discovered, generic/URL/username/gibberish
  filtered, similar phrasing merged, distinct themes kept apart,
  deterministic output, rare phrases never become topics.
- Frequency: exact mention counts, explicit analyzed denominator, share
  math, sentiment counts summing to mentions, percentages summing to 100.
- Positive themes: appreciated category, minimum-support gating, a small
  100%-positive topic never outranking a large 70%-positive one, and the
  documented frequency-weighted ordering.
- Pain points: repeated negative themes, negative-support gate, mixed
  topics never forced into good/bad.
- Topic x sentiment / emotion / intensity: aggregation of STORED verdicts
  only (no re-inference - proven by monkeypatching the classifiers).
- Service: untracked 404, INSUFFICIENT_DATA below the configured minimum,
  honest empty copy, memoization keyed by the analysis fingerprint,
  content-reset invalidation, language-skipped rows excluded from the
  denominator.
- Active video: A -> B switches can never surface A's topics for B.
- API: camelCase contract, insufficient/404/422 states, background job
  completes with the TOPIC phase, topic failure never fails the analysis
  (§39).
- Performance: representative datasets 100 / 500 / 1000 / 2500 / 5000.

No test fabricates intelligence: every topic comes from the real lexical
discovery over fixture text, every metric from the documented aggregation
rules over stored verdicts.
"""
import time

import pytest

from app.db.connection import Database
from app.db.repository import DatasetRepository
from app.services.sentiment import SentimentService
from app.services.topics import AnalyzedComment, TopicService, discover_topics
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

MIN_SUPPORT = 8
MAX_TOPICS = 24


def mk(text, sentiment="POSITIVE", emotion="TRUST", intensity="LOW"):
    return AnalyzedComment(text=text, sentiment=sentiment, emotion=emotion, intensity=intensity)


def theme(text, count, sentiment="POSITIVE", emotion="TRUST", intensity="LOW"):
    return [mk(text, sentiment, emotion, intensity) for _ in range(count)]


def labels_of(topics):
    return [t["label"] for t in topics]


def find(topics, needle):
    needle = needle.lower()
    for topic in topics:
        if needle in topic["label"].lower():
            return topic
    raise AssertionError(f"no topic containing {needle!r} in {labels_of(topics)}")


# ---------------------------------------------------------------------------
# Extraction (§5/§6/§7/§22)
# ---------------------------------------------------------------------------


class TestExtraction:
    def test_repeated_theme_discovered_with_real_counts(self):
        comments = (
            theme("this learning roadmap is really helpful", 40)
            + theme("installation failed with npm install error", 25, "NEGATIVE", "FEAR", "MEDIUM")
            + theme("nice video good stuff", 100, "NEUTRAL", "NEUTRAL", "LOW")
        )
        topics = discover_topics(comments, MIN_SUPPORT, MAX_TOPICS)
        assert len(topics) == 2
        roadmap = find(topics, "roadmap")
        assert roadmap["mentions"] == 40
        setup = find(topics, "npm")  # "Npm install error" theme label
        assert setup["mentions"] == 25

    def test_generic_words_never_become_topics(self):
        # §22: bare "video" / "good" / "nice" are not themes.
        comments = theme("nice video good stuff great video", 60, "NEUTRAL", "NEUTRAL")
        assert discover_topics(comments, MIN_SUPPORT, MAX_TOPICS) == []

    def test_urls_and_usernames_filtered(self):
        # URL and @mention text is stripped before phrase extraction; what
        # remains of these fixtures is generic-only, so no theme appears.
        comments = theme("https://spam.example.com/watch nice video", 40, "NEUTRAL", "NEUTRAL")
        comments += theme("@someuser123 is great great video", 40, "NEUTRAL", "NEUTRAL")
        assert discover_topics(comments, MIN_SUPPORT, MAX_TOPICS) == []

    def test_gibberish_ids_filtered(self):
        # Long non-word runs (ids/keyboard mash) exceed the token bound.
        comments = theme("aabbccddeeffgghhiijjkk", 40, "NEUTRAL", "NEUTRAL")
        assert discover_topics(comments, MIN_SUPPORT, MAX_TOPICS) == []

    def test_similar_phrasing_merges_into_one_theme(self):
        # §7: the same idea phrased with overlapping content words groups
        # into ONE theme with a deterministic human-readable label (§8).
        comments = theme("clear learning roadmap", 30)
        comments += theme("helpful learning roadmap", 30)
        topics = discover_topics(comments, MIN_SUPPORT, MAX_TOPICS)
        assert len(topics) == 1
        assert topics[0]["label"] == "Clear learning roadmap"
        assert topics[0]["mentions"] == 60

    def test_distinct_themes_stay_separate(self):
        comments = theme("installation failed npm error", 20, "NEGATIVE", "FEAR")
        comments += theme("career advice for job interviews", 20, "POSITIVE", "ANTICIPATION")
        topics = discover_topics(comments, MIN_SUPPORT, MAX_TOPICS)
        assert len(topics) == 2
        joined = " ".join(labels_of(topics)).lower()
        assert "npm" in joined  # setup-flavored discussion
        assert "career" in joined  # careers discussion - never merged

    def test_rare_phrases_never_become_topics(self):
        # Below the candidate df bound (3) a phrase is invisible; and the
        # final support gate keeps themes under TOPIC_MIN_SUPPORT out.
        comments = theme("the onboarding workflow confused everyone", 5)
        comments += theme("nice video good stuff", 50, "NEUTRAL", "NEUTRAL")
        assert discover_topics(comments, MIN_SUPPORT, MAX_TOPICS) == []

    def test_output_is_deterministic(self):
        comments = (
            theme("clear learning roadmap", 30)
            + theme("setup problems npm", 20, "NEGATIVE", "ANGER")
            + theme("react projects tutorial", 15, "NEUTRAL", "NEUTRAL")
        )
        first = discover_topics(comments, MIN_SUPPORT, MAX_TOPICS)
        second = discover_topics(comments, MIN_SUPPORT, MAX_TOPICS)
        assert first == second

    def test_empty_input_is_safe(self):
        assert discover_topics([], MIN_SUPPORT, MAX_TOPICS) == []


# ---------------------------------------------------------------------------
# Frequency + denominators (§9/§42)
# ---------------------------------------------------------------------------


class TestFrequency:
    def test_mentions_share_and_explicit_denominator(self):
        comments = theme("clear learning roadmap", 25) + theme("nice video good stuff", 75, "NEUTRAL")
        topics = discover_topics(comments, MIN_SUPPORT, MAX_TOPICS)
        roadmap = find(topics, "clear")
        assert roadmap["mentions"] == 25
        # Denominator = analyzed comments (100 here), documented §42.
        assert roadmap["share_percent"] == 25.0

    def test_topic_sentiment_counts_sum_to_mentions(self):
        comments = (
            theme("clear learning roadmap", 60, "POSITIVE")
            + theme("clear learning roadmap", 20, "NEUTRAL", "NEUTRAL")
            + theme("clear learning roadmap", 20, "NEGATIVE", "ANGER")
        )
        topics = discover_topics(comments, MIN_SUPPORT, MAX_TOPICS)
        roadmap = find(topics, "clear")
        counts = roadmap["sentiment_counts"]
        assert counts == {"POSITIVE": 60, "NEUTRAL": 20, "NEGATIVE": 20}
        assert sum(counts.values()) == roadmap["mentions"] == 100

    def test_one_comment_may_belong_to_two_themes(self):
        # Themes are not a partition (documented); each keeps its own
        # honest denominator.
        comments = theme("installation failed npm error", 20, "NEGATIVE", "FEAR")
        comments += theme("installation failed but career advice helped", 20, "POSITIVE", "JOY")
        topics = discover_topics(comments, MIN_SUPPORT, MAX_TOPICS)
        # Shared "installation failed" discussion bridges both batches into
        # one theme with 40 mentions (co-discussion merge rule), while the
        # career vocabulary stays its own separate theme.
        assert len(topics) == 2
        assert find(topics, "npm")["mentions"] == 40  # "Failed npm error"
        assert find(topics, "career")["mentions"] == 20


# ---------------------------------------------------------------------------
# Categories: appreciated / pain / mixed (§11/§14/§15/§19)
# ---------------------------------------------------------------------------


class TestCategories:
    def test_strong_positive_theme_is_appreciated(self):
        comments = theme("practical examples are excellent", 40)
        topics = discover_topics(comments, MIN_SUPPORT, MAX_TOPICS)
        assert find(topics, "practical")["category"] == "APPRECIATED"

    def test_repeated_negative_theme_is_pain_point(self):
        comments = theme("setup problems keep failing", 20, "NEGATIVE", "FEAR", "MEDIUM")
        comments += theme("nice video good stuff", 40, "NEUTRAL", "NEUTRAL")
        topics = discover_topics(comments, MIN_SUPPORT, MAX_TOPICS)
        assert find(topics, "setup")["category"] == "PAIN_POINT"

    def test_divided_theme_is_mixed_not_forced_good_or_bad(self):
        # 43% positive / 21% neutral / 36% negative -> MIXED (§15).
        comments = theme("react complexity is confusing", 43)
        comments += theme("react complexity is confusing", 21, "NEUTRAL", "NEUTRAL")
        comments += theme("react complexity is confusing", 36, "NEGATIVE", "ANGER")
        topics = discover_topics(comments, MIN_SUPPORT, MAX_TOPICS)
        react = find(topics, "react")
        assert react["category"] == "MIXED"
        assert react["mentions"] == 100

    def test_lean_theme_without_strong_side_is_plain_discussion(self):
        # 70% positive is leaning but below the 60%+... actually 70 >= 60,
        # so use a genuinely weak lean: 50/50 with no minority >= 20? Use
        # 55% pos / 5% neu / 40% neg -> that is PAIN (neg >= 40). The only
        # remaining bucket: pos 55 / neg 30 / neu 15 -> MIXED needs both
        # sides >= 20 (30 yes) -> MIXED. So plain MOST_DISCUSSED requires
        # a small side: 55/42/3 -> neg 42 >= 40 -> PAIN. Verified rule
        # coverage: pos 65 / neg 15 / neu 20 -> APPRECIATED; the fallback
        # happens when neg < 40, pos < 60 and one side < 20:
        # pos 55 / neu 35 / neg 10.
        comments = theme("weekly summary of the news", 55)
        comments += theme("weekly summary of the news", 35, "NEUTRAL", "NEUTRAL")
        comments += theme("weekly summary of the news", 10, "NEGATIVE", "SADNESS")
        topics = discover_topics(comments, MIN_SUPPORT, MAX_TOPICS)
        assert find(topics, "weekly")["category"] == "MOST_DISCUSSED"


class TestAppreciatedRanking:
    def test_small_high_positive_topic_never_enters(self):
        # §11/§12: 5 mentions at 100% positive cannot even qualify.
        comments = theme("tiny praise gem", 5)
        comments += theme("practical examples are excellent", 40)
        comments += theme("nice video good stuff", 60, "NEUTRAL", "NEUTRAL")
        topics = discover_topics(comments, MIN_SUPPORT, MAX_TOPICS)
        response_sections = _sections(topics)
        assert "tiny" not in " ".join(labels_of(topics)).lower()
        assert response_sections["most_appreciated"]

    def test_frequency_beats_pure_ratio(self):
        # X: 20/20 positive (100%). Y: 28/40 positive (70%).
        # PreferenceScore X = 0.6*(20/28) + 0.4*1.0 = 0.829
        # PreferenceScore Y = 0.6*1.0      + 0.4*0.7 = 0.880 -> Y first.
        x = theme("clear explanations are wonderful", 20)
        y = theme("practical examples are helpful", 28)
        y += theme("practical examples are boring", 12, "NEUTRAL", "NEUTRAL")
        comments = x + y + theme("nice video good stuff", 40, "NEUTRAL", "NEUTRAL")
        topics = discover_topics(comments, MIN_SUPPORT, MAX_TOPICS)
        sections = _sections(topics)
        order = labels_of(sections["most_appreciated"])
        assert order[0].startswith("Practical")
        assert order[1].startswith("Clear")

    def test_positive_support_gate_uses_positive_mentions(self):
        # Category APPRECIATED (60% positive) but only 6 POSITIVE
        # mentions (< 8): stays out of the ranked section entirely.
        comments = theme("lovely thumbnails design", 6)
        comments += theme("lovely thumbnails design", 4, "NEUTRAL", "NEUTRAL")
        comments += theme("practical examples are excellent", 30)
        comments += theme("nice video good stuff", 40, "NEUTRAL", "NEUTRAL")
        topics = discover_topics(comments, MIN_SUPPORT, MAX_TOPICS)
        sections = _sections(topics)
        labels = labels_of(sections["most_appreciated"])
        assert not any(label.startswith("Lovely") for label in labels)


class TestPainAndMixedRanking:
    def test_pain_requires_repeated_negative_support(self):
        # 14 mentions at 50% negative -> PAIN_POINT category, but only 7
        # negative mentions (< 8): excluded from the ranked pain list.
        comments = theme("glitchy cursor issue", 7, "NEGATIVE", "FEAR")
        comments += theme("glitchy cursor issue", 7, "POSITIVE", "JOY")
        comments += theme("setup problems keep failing", 20, "NEGATIVE", "ANGER", "MEDIUM")
        comments += theme("nice video good stuff", 60, "NEUTRAL", "NEUTRAL")
        topics = discover_topics(comments, MIN_SUPPORT, MAX_TOPICS)
        sections = _sections(topics)
        pain_labels = labels_of(sections["pain_points"])
        assert any("Setup" in label for label in pain_labels)
        assert not any("Glitchy" in label for label in pain_labels)

    def test_mixed_list_contains_only_divided_themes(self):
        comments = theme("react complexity is confusing", 43)
        comments += theme("react complexity is confusing", 21, "NEUTRAL", "NEUTRAL")
        comments += theme("react complexity is confusing", 36, "NEGATIVE", "ANGER")
        comments += theme("practical examples are excellent", 30)
        topics = discover_topics(comments, MIN_SUPPORT, MAX_TOPICS)
        sections = _sections(topics)
        mixed_labels = labels_of(sections["mixed_topics"])
        assert len(mixed_labels) == 1
        assert mixed_labels[0].startswith("React")
        appreciated = labels_of(sections["most_appreciated"])
        assert appreciated and appreciated[0].startswith("Practical")
        # The appreciated theme never appears in pain.
        assert not any(label.startswith("Practical") for label in labels_of(sections["pain_points"]))

    def test_most_discussed_orders_by_frequency(self):
        comments = (
            theme("learning roadmap path", 60)
            + theme("react projects ideas", 40, "NEUTRAL", "NEUTRAL")
            + theme("career opportunities jobs", 20, "POSITIVE", "ANTICIPATION")
        )
        topics = discover_topics(comments, MIN_SUPPORT, MAX_TOPICS)
        sections = _sections(topics)
        counts = [t["mentions"] for t in sections["most_discussed"]]
        assert counts == sorted(counts, reverse=True)
        assert counts[0] == 60


def _sections(topics):
    """Ranked sections through the SERVICE's documented section builder."""
    from app.services.topics import _split_sections

    return _split_sections(topics, MIN_SUPPORT)


# ---------------------------------------------------------------------------
# Topic x sentiment / emotion / intensity (§16/§17/§18)
# ---------------------------------------------------------------------------


class TestAggregates:
    def test_dominant_emotion_and_intensity_reuse_stored_verdicts(self):
        comments = theme("clear learning roadmap", 6, "POSITIVE", "TRUST", "LOW")
        comments += theme("clear learning roadmap", 4, "POSITIVE", "JOY", "MEDIUM")
        topics = discover_topics(comments, MIN_SUPPORT, MAX_TOPICS)
        roadmap = find(topics, "clear")
        assert roadmap["emotion_counts"] == {
            "FEAR": 0, "ANGER": 0, "ANTICIPATION": 0, "TRUST": 6,
            "SURPRISE": 0, "SADNESS": 0, "DISGUST": 0, "JOY": 4,
            "NEUTRAL": 0,
        }
        assert roadmap["intensity_counts"] == {"LOW": 6, "MEDIUM": 4, "HIGH": 0}

    def test_never_reinfers_sentiment_or_emotion(self, monkeypatch):
        # §17: aggregation only - the engine must never call the models.
        import app.services.sentiment as sentiment_module

        def boom(*_args, **_kwargs):
            raise AssertionError("topic discovery must not run model inference")

        monkeypatch.setattr(sentiment_module, "classify", boom)
        monkeypatch.setattr(sentiment_module, "classify_emotion", boom)
        comments = theme("clear learning roadmap", 30)
        topics = discover_topics(comments, MIN_SUPPORT, MAX_TOPICS)
        assert find(topics, "clear")["mentions"] == 30


# ---------------------------------------------------------------------------
# Service layer (§12/§24/§38 + memo)
# ---------------------------------------------------------------------------


@pytest.fixture
def repo():
    db = Database("sqlite:///:memory:")
    repository = DatasetRepository(db)
    seed_video(repository, VIDEO)
    yield repository
    db.close()


@pytest.fixture
def topic_service(repo):
    return TopicService(repo, make_settings())


def _analyzed(repo, count, text="clear learning roadmap for react", video_id=VIDEO):
    """Seed `count` comments and run them through the REAL sentiment
    pipeline so rows carry genuine verdicts."""
    repo.upsert_comments(
        [comment_row(f"c{i}", video_id=video_id, text=text) for i in range(count)],
        batch_size=100,
    )
    SentimentService(repo, make_settings()).get_analysis(video_id)


class TestService:
    def test_untracked_video_returns_none(self, topic_service):
        assert topic_service.get_analysis("aaaaaaaaaaa") is None

    def test_insufficient_below_min_comment_count(self, repo, topic_service):
        _analyzed(repo, 5)
        response = topic_service.get_analysis(VIDEO)
        assert response is not None
        assert response.status == "INSUFFICIENT_DATA"
        assert response.analyzed_comments == 5
        assert response.topics == []
        assert response.message == (
            "More audience responses are needed to identify repeated themes."
        )

    def test_ready_with_discovered_theme_at_scale(self, repo, topic_service):
        _analyzed(repo, 35)
        response = topic_service.get_analysis(VIDEO)
        assert response.status == "READY"
        assert response.analyzed_comments == 35
        assert response.message is None
        assert len(response.topics) >= 1
        assert response.topics[0].mentions >= MIN_SUPPORT
        assert response.most_discussed

    def test_empty_discovery_has_honest_copy(self, repo, topic_service):
        # 30+ analyzed comments but every text unique (no repeated phrase
        # reaches the candidate bound) -> READY with zero themes and the
        # §38 "not enough repeated discussion" message.
        def unique_word(index: int) -> str:
            # Letter-only, globally unique, <= 14 chars: every comment
            # carries vocabulary no other comment repeats (all document
            # frequencies stay at 1, below the candidate bound).
            return "x" * (index // 26 + 1) + chr(97 + index % 26)

        repo.upsert_comments(
            [
                comment_row(
                    f"c{i}",
                    text=" ".join(unique_word(i * 6 + j) for j in range(6)),
                )
                for i in range(35)
            ],
            batch_size=100,
        )
        SentimentService(repo, make_settings()).get_analysis(VIDEO)
        response = topic_service.get_analysis(VIDEO)
        assert response.status == "READY"
        assert response.topics == []
        assert response.message == "Not enough repeated discussion yet."

    def test_memo_reused_until_analysis_fingerprint_changes(self, repo, topic_service):
        _analyzed(repo, 35)
        first = topic_service.get_analysis(VIDEO)
        second = topic_service.get_analysis(VIDEO)
        assert first is second  # memo hit, no recomputation

        # Content change resets row c0 -> analyzed drops -> new fingerprint.
        changed = comment_row("c0", text="a completely different new text")
        repo.upsert_comments([changed], batch_size=10)
        third = topic_service.get_analysis(VIDEO)
        assert third is not first
        assert third.analyzed_comments == 34

    def test_language_skipped_rows_never_enter_denominator(self, repo, topic_service):
        repo.upsert_comments(
            [comment_row(f"c{i}", text="clear learning roadmap") for i in range(30)]
            + [
                comment_row(f"e{i}", text="buen tutorial muy bueno", language="es")
                for i in range(10)
            ],
            batch_size=100,
        )
        SentimentService(repo, make_settings()).get_analysis(VIDEO)
        response = topic_service.get_analysis(VIDEO)
        assert response.analyzed_comments == 30  # §23 correct denominator
        assert response.topics
        assert response.topics[0].mentions <= 30

    def test_response_invariants(self, repo, topic_service):
        _analyzed(repo, 40, text="clear learning roadmap")
        response = topic_service.get_analysis(VIDEO)
        for item in response.topics:
            counts = item.sentiment
            assert sum(share.count for share in counts.values()) == item.mentions
            assert sum(share.percent for share in counts.values()) == pytest.approx(100.0)
            assert 0.0 <= item.confidence <= 1.0
            assert item.category in (
                "MOST_DISCUSSED", "APPRECIATED", "PAIN_POINT", "MIXED",
            )
            # CamelCase serialization sanity.
            dump = item.model_dump(by_alias=True)
            assert {"topicId", "mentions", "sharePercent", "category"} <= set(dump)


# ---------------------------------------------------------------------------
# Active-video safety (§40)
# ---------------------------------------------------------------------------


class TestActiveVideo:
    def test_switching_video_never_surfaces_old_topics(self, repo, topic_service):
        _analyzed(repo, 35, video_id=VIDEO)
        first = topic_service.get_analysis(VIDEO)
        assert first.status == "READY"

        # Switch: A's rows are cascaded away (Sprint 4.2 lifecycle).
        repo.switch_active_video(OTHER_VIDEO)
        assert topic_service.get_analysis(VIDEO) is None  # 404, never stale
        second = topic_service.get_analysis(OTHER_VIDEO)
        assert second is not None
        assert second.video_id == OTHER_VIDEO
        assert second.topics == []  # B has no analyzed comments yet

    def test_rapid_switching_between_analyzed_videos(self, repo, topic_service):
        seed_video(repo, OTHER_VIDEO)
        _analyzed(repo, 35, video_id=VIDEO)
        a_topics = topic_service.get_analysis(VIDEO)
        assert a_topics.status == "READY"

        repo.switch_active_video(OTHER_VIDEO)
        _analyzed(repo, 35, video_id=OTHER_VIDEO, text="setup problems keep failing")
        b_topics = topic_service.get_analysis(OTHER_VIDEO)
        assert b_topics.video_id == OTHER_VIDEO
        # B's theme is B's own discussion, not A's memoized result.
        assert "setup" in b_topics.topics[0].label.lower()
        assert topic_service.get_analysis(VIDEO) is None


# ---------------------------------------------------------------------------
# API contract (§28) + job integration (§25/§39)
# ---------------------------------------------------------------------------


def _fake_with_comments(comments, video_id: str = VIDEO) -> FakeYouTubeClient:
    return FakeYouTubeClient(
        video=YtVideoListResponse.model_validate(video_payload(video_id)),
        pages={None: ([thread_payload(cid, text) for cid, text in comments], None)},
    )


def _acquire(client: TestClient, video_id: str = VIDEO) -> None:
    assert client.get(f"/api/videos/{video_id}").status_code == 200


THEME_COMMENTS = [
    (f"c{i}", "this learning roadmap is really helpful for react") for i in range(35)
]


class TestTopicsEndpoint:
    def test_camelcase_contract_over_real_analysis(self, app_factory):
        client = app_factory(_fake_with_comments(THEME_COMMENTS))
        _acquire(client)
        assert client.get(f"/api/videos/{VIDEO}/sentiment").status_code == 200

        body = client.get(f"/api/videos/{VIDEO}/topics").json()
        assert body["videoId"] == VIDEO
        assert body["status"] == "READY"
        assert body["analyzedComments"] == 35
        assert body["message"] is None
        topic = body["topics"][0]
        assert {"topicId", "label", "mentions", "sharePercent", "keyPhrases",
                "category", "confidence", "evidence", "sentiment",
                "dominantSentiment", "emotion", "intensity"} <= set(topic)
        assert sum(
            v["count"] for v in topic["sentiment"].values()
        ) == topic["mentions"]
        assert {"mostDiscussed", "mostAppreciated", "painPoints",
                "mixedTopics", "topics"} <= set(body)

    def test_insufficient_data_state(self, app_factory):
        client = app_factory(
            _fake_with_comments([("c1", "nice video good stuff")])
        )
        _acquire(client)
        client.get(f"/api/videos/{VIDEO}/sentiment")
        body = client.get(f"/api/videos/{VIDEO}/topics").json()
        assert body["status"] == "INSUFFICIENT_DATA"
        assert body["topics"] == []
        assert "More audience responses" in body["message"]

    def test_untracked_video_404_and_invalid_id_422(self, app_factory):
        client = app_factory(_fake_with_comments([]))
        assert client.get("/api/videos/aaaaaaaaaaa/topics").status_code == 404
        assert client.get("/api/videos/xx/topics").status_code == 422

    def test_background_job_completes_through_topic_phase(self, app_factory):
        from tests.test_analysis_jobs import _fake, wait_for_terminal

        pages = {None: ([thread_payload(cid, text) for cid, text in THEME_COMMENTS], None)}
        client = app_factory(_fake(pages=pages))
        assert client.post(f"/api/videos/{VIDEO}/analysis").status_code == 202
        final = wait_for_terminal(client, VIDEO)
        assert final["status"] == "COMPLETED"
        assert final["phase"] == "COMPLETE"
        body = client.get(f"/api/videos/{VIDEO}/topics").json()
        assert body["status"] == "READY"
        assert body["topics"]

    def test_topic_failure_never_fails_the_analysis(self, app_factory, monkeypatch):
        # §39: a topic-engine failure preserves sentiment + job outcome.
        from tests.test_analysis_jobs import _fake, wait_for_terminal

        pages = {None: ([thread_payload(cid, text) for cid, text in THEME_COMMENTS], None)}
        client = app_factory(_fake(pages=pages))

        def boom(*_args, **_kwargs):
            raise RuntimeError("discovery exploded")

        monkeypatch.setattr(client.app.state.topic_service, "get_analysis", boom)
        assert client.post(f"/api/videos/{VIDEO}/analysis").status_code == 202
        final = wait_for_terminal(client, VIDEO)
        assert final["status"] == "COMPLETED"
        # Sentiment results survive intact.
        sentiment = client.get(f"/api/videos/{VIDEO}/sentiment").json()
        assert sentiment["status"] == "PROCESSED"
        assert sentiment["stats"]["analyzed"] == 35

    def test_switching_videos_via_api_isolates_topics(self, app_factory):
        client = app_factory(_fake_with_comments(THEME_COMMENTS))
        _acquire(client, VIDEO)
        client.get(f"/api/videos/{VIDEO}/sentiment")
        assert client.get(f"/api/videos/{VIDEO}/topics").json()["status"] == "READY"

        _acquire(client, OTHER_VIDEO)  # active-video switch (Sprint 4.2)
        assert client.get(f"/api/videos/{VIDEO}/topics").status_code == 404
        body = client.get(f"/api/videos/{OTHER_VIDEO}/topics").json()
        assert body["videoId"] == OTHER_VIDEO
        assert body["topics"] == []  # B's fresh state, never A's themes


# ---------------------------------------------------------------------------
# Performance (§26)
# ---------------------------------------------------------------------------


class TestPerformance:
    @pytest.mark.parametrize("scale", [100, 500, 1000, 2500, 5000])
    def test_discovery_scales_to_dataset_sizes(self, scale):
        themes = [
            ("clear learning roadmap path", "POSITIVE", "TRUST"),
            ("setup problems npm install", "NEGATIVE", "FEAR"),
            ("react projects ideas portfolio", "NEUTRAL", "NEUTRAL"),
            ("career opportunities jobs", "POSITIVE", "ANTICIPATION"),
            ("explanation quality tutorial", "POSITIVE", "JOY"),
        ]
        comments = []
        for index in range(scale):
            text, sentiment, emotion = themes[index % len(themes)]
            intensity = "HIGH" if index % 3 == 0 else "LOW"
            comments.append(mk(text, sentiment, emotion, intensity))

        started = time.perf_counter()
        topics = discover_topics(comments, MIN_SUPPORT, MAX_TOPICS)
        elapsed = time.perf_counter() - started

        assert len(topics) == 5  # all five themes discovered
        assert all(t["mentions"] == scale // len(themes) or
                   t["mentions"] == -(-scale // len(themes)) for t in topics)
        # Generous CI-safe bound; the real measurements live in
        # backend/benchmarks/bench_topics.py (§26 report numbers).
        assert elapsed < 3.0, f"discovery took {elapsed:.2f}s at scale {scale}"

"""Unit tests: language detection metadata (documented method, never guessed).

Sprint 5.1 (§15): also covers the bounded exact-text result cache - the
optimization must never change a verdict, never grow unbounded, and never
serve a stale label (the detector is deterministic, seed 0, so a cached
result IS the detector's result).
"""
import pytest

import app.services.language as language_module
from app.services.language import MAX_CACHE_ENTRIES, UNKNOWN_LANGUAGE, detect_language

ENGLISH = "This video is absolutely amazing, great work! Thanks for sharing."
HINDI = "यह वीडियो बहुत अच्छा है और मैंने इसे दोबारा देखा"
KANNADA = "ಈ ವಿಡಿಯೋ ತುಂಬಾ ಚೆನ್ನಾಗಿದೆ ನಾನು ಮತ್ತೆ ನೋಡುತ್ತೇನೆ"


class TestDetectedLanguages:
    def test_english_detected(self):
        assert detect_language(ENGLISH) == "en"

    def test_hindi_detected(self):
        assert detect_language(HINDI) == "hi"

    def test_kannada_detected(self):
        assert detect_language(KANNADA) == "kn"


class TestUnknownInsteadOfGuessing:
    def test_emoji_only_is_unknown(self):
        assert detect_language("\U0001F602\U0001F602\U0001F525") == UNKNOWN_LANGUAGE

    def test_digits_and_empty_are_unknown(self):
        assert detect_language("") == UNKNOWN_LANGUAGE
        assert detect_language("123 456 7890") == UNKNOWN_LANGUAGE

    def test_short_ambiguous_text_below_confidence_is_unknown(self):
        # langdetect would label this Italian with only ~0.46 confidence;
        # the threshold must win over a fabricated label.
        assert detect_language("nice video") == UNKNOWN_LANGUAGE

    def test_transliterated_text_not_confident_enough(self):
        # Latin-script Hinglish: statistical detector says Swahili at ~0.86,
        # under MIN_CONFIDENCE (0.90) -> honest 'unknown'.
        assert detect_language("bhai mast video hai yaar") == UNKNOWN_LANGUAGE


class TestDeterminism:
    def test_same_text_same_label(self):
        results = {detect_language(ENGLISH) for _ in range(5)}
        assert results == {"en"}

    def test_no_english_leak_for_hindi(self):
        assert detect_language(HINDI) != "en"


class _Guess:
    """Minimal stand-in for langdetect's candidate object."""

    def __init__(self, lang="en", prob=0.99):
        self.lang = lang
        self.prob = prob


class TestBoundedResultCache:
    """Sprint 5.1 §15: cache correctness, reuse, and memory bounds."""

    @pytest.fixture(autouse=True)
    def _isolated_cache(self):
        # Module-level cache: isolate these tests from the real-detector
        # ones (and never leak faked results into them).
        language_module._cache.clear()
        yield
        language_module._cache.clear()

    @staticmethod
    def _fake_detector(calls: list):
        def _detect(text: str):
            calls.append(text)
            return [_Guess()]
        return _detect

    def test_identical_text_is_detected_once_and_result_is_reused(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(language_module, "detect_langs", self._fake_detector(calls))

        first = detect_language("wonderful heartfelt comment here")
        second = detect_language("wonderful heartfelt comment here")

        assert first == second == "en"
        assert len(calls) == 1  # second call served from the cache

    def test_distinct_texts_each_reach_the_detector(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(language_module, "detect_langs", self._fake_detector(calls))

        detect_language("first distinct sentence entirely")
        detect_language("second different sentence instead")
        detect_language("first distinct sentence entirely")

        assert len(calls) == 2  # only the repeat is served from cache

    def test_cached_unknown_is_still_unknown(self, monkeypatch):
        calls: list = []

        def _low_confidence(_text: str):
            calls.append(_text)
            return [_Guess(lang="it", prob=0.5)]  # below MIN_CONFIDENCE

        monkeypatch.setattr(language_module, "detect_langs", _low_confidence)
        text = "sotto soglia di confidenza"
        assert detect_language(text) == UNKNOWN_LANGUAGE
        assert detect_language(text) == UNKNOWN_LANGUAGE
        assert len(calls) == 1  # the unknown verdict is cached too

    def test_cache_never_exceeds_its_entry_bound(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(language_module, "detect_langs", self._fake_detector(calls))

        total = MAX_CACHE_ENTRIES + 64
        for index in range(total):
            detect_language(f"unique short comment number {index}")

        assert len(language_module._cache) <= MAX_CACHE_ENTRIES
        assert len(calls) == total  # every UNIQUE text still hit the detector

    def test_long_texts_are_detected_but_never_cached(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(language_module, "detect_langs", self._fake_detector(calls))
        long_text = ("a rather long comment that exceeds the cacheable size limit. " * 20).strip()
        assert len(long_text) > language_module.MAX_CACHE_TEXT

        assert detect_language(long_text) == "en"
        assert long_text not in language_module._cache

    def test_non_alpha_fast_path_bypasses_the_detector(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(language_module, "detect_langs", self._fake_detector(calls))
        assert detect_language("\U0001F602\U0001F602 12345") == UNKNOWN_LANGUAGE
        assert calls == []  # no alphabetic content - never a detector call
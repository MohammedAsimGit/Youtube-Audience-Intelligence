"""Sprint 5.2 §4 - exact-parity fast VADER scorer (app.services.vader_fast).

The fast path may change SPEED only, never a score: every test here
compares `FastSentimentIntensityAnalyzer` against the stock
`SentimentIntensityAnalyzer` on identical inputs - including the
adversarial negation/idiom/caps/emoji cases the algorithm special-cases
and a deterministic slice of the benchmark corpus. A vaderSentiment
upgrade that changes these methods trips these tests loudly.
"""
import pytest
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from app.services import sentiment as sentiment_module
from app.services.vader_fast import FastSentimentIntensityAnalyzer

# Module-level instances: lexicon files load once per class, tests only
# compare scores (loading cost is measured in the benchmark, not here).
STOCK = SentimentIntensityAnalyzer()
FAST = FastSentimentIntensityAnalyzer()

# Every branch of the hot path: booster words, "kind of"/"sort of",
# explicit negation windows (never/without/no + or/nor), ALL-CAPS
# differential, contrastive "but", special-case idioms, emoji conversion
# (with and without surrounding spaces), punctuation emphasis, empty and
# non-lexical inputs.
ADVERSARIAL = [
    "",
    "   ",
    "no",
    "not good",
    "kind of good",
    "The book was only kind of good.",
    "I am NOT so sure this is good",
    "without doubt this is great",
    "never so bad",
    "never so good and bad",
    "GOOD and BAD",
    "Amazing!!! Really???",
    "\U0001F525 fire \U0001F525 content \U0001F602",
    "\U0001F602",
    "lol \U0001F602\U0001F44D wow",
    "the shit",
    "yeah right",
    "bad ass",
    "At least it isn't a horrible book.",
    "but wait",
    "VERY GOOD but boring",
    "lol",
    "12345",
    "kind  of",
    "sort of ok",
    "This is the shit honestly",
    "kiss of death",
    "to die for",
    "bus stop",
    "no no no good",
    "This was bad, but the ending was good",
    "WORST. DAY. EVER!!!",
    "I guess it's fine... maybe??",
    "\U0001F525",
    "haha \U0001F602\U0001F602\U0001F602 wow",
    "Absolutely terrible, not good at all, never watching again.",
    "At least it wasn't boring, kind of fun actually",
    "The service was WITHOUT DOUBT the worst",
]


@pytest.mark.parametrize("text", ADVERSARIAL)
def test_adversarial_inputs_score_identically(text: str) -> None:
    assert STOCK.polarity_scores(text) == FAST.polarity_scores(text)


def test_benchmark_corpus_scores_identically() -> None:
    """Representative unique-comment corpus (deterministic, SEED=42):
    every text must score bit-for-bit the same on both analyzers."""
    from benchmarks.bench_pipeline import build_corpus

    checked = 0
    for _, text in build_corpus(500):
        stock = STOCK.polarity_scores(text)
        fast = FAST.polarity_scores(text)
        assert stock == fast, (
            f"parity broken for {text[:60]!r}: stock={stock} fast={fast}"
        )
        checked += 1
    assert checked == 500


def test_non_string_input_fails_like_the_stock_analyzer() -> None:
    # Same failure class as the base `for chr in text` loop - the fast path
    # must not silently accept inputs the stock analyzer rejects.
    with pytest.raises(TypeError):
        STOCK.polarity_scores(None)
    with pytest.raises(TypeError):
        FAST.polarity_scores(None)


def test_analyzer_is_the_fast_scorer_and_is_reused() -> None:
    """§3: model loaded once, reused (the fast scorer keeps the same
    lazy process-wide singleton contract)."""
    first = sentiment_module._get_analyzer()
    second = sentiment_module._get_analyzer()
    assert isinstance(first, FastSentimentIntensityAnalyzer)
    assert first is second

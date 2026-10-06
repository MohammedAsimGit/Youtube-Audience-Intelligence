"""Language metadata detection (Sprint 3) - documented, never fabricated.

Method (docs/architecture/data-pipeline.md §Language detection):
- Library: `langdetect` 1.0.9 (statistical n-gram model, pure Python).
- Determinism: `DetectorFactory.seed = 0` is set once here, so the same text
  always yields the same language label across runs/machines.
- Confidence: the top candidate must reach MIN_CONFIDENCE (0.90); otherwise
  the label would be a guess, so we store `unknown` instead. This matters
  most for short Latin-script text (e.g. Hinglish transliteration), which
  statistical detectors happily mislabel with high confidence below that
  bar.
- No alphabetic content (emoji-only, digits-only, empty) -> `unknown`
  without even consulting the detector.
- Output is an ISO 639-1 code (`en`, `hi`, `kn`, `mr`, `ur`, ...); langdetect's
  region variants (`zh-cn`/`zh-tw`) are folded to `zh`.

Known limitation (documented honestly): statistical detection is unreliable
on very short or transliterated comments (e.g. Latin-script Hinglish) - those
either fall below the confidence threshold or are labeled by majority-script
signal. No language is ever inferred from anything but the text itself.

Sprint 5.1 (§15 language detection profiling):
- Measured cost: ~1.6 ms fixed overhead + length-scaled work per call
  (benchmark: backend/benchmarks/bench_pipeline.py micro) - langdetect was
  89% of total pipeline time on a 5,000-comment dataset.
- Optimization: a bounded exact-text memo of RESULTS. `detect_language` is a
  deterministic pure function of its input (seed 0, set above), so a cached
  label is the same label the detector would produce - nothing is guessed,
  approximated, or skipped. Real comment sections repeat short comments
  heavily ("Nice!", "lol", "Same here"), so those detections are computed
  once and reused.
- Bounded memory: only texts up to MAX_CACHE_TEXT chars are cached (short
  comments - where duplicates live), at most MAX_CACHE_ENTRIES entries
  (~a few MiB worst case), FIFO eviction. Long, near-unique comments are
  simply recomputed every time.
- Not removed: every unique text still goes through the real detector;
  unsupported languages remain correctly excluded exactly as before.
"""
from langdetect import DetectorFactory, LangDetectException, detect_langs

# Must be set before any detect call for reproducible labels.
DetectorFactory.seed = 0

MIN_CONFIDENCE = 0.90
UNKNOWN_LANGUAGE = "unknown"

_REGION_TO_BASE = {"zh-cn": "zh", "zh-tw": "zh", "zh": "zh"}

# Sprint 5.1 §15: bounded exact-text result cache (insertion-ordered dict =
# FIFO eviction). Only short texts are candidates - they are both the most
# duplicated and the cheapest to keep; long unique comments never pollute it.
MAX_CACHE_ENTRIES = 8192
MAX_CACHE_TEXT = 256
_cache: "dict[str, str]" = {}


def _cache_put(text: str, result: str) -> None:
    if len(text) > MAX_CACHE_TEXT:
        return
    if len(_cache) >= MAX_CACHE_ENTRIES:
        _cache.pop(next(iter(_cache)))  # evict oldest (FIFO), never unbounded
    _cache[text] = result


def detect_language(text: str) -> str:
    """Best-effort ISO 639-1 language code, or `unknown` (never a guess).

    Identical verdicts with or without the cache: the memo stores the exact
    output of the deterministic detector for byte-identical input text.
    """
    if not text:
        return UNKNOWN_LANGUAGE
    cached = _cache.get(text)
    if cached is not None:
        return cached
    if not any(ch.isalpha() for ch in text):
        # Fast path (emoji-only, digits-only): not cached - it is already
        # cheaper than the dict lookup for the common short case.
        return UNKNOWN_LANGUAGE
    try:
        candidates = detect_langs(text)
    except LangDetectException:
        return UNKNOWN_LANGUAGE
    if not candidates:
        return UNKNOWN_LANGUAGE
    best = candidates[0]
    if best.prob < MIN_CONFIDENCE:
        result = UNKNOWN_LANGUAGE
    else:
        result = _REGION_TO_BASE.get(best.lang, best.lang)
    _cache_put(text, result)
    return result

"""Sentiment intelligence service (Sprint 4) - engine + orchestration.

Layering (docs/architecture/sentiment-analysis.md):

    Route -> SentimentService -> DatasetRepository -> SQLite
                     |
                     v
              SentimentEngine (pure text -> verdict, no I/O)

Two clearly separated halves:

1. `SentimentEngine` / `classify()` - PURE. Takes text, returns a structured
   verdict. No SQLite, no FastAPI, no YouTube, no network, no globals beyond
   the lazily-built VADER lexicon analyzer. This is the piece a later sprint
   can swap (emotion, LLM) without touching orchestration.

2. `SentimentService` - orchestration over the existing repository only:
   batch reads via `iter_comments(status=...)`, claims through the state
   machine (READY_FOR_ANALYSIS -> PROCESSING -> PROCESSED / -> FAILED),
   persistence exclusively through repository methods (this module contains
   no SQL), idempotent by construction (PROCESSED rows are never re-read;
   a content change resets the row to READY with cleared sentiment by the
   repository upsert).

Model (documented choice, Sprint 4):
- VADER (`vaderSentiment`), lexicon + rule based (Hutto & Gilbert, 2014).
  Pure Python, CPU-only, deterministic for identical input, emoji- and
  punctuation-aware, no model download, no GPU, no API. The compound score is
  a real continuous score in [-1, 1] (Negative <- 0 -> Positive), so no score
  is invented.

Language policy (explicit, no translation in Sprint 4):
- VADER's lexicon is English. Only rows whose detected `language` is `en`
  are classified; every other code (including `unknown`) is recorded as
  UNSUPPORTED_LANGUAGE - analyzed (PROCESSED) without a polarity verdict,
  reported as `skipped`, never mislabeled as neutral.

Sprint 5 audience intelligence (same module - no parallel service):
- Emotion: NRC Emotion Lexicon lookup through `nrclex` (Mohammad & Turney,
  2013) driven via `load_token_list()` with the pipeline's own tokenizer,
  so no NLTK/TextBlob corpora or downloads are ever needed. Real inference:
  every label comes from lexicon hits in the stored text; zero hits ->
  NEUTRAL (documented, not fabricated). English-only like VADER (same
  language gate).
- Intensity: DERIVED band on |VADER compound| (LOW < 0.35 <= MEDIUM < 0.70
  <= HIGH). Confidence: VADER's existing decision margin (documented,
  already stored per row). Audience mood: deterministic rules over the
  real aggregates (no LLM). All derived metrics are documented in
  docs/architecture/audience-intelligence.md.
- The emotion/intensity/confidence/mood aggregates ride on the existing
  batched, generation-guarded run: same claim -> classify -> persist pass,
  same active-video protection, same job worker thread (no new pipeline).

Safety:
- No raw comment text is ever logged; only video/comment ids, counts,
  timings, and exception class names.
- One bad comment becomes a per-row FAILED transition - the run continues.
"""
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from threading import Lock
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.repository import DatasetRepository, SentimentOutcome
from app.models.internal import (
    ConfidenceBreakdown,
    EmotionBreakdown,
    IntensityBreakdown,
    LabelShare,
    SentimentAnalysisResponse,
    SentimentDatasetMetrics,
    SentimentStats,
)
from app.models.processing import ProcessingStatus
from app.models.job import ACTIVE_JOB_STATUSES
from nrclex import NRCLex
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from app.services.vader_fast import FastSentimentIntensityAnalyzer

logger = get_logger("sentiment")

# Engine identity stored with every result (sentiment_model column) - the API
# never exposes implementation details, but the database records exactly what
# produced each verdict.
MODEL_ID = "vader-1.0"

# Sprint 5 emotion engine identity (stored per row in emotion_model).
EMOTION_MODEL_ID = "nrclex-4.1"

# NRC Emotion Lexicon categories exposed as the emotion axis, in the
# lexicon's canonical order (NRCLex.EMOTION_ORDER minus its polarity tags:
# 'positive'/'negative' are NOT emotions - polarity lives on the sentiment
# axis, reported separately).
EMOTION_CATEGORIES = (
    "FEAR",
    "ANGER",
    "ANTICIPATION",
    "TRUST",
    "SURPRISE",
    "SADNESS",
    "DISGUST",
    "JOY",
)
# NEUTRAL = zero emotion-word hits in the comment (bag-of-words outcome).
NEUTRAL_EMOTION = "NEUTRAL"
# Deterministic tie-break + display order: lexicon canonical order first,
# NEUTRAL last (a real emotion outranks "no emotion" at an exact tie).
EMOTION_PRIORITY = EMOTION_CATEGORIES + (NEUTRAL_EMOTION,)

# Sprint 5 intensity bands on |VADER compound| (§6: derived, documented):
#   |c| < 0.35        -> LOW     (includes VADER's neutral zone |c| < 0.05)
#   0.35 <= |c| < 0.70 -> MEDIUM
#   0.70 <= |c| <= 1   -> HIGH
INTENSITY_ORDER = ("LOW", "MEDIUM", "HIGH")
INTENSITY_MEDIUM_MIN = 0.35
INTENSITY_HIGH_MIN = 0.70

# Audience mood needs enough evidence to be meaningful (§12). Below this
# many analyzed comments the API reports null and the UI shows no mood.
AUDIENCE_MOOD_MIN_SAMPLE = 10
# Mood decision thresholds, all in percentage points of the analyzed set
# (documented with worked examples in audience-intelligence.md).
MOOD_BALANCE_MARGIN = 25.0       # positive% - negative% decides the lean
MOOD_HIGH_INTENSITY_MARGIN = 30.0
MOOD_LOW_INTENSITY_MARGIN = 50.0
MOOD_DOMINANT_EMOTION_SHARE = 25.0

# Language gate: the only language VADER's lexicon supports. `unknown` is
# deliberately absent - an undetectable language is not pretend-analyzable.
SUPPORTED_LANGUAGES = frozenset({"en"})

UNSUPPORTED_LANGUAGE = "UNSUPPORTED_LANGUAGE"

# VADER's published decision thresholds on the compound score.
POSITIVE_THRESHOLD = 0.05
NEGATIVE_THRESHOLD = -0.05


class SentimentLabel(str, Enum):
    POSITIVE = "POSITIVE"
    NEUTRAL = "NEUTRAL"
    NEGATIVE = "NEGATIVE"


class EmotionLabel(str, Enum):
    """NRC emotion categories + the zero-hit NEUTRAL outcome."""

    FEAR = "FEAR"
    ANGER = "ANGER"
    ANTICIPATION = "ANTICIPATION"
    TRUST = "TRUST"
    SURPRISE = "SURPRISE"
    SADNESS = "SADNESS"
    DISGUST = "DISGUST"
    JOY = "JOY"
    NEUTRAL = "NEUTRAL"


class IntensityLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


@dataclass(frozen=True)
class SentimentRunSummary:
    """Real outcome counters of one batched processing run (Sprint 4.3).

    stopped   - the run halted BEFORE finishing: either the dataset was
                superseded by a video switch (generation guard) or the
                owning background job was cancelled. Nothing written after
                the stop is trusted as "this run's output".
    processed - rows given a polarity verdict (the % denominator).
    failed    - rows moved to FAILED (retryable, one bad row != bad run).
    skipped   - rows resolved UNSUPPORTED_LANGUAGE (analyzed, no verdict).
    requeued  - recoverable rows returned to READY before this run.
    """

    processed: int = 0
    failed: int = 0
    skipped: int = 0
    requeued: int = 0
    stopped: bool = False


# ---------------------------------------------------------------------------
# Half 1: the pure engine (text in -> verdict out; no repository, no routes)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SentimentVerdict:
    """Structured classification result.

    score     = VADER compound in [-1.0, 1.0] (Negative <- 0 -> Positive).
    confidence = normalized distance from the decision boundary in [0, 1]:
                 neutral  (|c| <= 0.05): (0.05 - |c|) / 0.05
                 positive (c >= 0.05)  : (c - 0.05) / (1 - 0.05)
                 negative (c <= -0.05) : (-c - 0.05) / (1 - 0.05)
                 It is a DECISION MARGIN, not a calibrated probability: a
                 verdict sitting right on its threshold scores ~0, a verdict
                 deep inside its band scores ~1. Documented honestly in
                 docs/architecture/sentiment-analysis.md.
    """

    label: SentimentLabel
    score: float
    confidence: float


_analyzer: Optional[SentimentIntensityAnalyzer] = None


def _get_analyzer() -> SentimentIntensityAnalyzer:
    """Lazy, process-wide analyzer (the lexicon loads once; scoring is pure).

    Sprint 5.2 (§3/§4): builds the exact-parity FAST scorer
    (app.services.vader_fast) - same lexicon files, same public API,
    identical scores (tests/test_vader_fast.py parity), with the base
    class's per-call lowered-word rebuilds hoisted to once per comment.
    Baseline A/B runs (benchmarks/bench_pipeline.py --baseline) swap this
    for the stock instance to measure before/after on identical corpora.
    """
    global _analyzer
    if _analyzer is None:
        _analyzer = FastSentimentIntensityAnalyzer()
    return _analyzer


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def _confidence_for(label: SentimentLabel, score: float) -> float:
    if label is SentimentLabel.NEUTRAL:
        return _clamp01((POSITIVE_THRESHOLD - abs(score)) / POSITIVE_THRESHOLD)
    signed = score if label is SentimentLabel.POSITIVE else -score
    return _clamp01((signed - POSITIVE_THRESHOLD) / (1.0 - POSITIVE_THRESHOLD))


def classify(text: str) -> SentimentVerdict:
    """Classify one comment. Deterministic; safe for empty/whitespace text.

    Empty input has no lexical evidence, so it returns NEUTRAL with score 0
    and confidence 0 - a defined, honest outcome (never a fabricated verdict).
    The ingestion pipeline never persists empty text, so this path exists for
    robustness and is covered by tests.
    """
    if not text or not text.strip():
        return SentimentVerdict(label=SentimentLabel.NEUTRAL, score=0.0, confidence=0.0)
    scores = _get_analyzer().polarity_scores(text)
    compound = float(scores["compound"])
    if compound >= POSITIVE_THRESHOLD:
        label = SentimentLabel.POSITIVE
    elif compound <= NEGATIVE_THRESHOLD:
        label = SentimentLabel.NEGATIVE
    else:
        label = SentimentLabel.NEUTRAL
    return SentimentVerdict(
        label=label,
        score=compound,
        confidence=_confidence_for(label, compound),
    )


@dataclass(frozen=True)
class EmotionVerdict:
    """Emotion classification result (Sprint 5, real lexicon inference).

    label = winning NRC category, or NEUTRAL when the lexicon matched no
            emotion word (zero-hit outcome - documented, never invented).
    score = share of this comment's DETECTED emotion evidence (across the
            8 NRC categories) attributable to the winning label, in (0, 1].
            e.g. counts joy=2, sadness=1 -> joy, score 2/3. It is a
            proportion of lexicon hits, not a calibrated probability.
    Ties between categories resolve to the first label in EMOTION_PRIORITY
    (lexicon canonical order) - deterministic, never arbitrary.
    """

    label: EmotionLabel
    score: float


_TOKEN_RE = re.compile(r"[a-z]+")

# Sprint 5.2 §4/§15: direct emotion-lexicon counting switch. When True,
# classify_emotion counts NRC label hits straight from the singleton
# engine's bundled lexicon dict - skipping nrclex's `load_token_list`
# bookkeeping (affect_list / affect_dict / affect_percent /
# top_emotions) that this pipeline never reads. Output values are
# identical (tests/test_sentiment.py::TestEmotionDirectParity).
# Baseline A/B runs set this False to restore the stock path.
_EMOTION_DIRECT = True

# Process-wide emotion engine (Sprint 5.1 §3: load once, reuse). NRCLex 4.1's
# constructor resolves the lexicon source (filesystem probe + JSON load) on
# EVERY instantiation - measured at ~267us of the ~339us per-comment emotion
# cost (backend/benchmarks/bench_pipeline.py micro). Building the engine once
# removes that per-comment tax without changing a single verdict.
_emotion_engine: Optional[NRCLex] = None


def _get_emotion_engine() -> NRCLex:
    """Lazy, process-wide NRCLex instance (model loaded exactly once).

    Safety of reuse: `load_token_list()` fully REWRITES the instance's
    per-call state (words, affect frequencies, raw scores, top emotions are
    all overwritten, never accumulated), so no comment's emotion evidence
    can leak into the next. Threading: all production inference runs
    serialized under `SentimentService`'s run lock (one worker at a time),
    which is the documented contract for this shared engine; direct unit
    calls are single-threaded. Initialization needs no lock - a rare
    concurrent construction would build two engines and keep one, with
    identical lexicon content.
    """
    global _emotion_engine
    if _emotion_engine is None:
        _emotion_engine = NRCLex()
    return _emotion_engine


def classify_emotion(text: str) -> EmotionVerdict:
    """Classify one comment's emotion via the NRC Emotion Lexicon.

    Deterministic bag-of-words lookup:
    - Tokenizer: lowercase ASCII-word extraction (`[a-z]+`) over the
      already-normalized text. Deliberately our own tokenizer instead of
      NRCLex's TextBlob path, which requires NLTK corpora downloads
      (environment-dependent, network on first run). Exact lexicon-surface
      matching; no lemmatization/suffix guessing, no negation or sarcasm
      handling (documented limitations).
    - Zero hits (empty text or no emotion word) -> NEUTRAL with score 0.0.
    - One process-wide NRCLex instance (Sprint 5.1 §3): the bundled lexicon
      is loaded once and `load_token_list` resets all per-call state, so
      reuse is safe and never mixes two comments' evidence.
    """
    tokens = _TOKEN_RE.findall((text or "").lower())
    if not tokens:
        return EmotionVerdict(label=EmotionLabel.NEUTRAL, score=0.0)
    engine = _get_emotion_engine()
    raw = None
    if _EMOTION_DIRECT:
        # Direct counting over the SAME bundled lexicon the engine loaded
        # (nrclex stores it as a dunder-keyed instance dict - no name
        # mangling because the key ends in `__`; feature-detected so a
        # library change falls back to the stock public path below).
        lexicon = getattr(engine, "__lexicon__", None)
        if lexicon is not None:
            raw = {}
            for token in tokens:
                labels = lexicon.get(token)
                if labels:
                    for label in labels:
                        raw[label] = raw.get(label, 0) + 1
    if raw is None:
        engine.load_token_list(tokens)
        raw = engine.raw_emotion_scores  # lowercase label -> hit count
    counts = [int(raw.get(category.lower(), 0)) for category in EMOTION_CATEGORIES]
    total = sum(counts)
    if total == 0:
        return EmotionVerdict(label=EmotionLabel.NEUTRAL, score=0.0)
    highest = max(counts)
    for index, count in enumerate(counts):
        if count == highest:  # EMOTION_PRIORITY order = deterministic tie-break
            return EmotionVerdict(
                label=EmotionLabel(EMOTION_CATEGORIES[index]),
                score=highest / total,
            )
    raise AssertionError("unreachable: highest always matches a count")


def intensity_for(score: float) -> IntensityLevel:
    """Derive the sentiment-intensity band from a VADER compound score.

    DERIVED metric (§6): intensity = how strongly the comment expresses its
    sentiment = |compound| bucketed into documented bands - it is not
    produced by the model as a separate signal, and never random.
        |c| < 0.35          -> LOW     (VADER's neutral zone is LOW too:
                                no/weak expression, honest low band)
        0.35 <= |c| < 0.70  -> MEDIUM
        0.70 <= |c| <= 1.0  -> HIGH
    Boundary values land in the UPPER band (0.35 -> MEDIUM, 0.70 -> HIGH),
    so the bands are closed-low/open-high exactly as written above.
    """
    magnitude = abs(float(score))
    if magnitude < INTENSITY_MEDIUM_MIN:
        return IntensityLevel.LOW
    if magnitude < INTENSITY_HIGH_MIN:
        return IntensityLevel.MEDIUM
    return IntensityLevel.HIGH


# ---------------------------------------------------------------------------
# Aggregation helpers (pure; unit-tested in isolation)
# ---------------------------------------------------------------------------


def _percentages(counts: Sequence[int]) -> List[float]:
    """N-bucket largest-remainder percentages (the Sprint 4 algorithm,
    generalized for Sprint 5's 9-emotion and 3-intensity distributions).

    Integer TENTHS of a percent, floored, leftover tenths to the biggest
    remainders - so every value is a multiple of 0.1, the sum is exactly
    100.0 for any positive input, and no floating-point artifacts appear.
    Ties in the leftover tenths break by list order (caller's documented
    priority). Empty/zero input -> all zeros.
    """
    total = sum(counts)
    if total <= 0:
        return [0.0 for _ in counts]
    floors = [(count * 1000) // total for count in counts]
    remainders = [(count * 1000) % total for count in counts]
    leftover = 1000 - sum(floors)
    order = sorted(range(len(counts)), key=lambda i: (-remainders[i], i))
    for index in order[:leftover]:
        floors[index] += 1
    return [floor / 10.0 for floor in floors]


def distribution_percentages(
    positive: int, neutral: int, negative: int
) -> Tuple[float, float, float]:
    """Percentages with DENOMINATOR `analyzed`, summing to exactly 100.

    Largest-remainder method executed in INTEGER TENTHS of a percent, then
    divided by 10 - so every value is a multiple of 0.1 (56.0, 32.5, ...),
    the sum is exactly 100.0 for any positive input, and no
    floating-point artifacts can appear (spec §20 deterministic rounding):

        56 / 37 / 7  of 100  -> (56.0, 37.0, 7.0)
        220 / 130 / 50 of 400 -> (55.0, 32.5, 12.5)
        1 / 1 / 1     of 3   -> (33.4, 33.3, 33.3)

    Ties in the leftover tenth break by label priority
    POSITIVE > NEUTRAL > NEGATIVE (documented, deterministic). Empty input
    -> (0.0, 0.0, 0.0). The API/UI display these via a trim helper, so a
    whole number renders as "64%" and a fractional one as "32.5%".
    """
    return tuple(_percentages([positive, neutral, negative]))  # type: ignore[return-value]


def dominant_sentiment(positive: int, neutral: int, negative: int) -> Optional[str]:
    """Most frequent analyzed label, or None when nothing was analyzed.

    Ties resolve deterministically by label priority: POSITIVE > NEUTRAL >
    NEGATIVE (first label reaching the shared maximum wins).
    """
    if positive <= 0 and neutral <= 0 and negative <= 0:
        return None
    highest = max(positive, neutral, negative)
    for label, count in (
        (SentimentLabel.POSITIVE, positive),
        (SentimentLabel.NEUTRAL, neutral),
        (SentimentLabel.NEGATIVE, negative),
    ):
        if count == highest:
            return label.value
    return None  # pragma: no cover - unreachable above


def label_distribution(counts: Dict[str, int], order: Sequence[str]) -> Dict[str, float]:
    """Ordered label -> percentage map over the given priority order.

    Percentages come from the shared largest-remainder method (sum to 100
    iff any row carries the axis), so emotion/intensity distributions are
    exactly as deterministic as the sentiment one. Absent labels appear
    with 0.0 - the UI always gets the full vocabulary.
    """
    ordered_counts = [counts.get(label, 0) for label in order]
    percentages = _percentages(ordered_counts)
    return {label: percentages[index] for index, label in enumerate(order)}


def dominant_label(counts: Dict[str, int], order: Sequence[str]) -> Optional[str]:
    """Highest-count label in `order`, ties to the FIRST label in `order`
    (the caller's documented priority); None when every count is 0.
    """
    highest = max(counts.values(), default=0)
    if highest <= 0:
        return None
    for label in order:
        if counts.get(label, 0) == highest:
            return label
    return None  # pragma: no cover - unreachable above


def audience_mood(
    analyzed: int,
    positive_percent: float,
    negative_percent: float,
    emotion_counts: Dict[str, int],
    intensity_counts: Dict[str, int],
) -> Optional[str]:
    """Deterministic audience mood over REAL aggregates (Sprint 5, §12).

    Derived only from sentiment balance, intensity shares and emotion
    dominance of the analyzed set - no LLM, no hard-coded per-video values.
    Returns one of POSITIVE / CALM / EXCITED / MIXED / CONCERNED /
    NEGATIVE, or None when the sample is too small to claim a mood.

    Rules (first match wins; all thresholds are module constants):
      1. analyzed < AUDIENCE_MOOD_MIN_SAMPLE  -> None (no mood claimed)
      2. balance = positive% - negative%
      3. balance >= +25:
           HIGH intensity >= 30%                 -> EXCITED
           or dominant emotion JOY/ANTICIPATION with >= 25% share -> EXCITED
           else LOW intensity >= 50%             -> CALM
           else                                  -> POSITIVE
      4. balance <= -25:
           HIGH intensity >= 30%                 -> NEGATIVE
           or dominant emotion ANGER/DISGUST with >= 25% share   -> NEGATIVE
           else                                  -> CONCERNED
      5. otherwise (-25 < balance < +25)         -> MIXED

    Emotion share = that emotion's count / analyzed. A NEUTRAL dominant
    emotion never triggers the emotion branches (no emotion evidence).
    """
    if analyzed < AUDIENCE_MOOD_MIN_SAMPLE:
        return None

    def share(count: int) -> float:
        return count * 100.0 / analyzed

    balance = positive_percent - negative_percent
    high_share = share(intensity_counts.get("HIGH", 0))
    low_share = share(intensity_counts.get("LOW", 0))
    dominant = dominant_label(emotion_counts, EMOTION_PRIORITY)
    dominant_share = share(emotion_counts.get(dominant, 0)) if dominant else 0.0

    if balance >= MOOD_BALANCE_MARGIN:
        if high_share >= MOOD_HIGH_INTENSITY_MARGIN or (
            dominant in ("JOY", "ANTICIPATION")
            and dominant_share >= MOOD_DOMINANT_EMOTION_SHARE
        ):
            return "EXCITED"
        if low_share >= MOOD_LOW_INTENSITY_MARGIN:
            return "CALM"
        return "POSITIVE"
    if balance <= -MOOD_BALANCE_MARGIN:
        if high_share >= MOOD_HIGH_INTENSITY_MARGIN or (
            dominant in ("ANGER", "DISGUST")
            and dominant_share >= MOOD_DOMINANT_EMOTION_SHARE
        ):
            return "NEGATIVE"
        return "CONCERNED"
    return "MIXED"


def derive_status(
    status_counts: Dict[str, int],
    active_for_video: bool,
) -> str:
    """Map row statuses (+ in-process run) to the public analysis lifecycle.

    NOT_ANALYZED  no verdict yet (empty dataset, or nothing claimed)
    PROCESSING    this video's run is active in-process, or rows are claimed
    FAILED        nothing analyzed and at least one failure (retry on next GET)
    PROCESSED     at least one verdict exists (possibly partial: rows reset by
                  a content change wait for the next trigger)

    Documented order matters: an active/claimed run always wins, so the UI
    never shows stale READY copy while analysis is in flight.
    """
    if active_for_video:
        return "PROCESSING"
    processing = status_counts.get(ProcessingStatus.PROCESSING.value, 0)
    if processing > 0:
        return "PROCESSING"
    processed = status_counts.get(ProcessingStatus.PROCESSED.value, 0)
    failed = status_counts.get(ProcessingStatus.FAILED.value, 0)
    if processed == 0:
        return "FAILED" if failed > 0 else "NOT_ANALYZED"
    return "PROCESSED"


# ---------------------------------------------------------------------------
# Half 2: orchestration over the repository (no SQL in this class)
# ---------------------------------------------------------------------------


class SentimentService:
    """Batched, idempotent sentiment processing for one video dataset."""

    def __init__(self, repository: DatasetRepository, settings: Settings) -> None:
        self._repo = repository
        self._settings = settings
        # One run at a time per process (single-process deployment, see docs).
        # Held across the whole run so a concurrent GET reports PROCESSING
        # instead of double-processing the same rows. Also the documented
        # serialization point for the shared emotion engine (§3).
        self._run_lock = Lock()
        self._active_video: Optional[str] = None

    def _inference_batch(self) -> int:
        """Comments claimed -> classified -> persisted per analysis batch.

        SENTIMENT_INFERENCE_BATCH_SIZE when explicitly configured, otherwise
        COMMENT_BATCH_SIZE (§4). Benchmark-selected: throughput plateaus at
        ~512 comments/batch (see config.py for the measured curve), so the
        production default of 500 already sits on the plateau.
        """
        configured = self._settings.sentiment_inference_batch_size
        return configured or self._settings.comment_batch_size

    # ------------------------------------------------------------------ api
    def get_analysis(self, video_id: str) -> Optional[SentimentAnalysisResponse]:
        """Current analysis state for one video; None when untracked.

        Backend-owned trigger (spec §17): if the dataset has pending rows
        (READY / FAILED / orphaned PROCESSING) and no run is active, this
        processes them synchronously in batches, then reports. The extension
        never runs the model - it only GETs this endpoint.

        Sprint 4.2 (§38/§39): processing is allowed only for the ACTIVE
        working dataset. The activation generation is captured here and
        re-checked before every batch, so a video switch mid-run invalidates
        the run before anything can commit - stale results never touch the
        new dataset.
        """
        video_row = self._repo.get_video(video_id)
        if video_row is None:
            return None

        active = self._repo.get_active_video()
        if active is not None and active["video_id"] != video_id:
            # Not the working dataset (legacy stray row): report its current
            # state, never process it, never activate (§40).
            return self._build_response(
                video_id, self._repo.get_status_counts(video_id), video_row
            )
        generation = int(active["dataset_generation"]) if active is not None else None

        # Sprint 5.1 §8/§20: while a background job owns this dataset the GET
        # is a FAST READ. Inline processing would block the extension's poll
        # behind a full drain, and the job - plus its interim per-page drain
        # (§6) - is already doing the work. Status honestly reports
        # PROCESSING for the whole job lifetime, even between batches.
        job = self._repo.get_latest_job(video_id)
        job_active = job is not None and str(job["status"]) in ACTIVE_JOB_STATUSES

        status_counts = self._repo.get_status_counts(video_id)
        if (
            self._is_pending(status_counts)
            and not job_active
            and self.process_pending(video_id, generation) is not None
        ):
            status_counts = self._repo.get_status_counts(video_id)

        return self._build_response(
            video_id, status_counts, video_row, job_active=job_active
        )

    def process_pending(
        self,
        video_id: str,
        generation: Optional[int] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
        wait: bool = False,
    ) -> Optional[SentimentRunSummary]:
        """Batched processing entry point (Sprint 4.3, shared by both paths).

        - Synchronous `GET /sentiment` (§29 compatibility): wait=False -
          if another run already holds the lock the caller gets None and
          simply reports PROCESSING instead of double-processing rows.
        - Background job (§4): wait=True - briefly waits for an in-flight
          sync run, then processes everything pending in batches.

        The run is generation-guarded (Sprint 4.2): every batch re-verifies
        that (video_id, generation) still owns the working dataset, and
        `should_cancel` lets the owning job stop immediately on
        cancellation - either way nothing further is written (§19).
        """
        if not self._run_lock.acquire(blocking=wait):
            return None
        try:
            self._active_video = video_id
            return self._run_pending(video_id, generation, should_cancel)
        finally:
            self._active_video = None
            self._run_lock.release()

    def _still_active(self, video_id: str, generation: Optional[int]) -> bool:
        """True while (video_id, generation) still owns the working dataset.

        `generation is None` = no lifecycle (direct unit-test seeds) -> never
        invalidated. Production runs always carry the generation captured at
        entry, so a switch to another video fails this check on the next
        batch (§13).
        """
        if generation is None:
            return True
        active = self._repo.get_active_video()
        return (
            active is not None
            and active["video_id"] == video_id
            and int(active["dataset_generation"]) == generation
        )

    def _stopped(
        self,
        video_id: str,
        generation: Optional[int],
        should_cancel: Optional[Callable[[], bool]],
    ) -> bool:
        """True when this run must stop before writing the next batch:
        cancellation requested (cheap check first) or the dataset was
        superseded (Sprint 4.2 generation guard)."""
        if should_cancel is not None and should_cancel():
            return True
        return not self._still_active(video_id, generation)

    # ------------------------------------------------------------- internals
    @staticmethod
    def _is_pending(status_counts: Dict[str, int]) -> bool:
        return any(
            status_counts.get(status.value, 0) > 0
            for status in (
                ProcessingStatus.READY_FOR_ANALYSIS,
                ProcessingStatus.FAILED,
                ProcessingStatus.PROCESSING,  # orphaned claims from a crashed run
            )
        )

    def _build_response(
        self,
        video_id: str,
        status_counts: Dict[str, int],
        video_row,
        job_active: bool = False,
    ) -> SentimentAnalysisResponse:
        sentiment_counts = self._repo.get_sentiment_counts(video_id)
        positive = sentiment_counts.get(SentimentLabel.POSITIVE.value, 0)
        neutral = sentiment_counts.get(SentimentLabel.NEUTRAL.value, 0)
        negative = sentiment_counts.get(SentimentLabel.NEGATIVE.value, 0)
        skipped = sentiment_counts.get(UNSUPPORTED_LANGUAGE, 0)
        analyzed = positive + neutral + negative
        pos_pct, neu_pct, neg_pct = distribution_percentages(
            positive, neutral, negative
        )
        status = derive_status(
            status_counts,
            # An active background job means PROCESSING even between batches
            # (Sprint 5.1 §8): the UI must never read partial aggregates as
            # a finished result while the run is still going.
            active_for_video=(self._active_video == video_id) or job_active,
        )

        # Sprint 4.1 dataset metrics (SQL-side counts, never row loads).
        stored = sum(status_counts.values())
        failed_rows = status_counts.get(ProcessingStatus.FAILED.value, 0)
        has_more = bool(video_row["has_more"])
        limit_reached = has_more and stored >= self._settings.comment_acquisition_max_comments

        # Sprint 5 audience intelligence (SQL-side, same read pass):
        # emotion/intensity distributions over their own row sets (by the
        # write-path invariant both == analyzed), confidence as the average
        # decision margin (null when nothing analyzed), mood derived from
        # the real aggregates only when the sample is large enough.
        emotion_counts = self._repo.get_emotion_counts(video_id)
        intensity_counts = self._repo.get_intensity_counts(video_id)
        average_confidence = self._repo.get_average_sentiment_confidence(video_id)
        emotion_percentages = label_distribution(emotion_counts, EMOTION_PRIORITY)
        intensity_percentages = label_distribution(intensity_counts, INTENSITY_ORDER)
        dominant_emotion_label = dominant_label(emotion_counts, EMOTION_PRIORITY)
        dominant_intensity_label = dominant_label(intensity_counts, INTENSITY_ORDER)
        mood = audience_mood(
            analyzed, pos_pct, neg_pct, emotion_counts, intensity_counts
        )

        return SentimentAnalysisResponse(
            video_id=video_id,
            status=status,  # type: ignore[arg-value]
            stats=SentimentStats(
                total_comments=stored,
                analyzed=analyzed,
                skipped=skipped,
                positive=positive,
                neutral=neutral,
                negative=negative,
                positive_percent=pos_pct,
                neutral_percent=neu_pct,
                negative_percent=neg_pct,
            ),
            dataset=SentimentDatasetMetrics(
                collected=stored,  # == stored by construction (model docstring)
                stored=stored,
                analyzed=analyzed,
                skipped=skipped,
                failed=failed_rows,
                has_more=has_more,
                limit_reached=limit_reached,
            ),
            dominant_sentiment=dominant_sentiment(  # type: ignore[arg-value]
                positive, neutral, negative
            ),
            emotion=EmotionBreakdown(
                dominant=dominant_emotion_label,  # type: ignore[arg-value]
                dominant_percent=(
                    emotion_percentages.get(dominant_emotion_label, 0.0)
                    if dominant_emotion_label is not None
                    else 0.0
                ),
                distribution={
                    label: LabelShare(
                        count=emotion_counts.get(label, 0),
                        percent=emotion_percentages[label],
                    )
                    for label in EMOTION_PRIORITY
                },
            ),
            intensity=IntensityBreakdown(
                overall=dominant_intensity_label,  # type: ignore[arg-value]
                distribution={
                    label: LabelShare(
                        count=intensity_counts.get(label, 0),
                        percent=intensity_percentages[label],
                    )
                    for label in INTENSITY_ORDER
                },
            ),
            confidence=ConfidenceBreakdown(
                average=(
                    round(average_confidence, 3)
                    if average_confidence is not None
                    else None
                )
            ),
            audience_mood=mood,  # type: ignore[arg-value]
        )

    def _run_pending(
        self,
        video_id: str,
        generation: Optional[int] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> SentimentRunSummary:
        """Requeue recoverable rows, then claim -> classify -> persist in
        batches of the inference batch size (SENTIMENT_INFERENCE_BATCH_SIZE
        or COMMENT_BATCH_SIZE, §4). Never loads a full dataset: reads go
        through the repository's keyset reader one batch at a time.

        Before every batch the run re-verifies that this dataset is still
        the active one AND that no cancellation was requested (Sprint 4.2
        generation guard + Sprint 4.3 job cancel): after a video switch the
        run aborts with SENTIMENT_RUN_SUPERSEDED without writing anything.
        Caller must hold the run lock.
        """
        started = time.monotonic()
        # We hold the run lock, so no in-process run owns PROCESSING rows:
        # they are orphans from a crashed run and take the legal path
        # PROCESSING -> FAILED -> READY_FOR_ANALYSIS (requeue).
        requeued = self._transition_by_status(
            video_id, ProcessingStatus.PROCESSING, ProcessingStatus.FAILED
        )
        requeued += self._transition_by_status(
            video_id, ProcessingStatus.FAILED, ProcessingStatus.READY_FOR_ANALYSIS
        )

        batch_size = self._inference_batch()
        processed = failed = skipped = 0
        # §9 time_to_first_insight: logged once, from the REAL clock, when
        # the first polarity verdicts of this run are persisted - never
        # estimated, never fabricated.
        first_insight_logged = False
        rows: List[sqlite3.Row] = []
        for row in self._repo.iter_comments(
            video_id, batch_size, status=ProcessingStatus.READY_FOR_ANALYSIS.value
        ):
            rows.append(row)
            if len(rows) >= batch_size:
                if self._stopped(video_id, generation, should_cancel):
                    logger.warning(
                        "SENTIMENT_RUN_SUPERSEDED",
                        extra={"video_id": video_id, "processed": processed},
                    )
                    return SentimentRunSummary(
                        processed=processed,
                        failed=failed,
                        skipped=skipped,
                        requeued=requeued,
                        stopped=True,
                    )
                p, f, s = self._process_batch(video_id, rows)
                processed += p
                failed += f
                skipped += s
                if not first_insight_logged and processed > 0:
                    first_insight_logged = True
                    logger.info(
                        "SENTIMENT_FIRST_INSIGHT",
                        extra={
                            "video_id": video_id,
                            "analyzed": processed,
                            "elapsed_ms": int((time.monotonic() - started) * 1000),
                        },
                    )
                rows = []
        if rows:
            if self._stopped(video_id, generation, should_cancel):
                logger.warning(
                    "SENTIMENT_RUN_SUPERSEDED",
                    extra={"video_id": video_id, "processed": processed},
                )
                return SentimentRunSummary(
                    processed=processed,
                    failed=failed,
                    skipped=skipped,
                    requeued=requeued,
                    stopped=True,
                )
            p, f, s = self._process_batch(video_id, rows)
            processed += p
            failed += f
            skipped += s
            if not first_insight_logged and processed > 0:
                first_insight_logged = True
                logger.info(
                    "SENTIMENT_FIRST_INSIGHT",
                    extra={
                        "video_id": video_id,
                        "analyzed": processed,
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                    },
                )

        elapsed = time.monotonic() - started  # float seconds (sub-ms precise)
        elapsed_ms = int(elapsed * 1000)
        total = processed + failed + skipped
        logger.info(
            "SENTIMENT_RUN_COMPLETED",
            extra={
                "video_id": video_id,
                "processed": processed,
                "failed": failed,
                "skipped_unsupported_language": skipped,
                "requeued": requeued,
                "elapsed_ms": elapsed_ms,
                # Real throughput, never estimated (null only if the clock
                # measured a true zero-duration run).
                "comments_per_second": (
                    round(total / elapsed, 1) if elapsed > 0 else None
                ),
            },
        )
        return SentimentRunSummary(
            processed=processed,
            failed=failed,
            skipped=skipped,
            requeued=requeued,
            stopped=False,
        )

    def _transition_by_status(
        self,
        video_id: str,
        source: ProcessingStatus,
        target: ProcessingStatus,
    ) -> int:
        """Move every row in `source` to `target` via the repository's
        state-machine-validated transition, one batch at a time."""
        changed = 0
        pending: List[str] = []
        for row in self._repo.iter_comments(
            video_id, self._settings.comment_batch_size, status=source.value
        ):
            pending.append(row["comment_id"])
            if len(pending) >= self._settings.comment_batch_size:
                changed += self._flush_transitions(video_id, pending, target)
                pending = []
        if pending:
            changed += self._flush_transitions(video_id, pending, target)
        return changed

    def _flush_transitions(
        self, video_id: str, comment_ids: Sequence[str], target: ProcessingStatus
    ) -> int:
        changed = 0
        for comment_id in comment_ids:
            changed += self._repo.update_processing_status(
                video_id, comment_id, target.value
            )
        return changed

    def _process_batch(
        self, video_id: str, rows: Sequence[sqlite3.Row]
    ) -> Tuple[int, int, int]:
        """Claim, classify, and persist one batch. Returns
        (processed, failed, skipped). Failures are per-row: one bad comment
        never aborts the batch, and no raw text is ever logged."""
        comment_ids = [row["comment_id"] for row in rows]
        claimed = self._repo.claim_for_processing(video_id, comment_ids)
        if len(claimed) < len(comment_ids):
            logger.debug(
                "SENTIMENT_CLAIM_SKIPPED",
                extra={
                    "video_id": video_id,
                    "requested": len(comment_ids),
                    "claimed": len(claimed),
                },
            )

        processed_at = datetime.now(timezone.utc).isoformat()
        outcomes: List[SentimentOutcome] = []
        processed = failed = skipped = 0
        for row in rows:
            comment_id = row["comment_id"]
            if comment_id not in claimed:
                continue
            language = row["language"]
            if language not in SUPPORTED_LANGUAGES:
                # Documented policy: analyze only what the model supports.
                outcomes.append(
                    SentimentOutcome(
                        comment_id=comment_id,
                        label=UNSUPPORTED_LANGUAGE,
                        score=None,
                        confidence=None,
                        model=MODEL_ID,
                        processed_at=processed_at,
                        target_status=ProcessingStatus.PROCESSED.value,
                    )
                )
                skipped += 1
                continue
            try:
                verdict = classify(row["normalized_text"])
                # Sprint 5: same single per-row pass also derives the
                # intensity band and runs real emotion inference. Any
                # exception here fails ONLY this row (see catch below) -
                # a bad comment never aborts the batch or the job.
                emotion = classify_emotion(row["normalized_text"])
                intensity = intensity_for(verdict.score)
                outcomes.append(
                    SentimentOutcome(
                        comment_id=comment_id,
                        label=verdict.label.value,
                        score=verdict.score,
                        confidence=verdict.confidence,
                        model=MODEL_ID,
                        processed_at=processed_at,
                        target_status=ProcessingStatus.PROCESSED.value,
                        intensity=intensity.value,
                        emotion_label=emotion.label.value,
                        emotion_score=emotion.score,
                        emotion_model=EMOTION_MODEL_ID,
                    )
                )
                processed += 1
            except Exception as exc:  # noqa: BLE001 - one bad row != failed run
                outcomes.append(
                    SentimentOutcome(
                        comment_id=comment_id,
                        label=None,
                        score=None,
                        confidence=None,
                        model=MODEL_ID,
                        processed_at=processed_at,
                        target_status=ProcessingStatus.FAILED.value,
                    )
                )
                failed += 1
                logger.error(
                    "SENTIMENT_COMMENT_FAILED",
                    extra={
                        "video_id": video_id,
                        "comment_id": comment_id,
                        "error": type(exc).__name__,
                    },
                )
        self._repo.save_sentiment_results(video_id, outcomes, self._inference_batch())
        return processed, failed, skipped

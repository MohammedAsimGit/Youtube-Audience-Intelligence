"""Internal (normalized) data models - the project's own contract.

These models are YouTube-agnostic: the rest of the pipeline (storage, AI,
overlay) depends on these, never on YouTube's raw response shape.
Serialization uses camelCase aliases so the JSON contract matches the
documented API contract (docs/13-backend-api-contract.md).
"""
from datetime import datetime
from typing import Dict, List, Literal, Optional

from pydantic import AliasGenerator, BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=AliasGenerator(serialization_alias=to_camel),
        populate_by_name=True,
        extra="ignore",
    )


class VideoStatistics(ApiModel):
    view_count: Optional[int] = None
    like_count: Optional[int] = None
    comment_count: Optional[int] = None


class VideoMetadata(ApiModel):
    video_id: str
    title: Optional[str] = None
    description: Optional[str] = None
    channel_id: Optional[str] = None
    channel_title: Optional[str] = None
    published_at: Optional[datetime] = None
    category_id: Optional[str] = None
    duration: Optional[str] = None
    statistics: VideoStatistics = VideoStatistics()


class Comment(ApiModel):
    comment_id: str
    video_id: str
    author: Optional[str] = None
    text: str
    text_normalized: str
    published_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    like_count: int = 0
    is_reply: bool = False
    # Thread linkage (Sprint 3 storage will index on this).
    parent_id: Optional[str] = None


class CommentCollection(ApiModel):
    items: List[Comment] = []
    count: int = 0
    has_more: bool = False
    # ok = acquired under cap · none = no accessible comments ·
    # disabled = YouTube reported commentsDisabled (not an error state).
    status: Literal["ok", "none", "disabled"] = "ok"


class SourceInfo(ApiModel):
    provider: Literal["youtube"] = "youtube"
    retrieved_at: datetime
    cached: bool = False


class VideoDataResponse(ApiModel):
    video: VideoMetadata
    comments: CommentCollection
    source: SourceInfo


# ---------------------------------------------------------------------------
# Sprint 3: dataset statistics (real aggregates over the stored dataset)
# ---------------------------------------------------------------------------

class IngestionSummary(ApiModel):
    """Data-quality numbers from the most recent ingestion run (§27)."""

    fetched: int
    valid: int
    rejected: int
    duplicates: int
    inserted: int
    updated: int
    storage_ok: bool
    duration_ms: int
    finished_at: Optional[datetime] = None


class DatasetStats(ApiModel):
    """Per-video dataset statistics - acquisition facts only, never sentiment
    (§26: sentiment statistics belong to Sprint 4)."""

    video_id: str
    total_comments: int
    unique_comments: int
    reply_count: int
    top_level_comment_count: int
    oldest_comment_timestamp: Optional[datetime] = None
    newest_comment_timestamp: Optional[datetime] = None
    last_acquired_at: Optional[datetime] = None
    comments_status: str = "ok"
    processing_status: Dict[str, int] = {}
    last_ingest: Optional[IngestionSummary] = None


# ---------------------------------------------------------------------------
# Sprint 4: sentiment analysis contract (docs/13-backend-api-contract.md)
# ---------------------------------------------------------------------------

# Public analysis lifecycle (never exposes internal row states like READY_-
# FOR_ANALYSIS or per-row PROCESSING details).
SentimentStatus = Literal["NOT_ANALYZED", "PROCESSING", "PROCESSED", "FAILED"]
SentimentLabelLiteral = Literal["POSITIVE", "NEUTRAL", "NEGATIVE"]


class SentimentStats(ApiModel):
    """Aggregated verdicts for one video's stored dataset.

    Percentages use the DENOMINATOR `analyzed` (never collected/stored):
    they are multiples of 0.1 produced by the integer largest-remainder
    method (see `distribution_percentages`), so they sum to exactly 100
    whenever `analyzed > 0` (no float artifacts). `skipped` counts rows
    whose language the model does not support (UNSUPPORTED_LANGUAGE) -
    they are never folded into NEUTRAL.
    """

    total_comments: int
    analyzed: int
    skipped: int
    positive: int
    neutral: int
    negative: int
    positive_percent: float
    neutral_percent: float
    negative_percent: float


class SentimentDatasetMetrics(ApiModel):
    """Dataset facts behind the sentiment analysis (Sprint 4.1, §22).

    Every number distinguishes what was collected / stored / analyzed /
    skipped / failed so the overlay can reconcile its displayed metrics:
        stored == analyzed + skipped + failed + pending rows

    `collected` equals `stored` by construction in this architecture:
    acquisition validates before persisting and every valid comment is
    written immediately (page -> batch -> SQLite). Records rejected during
    validation never enter the dataset - they are reported through
    `GET /stats` -> `lastIngest.fetched/valid/rejected`, not here.

    `has_more` mirrors the stored acquisition flag (YouTube returned a
    nextPageToken when acquisition stopped). `limit_reached` is true only
    when more comments were indicated WHILE the stored dataset is at or
    above the configured COMMENT_ACQUISITION_MAX_COMMENTS - availability
    is never inferred without backend evidence (§14/§22).
    """

    collected: int
    stored: int
    analyzed: int
    skipped: int
    failed: int
    has_more: bool
    limit_reached: bool


# ---------------------------------------------------------------------------
# Sprint 5: audience intelligence contract (docs/13-backend-api-contract.md)
# ---------------------------------------------------------------------------

# Real NRC Emotion Lexicon categories (Mohammad & Turney, 2013, via nrclex).
# NEUTRAL = the model found no emotion word in the comment (bag-of-words,
# zero hits) - a defined outcome, never a fabricated emotion. The lexicon's
# polarity tags (positive/negative) are deliberately absent: polarity is
# already the sentiment axis.
EmotionLabelLiteral = Literal[
    "FEAR",
    "ANGER",
    "ANTICIPATION",
    "TRUST",
    "SURPRISE",
    "SADNESS",
    "DISGUST",
    "JOY",
    "NEUTRAL",
]

# Derived intensity band on |VADER compound| (documented in
# docs/architecture/audience-intelligence.md): not a model output.
IntensityLevelLiteral = Literal["LOW", "MEDIUM", "HIGH"]

# Deterministic audience-mood vocabulary (rules documented in
# docs/architecture/audience-intelligence.md; derived, never LLM-generated).
AudienceMoodLiteral = Literal[
    "POSITIVE", "CALM", "EXCITED", "MIXED", "CONCERNED", "NEGATIVE"
]


class LabelShare(ApiModel):
    """Count + percentage pair for one distribution bucket.

    `percent` uses the same integer largest-remainder method as the
    sentiment distribution (multiples of 0.1, sums to exactly 100 when the
    distribution is non-empty), denominator = rows carrying this axis.
    """

    count: int
    percent: float


class EmotionBreakdown(ApiModel):
    """Emotion distribution over emotion-analyzed rows (Sprint 5, §13).

    `dominant` is null exactly when nothing carries an emotion verdict.
    `dominant_percent` is that label's share of the emotion-analyzed set
    (by the write-path invariant this set == polarity-analyzed rows).
    Distribution keys always cover the full label vocabulary (0 for absent
    buckets) so the UI never has to invent missing entries.
    """

    dominant: Optional[EmotionLabelLiteral] = None
    dominant_percent: float = 0.0
    distribution: Dict[EmotionLabelLiteral, LabelShare] = {}


class IntensityBreakdown(ApiModel):
    """Intensity distribution over intensity-tagged rows (Sprint 5, §14).

    `overall` = band with the largest share (ties resolve to the WEAKER
    band - LOW > MEDIUM > HIGH - so intensity is never overstated);
    null when nothing carries an intensity band.
    """

    overall: Optional[IntensityLevelLiteral] = None
    distribution: Dict[IntensityLevelLiteral, LabelShare] = {}


class ConfidenceBreakdown(ApiModel):
    """Average model confidence over polarity-analyzed rows (Sprint 5, §7).

    This is VADER's documented DECISION MARGIN (distance from the label
    boundary, [0, 1]) - a genuine, deterministic model-derived signal, not
    a calibrated probability (docs/architecture/sentiment-analysis.md).
    `average` is null exactly when nothing has been analyzed - the API
    never reports a fabricated 0.
    """

    average: Optional[float] = None


# ---------------------------------------------------------------------------
# Sprint 4.3: background analysis job contract (docs/13-backend-api-contract.md)
# ---------------------------------------------------------------------------

# Job lifecycle reported by the analysis trigger/status endpoints. CANCELLED
# = superseded by another active video (Sprint 4.2 invalidation), STALE =
# interrupted by a server restart (§32); both are terminal and retryable.
JobStatusLiteral = Literal[
    "QUEUED", "ACQUIRING", "ANALYZING", "COMPLETED", "FAILED", "CANCELLED", "STALE"
]
JobPhaseLiteral = Literal[
    "NONE", "ACQUISITION", "SENTIMENT", "TOPIC", "INSIGHT", "COMPLETE"
]


class AnalysisJobCreatedResponse(ApiModel):
    """POST /api/videos/{video_id}/analysis payload (202 Accepted, §9).

    Returned as soon as the job row exists and the worker thread is
    started - never waits for YouTube pages or sentiment batches. An
    already-running job for the same video returns its id (idempotent
    trigger, §21).
    """

    job_id: str
    video_id: str
    status: JobStatusLiteral


class AnalysisJobStatusResponse(ApiModel):
    """GET /api/videos/{video_id}/analysis/status payload (§7/§10).

    All counts are REAL: `collected` is persisted page progress from the
    job row; `stored`/`analyzed`/`skipped`/`failed`/`pending` are computed
    live from the dataset tables at read time, so they can never drift
    from what the store actually holds.

    Definitions (§23):
        collected - comments fetched and persisted by this job's
                    acquisition (or the stored count when the job reused a
                    fresh dataset without re-acquiring)
        stored    - rows currently in the dataset for this video
        analyzable- rows submitted to the analysis pipeline (== stored:
                    language policy resolves inside, never pre-filtered)
        analyzed  - rows with a polarity verdict (the % denominator, §23)
        skipped   - analyzed rows in unsupported languages
        failed    - rows whose processing failed (retryable)
        pending   - stored rows not yet through the pipeline
    """

    job_id: str
    video_id: str
    status: JobStatusLiteral
    phase: JobPhaseLiteral
    collected: int
    stored: int
    analyzable: int
    analyzed: int
    skipped: int
    failed: int
    pending: int
    has_more: bool
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    finished_at: Optional[datetime] = None


class SentimentAnalysisResponse(ApiModel):
    """GET /api/videos/{video_id}/sentiment payload (Sprint 4, extended 4.1/5).

    `stats` keeps the original Sprint 4 fields (backward compatible);
    `dataset` adds the Sprint 4.1 collected/stored/analyzed/skipped/failed
    + hasMore/limitReached metrics. `dominant_sentiment` is null exactly
    when nothing has been analyzed (empty dataset / not analyzed / all rows
    skipped or failed) - a dominant label is never invented without data.
    Sprint 5 adds `emotion`, `intensity`, `confidence` and `audience_mood`
    (all defaulted, so pre-Sprint-5 consumers keep working): emotion from
    real NRC lexicon inference, intensity/confidence/mood derived by the
    documented deterministic rules - never fabricated AI metrics.
    Model identity, per-comment scores and raw text are intentionally NOT
    exposed here.
    """

    video_id: str
    status: SentimentStatus
    stats: SentimentStats
    dataset: SentimentDatasetMetrics
    dominant_sentiment: Optional[SentimentLabelLiteral] = None
    emotion: EmotionBreakdown = EmotionBreakdown()
    intensity: IntensityBreakdown = IntensityBreakdown()
    confidence: ConfidenceBreakdown = ConfidenceBreakdown()
    audience_mood: Optional[AudienceMoodLiteral] = None


# ---------------------------------------------------------------------------
# Sprint 6: topic / discussion / audience-preference intelligence contract
# ---------------------------------------------------------------------------

# READY = discovery ran and produced the (possibly empty) topic set;
# INSUFFICIENT_DATA = fewer than TOPIC_MIN_COMMENT_COUNT analyzed comments,
# so no theme is claimed at all (§38 - never fabricate topics).
TopicStatusLiteral = Literal["READY", "INSUFFICIENT_DATA"]

# Evidence-based categories (§10-§15), mutually exclusive by construction
# (see topics.py for the exact percentage gates):
#   MOST_DISCUSSED - recurring theme without a strong sentiment lean
#   APPRECIATED    - repeatedly discussed AND strongly positive
#   PAIN_POINT     - repeatedly discussed AND strongly negative
#   MIXED          - audience divided (both sides meaningful)
TopicCategoryLiteral = Literal[
    "MOST_DISCUSSED", "APPRECIATED", "PAIN_POINT", "MIXED"
]


class TopicItem(ApiModel):
    """One discovered discussion theme (Sprint 6, §29).

    Every count is an AGGREGATION of verdicts already stored per comment -
    the topic layer never re-infers sentiment/emotion/intensity. All
    percentages carry explicit denominators:
    - share_percent: mentions / analyzedComments (a comment may belong to
      several topics - themes are not a partition of the dataset),
    - sentiment distribution: counts sum to exactly `mentions`,
    - emotion/intensity distributions: their own tagged row sets inside
      the topic (largest-remainder percentages like everywhere else).
    `confidence` is topic-discovery confidence (support + cluster
    cohesion, documented in topics.py) - NOT the sentiment model's
    decision margin. `evidence` is a factual summary of the computed
    numbers, never an objective claim about the video (§20/§47).
    """

    topic_id: str
    label: str
    mentions: int
    share_percent: float
    key_phrases: List[str] = []
    category: TopicCategoryLiteral
    confidence: float
    evidence: str
    sentiment: Dict[SentimentLabelLiteral, LabelShare] = {}
    dominant_sentiment: Optional[SentimentLabelLiteral] = None
    emotion: EmotionBreakdown = EmotionBreakdown()
    intensity: IntensityBreakdown = IntensityBreakdown()


class TopicAnalysisResponse(ApiModel):
    """GET /api/videos/{video_id}/topics payload (Sprint 6, §28).

    `analyzedComments` is THE topic denominator (polarity-analyzed rows
    only; skipped language rows never count, §23). The four ranked lists
    are views over `topics` with documented gates and scores (minimum
    support, PreferenceScore/PainScore ordering - scores themselves are
    never exposed, §19). `message` carries the honest empty/insufficient
    copy (§38) and is null whenever topics exist.
    """

    video_id: str
    status: TopicStatusLiteral
    analyzed_comments: int
    message: Optional[str] = None
    topics: List[TopicItem] = []
    most_discussed: List[TopicItem] = []
    most_appreciated: List[TopicItem] = []
    pain_points: List[TopicItem] = []
    mixed_topics: List[TopicItem] = []


# ---------------------------------------------------------------------------
# Sprint 7: evidence-based audience insight contract (docs/13-backend-api-
# contract.md)
# ---------------------------------------------------------------------------

# READY = insight generated (from the LLM provider or the deterministic
# generator); INSUFFICIENT_DATA = too little analyzed evidence to claim
# anything (below the Sprint 6 thresholds) - honest copy, never fabricate.
InsightStatusLiteral = Literal["READY", "INSUFFICIENT_DATA"]

# HOW the phrasing was produced - the UI must never present deterministic
# fallback text as AI-generated (Sprint 7 §23/§45):
#   deterministic - default provider, templates over the evidence
#   llm           - a configured LLM provider returned validated output
#   fallback      - an LLM was configured but failed/timed out/invalidated,
#                   so the deterministic generator produced the text instead
InsightSourceLiteral = Literal["deterministic", "llm", "fallback"]

# Insight categories (Sprint 7 §8), one card each in the overlay (§19).
InsightCardCategoryLiteral = Literal[
    "OVERALL_REACTION",
    "WHAT_WORKED",
    "MAIN_DISCUSSION",
    "PAIN_POINT",
    "EMOTIONAL_SIGNAL",
    "TAKEAWAY",
]


class InsightEvidenceLine(ApiModel):
    """One evidence line behind an insight (§20 "Why?" panels).

    Pure presentation of already-measured numbers: `kind` classifies the
    line for the UI, `label`/`value`/`detail` render it, and `topic_id`
    links the line to the underlying topic card when one exists (§21
    insight -> data linking). Never contains raw comment text.
    """

    kind: Literal[
        "SENTIMENT", "EMOTION", "INTENSITY", "CONFIDENCE", "TOPIC",
        "APPRECIATED", "PAIN_POINT", "MIXED", "SAMPLE",
    ]
    label: str
    value: str
    detail: Optional[str] = None
    topic_id: Optional[str] = None


class InsightCard(ApiModel):
    """One structured insight card (§8 categories, §15 structured output).

    `body` is the human sentence; `evidence` is the inspectable backing
    (§20). Cards whose evidence is insufficient are omitted entirely
    instead of rendering an empty claim (§10/§31).
    """

    category: InsightCardCategoryLiteral
    title: str
    body: str
    evidence: List[InsightEvidenceLine] = []


class InsightSampleSize(ApiModel):
    """Denominator honesty (§11): conclusions rest on `analyzed` comments,
    never on collected - skipped rows are never described as analyzed."""

    collected: int
    analyzed: int
    skipped: int


class InsightProviderInfo(ApiModel):
    """Generation provenance for Technical Information (§35) - kept out of
    the main UI. `model` is null for the deterministic generator."""

    name: str
    model: Optional[str] = None


class InsightAnalysisResponse(ApiModel):
    """GET /api/videos/{video_id}/insight payload (Sprint 7 §15).

    `source` distinguishes AI-generated phrasing from data-derived text
    (§23) - the UI must never claim AI generation for `deterministic` or
    `fallback`. `evidence_version` is the analysis fingerprint the
    insight was built from (cache/invalidation, §28). `message` carries
    the honest insufficient-data copy and is null whenever `status` is
    READY. All numbers inside cards are aggregates of stored verdicts;
    the phrasing layer only words them (§5).
    """

    video_id: str
    status: InsightStatusLiteral
    message: Optional[str] = None
    headline: str = ""
    summary: str = ""
    cards: List[InsightCard] = []
    sample: InsightSampleSize = InsightSampleSize(collected=0, analyzed=0, skipped=0)
    source: InsightSourceLiteral = "deterministic"
    provider: InsightProviderInfo = InsightProviderInfo(name="deterministic", model=None)
    evidence_version: str = ""
    generated_at: Optional[datetime] = None
    generation_ms: int = 0


# ---------------------------------------------------------------------------
# Sprint 8: realtime audience intelligence (GET .../realtime)
# ---------------------------------------------------------------------------

# Trend is a direction claim over the NET sentiment balance (positive% -
# negative%) against a configurable percentage-point noise floor - never a
# color-only or fabricated signal (§10: every displayed change is real data).
RealtimeTrendState = Literal["RISING", "FALLING", "STABLE"]
RealtimeActivityLevel = Literal["LOW", "MODERATE", "HIGH"]


class RealtimeSentimentBreakdown(ApiModel):
    """Three-way share of the ANALYZED set (0.0 when nothing analyzed).

    Values come from the shared largest-remainder rounding, so they are
    multiples of 0.1 and sum to exactly 100 whenever analyzed > 0 - the
    same deterministic percentages the sentiment endpoint serves.
    """

    positive: float = 0.0
    neutral: float = 0.0
    negative: float = 0.0


class RealtimeTrend(ApiModel):
    """Sentiment movement since the previous analyzed-batch snapshot.

    `state` is STABLE until the net balance moves at least
    REALTIME_TREND_MIN_CHANGE percentage points; the per-label deltas are
    exposed so the UI can render exact numbers next to the arrow instead
    of a bare color (§16/§21: text labels, never color-only encoding).
    """

    state: RealtimeTrendState = "STABLE"
    change_pp: float = 0.0  # net (positive% - negative%) movement
    positive_pp: float = 0.0
    neutral_pp: float = 0.0
    negative_pp: float = 0.0


class RealtimeActivity(ApiModel):
    """Audience activity over a trailing window of REAL published_at data.

    `new_recent` counts stored comments published within `window_minutes`;
    `rate_per_minute` is that count over the window and decides `level`
    (documented bands in app/services/realtime.py). Zero when no dataset
    rows fall inside the window - activity is never invented (§9).
    """

    level: RealtimeActivityLevel = "LOW"
    new_recent: int = 0
    window_minutes: float = 1.0
    rate_per_minute: float = 0.0


class RealtimeStatusResponse(ApiModel):
    """GET /api/videos/{video_id}/realtime payload (Sprint 8 §19).

    `enabled` mirrors REALTIME_ENABLED; `monitoring` is true only while a
    live monitor thread owns THIS video (active + enabled + extension
    polling). `last_checked_at` is the last YouTube poll attempt;
    `last_updated_at` + `new_comments` describe the most recent data
    change (comments acquired by that update). `version` is a cheap
    change-detection marker over (stored, analyzed) - the overlay refetches
    silently when it flips. Counts are computed live from the store at read
    time (never cached), so the response can never drift from the data.
    """

    video_id: str
    enabled: bool = True
    monitoring: bool = False
    poll_interval_seconds: float = 30.0
    last_checked_at: Optional[datetime] = None
    last_updated_at: Optional[datetime] = None
    new_comments: int = 0
    total_comments: int = 0
    analyzed: int = 0
    pending: int = 0
    skipped: int = 0
    failed: int = 0
    sentiment: RealtimeSentimentBreakdown = RealtimeSentimentBreakdown()
    trend: RealtimeTrend = RealtimeTrend()
    activity: RealtimeActivity = RealtimeActivity()
    dominant_emotion: Optional[str] = None
    version: str = ""

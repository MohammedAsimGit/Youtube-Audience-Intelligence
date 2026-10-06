"""Evidence-based audience insight engine + service (Sprint 7).

Answers "what does all this audience data actually mean?" by PHRASING the
structured signals Sprints 4-6 already computed - never by re-analyzing
comments, never by inventing facts. The pipeline is strictly layered:

    SentimentAnalysisResponse + TopicAnalysisResponse (source of truth)
        -> AudienceEvidence            (structured snapshot, Section 5)
        -> InsightCandidates           (rules/ranking, Section 6)
        -> InsightProvider             (phrasing behind an abstraction)
        -> validated InsightDraft      (schema + grounding, Sections 7/15)
        -> cards + evidence lines      (Section 15/20, built deterministically)

Key properties (documented so they can't drift):

- NO RAW COMMENTS. The phrasing layer sees only aggregates the Sprint 4-6
  engines already persisted (Section 13: bounded prompt, no per-comment
  text, no token blowup, cost independent of comment count).
- PROVIDER-INDEPENDENT (Section 12): `InsightProvider` is a tiny protocol.
  The default `DeterministicProvider` builds the exact same structured
  output from templates over the evidence - no network, no key, always
  available. `OpenAICompatibleProvider` is an optional adapter selected
  purely by configuration (INSIGHT_PROVIDER), never hardcoded elsewhere.
- HONEST SOURCE (Section 23/45): every response reports `source`:
  deterministic (default generator), llm (validated model output), or
  fallback (model configured but failed/timed out/invalidated - the
  deterministic text was served instead). The UI must never present
  deterministic or fallback text as AI-generated.
- GROUNDING (Section 7/14/15): `validate_draft` rejects any draft whose
  numbers are not in the evidence's allowed number set, whose headline is
  outside the length budget, or that uses banned claim language
  (objective superiority / universal claims / causality). Rejection ->
  bounded retry -> deterministic fallback; malformed output is never
  displayed (Section 15).
- THRESHOLDS REUSED (Section 10): the same TOPIC_MIN_COMMENT_COUNT and
  TOPIC_MIN_SUPPORT settings gate this engine - no second threshold
  system. Below the sample floor the API answers INSUFFICIENT_DATA with
  honest copy instead of extrapolating from tiny samples.
- CACHE/REUSE (Section 28): a one-entry memo keyed by
  (video_id, analyzed, latest verdict timestamp) - identical semantics to
  TopicService - makes repeated GETs and the job warm-up free, and any
  dataset change invalidates it (evidence_version changes with it).
- SAFETY (Section 26): generation runs on the caller's thread with the
  provider's own explicit timeout + bounded retries; active-video safety
  is structural (a read only ever sees the active video's rows) plus the
  store-side video guards on the frontend. Failure never fails the job
  (Section 39 pattern): the service answers with fallback text or the
  API's honest error, sentiment/topic intelligence stays intact.
- No raw comment text and no secrets are ever logged; only ids, counts,
  source, timings, and exception class names.
"""
import re
import time
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, List, Optional, Protocol, Tuple

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.repository import DatasetRepository
from app.models.internal import (
    InsightAnalysisResponse,
    InsightCard,
    InsightEvidenceLine,
    InsightProviderInfo,
    InsightSampleSize,
    SentimentAnalysisResponse,
    TopicAnalysisResponse,
    TopicItem,
)
from app.services.sentiment import SentimentService
from app.services.topics import TopicService

logger = get_logger("insights")

_INSUFFICIENT_MESSAGE = (
    "Not enough analyzed audience evidence to generate a reliable insight."
)
_NO_PREFERENCE_MESSAGE = (
    "Not enough repeated audience evidence to identify a strong preference."
)
_NO_PAIN_MESSAGE = (
    "No recurring negative theme reached the evidence threshold."
)

# Banned claim language (Section 7/31): objective superiority, universal
# agreement, causality, recommendations. Deterministic templates never
# produce these; model output containing them is rejected and retried.
_BANNED_CLAIM_RE = re.compile(
    r"\b("
    r"everyone|all viewers|all of (?:the )?audience|every viewer|"
    r"the best|best on youtube|better than|superior|guaranteed|"
    r"caused by|cause of|proves?|therefore|"
    r"recommends?|you should|you will|will love|will hate"
    r")\b",
    re.IGNORECASE,
)

# Numbers appearing in generated text must exist in the evidence
# (Section 7: no invented statistics). Match 1,211 / 68.3 / 37.7%.
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


# ---------------------------------------------------------------------------
# 1. Evidence - structured snapshot of the source-of-truth signals (Section 5)
# ---------------------------------------------------------------------------


class TopicRef(BaseModel):
    """Compact, presentation-ready reference to one ranked topic."""

    model_config = ConfigDict(extra="forbid")

    topic_id: str
    label: str
    mentions: int
    share_percent: float
    positive_percent: float = 0.0
    negative_percent: float = 0.0


class AudienceEvidence(BaseModel):
    """Everything the phrasing layer may use - and nothing else.

    Every field is copied from a Sprint 4-6 response body or derived by
    the documented gates below; there are no free-form strings a provider
    could smuggle a claim through (labels are fixed vocabularies, topic
    labels come from discovered clusters).
    """

    model_config = ConfigDict(extra="forbid")

    video_id: str
    evidence_version: str
    collected: int
    analyzed: int
    skipped: int
    positive_percent: float
    neutral_percent: float
    negative_percent: float
    dominant_sentiment: Optional[str] = None
    dominant_emotion: Optional[str] = None
    dominant_emotion_percent: float = 0.0
    intensity_overall: Optional[str] = None
    confidence_average: Optional[float] = None
    audience_mood: Optional[str] = None
    top_topic: Optional[TopicRef] = None
    appreciated: List[TopicRef] = []
    pain_points: List[TopicRef] = []
    mixed: List[TopicRef] = []

    def allowed_numbers(self) -> set:
        """Canonical string forms of every number this evidence contains.

        Grounding check accepts a number in the text iff one of its
        canonical forms (.1f / .2f / %g / int) matches this set - so
        "68.3%", "68.3", "68.3% positive" all pass while 61.2 never can.
        """
        values: List[float] = [
            self.collected, self.analyzed, self.skipped,
            self.positive_percent, self.neutral_percent, self.negative_percent,
        ]
        if self.dominant_emotion_percent:
            values.append(self.dominant_emotion_percent)
        if self.confidence_average is not None:
            values.append(self.confidence_average)
        for ref in (
            [self.top_topic] + self.appreciated + self.pain_points + self.mixed
        ):
            if ref is None:
                continue
            values.extend([ref.mentions, ref.share_percent,
                           ref.positive_percent, ref.negative_percent])
        allowed: set = set()
        for value in values:
            allowed.add(f"{value:.1f}")
            allowed.add(f"{value:.2f}")
            allowed.add(f"{value:g}")
            if float(value).is_integer():
                allowed.add(str(int(value)))
        return allowed


def _topic_ref(item: TopicItem) -> TopicRef:
    return TopicRef(
        topic_id=item.topic_id,
        label=item.label,
        mentions=item.mentions,
        share_percent=item.share_percent,
        positive_percent=item.sentiment["POSITIVE"].percent,
        negative_percent=item.sentiment["NEGATIVE"].percent,
    )


def build_evidence(
    video_id: str,
    sentiment: SentimentAnalysisResponse,
    topics: TopicAnalysisResponse,
    evidence_version: str,
) -> AudienceEvidence:
    """Pure: source-of-truth responses -> structured evidence (Section 5).

    Only metrics that actually exist are copied - missing blocks stay
    null/empty instead of being invented (Section 4: never invent). Topic
    lists keep their ranked order: the first entry of each ranked section
    IS the strongest supported candidate by the Sprint 6 scores.
    """
    emotion = sentiment.emotion
    return AudienceEvidence(
        video_id=video_id,
        evidence_version=evidence_version,
        collected=sentiment.dataset.collected,
        analyzed=sentiment.dataset.analyzed,
        skipped=sentiment.dataset.skipped,
        positive_percent=sentiment.stats.positive_percent,
        neutral_percent=sentiment.stats.neutral_percent,
        negative_percent=sentiment.stats.negative_percent,
        dominant_sentiment=sentiment.dominant_sentiment,
        dominant_emotion=emotion.dominant,
        dominant_emotion_percent=emotion.dominant_percent,
        intensity_overall=sentiment.intensity.overall,
        confidence_average=sentiment.confidence.average,
        audience_mood=sentiment.audience_mood,
        top_topic=_topic_ref(topics.most_discussed[0])
        if topics.most_discussed else None,
        appreciated=[_topic_ref(t) for t in topics.most_appreciated],
        pain_points=[_topic_ref(t) for t in topics.pain_points],
        mixed=[_topic_ref(t) for t in topics.mixed_topics],
    )


# ---------------------------------------------------------------------------
# 2. Insight candidates - rules/ranking over the evidence (Section 6)
# ---------------------------------------------------------------------------


class OverallCandidate(BaseModel):
    headline: str
    detail: str  # e.g. "68.3% positive, 29.0% neutral, 2.7% negative"


class EmotionCandidate(BaseModel):
    label: str
    percent: float


class InsightCandidates(BaseModel):
    """Structured insight candidates - selected BEFORE any phrasing.

    Each optional field is None exactly when the evidence does not clear
    the reused Sprint 6 thresholds (Section 10): tiny samples and
    below-support themes can never become claims.
    """

    model_config = ConfigDict(extra="forbid")

    overall: Optional[OverallCandidate] = None
    appreciated: Optional[TopicRef] = None
    main_discussion: Optional[TopicRef] = None
    pain_point: Optional[TopicRef] = None
    emotion: Optional[EmotionCandidate] = None
    mixed: Optional[TopicRef] = None


def _overall_candidate(evidence: AudienceEvidence) -> OverallCandidate:
    """Section 8.1 gates over the measured shares - never a vibe check."""
    pos, neg = evidence.positive_percent, evidence.negative_percent
    if pos >= 60:
        headline = "The audience response is strongly positive"
    elif pos >= 50 and pos > neg:
        headline = "The audience response is mostly positive"
    elif neg >= 60:
        headline = "The audience response is strongly negative"
    elif neg >= 50 and neg > pos:
        headline = "The audience response is mostly negative"
    elif evidence.dominant_sentiment == "NEUTRAL" or abs(pos - neg) < 10:
        headline = "The audience response is mixed"
    elif pos > neg:
        headline = "The audience response leans positive"
    else:
        headline = "The audience response leans negative"
    detail = (
        f"{evidence.positive_percent:.1f}% positive, "
        f"{evidence.neutral_percent:.1f}% neutral, "
        f"{evidence.negative_percent:.1f}% negative"
    )
    return OverallCandidate(headline=headline, detail=detail)


def select_candidates(
    evidence: AudienceEvidence, min_support: int
) -> InsightCandidates:
    """Pure selection rules (Sections 6/9/10) - testable without phrasing.

    Gates: analyzed >= TOPIC_MIN_COMMENT_COUNT is enforced by the service
    (INSUFFICIENT_DATA otherwise); each topic candidate must independently
    clear TOPIC_MIN_SUPPORT mentions; the emotion signal must be a real
    non-neutral dominant above a meaningful share (a 12% TRUST plurality
    is not a signal worth a card).
    """
    overall = _overall_candidate(evidence) if evidence.analyzed > 0 else None

    main = evidence.top_topic
    if main is not None and main.mentions < min_support:
        main = None

    def supported(refs: List[TopicRef]) -> Optional[TopicRef]:
        for ref in refs:
            if ref.mentions >= min_support:
                return ref
        return None

    emotion = None
    if (
        evidence.dominant_emotion is not None
        and evidence.dominant_emotion != "NEUTRAL"
        and evidence.dominant_emotion_percent >= 20.0
    ):
        emotion = EmotionCandidate(
            label=evidence.dominant_emotion,
            percent=evidence.dominant_emotion_percent,
        )

    return InsightCandidates(
        overall=overall,
        appreciated=supported(evidence.appreciated),
        main_discussion=main,
        pain_point=supported(evidence.pain_points),
        emotion=emotion,
        mixed=supported(evidence.mixed),
    )


# ---------------------------------------------------------------------------
# 3. Draft (structured output) + grounding validation (Sections 7/14/15)
# ---------------------------------------------------------------------------


class InsightDraft(BaseModel):
    """Structured phrasing output (Section 15) - typed, validated, bounded.

    `extra=forbid`: a model returning extra keys is malformed output, not
    something to silently pick through. Null fields mean "no card" - the
    UI shows nothing rather than an empty claim (Section 31).
    """

    model_config = ConfigDict(extra="forbid")

    headline: str
    summary: str
    what_worked: Optional[str] = None
    main_discussion: Optional[str] = None
    pain_point: Optional[str] = None
    emotional_signal: Optional[str] = None
    takeaway: Optional[str] = None

    @field_validator("headline", "summary")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be empty")
        return value.strip()


class ProviderError(Exception):
    """Provider transport or output failure - never surfaces raw details
    to users (Section 38); the service logs the class name only.

    `permanent=True` marks failures where retrying cannot help (missing
    key, 4xx rejection): the service stops the bounded retry loop at once
    instead of burning attempts on a request the provider will refuse
    again (Section 26).
    """

    def __init__(self, message: str, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class InsightProvider(Protocol):
    """Section 12 abstraction: evidence in, structured draft out."""

    name: str
    model: Optional[str]

    def generate(
        self, evidence: AudienceEvidence, candidates: InsightCandidates
    ) -> dict:  # pragma: no cover - protocol
        ...


def _numbers_grounded(text: str, allowed: set) -> bool:
    for token in _NUMBER_RE.findall(text):
        raw = token.replace(",", "")
        try:
            value = float(raw)
        except ValueError:  # pragma: no cover - regex guarantees numeric
            return False
        forms = {f"{value:.1f}", f"{value:.2f}", f"{value:g}"}
        if value.is_integer():
            forms.add(str(int(value)))
        if not (forms & allowed):
            return False
    return True


def validate_draft(payload: object, evidence: AudienceEvidence) -> InsightDraft:
    """Schema + grounding gate (Sections 7/15/39) - raises ProviderError.

    Checks, in order: typed schema (extra=forbid, non-empty), length
    budgets (Section 17: headline 2-12 words, bodies bounded), banned
    claim language, and number grounding against the evidence. The
    deterministic generator passes by construction (its numbers come
    from the evidence objects themselves).
    """
    if not isinstance(payload, dict):
        raise ProviderError("draft is not an object")
    try:
        draft = InsightDraft.model_validate(payload)
    except ValidationError as exc:
        raise ProviderError(f"draft schema invalid: {exc.error_count()} errors") from exc

    headline_words = draft.headline.split()
    if not (2 <= len(headline_words) <= 12):
        raise ProviderError("headline outside 2-12 word budget")
    if len(draft.summary) > 700:
        raise ProviderError("summary exceeds length budget")

    allowed = evidence.allowed_numbers()
    fields = [
        draft.headline, draft.summary, draft.what_worked, draft.main_discussion,
        draft.pain_point, draft.emotional_signal, draft.takeaway,
    ]
    for text in fields:
        if text is None:
            continue
        if len(text) > 500:
            raise ProviderError("insight body exceeds length budget")
        if _BANNED_CLAIM_RE.search(text):
            raise ProviderError("banned claim language in draft")
        if not _numbers_grounded(text, allowed):
            raise ProviderError("draft contains a number not present in evidence")
    return draft


# ---------------------------------------------------------------------------
# 4. Providers (Section 12: abstraction + default + optional adapter)
# ---------------------------------------------------------------------------


class DeterministicProvider:
    """Default generator: templates over the evidence (Section 16).

    Produces the exact structured draft an LLM would - so the fallback
    path is a drop-in replacement with the same schema and the same
    grounding guarantees (every number formats from an evidence value).
    """

    name = "deterministic"
    model: Optional[str] = None

    def generate(
        self, evidence: AudienceEvidence, candidates: InsightCandidates
    ) -> dict:
        overall = candidates.overall
        headline = overall.headline if overall is not None else "Audience insight"
        detail = overall.detail if overall is not None else ""

        sentences = [
            f"The analyzed audience shows {detail} across "
            f"{evidence.analyzed:,} analyzed comments."
        ]
        if candidates.appreciated is not None:
            ref = candidates.appreciated
            sentences.append(
                f"Viewers repeatedly praised {ref.label} "
                f"({ref.mentions} mentions, {ref.positive_percent:.1f}% positive)."
            )
        if candidates.main_discussion is not None:
            ref = candidates.main_discussion
            sentence = (
                f"Most discussion centers on {ref.label} "
                f"({ref.mentions} mentions)"
            )
            if candidates.pain_point is not None:
                pain = candidates.pain_point
                sentence += (
                    f", while {pain.label} is a recurring concern "
                    f"({pain.mentions} mentions, "
                    f"{pain.negative_percent:.1f}% negative)"
                )
            sentences.append(sentence + ".")
        summary = " ".join(sentences[:3])

        if candidates.appreciated is not None:
            ref = candidates.appreciated
            what_worked = (
                f"Viewers repeatedly praised {ref.label} "
                f"({ref.mentions} mentions, {ref.positive_percent:.1f}% positive)."
            )
        else:
            what_worked = _NO_PREFERENCE_MESSAGE

        if candidates.main_discussion is not None:
            ref = candidates.main_discussion
            main_discussion = (
                f"{ref.label} is the main discussion theme "
                f"({ref.mentions} mentions, {ref.share_percent:.1f}% of analyzed comments)."
            )
        else:
            main_discussion = None

        if candidates.pain_point is not None:
            ref = candidates.pain_point
            pain_point = (
                "Some viewers expressed recurring frustration with "
                f"{ref.label} ({ref.mentions} mentions, "
                f"{ref.negative_percent:.1f}% negative)."
            )
        else:
            pain_point = _NO_PAIN_MESSAGE

        if candidates.emotion is not None:
            emotional_signal = (
                f"{candidates.emotion.label} is the dominant detected emotion "
                f"({candidates.emotion.percent:.1f}% of analyzed comments), "
                f"associated with the dominant "
                f"{(evidence.dominant_sentiment or 'NEUTRAL').lower()} sentiment."
            )
        else:
            emotional_signal = None

        parts = [f"Overall, {headline[0].lower() + headline[1:]}, with the strongest appreciation centered on {candidates.appreciated.label}." if candidates.appreciated is not None else f"Overall, {headline[0].lower() + headline[1:]}, with no single theme dominating appreciation."]
        if candidates.pain_point is not None:
            parts.append(
                f"{candidates.pain_point.label} remains a recurring concern."
            )
        takeaway = " ".join(parts)

        return {
            "headline": headline,
            "summary": summary,
            "what_worked": what_worked,
            "main_discussion": main_discussion,
            "pain_point": pain_point,
            "emotional_signal": emotional_signal,
            "takeaway": takeaway,
        }


_SYSTEM_PROMPT = """You are generating an audience insight from structured analysis.

Use ONLY the supplied evidence.
Do not invent:
- topics
- statistics
- opinions
- causes
- recommendations
- comparisons
- facts outside the supplied data

Do not claim objective superiority.
Never say everyone/all viewers agreed; describe the analyzed audience only.
Prefer phrases such as: viewers repeatedly mentioned, the analyzed audience,
the strongest positive theme, the most discussed topic, a recurring concern,
associated with (never caused by).

Respond with a single JSON object with exactly these keys:
headline (2-10 words), summary (1-3 sentences), what_worked, main_discussion,
pain_point, emotional_signal, takeaway (each 1 sentence or null when the
evidence does not support that category).
Every number you write must appear verbatim in the evidence."""


def _evidence_prompt(evidence: AudienceEvidence, candidates: InsightCandidates) -> str:
    """Compact evidence block (Section 13): bounded, aggregate-only.

    Size is O(topics), NOT O(comments) - 5,000 analyzed comments produce
    the same handful of lines as 100.
    """
    lines = [
        f"Overall sentiment: {evidence.positive_percent:.1f}% positive, "
        f"{evidence.neutral_percent:.1f}% neutral, "
        f"{evidence.negative_percent:.1f}% negative",
        f"Sample: {evidence.analyzed} analyzed of {evidence.collected} collected "
        f"({evidence.skipped} skipped unsupported language)",
    ]
    if evidence.dominant_emotion is not None:
        lines.append(
            f"Dominant emotion: {evidence.dominant_emotion} "
            f"{evidence.dominant_emotion_percent:.1f}%"
        )
    if evidence.intensity_overall is not None:
        lines.append(f"Overall intensity: {evidence.intensity_overall}")
    if evidence.confidence_average is not None:
        lines.append(f"Average sentiment confidence: {evidence.confidence_average:.2f}")
    if evidence.audience_mood is not None:
        lines.append(f"Audience mood: {evidence.audience_mood}")
    if candidates.main_discussion is not None:
        ref = candidates.main_discussion
        lines.append(
            f"Most discussed: {ref.label} - {ref.mentions} mentions, "
            f"{ref.share_percent:.1f}% share"
        )
    for ref in candidates.appreciated and [candidates.appreciated] or []:
        lines.append(
            f"Appreciated: {ref.label} - {ref.mentions} mentions, "
            f"{ref.positive_percent:.1f}% positive"
        )
    if candidates.pain_point is not None:
        ref = candidates.pain_point
        lines.append(
            f"Pain point: {ref.label} - {ref.mentions} mentions, "
            f"{ref.negative_percent:.1f}% negative"
        )
    if candidates.mixed is not None:
        ref = candidates.mixed
        lines.append(
            f"Mixed discussion: {ref.label} - {ref.mentions} mentions, "
            f"{ref.positive_percent:.1f}% positive / "
            f"{ref.negative_percent:.1f}% negative"
        )
    if candidates.emotion is not None:
        lines.append(
            f"Emotional signal: {candidates.emotion.label} "
            f"{candidates.emotion.percent:.1f}%"
        )
    return "\n".join(lines)


class OpenAICompatibleProvider:
    """Optional chat-completions adapter (Section 12 - configured, not hardcoded).

    Posts to {INSIGHT_API_BASE_URL}/chat/completions with the structured
    evidence as the user message. Explicit timeout + bounded retries come
    from Settings (Section 26); the API key never leaves the server and
    is never logged (Section 38). Any failure raises ProviderError and
    the service falls back to the deterministic generator.
    """

    def __init__(self, settings: Settings) -> None:
        self.name = "openai_compatible"
        self.model = settings.insight_model
        self._settings = settings

    def generate(
        self, evidence: AudienceEvidence, candidates: InsightCandidates
    ) -> dict:
        settings = self._settings
        if not settings.insight_api_key:
            raise ProviderError("insight API key not configured", permanent=True)
        url = settings.insight_api_base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": settings.insight_model,
            "temperature": 0.2,
            "max_tokens": settings.insight_max_output_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": "Evidence:\n"
                    + _evidence_prompt(evidence, candidates),
                },
            ],
        }
        attempts = settings.insight_retry_attempts
        last_error: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                response = httpx.post(
                    url,
                    json=payload,
                    timeout=settings.insight_timeout_seconds,
                    headers={
                        "Authorization": f"Bearer {settings.insight_api_key}",
                        "Content-Type": "application/json",
                    },
                )
                if response.status_code >= 500 or response.status_code == 429:
                    last_error = ProviderError(
                        f"provider status {response.status_code}"
                    )
                    continue  # transient - retry within the bounded budget
                if response.status_code >= 400:
                    # 4xx (bad key/model): permanent, do not burn retries.
                    raise ProviderError(
                        f"provider rejected request ({response.status_code})",
                        permanent=True,
                    )
                body = response.json()
                content = body["choices"][0]["message"]["content"]
                import json as _json

                return _json.loads(content)
            except ProviderError:
                raise
            except Exception as exc:  # noqa: BLE001 - classified, never surfaced
                last_error = exc
                if attempt + 1 < attempts:
                    continue
        raise ProviderError(
            f"provider call failed: {type(last_error).__name__}"
        ) from last_error


# ---------------------------------------------------------------------------
# 5. Cards - deterministic assembly of body + evidence lines (Sections 15/20)
# ---------------------------------------------------------------------------


def _topic_lines(refs: List[TopicRef], kind: str) -> List[InsightEvidenceLine]:
    lines = []
    for ref in refs:
        if kind == "APPRECIATED":
            detail = f"{ref.positive_percent:.1f}% positive"
        elif kind == "PAIN_POINT":
            detail = f"{ref.negative_percent:.1f}% negative"
        else:
            detail = f"{ref.share_percent:.1f}% of analyzed"
        lines.append(
            InsightEvidenceLine(
                kind=kind,  # type: ignore[arg-type]
                label=ref.label,
                value=f"{ref.mentions} mentions",
                detail=detail,
                topic_id=ref.topic_id,
            )
        )
    return lines


def _sample_line(evidence: AudienceEvidence) -> InsightEvidenceLine:
    return InsightEvidenceLine(
        kind="SAMPLE",
        label="Evidence base",
        value=f"{evidence.analyzed} analyzed comments",
        detail=f"{evidence.collected} collected · {evidence.skipped} skipped",
    )


def _sentiment_line(evidence: AudienceEvidence) -> InsightEvidenceLine:
    return InsightEvidenceLine(
        kind="SENTIMENT",
        label="Overall sentiment",
        value=f"{evidence.positive_percent:.1f}% positive",
        detail=f"{evidence.neutral_percent:.1f}% neutral · {evidence.negative_percent:.1f}% negative",
    )


def build_cards(
    evidence: AudienceEvidence,
    candidates: InsightCandidates,
    draft: InsightDraft,
) -> List[InsightCard]:
    """Section 15/20: cards are assembled HERE, never by the provider.

    The phrasing layer supplies sentences only; every evidence line is
    generated from the evidence object, so a "Why?" panel is always real
    and topic lines always link to their topic cards (Section 21).
    """
    cards: List[InsightCard] = []

    overall_lines = [_sentiment_line(evidence)]
    if candidates.emotion is not None:
        overall_lines.append(
            InsightEvidenceLine(
                kind="EMOTION",
                label="Dominant emotion",
                value=f"{candidates.emotion.label} {candidates.emotion.percent:.1f}%",
            )
        )
    if candidates.mixed is not None:
        overall_lines.append(
            InsightEvidenceLine(
                kind="MIXED",
                label="Divided discussion",
                value=f"{candidates.mixed.label} · {candidates.mixed.mentions} mentions",
                topic_id=candidates.mixed.topic_id,
            )
        )
    overall_lines.append(_sample_line(evidence))
    cards.append(
        InsightCard(
            category="OVERALL_REACTION",
            title=draft.headline,
            body=draft.summary,
            evidence=overall_lines,
        )
    )

    if draft.what_worked is not None:
        appreciated_refs = (
            [candidates.appreciated] if candidates.appreciated is not None else []
        )
        cards.append(
            InsightCard(
                category="WHAT_WORKED",
                title=candidates.appreciated.label
                if candidates.appreciated is not None
                else "Repeated appreciation",
                body=draft.what_worked,
                evidence=_topic_lines(appreciated_refs, "APPRECIATED")
                + [_sentiment_line(evidence)],
            )
        )

    if draft.main_discussion is not None and candidates.main_discussion is not None:
        ref = candidates.main_discussion
        lines = [
            InsightEvidenceLine(
                kind="TOPIC",
                label=ref.label,
                value=f"{ref.mentions} mentions",
                detail=f"{ref.share_percent:.1f}% of analyzed",
                topic_id=ref.topic_id,
            )
        ]
        if candidates.mixed is not None:
            lines.append(
                InsightEvidenceLine(
                    kind="MIXED",
                    label=candidates.mixed.label,
                    value=f"{candidates.mixed.mentions} mentions",
                    detail=f"{candidates.mixed.positive_percent:.1f}% positive / "
                    f"{candidates.mixed.negative_percent:.1f}% negative",
                    topic_id=candidates.mixed.topic_id,
                )
            )
        cards.append(
            InsightCard(
                category="MAIN_DISCUSSION",
                title=ref.label,
                body=draft.main_discussion,
                evidence=lines,
            )
        )

    if draft.pain_point is not None:
        pain_refs = (
            [candidates.pain_point] if candidates.pain_point is not None else []
        )
        cards.append(
            InsightCard(
                category="PAIN_POINT",
                title=candidates.pain_point.label
                if candidates.pain_point is not None
                else "Recurring concerns",
                body=draft.pain_point,
                evidence=_topic_lines(pain_refs, "PAIN_POINT")
                + [_sentiment_line(evidence)],
            )
        )

    if draft.emotional_signal is not None and candidates.emotion is not None:
        lines = [
            InsightEvidenceLine(
                kind="EMOTION",
                label=candidates.emotion.label,
                value=f"{candidates.emotion.percent:.1f}% of analyzed comments",
            )
        ]
        if evidence.intensity_overall is not None:
            lines.append(
                InsightEvidenceLine(
                    kind="INTENSITY",
                    label="Overall intensity",
                    value=evidence.intensity_overall,
                )
            )
        if evidence.confidence_average is not None:
            lines.append(
                InsightEvidenceLine(
                    kind="CONFIDENCE",
                    label="Sentiment confidence",
                    value=f"{evidence.confidence_average:.2f}",
                )
            )
        cards.append(
            InsightCard(
                category="EMOTIONAL_SIGNAL",
                title=candidates.emotion.label,
                body=draft.emotional_signal,
                evidence=lines,
            )
        )

    if draft.takeaway is not None:
        takeaway_lines = [_sentiment_line(evidence)]
        if candidates.appreciated is not None:
            takeaway_lines += _topic_lines([candidates.appreciated], "APPRECIATED")
        if candidates.pain_point is not None:
            takeaway_lines += _topic_lines([candidates.pain_point], "PAIN_POINT")
        cards.append(
            InsightCard(
                category="TAKEAWAY",
                title="Key takeaway",
                body=draft.takeaway,
                evidence=takeaway_lines,
            )
        )

    return cards


# ---------------------------------------------------------------------------
# 6. Service (orchestration + memo + fallback, Sections 24/26/28)
# ---------------------------------------------------------------------------


class InsightService:
    """Compute-on-read audience insight for one video dataset.

    Same persistence decision as TopicService (no new tables): the
    evidence is assembled from two memoized service responses, the
    phrasing is provider-generated, and a one-entry
    (video_id, analyzed, latest) memo makes repeated GETs and the job
    warm-up free while any analysis change rebuilds it (Section 28).
    """

    def __init__(
        self,
        repository: DatasetRepository,
        sentiment: SentimentService,
        topics: TopicService,
        settings: Settings,
    ) -> None:
        self._repo = repository
        self._sentiment = sentiment
        self._topics = topics
        self._settings = settings
        self._lock = Lock()
        self._memo: Optional[Tuple[Tuple[object, ...], InsightAnalysisResponse]] = None
        self._deterministic = DeterministicProvider()
        if settings.insight_provider == "openai_compatible":
            self._llm: Optional[InsightProvider] = OpenAICompatibleProvider(settings)
        else:
            self._llm = None

    def get_insight(self, video_id: str) -> Optional[InsightAnalysisResponse]:
        """Audience insight for one video; None when untracked (404)."""
        if self._repo.get_video(video_id) is None:
            return None

        # Fast path: a warm memo serves from the cheap fingerprint alone.
        analyzed, latest = self._repo.get_analysis_fingerprint(video_id)
        key = (video_id, analyzed, latest)
        with self._lock:
            if self._memo is not None and self._memo[0] == key:
                return self._memo[1]

        # Cache miss: one sentiment read serves BOTH the sample size and
        # the evidence (its aggregates come back cheap on an already-
        # PROCESSED video; on a pending dataset it runs the engine inline
        # like GET /sentiment does). The fingerprint is RE-READ after it,
        # so the key always reflects the final analyzed set this insight
        # is built from (Section 28).
        sentiment_response = self._sentiment.get_analysis(video_id)
        analyzed, latest = self._repo.get_analysis_fingerprint(video_id)
        key = (video_id, analyzed, latest)
        with self._lock:
            if self._memo is not None and self._memo[0] == key:
                return self._memo[1]

        version = f"{analyzed}:{latest or 'none'}"
        started = time.monotonic()
        generation_ms = 0
        sample = self._sample_of(sentiment_response)

        if analyzed < self._settings.topic_min_comment_count:
            # Section 10/11: never extrapolate from tiny samples - honest
            # insufficient copy over the REAL denominators.
            response = InsightAnalysisResponse(
                video_id=video_id,
                status="INSUFFICIENT_DATA",
                message=_INSUFFICIENT_MESSAGE,
                sample=sample,
                source="deterministic",
                provider=InsightProviderInfo(name="deterministic", model=None),
                evidence_version=version,
                generated_at=datetime.now(timezone.utc),
                generation_ms=0,
            )
        else:
            topics_response = self._topics.get_analysis(video_id)
            if sentiment_response is None or topics_response is None:
                # Rows vanished mid-read (video switch): answer honestly
                # instead of building evidence from partial data.
                response = InsightAnalysisResponse(
                    video_id=video_id,
                    status="INSUFFICIENT_DATA",
                    message=_INSUFFICIENT_MESSAGE,
                    sample=sample,
                    evidence_version=version,
                    generated_at=datetime.now(timezone.utc),
                )
            else:
                evidence = build_evidence(
                    video_id, sentiment_response, topics_response, version
                )
                candidates = select_candidates(
                    evidence, self._settings.topic_min_support
                )
                draft, source, provider, generation_ms = self._phrase(
                    evidence, candidates
                )
                response = InsightAnalysisResponse(
                    video_id=video_id,
                    status="READY",
                    message=None,
                    headline=draft.headline,
                    summary=draft.summary,
                    cards=build_cards(evidence, candidates, draft),
                    sample=sample,
                    source=source,
                    provider=provider,
                    evidence_version=version,
                    generated_at=datetime.now(timezone.utc),
                    generation_ms=generation_ms,
                )

        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "INSIGHT_GENERATED",
            extra={
                "video_id": video_id,
                "status": response.status,
                "source": response.source,
                "cards": len(response.cards),
                "generation_ms": generation_ms if response.status == "READY" else 0,
                "elapsed_ms": elapsed_ms,
            },
        )
        with self._lock:
            self._memo = (key, response)
        return response

    # ------------------------------------------------------------- internals
    @staticmethod
    def _sample_of(sentiment_response: Optional[SentimentAnalysisResponse]) -> InsightSampleSize:
        if sentiment_response is None:
            return InsightSampleSize(collected=0, analyzed=0, skipped=0)
        dataset = sentiment_response.dataset
        return InsightSampleSize(
            collected=dataset.collected,
            analyzed=dataset.analyzed,
            skipped=dataset.skipped,
        )

    def _phrase(
        self, evidence: AudienceEvidence, candidates: InsightCandidates
    ) -> Tuple[InsightDraft, str, InsightProviderInfo, int]:
        """Provider call + validation + bounded retry + fallback (Section 16).

        Returns (draft, source, provider_info, generation_ms). A failed
        LLM path never raises: the deterministic generator serves the
        same schema and `source=fallback` records the truth (Section 23).
        """
        if self._llm is None:
            started = time.monotonic()
            draft = validate_draft(
                self._deterministic.generate(evidence, candidates), evidence
            )
            ms = int((time.monotonic() - started) * 1000)
            return (
                draft,
                "deterministic",
                InsightProviderInfo(name="deterministic", model=None),
                ms,
            )

        started = time.monotonic()
        last_error: Optional[str] = None
        for _attempt in range(self._settings.insight_retry_attempts):
            try:
                payload = self._llm.generate(evidence, candidates)
                draft = validate_draft(payload, evidence)
                ms = int((time.monotonic() - started) * 1000)
                logger.info(
                    "INSIGHT_LLM_OK",
                    extra={
                        "video_id": evidence.video_id,
                        "model": self._llm.model,
                        "generation_ms": ms,
                    },
                )
                return (
                    draft,
                    "llm",
                    InsightProviderInfo(
                        name=self._llm.name, model=self._llm.model
                    ),
                    ms,
                )
            except Exception as exc:  # noqa: BLE001 - classified, then fallback
                last_error = type(exc).__name__
                if isinstance(exc, ProviderError) and exc.permanent:
                    break  # retrying a refused request cannot help (§26)

        logger.warning(
            "INSIGHT_LLM_FALLBACK",
            extra={"video_id": evidence.video_id, "error": last_error},
        )
        draft = validate_draft(
            self._deterministic.generate(evidence, candidates), evidence
        )
        ms = int((time.monotonic() - started) * 1000)
        return (
            draft,
            "fallback",
            InsightProviderInfo(name="deterministic", model=None),
            ms,
        )

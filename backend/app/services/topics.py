"""Topic / discussion intelligence engine + service (Sprint 6).

Answers "what is the audience talking about, appreciating, and struggling
with?" from the SAME analyzed comments the sentiment/emotion/intensity axes
already describe - no second NLP pipeline, no model download, no new
dependency (inspected: the project runs on pure-Python CPU libraries only,
so discovery here is deterministic lexical analysis over stored text).

Layering (same as every other service):

    Route -> TopicService -> DatasetRepository -> SQLite
                     |
                     v
            discover_topics() (pure text -> topics, no I/O)

Two clearly separated halves:

1. `discover_topics()` - PURE. Takes the already-analyzed comments
   (text + stored sentiment/emotion/intensity verdicts) and returns topic
   results. No SQLite, no FastAPI, no network, no globals. It NEVER runs
   model inference: every sentiment/emotion/intensity number inside a
   topic is an AGGREGATION of verdicts the Sprint 4/5 engines already
   persisted on each row (§17/§18: reuse, never re-infer).

2. `TopicService` - orchestration over the existing repository only:
   reads analyzed rows through `iter_comments`, answers INSUFFICIENT_DATA
   below TOPIC_MIN_COMMENT_COUNT (§12/§38), and memoizes the last result
   per (video, analysis fingerprint) so repeated GETs are cheap while any
   content change (new verdicts / reset rows) invalidates the memo.

Discovery algorithm (documented, deterministic, §6/§7/§8):

    normalized text
      -> strip URLs + @mentions (quality filter, §22)
      -> lowercase ASCII-word tokens (the SAME tokenizer the sentiment
         engine uses, so topic and sentiment evidence share one text view)
      -> content segments (split at stopwords / short / gibberish tokens)
      -> candidate phrases = 1-3 gram runs inside segments
      -> drop all-generic phrases (bare "video" / "good" / "nice" ... §22)
      -> document frequency per phrase (comments are the unit, never
         raw occurrence counts)
      -> rank by df * idf, keep the top candidates
      -> greedy clustering: a phrase joins the first existing cluster that
         contains a member phrase with token-Jaccard >= 0.6 (§7 "same idea
         expressed differently" when the phrasings share their content
         words)
      -> second pass: merge clusters covering the SAME discussion - at
         least 70% of the LARGER cluster's comments must overlap (the
         symmetric bound stops a hub word like "react" from gluing every
         theme it appears in; §7 still fires for genuinely same-thread
         phrasings such as "roadmap" vs "path" when the same comments
         carry both)
      -> third pass: absorb duplicate topics (§22) - a cluster whose
         comments are fully contained in another cluster's AND that
         shares at least one content word with it is folded in (bare
         "Clear" next to "Clear learning roadmap" is the same idea said
         worse, not a second theme)
      -> quality + minimum-support gates (§12/§22), deterministic labels
         from the cluster's own phrases (§8), metrics aggregated from the
         stored verdicts (§9/§16/§17/§18), confidence from support +
         cluster cohesion (§21), categories with transparent, documented
         scores (§11/§14/§15/§19).

Known limitations (documented honestly, §6/§7):
- Lexical, not distributional: grouping shares CONTENT WORDS or COMMENTS,
  it does not embed meaning. Truly synonym-only phrasings with no shared
  token and no co-discussed bridge ("roadmap" vs "path" appearing in
  disjoint comments) stay separate themes. No claim of LLM-level
  semantics is made anywhere in the UI.
- English tokenization only (the pipeline's language gate already means
  only `en` rows carry verdicts; skipped rows never enter the topic
  denominator, §23).
- One comment may belong to several topics (themes are not a partition of
  the dataset); every per-topic metric uses its own explicit denominator.

Safety (same contract as sentiment.py):
- No raw comment text is ever logged; only ids, counts, timings, and
  exception class names.
- Topic failure never fails sentiment: the job worker catches and still
  completes (§39), and the API answers INSUFFICIENT_DATA / empty topics
  instead of fabricating themes (§38).
"""
import math
import re
import sqlite3
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, List, Optional, Sequence, Tuple

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.repository import DatasetRepository
from app.models.internal import (
    EmotionBreakdown,
    IntensityBreakdown,
    LabelShare,
    TopicAnalysisResponse,
    TopicItem,
)
from app.models.processing import ProcessingStatus
from app.services.sentiment import (
    EMOTION_PRIORITY,
    INTENSITY_ORDER,
    SentimentLabel,
    dominant_label,
    dominant_sentiment,
    label_distribution,
)

# ---------------------------------------------------------------------------
# Text quality filters (§22)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z]+")
# URLs and @usernames are never discussion themes (§22: no URLs, no
# usernames). Stripped BEFORE tokenization so their fragments can never
# become phrase candidates.
_URL_RE = re.compile(r"(?:https?://|www\.)\S+")
_MENTION_RE = re.compile(r"@\w+")

# English stopwords (function words only - content words stay). The list is
# deliberately conservative: splitting segments here is what makes
# "this roadmap is really helpful" yield [roadmap] + [helpful] instead of
# one stopword-riddled run.
STOPWORDS = frozenset(
    """
    a about above after again against all am an and any are as at
    be because been before being below between both but by
    can cannot could
    did do does doing don down during
    each few for from further
    had has have having he her here hers herself him himself his how
    i if in into is it its itself
    just
    let like
    me more most must my myself
    no nor not now
    of off on once only or other our ours ourselves out over own
    same she should so some such
    than that the their theirs them themselves then there these they this those
    through to too under until up
    very
    was we were what when where which while who whom why will with
    you your yours yourself yourselves
    dont isnt doesnt didnt wont cant wouldnt couldnt shouldnt thats whats
    theres theyre youre im ive ill
    also really much many one two way thing things still even well
    back go going went come came make made use used want need try keep
    put say said tell told ask
    get got
    """
    .split()
)

# Bare words that never identify a topic BY THEMSELVES (§22: generic words
# like "video", "good", "nice" unless contextually meaningful). A phrase is
# dropped only when EVERY token is generic, so "video quality" survives
# (quality is not generic) while "good video" does not.
GENERIC_TERMS = frozenset(
    """
    video videos channel channels youtube youtu http https www com org net
    html href
    comment comments reply replies subscribe subscribed subscription
    like likes loved
    good great nice bad worst best amazing awesome excellent perfect
    wonderful fantastic enjoyed enjoy love cool fine okay ok yeah
    thing things stuff
    bro dude guys ppl pls plz
    watch watched watching see
    """
    .split()
)

# Token-length bounds: >= 2 drops single-letter fragments from split
# contractions ("don't" -> don + t); <= 14 drops long runs that are URLs,
# ids or keyboard mashing, never real topic words.
_MIN_TOKEN_LEN = 2
_MAX_TOKEN_LEN = 14

# ---------------------------------------------------------------------------
# Discovery constants (all documented, all testable)
# ---------------------------------------------------------------------------

# A phrase must appear in at least this many comments to be a clustering
# candidate. Lower than TOPIC_MIN_SUPPORT on purpose: small phrases merge
# into themes that then clear the support gate.
_MIN_CANDIDATE_DF = 3
# Candidates considered for clustering (ranked by df * idf). Bounds the
# O(phrases x topics) clustering cost on 5k-comment datasets.
_MAX_CANDIDATES = 400
# Token-Jaccard threshold for "these two phrasings express the same idea"
# (§7). 0.6 accepts "learning roadmap" vs "clear learning roadmap" (2/3)
# and unigram-in-phrase absorption only via an exact-member match
# ("roadmap" vs "learning roadmap" is 1/3 -> separate), which keeps
# sibling themes ("react projects" vs "react fundamentals", 1/4) apart.
_MERGE_JACCARD = 0.6
# Second pass: merge two clusters when the overlap covers >= 70% of the
# LARGER cluster's comments - i.e. both clusters are ~the same comment
# set (bounded by the smaller, so this implies >= 70% of the smaller
# too). Requires >= _MIN_CANDIDATE_DF overlapping comments so two tiny
# clusters never fuse on 3 shared rows. Measuring against the LARGER set
# is deliberate: a hub phrase ("react") whose comments span two distinct
# themes shares 100% of each small theme's comments but only ~50% of its
# own, so unrelated themes stay apart.
_MERGE_DOC_OVERLAP = 0.7
_MERGE_MIN_SHARED_DOCS = _MIN_CANDIDATE_DF

# Category rules (percentage POINTS of the topic's own mention count).
# Mutually exclusive by construction:
#   APPRECIATED: pos >= 60           (neg then can never reach 40)
#   PAIN_POINT:  neg >= 40
#   MIXED:       pos < 60 and neg < 40 and pos >= 20 and neg >= 20
#   MOST_DISCUSSED otherwise (a recurring theme without a strong lean).
_APPRECIATED_MIN_POSITIVE_PERCENT = 60.0
_PAIN_MIN_NEGATIVE_PERCENT = 40.0
_MIXED_MIN_SIDE_PERCENT = 20.0

# Ranked-list sizes (§10/§11/§14/§15 examples show 3-4; 6 for frequency).
_MOST_DISCUSSED_TOP = 6
_APPRECIATED_TOP = 4
_PAIN_TOP = 4
_MIXED_TOP = 4

# PreferenceScore / PainScore (§19): frequency (normalized within the
# candidate set) weighted 0.6 over the sentiment RATIO (already in [0,1])
# weighted 0.4. Minimum support is enforced as a GATE, not a weight -
# a 5-mention 100%-positive topic can never enter the list at all (§11).
# Scores are internal ordering only and NEVER exposed to users (§19).
_FREQUENCY_WEIGHT = 0.6
_RATIO_WEIGHT = 0.4

# Topic confidence (§21): 0.5 * support (reaches 1.0 at 3x
# TOPIC_MIN_SUPPORT mentions) + 0.5 * cluster cohesion (mean pairwise
# token-Jaccard of member phrases; 1.0 for a single-phrase cluster - no
# conflicting phrasing was ever assigned). Deterministic and documented;
# explicitly NOT the sentiment model's decision margin (different signal,
# different name in the UI).
_CONFIDENCE_SUPPORT_WEIGHT = 0.5
_CONFIDENCE_COHESION_WEIGHT = 0.5
_CONFIDENCE_FULL_SUPPORT_MULTIPLE = 3

# Evidence copy (§20/§29): factual summaries of the computed numbers, not
# objective claims about the video.
_EVIDENCE = {
    "APPRECIATED": "Frequently praised: {pos}% positive across {mentions} mentions",
    "PAIN_POINT": "Repeatedly discussed negative theme: {neg}% negative across {mentions} mentions",
    "MIXED": "Mixed discussion: {pos}% positive, {neg}% negative across {mentions} mentions",
    "MOST_DISCUSSED": "Recurring discussion: {mentions} mentions ({share}% of analyzed comments)",
}

_INSUFFICIENT_MESSAGE = "More audience responses are needed to identify repeated themes."
_EMPTY_MESSAGE = "Not enough repeated discussion yet."

# How many key phrases are exposed per topic (§13: the idea, not a raw
# comment list).
_KEY_PHRASES_TOP = 4


logger = get_logger("topics")


# ---------------------------------------------------------------------------
# Pure engine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnalyzedComment:
    """One polarity-analyzed comment - the ONLY topic input.

    Verdicts are passed through exactly as stored on the row: the engine
    aggregates, never re-infers (§17/§18).
    """

    text: str
    sentiment: str
    emotion: Optional[str]
    intensity: Optional[str]


@dataclass
class _Cluster:
    """Greedy cluster accumulator (phrase -> comments)."""

    members: List[Tuple[str, frozenset, int, float]] = field(default_factory=list)
    # (phrase, token set, df, df*idf score)
    docs: set = field(default_factory=set)
    label: str = ""
    topic_id: str = ""


def _segments(text: str) -> List[List[str]]:
    """Content-token segments of one comment (split at filters)."""
    cleaned = _MENTION_RE.sub(" ", _URL_RE.sub(" ", (text or "").lower()))
    segments: List[List[str]] = []
    current: List[str] = []
    for token in _TOKEN_RE.findall(cleaned):
        if (
            len(token) < _MIN_TOKEN_LEN
            or len(token) > _MAX_TOKEN_LEN
            or token in STOPWORDS
        ):
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _phrases(text: str) -> set:
    """Candidate phrases of one comment (deduplicated within the comment).

    n-grams of length 1..3 inside content segments. All-generic phrases
    (every token in GENERIC_TERMS) are dropped here - a bare "good" or
    "video" is never a theme (§22).
    """
    out = set()
    for segment in _segments(text):
        length = len(segment)
        for n in (1, 2, 3):
            if n > length:
                break
            for start in range(length - n + 1):
                phrase = " ".join(segment[start:start + n])
                if all(token in GENERIC_TERMS for token in segment[start:start + n]):
                    continue
                out.add(phrase)
    return out


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / (len(a) + len(b) - inter)


def _phrase_score(df: int, total: int) -> float:
    """df * idf with idf = ln(total / df) + 1 (never zero, so a phrase in
    every comment still ranks by sheer frequency)."""
    return df * (math.log(total / df) + 1.0)


def _slug(phrase: str) -> str:
    return "-".join(phrase.split())


def _cluster_tokens(cluster: _Cluster) -> set:
    """Union of every content token in the cluster's member phrases."""
    tokens: set = set()
    for member in cluster.members:
        tokens.update(member[1])
    return tokens


def discover_topics(
    comments: Sequence[AnalyzedComment],
    min_support: int,
    max_topics: int,
) -> List[Dict[str, object]]:
    """Discover discussion themes from analyzed comments (pure, §6).

    Returns topic result dicts sorted by mentions (desc, label asc) - at
    most `max_topics`, each with >= `min_support` mentions. Every dict
    carries raw counts; percentages are computed against `len(comments)`
    (the analyzed denominator) by the caller.
    """
    total = len(comments)
    if total == 0:
        return []

    # ------------------------------------------------ df over comments
    df: Dict[str, int] = {}
    docs: Dict[str, set] = {}
    for index, comment in enumerate(comments):
        for phrase in _phrases(comment.text):
            df[phrase] = df.get(phrase, 0) + 1
            docs.setdefault(phrase, set()).add(index)

    # ------------------------------------------------ candidate ranking
    scored = sorted(
        (
            (_phrase_score(count, total), phrase)
            for phrase, count in df.items()
            if count >= _MIN_CANDIDATE_DF
        ),
        key=lambda item: (-item[0], item[1]),
    )[:_MAX_CANDIDATES]

    # ------------------------------------------------ greedy clustering
    clusters: List[_Cluster] = []
    for score, phrase in scored:
        tokens = frozenset(phrase.split())
        phrase_df = df[phrase]
        best_index = -1
        best_sim = 0.0
        for index, cluster in enumerate(clusters):
            for _, member_tokens, _, _ in cluster.members:
                sim = _jaccard(tokens, member_tokens)
                if sim > best_sim:
                    best_sim = sim
                    best_index = index
                    if sim >= 1.0:
                        break
            if best_sim >= 1.0:
                break
        if best_index >= 0 and best_sim >= _MERGE_JACCARD:
            target = clusters[best_index]
        else:
            target = _Cluster()
            clusters.append(target)
        target.members.append((phrase, tokens, phrase_df, score))
        target.docs.update(docs[phrase])

    # ------------------------------------ co-discussion merge pass (§7)
    # Size-ratio pre-filter: shared <= min, so a pair that cannot reach
    # _MERGE_DOC_OVERLAP of the larger set is skipped without touching
    # the doc sets (keeps the O(clusters^2) pass cheap at 5k comments).
    merged = True
    while merged:
        merged = False
        for left in range(len(clusters)):
            for right in range(left + 1, len(clusters)):
                a, b = clusters[left], clusters[right]
                big = max(len(a.docs), len(b.docs))
                small = min(len(a.docs), len(b.docs))
                if big <= 0 or small / big < _MERGE_DOC_OVERLAP:
                    continue
                shared = len(a.docs & b.docs)
                if shared < _MERGE_MIN_SHARED_DOCS:
                    continue
                if shared / big < _MERGE_DOC_OVERLAP:
                    continue
                a.members.extend(b.members)
                a.docs.update(b.docs)
                clusters.pop(right)
                merged = True
                break
            if merged:
                break

    # ------------------------------- duplicate-topic absorption (§22)
    # A cluster whose comments are fully contained in another cluster's
    # AND that shares >= 1 content word with it is the same idea expressed
    # less specifically ("Clear" inside "Clear learning roadmap"). Fold it
    # in so the UI never shows near-duplicate topics. The containment
    # requirement is what keeps a hub word from absorbing themes whose
    # comments merely happen to include it (docs must be a subset).
    absorbed = True
    while absorbed:
        absorbed = False
        for index, a in enumerate(clusters):
            tokens_a = _cluster_tokens(a)
            for b in clusters:
                if b is a:
                    continue
                if not a.docs <= b.docs:
                    continue
                if tokens_a & _cluster_tokens(b):
                    b.members.extend(a.members)
                    b.docs.update(a.docs)
                    clusters.pop(index)
                    absorbed = True
                    break
            if absorbed:
                break

    # ------------------------------------------------ labels (§8)
    for cluster in clusters:
        support = len(cluster.docs)
        # Label band: phrases carrying at least half of the cluster's own
        # comments are label candidates; among them prefer the most
        # specific (most tokens), then the highest score, then the
        # lexicographically smallest phrase - fully deterministic.
        eligible = [
            member for member in cluster.members if member[2] >= 0.5 * support
        ]
        pool = eligible or cluster.members
        best = sorted(
            pool, key=lambda member: (-len(member[1]), -member[3], member[0])
        )[0]
        cluster.label = best[0][0].upper() + best[0][1:]
        cluster.topic_id = _slug(best[0])

    # ------------------------------------------------ gates + ordering
    kept = [c for c in clusters if len(c.docs) >= min_support]
    kept.sort(key=lambda c: (-len(c.docs), c.label))
    kept = kept[:max_topics]

    # Deterministic unique ids even if two labels slug identically.
    seen: Dict[str, int] = {}
    results: List[Dict[str, object]] = []
    for cluster in kept:
        topic_id = cluster.topic_id
        if topic_id in seen:
            seen[topic_id] += 1
            topic_id = f"{topic_id}-{seen[topic_id]}"
        else:
            seen[topic_id] = 1
        results.append(_metrics(cluster, topic_id, comments, total, min_support))
    return results


def _metrics(
    cluster: _Cluster,
    topic_id: str,
    comments: Sequence[AnalyzedComment],
    total: int,
    min_support: int,
) -> Dict[str, object]:
    """Aggregate stored verdicts over the cluster's comments (§9/§16-18)."""
    mentions = len(cluster.docs)
    sentiment_counts = {"POSITIVE": 0, "NEUTRAL": 0, "NEGATIVE": 0}
    emotion_counts: Dict[str, int] = {label: 0 for label in EMOTION_PRIORITY}
    intensity_counts: Dict[str, int] = {label: 0 for label in INTENSITY_ORDER}
    for index in cluster.docs:
        comment = comments[index]
        if comment.sentiment in sentiment_counts:
            sentiment_counts[comment.sentiment] += 1
        if comment.emotion is not None:
            emotion_counts[comment.emotion] = emotion_counts.get(comment.emotion, 0) + 1
        if comment.intensity is not None:
            intensity_counts[comment.intensity] = intensity_counts.get(comment.intensity, 0) + 1

    positive = sentiment_counts["POSITIVE"]
    neutral = sentiment_counts["NEUTRAL"]
    negative = sentiment_counts["NEGATIVE"]
    pos_percent = positive * 100.0 / mentions
    neg_percent = negative * 100.0 / mentions

    # ---------------------------------------------- category (§11/14/15)
    if pos_percent >= _APPRECIATED_MIN_POSITIVE_PERCENT:
        category = "APPRECIATED"
    elif neg_percent >= _PAIN_MIN_NEGATIVE_PERCENT:
        category = "PAIN_POINT"
    elif (
        pos_percent >= _MIXED_MIN_SIDE_PERCENT
        and neg_percent >= _MIXED_MIN_SIDE_PERCENT
    ):
        category = "MIXED"
    else:
        category = "MOST_DISCUSSED"

    share = round(mentions * 100.0 / total, 1)
    evidence = _EVIDENCE[category].format(
        pos=round(pos_percent),
        neg=round(neg_percent),
        mentions=mentions,
        share=share,
    )

    # Key phrases (§13): the ideas behind the theme, highest df first,
    # label guaranteed first - never raw comments or usernames.
    key_phrases: List[str] = [cluster.label]
    for member in sorted(cluster.members, key=lambda m: (-m[2], m[0])):
        rendered = member[0][0].upper() + member[0][1:]
        if rendered not in key_phrases:
            key_phrases.append(rendered)
        if len(key_phrases) >= _KEY_PHRASES_TOP:
            break

    # ---------------------------------------------- confidence (§21)
    members = [member[1] for member in cluster.members]
    if len(members) > 1:
        pairs = [
            _jaccard(members[i], members[j])
            for i in range(len(members))
            for j in range(i + 1, len(members))
        ]
        cohesion = sum(pairs) / len(pairs)
    else:
        cohesion = 1.0
    support_norm = min(
        1.0, mentions / (_CONFIDENCE_FULL_SUPPORT_MULTIPLE * min_support)
    )
    confidence = round(
        _CONFIDENCE_SUPPORT_WEIGHT * support_norm
        + _CONFIDENCE_COHESION_WEIGHT * cohesion,
        2,
    )

    return {
        "topic_id": topic_id,
        "label": cluster.label,
        "mentions": mentions,
        "share_percent": share,
        "key_phrases": key_phrases,
        "category": category,
        "confidence": confidence,
        "evidence": evidence,
        "sentiment_counts": sentiment_counts,
        "dominant_sentiment": dominant_sentiment(positive, neutral, negative),
        "emotion_counts": emotion_counts,
        "intensity_counts": intensity_counts,
    }

# ---------------------------------------------------------------------------
# Ranked sections (§10/§11/§14/§15 + documented scores §19)
# ---------------------------------------------------------------------------


def _split_sections(
    topics: List[Dict[str, object]], min_support: int
) -> Dict[str, List[Dict[str, object]]]:
    """Most Discussed / Appreciated / Pain Points / Mixed views (§10-15).

    Ordering rules (deterministic, documented):
    - most_discussed: mentions desc, label asc.
    - most_appreciated: PreferenceScore = 0.6 * freq_norm + 0.4 *
      positive_ratio, gated by positive mentions >= TOPIC_MIN_SUPPORT;
      ties -> more positive mentions, then label asc.
    - pain_points: PainScore with the negative axis, gated the same way.
    - mixed_topics: mentions desc.
    Frequencies are normalized within each candidate set (the set's own
    maximum), so the score stays in [0, 1] regardless of dataset size.
    """
    by_mentions = sorted(
        topics, key=lambda t: (-int(t["mentions"]), str(t["label"]))
    )
    discussed = by_mentions[:_MOST_DISCUSSED_TOP]

    appreciated = [
        t
        for t in topics
        if t["category"] == "APPRECIATED"
        and int(t["sentiment_counts"]["POSITIVE"]) >= min_support  # type: ignore[index]
    ]

    def pref_key(t: Dict[str, object]):
        counts = t["sentiment_counts"]  # type: ignore[assignment]
        mentions = int(t["mentions"])
        pos = int(counts["POSITIVE"])  # type: ignore[index]
        freq_norm = pos / max(int(x["sentiment_counts"]["POSITIVE"]) for x in appreciated)  # type: ignore[index]
        score = _FREQUENCY_WEIGHT * freq_norm + _RATIO_WEIGHT * (pos / mentions)
        return (-score, -pos, str(t["label"]))

    appreciated = sorted(appreciated, key=pref_key)[:_APPRECIATED_TOP]

    pain = [
        t
        for t in topics
        if t["category"] == "PAIN_POINT"
        and int(t["sentiment_counts"]["NEGATIVE"]) >= min_support  # type: ignore[index]
    ]

    def pain_key(t: Dict[str, object]):
        counts = t["sentiment_counts"]  # type: ignore[assignment]
        mentions = int(t["mentions"])
        neg = int(counts["NEGATIVE"])  # type: ignore[index]
        freq_norm = neg / max(int(x["sentiment_counts"]["NEGATIVE"]) for x in pain)  # type: ignore[index]
        score = _FREQUENCY_WEIGHT * freq_norm + _RATIO_WEIGHT * (neg / mentions)
        return (-score, -neg, str(t["label"]))

    pain = sorted(pain, key=pain_key)[:_PAIN_TOP]

    mixed = [t for t in topics if t["category"] == "MIXED"][:_MIXED_TOP]

    return {
        "most_discussed": discussed,
        "most_appreciated": appreciated,
        "pain_points": pain,
        "mixed_topics": mixed,
    }


# ---------------------------------------------------------------------------
# Service (repository orchestration + memo)
# ---------------------------------------------------------------------------


class TopicService:
    """Compute-on-read topic intelligence for one video dataset.

    Sprint 6 persistence decision (§24): NO new tables. Topic discovery is
    a pure function over rows the pipeline already stores, measured in the
    benchmark to run well under a second at the 5000-comment scale, so it
    is computed on read instead of persisted - which also makes active-
    video safety structural: the read only ever sees the active video's
    rows, and a video switch (rows cascaded away) can never leave stale
    topic state behind (§40). A one-entry memo keyed by
    (video_id, analyzed, latest verdict timestamp) makes repeated GETs and
    the job worker's warm-up pass free while any analysis change
    invalidates it.
    """

    def __init__(self, repository: DatasetRepository, settings: Settings) -> None:
        self._repo = repository
        self._settings = settings
        self._lock = Lock()
        self._memo: Optional[Tuple[Tuple[object, ...], TopicAnalysisResponse]] = None

    def get_analysis(self, video_id: str) -> Optional[TopicAnalysisResponse]:
        """Topic intelligence for one video; None when untracked (404)."""
        video_row = self._repo.get_video(video_id)
        if video_row is None:
            return None

        analyzed, latest = self._repo.get_analysis_fingerprint(video_id)
        key = (video_id, analyzed, latest)
        with self._lock:
            if self._memo is not None and self._memo[0] == key:
                return self._memo[1]

        started = time.monotonic()
        if analyzed < self._settings.topic_min_comment_count:
            response = TopicAnalysisResponse(
                video_id=video_id,
                status="INSUFFICIENT_DATA",
                analyzed_comments=analyzed,
                message=_INSUFFICIENT_MESSAGE,
            )
        else:
            comments = self._load(video_id)
            raw_topics = discover_topics(
                comments,
                min_support=self._settings.topic_min_support,
                max_topics=self._settings.topic_max_topics,
            )
            response = self._build(video_id, analyzed, raw_topics)

        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "TOPIC_DISCOVERY_COMPLETED",
            extra={
                "video_id": video_id,
                "analyzed": analyzed,
                "topics": len(response.topics),
                "elapsed_ms": elapsed_ms,
            },
        )
        with self._lock:
            self._memo = (key, response)
        return response

    # ------------------------------------------------------------- internals
    def _load(self, video_id: str) -> List[AnalyzedComment]:
        """Stream analyzed rows through the keyset reader (bounded batches).

        Only rows carrying a real polarity verdict enter (§23): skipped
        UNSUPPORTED_LANGUAGE rows and failed rows are PROCESSED/FAILED
        without a verdict and are never counted in any denominator.
        """
        comments: List[AnalyzedComment] = []
        batch = self._settings.comment_batch_size
        for row in self._repo.iter_comments(
            video_id, batch, status=ProcessingStatus.PROCESSED.value
        ):
            label = row["sentiment_label"]
            if label not in (
                SentimentLabel.POSITIVE.value,
                SentimentLabel.NEUTRAL.value,
                SentimentLabel.NEGATIVE.value,
            ):
                continue
            comments.append(
                AnalyzedComment(
                    text=row["normalized_text"],
                    sentiment=label,
                    emotion=row["emotion_label"],
                    intensity=row["sentiment_intensity"],
                )
            )
        return comments

    def _build(
        self,
        video_id: str,
        analyzed: int,
        raw_topics: List[Dict[str, object]],
    ) -> TopicAnalysisResponse:
        """Typed response: Pydantic items + ranked sections (§28)."""
        items = [self._item(raw) for raw in raw_topics]
        # Re-map raw dicts for section selection on the typed items.
        sections = _split_sections(raw_topics, self._settings.topic_min_support)
        by_id = {item.topic_id: item for item in items}

        def typed(keys: List[Dict[str, object]]) -> List[TopicItem]:
            return [by_id[str(raw["topic_id"])] for raw in keys]

        return TopicAnalysisResponse(
            video_id=video_id,
            status="READY",
            analyzed_comments=analyzed,
            message=None if items else _EMPTY_MESSAGE,
            topics=items,
            most_discussed=typed(sections["most_discussed"]),
            most_appreciated=typed(sections["most_appreciated"]),
            pain_points=typed(sections["pain_points"]),
            mixed_topics=typed(sections["mixed_topics"]),
        )

    @staticmethod
    def _item(raw: Dict[str, object]) -> TopicItem:
        sentiment_counts: Dict[str, int] = raw["sentiment_counts"]  # type: ignore[assignment]
        emotion_counts: Dict[str, int] = raw["emotion_counts"]  # type: ignore[assignment]
        intensity_counts: Dict[str, int] = raw["intensity_counts"]  # type: ignore[assignment]
        sentiment_pct = label_distribution(
            sentiment_counts,
            (SentimentLabel.POSITIVE.value, SentimentLabel.NEUTRAL.value,
             SentimentLabel.NEGATIVE.value),
        )
        emotion_pct = label_distribution(emotion_counts, EMOTION_PRIORITY)
        intensity_pct = label_distribution(intensity_counts, INTENSITY_ORDER)
        dominant_emotion = dominant_label(emotion_counts, EMOTION_PRIORITY)
        dominant_intensity = dominant_label(intensity_counts, INTENSITY_ORDER)
        return TopicItem(
            topic_id=str(raw["topic_id"]),
            label=str(raw["label"]),
            mentions=int(raw["mentions"]),
            share_percent=float(raw["share_percent"]),
            key_phrases=list(raw["key_phrases"]),  # type: ignore[arg-type]
            category=str(raw["category"]),  # type: ignore[arg-type]
            confidence=float(raw["confidence"]),
            evidence=str(raw["evidence"]),
            sentiment={
                label: LabelShare(
                    count=sentiment_counts.get(label, 0),
                    percent=sentiment_pct[label],
                )
                for label in (
                    SentimentLabel.POSITIVE.value,
                    SentimentLabel.NEUTRAL.value,
                    SentimentLabel.NEGATIVE.value,
                )
            },
            dominant_sentiment=raw["dominant_sentiment"],  # type: ignore[arg-type]
            emotion=EmotionBreakdown(
                dominant=dominant_emotion,  # type: ignore[arg-type]
                dominant_percent=(
                    emotion_pct.get(dominant_emotion, 0.0)
                    if dominant_emotion is not None
                    else 0.0
                ),
                distribution={
                    label: LabelShare(
                        count=emotion_counts.get(label, 0),
                        percent=emotion_pct[label],
                    )
                    for label in EMOTION_PRIORITY
                },
            ),
            intensity=IntensityBreakdown(
                overall=dominant_intensity,  # type: ignore[arg-type]
                distribution={
                    label: LabelShare(
                        count=intensity_counts.get(label, 0),
                        percent=intensity_pct[label],
                    )
                    for label in INTENSITY_ORDER
                },
            ),
        )

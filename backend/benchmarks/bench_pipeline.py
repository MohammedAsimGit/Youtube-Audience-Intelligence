"""Repeatable performance benchmark (Sprint 5.1 §23).

Measures the REAL pipeline (repository, ingestion, sentiment engine, job
worker) over deterministic synthetic corpora. Test data is clearly synthetic
and is never presented as real YouTube data (Sprint 2 mock-data policy).

Run from backend/:

    ./.venv/Scripts/python benchmarks/bench_pipeline.py stages
    ./.venv/Scripts/python benchmarks/bench_pipeline.py micro
    ./.venv/Scripts/python benchmarks/bench_pipeline.py e2e --api-latency-ms 300
    ./.venv/Scripts/python benchmarks/bench_pipeline.py memory

Modes
-----
stages   Per-size stage breakdown: acquisition (validate/normalize/langdetect/
         DB persist), analysis (VADER/emotion inference, claim/save DB),
         aggregation read path. Reports first-insight contribution per stage.
micro    Per-comment costs (VADER / emotion / langdetect / NRCLex ctor) and
         the analysis batch-size sweep (32..512) used to pick
         SENTIMENT_INFERENCE_BATCH_SIZE (§4). In baseline runs the only
         analysis batch knob is COMMENT_BATCH_SIZE.
e2e      Full background job end-to-end (acquire -> analyze -> terminal) with
         optional simulated per-page API latency (--api-latency-ms). Records
         time-to-first-insight (first poll reporting analyzed > 0) and total.
memory   tracemalloc peak per size (separate pass; never mixed with timings).

All timings use time.perf_counter. Percentages/throughput are computed from
measured wall time only - nothing is extrapolated.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import tracemalloc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.conftest import (  # noqa: E402  (path set above)
    FakeYouTubeClient,
    make_settings,
    thread_payload,
    video_payload,
)

from app.core.logging import configure_logging  # noqa: E402
from app.db.connection import Database  # noqa: E402
from app.db.repository import DatasetRepository  # noqa: E402
from app.services.acquisition import VideoDataService  # noqa: E402
from app.services.cache import TTLCache  # noqa: E402
from app.services.dataset import DatasetService  # noqa: E402
from app.services.ingestion import IngestionService  # noqa: E402
from app.services.jobs import AnalysisJobService  # noqa: E402
from app.services.sentiment import SentimentService  # noqa: E402

VIDEO_ID = "benchvid01"
DEFAULT_SIZES = (100, 500, 1000, 2500, 5000)
INFERENCE_BATCH_SIZES = (32, 64, 128, 256, 512)
SEED = 42

# --------------------------------------------------------------------------
# Deterministic synthetic corpus
# --------------------------------------------------------------------------

_POS_TEMPLATES = [
    "Absolutely loved this video, {w} editing and the pacing were fantastic!",
    "Great work as always, {w} content that made my day better.",
    "This is exactly what I needed today - {w} and honestly inspiring.",
    "Best channel on YouTube, {w} every single upload.",
    "I smiled the whole time, {w} job keeping this going!",
    "Amazing explanation, {w} clear and easy to follow. Thank you!",
]
_NEG_TEMPLATES = [
    "Terrible take, {w} video that completely wasted my time.",
    "I disagree with almost everything here, {w} reasoning overall.",
    "This was painful to watch, {w} editing and unclear points.",
    "Worst upload this month, {w} effort went into this.",
    "Honestly disappointed, {w} handling of the topic.",
    "Nobody asked for this, {w} direction for the channel.",
]
_NEU_TEMPLATES = [
    "Interesting video, {w} the topic and the comments too.",
    "Just another upload, {w} nothing unusual here.",
    "Watched it twice, {w} thoughts after the second viewing.",
    "Okay I guess, {w} video for a Tuesday afternoon.",
    "Not sure what to say, {w} feelings about this one.",
    "The second half was {w} compared with the start.",
]
_LONG_TEMPLATES = [
    "Long form comment here: I have been following this channel for years "
    "and the quality keeps changing. Some uploads feel rushed while others "
    "are incredibly detailed. {w} overall, but I understand the schedule "
    "pressure and I will keep watching anyway. What matters is that the "
    "discussion in the replies is usually smarter than the video itself, "
    "and that is {w} for a community of this size. Curious where the next "
    "series goes from here.",
    "I want to add a bit more context because the video skimmed over it. "
    "The underlying data supports a {w} interpretation if you look at the "
    "trend rather than one week of numbers. I pulled the public dataset "
    "myself last night and the shape matches what was described, although "
    "the scaling is {w} at the edges. Would love a follow-up video that "
    "walks through the methodology step by step with the raw table on "
    "screen so viewers can verify it themselves.",
]
_POS_WORDS = ["smooth", "brilliant", "perfect", "sharp", "delightful"]
_NEG_WORDS = ["rough", "weak", "boring", "sloppy", "confusing"]
_NEU_WORDS = ["mixed", "steady", "usual", "plain", "normal"]
_FOREIGN = [
    "Este video es increíble, me encantó la explicación completa.",
    "यह वीडियो बहुत अच्छा है, धन्यवाद इसे साझा करने के लिए",
    "Cette chaîne est toujours excellente, bonne continuation !",
    "هذا المقطع رائع جداً شكراً لك على هذا المحتوى",
    " video genial me encanto la edicion del final",
]
_SHORT_REPEATS = [
    "Nice!", "lol", "First!", "Same here", "Great video!", "Wow",
    "Thanks for sharing", "so true", "underrated", "Bookmarking this",
]


_UNIQUE_TAIL = [
    "still thinking about the part where", "not sure if anyone else noticed",
    "the way this ended reminded me of", "came back to rewatch and",
    "my honest reaction after the second watch was", "comparing this to the last upload",
    "the comments under this one are wild because", "whoever edited this deserves credit for",
    "i sent this to three friends and", "small detail but",
]
# Vocabulary pools for the unique tail (§23: representative uniqueness).
_FILLER = [
    "video", "editing", "channel", "comment", "community", "upload",
    "tutorial", "moment", "part", "ending", "intro", "series", "take",
    "point", "discussion", "thread", "reply", "section", "story",
    "format", "style", "audio", "thumbnail", "title",
]
_CONNECTORS = [
    "because", "although", "honestly", "especially", "overall", "still",
    "maybe", "probably", "actually", "then", "so", "and", "but", "just",
    "when", "while", "after", "before", "since", "though",
]
_UNIQUE_POOL = _POS_WORDS + _NEG_WORDS + _NEU_WORDS + _FILLER + _CONNECTORS
_UNIQUENESS_WORDS = _UNIQUE_POOL


def _unique_tail(rng: random.Random) -> str:
    """3-6 vocabulary draws (~90^4+ combinations) appended to template
    comments so that, like real comment sections, almost every comment's
    text is UNIQUE. Without this the template-only corpus repeated ~90% of
    its texts and the exact-text language cache would show an unrealistically
    large win (§23: measurements must be representative)."""
    words = [rng.choice(_UNIQUENESS_WORDS) for _ in range(rng.randint(3, 6))]
    return rng.choice(_UNIQUE_TAIL) + " " + " ".join(words)


def build_corpus(n: int) -> List[Tuple[str, str]]:
    """Deterministic (comment_id, text) list modeled on a real comment
    section: ~86% English (positive/neutral/negative mix, almost all unique
    via template slots + a unique tail), ~6% exact short repeats (the honest
    cacheable share - real sections DO repeat "lol"/"Nice!"), ~4% non-English
    (exercises the language gate), ~4% long-form (300-700 chars)."""
    rng = random.Random(SEED + n)
    out: List[Tuple[str, str]] = []
    for i in range(n):
        roll = rng.random()
        if roll < 0.04:
            text = rng.choice(_FOREIGN)
        elif roll < 0.10:
            text = rng.choice(_SHORT_REPEATS)
        elif roll < 0.14:
            text = (
                rng.choice(_LONG_TEMPLATES).format(w=rng.choice(_POS_WORDS))
                + " "
                + _unique_tail(rng)
            )
        else:
            kind = rng.random()
            if kind < 0.36:
                text = rng.choice(_POS_TEMPLATES).format(w=rng.choice(_POS_WORDS))
            elif kind < 0.62:
                text = rng.choice(_NEG_TEMPLATES).format(w=rng.choice(_NEG_WORDS))
            else:
                text = rng.choice(_NEU_TEMPLATES).format(w=rng.choice(_NEU_WORDS))
            text = text + " " + _unique_tail(rng)
            if rng.random() < 0.25:
                text += " " + rng.choice(["🔥", "😂", "🤔", "👏", "💯", ""])
        out.append((f"bench{i:06d}", text))
    return out


def build_pages(corpus: Sequence[Tuple[str, str]], page_size: int):
    """FakeYouTubeClient `pages` dict: token chain over comment_threads items.
    The final page carries no next token (like real YouTube), so no extra
    empty fetch is ever made."""
    pages: dict = {}
    token: Optional[str] = None
    keys: List[Optional[str]] = []
    for start in range(0, len(corpus), page_size):
        chunk = corpus[start:start + page_size]
        items = [thread_payload(cid, text) for cid, text in chunk]
        next_token = f"tok{start // page_size + 1}"
        pages[token] = (items, next_token)
        keys.append(token)
        token = next_token
    if keys:
        last_key = keys[-1]
        items, _ = pages[last_key]
        pages[last_key] = (items, None)
    return pages


# --------------------------------------------------------------------------
# Timing helpers
# --------------------------------------------------------------------------

class StageTimer:
    """Accumulates wall time per instrumented stage (calls, seconds)."""

    def __init__(self) -> None:
        self._data: Dict[str, List[float]] = {}

    def add(self, name: str, seconds: float) -> None:
        slot = self._data.setdefault(name, [0.0, 0.0])
        slot[0] += 1
        slot[1] += seconds

    def wrap(self, name: str, fn: Callable):
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                self.add(name, time.perf_counter() - t0)

        return wrapper

    def get(self, name: str) -> Tuple[int, float]:
        calls, secs = self._data.get(name, (0.0, 0.0))
        return int(calls), secs

    def total(self, prefix: str) -> float:
        return sum(secs for name, (_, secs) in self._data.items() if name.startswith(prefix))

    def as_dict(self) -> Dict[str, dict]:
        return {name: {"calls": int(c[0]), "seconds": round(c[1], 6)}
                for name, c in sorted(self._data.items())}


# --------------------------------------------------------------------------
# Environment (mirrors app.main wiring with a fake YouTube client)
# --------------------------------------------------------------------------

class LatencyFakeYouTubeClient(FakeYouTubeClient):
    """Fake client with simulated per-request API latency (models YouTube
    round-trip; clearly labeled in results - network is never measured
    against the real API in this harness)."""

    def __init__(self, latency_seconds: float, **kwargs) -> None:
        super().__init__(**kwargs)
        self.latency_seconds = latency_seconds

    def get_comment_threads(self, video_id, max_results, page_token=None):
        if self.latency_seconds > 0:
            time.sleep(self.latency_seconds)
        return super().get_comment_threads(video_id, max_results, page_token)


@dataclass
class Env:
    settings: object
    db: Database
    repo: DatasetRepository
    ingestion: IngestionService
    dataset: DatasetService
    sentiment: SentimentService
    acquisition: VideoDataService
    jobs: AnalysisJobService
    client: FakeYouTubeClient
    timers: StageTimer = field(default_factory=StageTimer)


def build_env(
    corpus: Sequence[Tuple[str, str]],
    page_size: int = 100,
    comment_batch_size: int = 500,
    api_latency_seconds: float = 0.0,
) -> Env:
    settings = make_settings(
        max_comments_per_request=page_size,
        comment_acquisition_max_comments=5000,
        comment_batch_size=comment_batch_size,
        database_url="sqlite:///:memory:",
        log_level="WARNING",
    )
    from app.schemas.youtube import YtVideoListResponse
    kwargs = dict(
        video=YtVideoListResponse.model_validate(video_payload(VIDEO_ID)),
        pages=build_pages(corpus, page_size),
    )
    if api_latency_seconds > 0:
        client: FakeYouTubeClient = LatencyFakeYouTubeClient(api_latency_seconds, **kwargs)
    else:
        client = FakeYouTubeClient(**kwargs)

    db = Database(settings.database_url)
    repo = DatasetRepository(db)
    ingestion = IngestionService(repo, settings)
    dataset = DatasetService(repo, settings)
    sentiment = SentimentService(repo, settings)
    cache = TTLCache(ttl_seconds=settings.cache_ttl_seconds,
                     max_entries=settings.cache_max_entries)
    acquisition = VideoDataService(
        client=client, settings=settings, cache=cache,
        ingestion=ingestion, dataset=dataset,
    )
    jobs = AnalysisJobService(
        repository=repo, dataset=dataset, acquisition=acquisition,
        sentiment=sentiment, settings=settings, cache=cache,
    )
    return Env(
        settings=settings, db=db, repo=repo, ingestion=ingestion,
        dataset=dataset, sentiment=sentiment, acquisition=acquisition,
        jobs=jobs, client=client,
    )


def instrument(env: Env) -> StageTimer:
    """Wrap the real pipeline stages with wall-time accumulators."""
    t = env.timers

    # Repository DB work (writes + read-path queries), by method.
    from app.db import repository as repo_mod
    for name in (
        "upsert_video", "upsert_comments", "record_ingestion_run",
        "claim_for_processing", "save_sentiment_results",
        "update_processing_status",
    ):
        setattr(env.repo, name, t.wrap(f"db.write.{name}", getattr(env.repo, name)))
    for name in (
        "get_status_counts", "get_sentiment_counts", "get_emotion_counts",
        "get_intensity_counts", "get_average_sentiment_confidence",
        "get_latest_job", "get_active_video",
    ):
        setattr(env.repo, name, t.wrap(f"db.read.{name}", getattr(env.repo, name)))

    # Ingestion sub-stages (module-level imports inside ingestion.py).
    from app.services import ingestion as ing_mod
    ing_mod.detect_language = t.wrap("ingest.langdetect", ing_mod.detect_language)
    ing_mod.normalize_comment_text = t.wrap("ingest.normalize", ing_mod.normalize_comment_text)
    ing_mod.validate_comments = t.wrap("ingest.validate", ing_mod.validate_comments)

    # NLP inference (module-level globals resolved at call time in
    # SentimentService._process_batch).
    from app.services import sentiment as sent_mod
    sent_mod.classify = t.wrap("nlp.vader", sent_mod.classify)
    sent_mod.classify_emotion = t.wrap("nlp.emotion", sent_mod.classify_emotion)
    return t


def _ms(seconds: float) -> float:
    return round(seconds * 1000.0, 1)


def apply_baseline_mode() -> None:
    """Disable the three measured Sprint 5.1 optimizations in-process so
    BEFORE and AFTER runs execute the exact same corpus and harness (§23).

    1. §15 language cache      -> every comment runs the real detector;
    2. §3  emotion singleton   -> NRCLex constructed per comment (baseline
                                  constructor cost included);
    3. §6  interim analysis    -> `after_page` stripped, so sentiment runs
                                  only after acquisition completes (the
                                  original sequential phases).
    4. Sprint 5.2 fast VADER   -> stock SentimentIntensityAnalyzer (per-call
                                  lowered-word rebuilds restored);
    5. Sprint 5.2 direct NRC   -> stock nrclex load_token_list bookkeeping.
    The bounded fetch/ingest hand-off itself is structural and stays in both
    directions; without API latency it is serialization-equivalent anyway.
    """
    import app.services.language as language_mod
    from nrclex import NRCLex
    import app.services.sentiment as sentiment_mod
    from app.services.acquisition import VideoDataService
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

    language_mod._cache_put = lambda *_a, **_k: None
    sentiment_mod._get_emotion_engine = lambda: NRCLex()
    sentiment_mod._analyzer = SentimentIntensityAnalyzer()
    sentiment_mod._EMOTION_DIRECT = False
    original = VideoDataService.acquire_dataset

    def _sequential(self, *args, **kwargs):
        kwargs.pop("after_page", None)
        return original(self, *args, **kwargs)

    VideoDataService.acquire_dataset = _sequential  # type: ignore[assignment]
    print(
        "[baseline mode] optimizations DISABLED "
        "(langdetect cache, emotion singleton, interim analysis, "
        "fast VADER, direct emotion counting)",
        file=sys.stderr,
    )


# --------------------------------------------------------------------------
# stages mode
# --------------------------------------------------------------------------

def run_stages(size: int, page_size: int, comment_batch_size: int) -> dict:
    corpus = build_corpus(size)
    env = build_env(corpus, page_size=page_size, comment_batch_size=comment_batch_size)
    t = instrument(env)

    # ---------------- acquisition -------------------------------------
    first_save_offset: List[float] = []
    acq_t0 = time.perf_counter()
    activation = env.dataset.ensure_active(VIDEO_ID)
    page_marks: List[float] = []
    run = env.acquisition.acquire_dataset(
        VIDEO_ID, activation.generation,
        on_page=lambda pages, collected, has_more: page_marks.append(
            time.perf_counter() - acq_t0
        ),
    )
    acq_s = time.perf_counter() - acq_t0

    # ---------------- analysis ----------------------------------------
    orig_save = env.repo.save_sentiment_results
    an_t0 = time.perf_counter()

    def save_probe(*args, **kwargs):
        result = orig_save(*args, **kwargs)
        if not first_save_offset:
            first_save_offset.append(time.perf_counter() - an_t0)
        return result

    env.repo.save_sentiment_results = save_probe
    summary = env.sentiment.process_pending(VIDEO_ID, activation.generation)
    an_s = time.perf_counter() - an_t0

    # ---------------- aggregation read path ---------------------------
    agg_t0 = time.perf_counter()
    analysis = env.sentiment.get_analysis(VIDEO_ID)
    agg_s = time.perf_counter() - agg_t0
    status_t0 = time.perf_counter()
    env.jobs.get_status(VIDEO_ID)
    status_s = time.perf_counter() - status_t0

    first_batch_s = first_save_offset[0] if first_save_offset else an_s
    total_s = acq_s + an_s + agg_s

    analyzed = analysis.stats.analyzed if analysis else 0
    nlp_s = t.total("nlp.")
    db_write_s = t.total("db.write.")
    lang_s = t.get("ingest.langdetect")[1]

    env.jobs.shutdown(timeout=2.0)
    env.db.close()

    return {
        "size": size,
        "acquisition_s": round(acq_s, 3),
        "analysis_s": round(an_s, 3),
        "aggregation_s": round(agg_s, 4),
        "job_status_poll_s": round(status_s, 4),
        "total_s": round(total_s, 3),
        "first_page_persisted_s": round(page_marks[0], 3) if page_marks else None,
        "first_batch_saved_s": round(first_batch_s, 3),
        # Sequential baseline: first insight = full acquisition + first batch.
        "first_insight_sequential_s": round(acq_s + first_batch_s, 3),
        "comments_per_second": round((summary.processed + summary.failed + summary.skipped) / an_s, 1) if an_s else 0,
        "end_to_end_per_second": round(size / total_s, 1) if total_s else 0,
        "api_pages": run.outcome.pages_fetched,
        "api_calls": run.outcome.pages_fetched + 1,  # + videos.list
        "batch_size": comment_batch_size,
        "nlp_inference_s": round(nlp_s, 3),
        "vader_s": round(t.get("nlp.vader")[1], 3),
        "emotion_s": round(t.get("nlp.emotion")[1], 3),
        "langdetect_s": round(lang_s, 3),
        "db_write_s": round(db_write_s, 3),
        "counts": {
            "processed": summary.processed,
            "failed": summary.failed,
            "skipped": summary.skipped,
            "analyzed": analyzed,
        },
        "stages": t.as_dict(),
    }


# --------------------------------------------------------------------------
# micro mode
# --------------------------------------------------------------------------

def run_micro(n: int, batch_sizes: Sequence[int]) -> dict:
    from nrclex import NRCLex
    from app.services.language import detect_language
    from app.services.sentiment import (
        _get_analyzer, classify, classify_emotion,
    )

    corpus = build_corpus(n)
    texts = [text for _, text in corpus]
    _get_analyzer()  # warm model once (singleton load excluded from timings)

    def bench(fn, items) -> float:
        t0 = time.perf_counter()
        for item in items:
            fn(item)
        return time.perf_counter() - t0

    vader_s = bench(classify, texts)
    emotion_s = bench(classify_emotion, texts)
    lang_s = bench(detect_language, texts)
    t0 = time.perf_counter()
    for _ in range(n):
        NRCLex()
    ctor_s = time.perf_counter() - t0

    per_comment = {
        "vader_us": round(vader_s / n * 1e6, 1),
        "emotion_us": round(emotion_s / n * 1e6, 1),
        "langdetect_us": round(lang_s / n * 1e6, 1),
        "nrcler_ctor_us": round(ctor_s / n * 1e6, 1),
        "n": n,
    }

    # Batch-size sweep: seed READY rows directly (no acquisition), then run
    # the real process_pending at each analysis batch size.
    sweep = []
    for bs in batch_sizes:
        env = build_env([], comment_batch_size=bs)
        _seed_ready_rows(env, corpus)
        first_save: List[float] = []
        orig_save = env.repo.save_sentiment_results

        def save_probe(*args, **kwargs):
            result = orig_save(*args, **kwargs)
            if not first_save:
                first_save.append(time.perf_counter() - probe_t0)
            return result

        t0 = time.perf_counter()
        probe_t0 = t0
        env.repo.save_sentiment_results = save_probe
        summary = env.sentiment.process_pending(VIDEO_ID, None)
        elapsed = time.perf_counter() - t0
        rows = summary.processed + summary.failed + summary.skipped
        sweep.append({
            "batch_size": bs,
            "total_s": round(elapsed, 3),
            "first_batch_s": round(first_save[0], 3) if first_save else None,
            "comments_per_second": round(rows / elapsed, 1) if elapsed else 0,
            "processed": summary.processed,
        })
        env.db.close()

    return {"per_comment": per_comment, "batch_sweep": sweep}


def _seed_ready_rows(env: Env, corpus: Sequence[Tuple[str, str]]) -> None:
    from tests.conftest import comment_row, seed_video
    seed_video(env.repo, VIDEO_ID)
    env.dataset.ensure_active(VIDEO_ID)
    rows = [comment_row(cid, video_id=VIDEO_ID, text=text, language="en")
            for cid, text in corpus]
    chunk = 500
    for start in range(0, len(rows), chunk):
        env.repo.upsert_comments(rows[start:start + chunk], chunk)


# --------------------------------------------------------------------------
# e2e mode
# --------------------------------------------------------------------------

def run_e2e(size: int, page_size: int, comment_batch_size: int,
            api_latency_ms: float) -> dict:
    corpus = build_corpus(size)
    env = build_env(
        corpus, page_size=page_size, comment_batch_size=comment_batch_size,
        api_latency_seconds=api_latency_ms / 1000.0,
    )
    t = instrument(env)

    t0 = time.perf_counter()
    env.jobs.start(VIDEO_ID)

    first_insight: Optional[dict] = None
    phase_log: List[dict] = []
    last_status = None
    last_phase = None
    terminal = None
    while True:
        st = env.jobs.get_status(VIDEO_ID)
        now = time.perf_counter() - t0
        if st is None:
            break
        if (st.status, st.phase) != (last_status, last_phase):
            phase_log.append({
                "t_s": round(now, 3), "status": st.status, "phase": st.phase,
                "analyzed": st.analyzed, "collected": st.collected,
            })
            last_status, last_phase = st.status, st.phase
        if first_insight is None and st.analyzed > 0:
            first_insight = {
                "t_s": round(now, 3),
                "analyzed": st.analyzed,
                "collected": st.collected,
            }
        if st.status in ("COMPLETED", "FAILED", "CANCELLED", "STALE"):
            terminal = st
            break
        time.sleep(0.005)
    total_s = time.perf_counter() - t0

    result = {
        "size": size,
        "api_latency_ms_per_page": api_latency_ms,
        "first_insight_s": first_insight["t_s"] if first_insight else None,
        "first_insight_analyzed": first_insight["analyzed"] if first_insight else None,
        "total_s": round(total_s, 3),
        "comments_per_second": round(size / total_s, 1) if total_s else 0,
        "terminal_status": terminal.status if terminal else None,
        "analyzed": terminal.analyzed if terminal else 0,
        "skipped": terminal.skipped if terminal else 0,
        "failed": terminal.failed if terminal else 0,
        "phase_log": phase_log,
        "stages": t.as_dict(),
    }
    env.jobs.shutdown(timeout=5.0)
    env.db.close()
    return result


# --------------------------------------------------------------------------
# memory mode
# --------------------------------------------------------------------------

def run_memory(size: int, page_size: int, comment_batch_size: int) -> dict:
    corpus = build_corpus(size)
    env = build_env(corpus, page_size=page_size, comment_batch_size=comment_batch_size)
    tracemalloc.start()
    activation = env.dataset.ensure_active(VIDEO_ID)
    env.acquisition.acquire_dataset(VIDEO_ID, activation.generation)
    _, peak_acq = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    env.sentiment.process_pending(VIDEO_ID, activation.generation)
    _, peak_an = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    env.jobs.shutdown(timeout=2.0)
    env.db.close()
    return {
        "size": size,
        "peak_acquire_mib": round(peak_acq / (1024 * 1024), 2),
        "peak_analyze_mib": round(peak_an / (1024 * 1024), 2),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _parse_sizes(value: str) -> Tuple[int, ...]:
    return tuple(int(v) for v in value.split(",") if v.strip())


def _print_table(rows: List[dict], columns: Sequence[Tuple[str, str]]) -> None:
    # columns: (key, header); value printed via str unless float formatting.
    header = "  ".join(h for _, h in columns)
    print(header)
    print("-" * len(header))
    for row in rows:
        cells = []
        for key, _ in columns:
            val = row.get(key)
            cells.append("-" if val is None else
                         f"{val:.3f}" if isinstance(val, float) else str(val))
        print("  ".join(cells))


def main(argv: Optional[Sequence[str]] = None) -> int:
    configure_logging("WARNING")
    parser = argparse.ArgumentParser(description="Sprint 5.1 pipeline benchmark")
    parser.add_argument("mode", choices=("stages", "micro", "e2e", "memory"))
    parser.add_argument("--sizes", default=",".join(str(s) for s in DEFAULT_SIZES))
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=500,
                        help="COMMENT_BATCH_SIZE (production default 500)")
    parser.add_argument("--api-latency-ms", type=float, default=0.0,
                        help="simulated per-page API latency for e2e mode")
    parser.add_argument("--micro-n", type=int, default=2000)
    parser.add_argument("--baseline", action="store_true",
                        help="disable the Sprint 5.1 optimizations for A/B runs")
    parser.add_argument("--json", default=None, help="write raw results JSON")
    args = parser.parse_args(argv)
    sizes = _parse_sizes(args.sizes)
    if args.baseline:
        apply_baseline_mode()

    results: dict = {"mode": args.mode, "page_size": args.page_size,
                     "batch_size": args.batch_size,
                     "api_latency_ms": args.api_latency_ms,
                     "baseline": bool(args.baseline)}

    if args.mode == "stages":
        rows = []
        for size in sizes:
            print(f"[stages] size={size} ...", file=sys.stderr)
            rows.append(run_stages(size, args.page_size, args.batch_size))
        results["rows"] = rows
        print("\n== stages (seconds unless noted) ==")
        _print_table(rows, [
            ("size", "size"), ("acquisition_s", "acquire"),
            ("analysis_s", "analysis"), ("aggregation_s", "aggregat"),
            ("total_s", "total"), ("first_insight_sequential_s", "1st-insight"),
            ("comments_per_second", "analyzed/s"), ("end_to_end_per_second", "e2e/s"),
            ("api_calls", "api-calls"), ("nlp_inference_s", "nlp"),
            ("langdetect_s", "langdet"), ("db_write_s", "db-write"),
        ])

    elif args.mode == "micro":
        print(f"[micro] n={args.micro_n}", file=sys.stderr)
        results.update(run_micro(args.micro_n, INFERENCE_BATCH_SIZES))
        pc = results["per_comment"]
        print("\n== per-comment cost ==")
        for key in ("vader_us", "emotion_us", "nrcler_ctor_us", "langdetect_us"):
            print(f"{key:<18} {pc[key]:>10.1f} us")
        print("\n== analysis batch-size sweep ==")
        _print_table(results["batch_sweep"], [
            ("batch_size", "batch"), ("total_s", "total"),
            ("first_batch_s", "1st-batch"), ("comments_per_second", "comments/s"),
            ("processed", "processed"),
        ])

    elif args.mode == "e2e":
        rows = []
        for size in sizes:
            print(f"[e2e] size={size} latency={args.api_latency_ms}ms ...",
                  file=sys.stderr)
            rows.append(run_e2e(size, args.page_size, args.batch_size,
                                args.api_latency_ms))
        results["rows"] = rows
        print(f"\n== e2e job (simulated API latency {args.api_latency_ms} ms/page) ==")
        _print_table(rows, [
            ("size", "size"), ("first_insight_s", "first-insight"),
            ("total_s", "total"), ("comments_per_second", "comments/s"),
            ("analyzed", "analyzed"), ("skipped", "skipped"),
            ("failed", "failed"), ("terminal_status", "terminal"),
        ])

    elif args.mode == "memory":
        rows = []
        for size in sizes:
            print(f"[memory] size={size} ...", file=sys.stderr)
            rows.append(run_memory(size, args.page_size, args.batch_size))
        results["rows"] = rows
        print("\n== tracemalloc peak (MiB) ==")
        _print_table(rows, [("size", "size"),
                            ("peak_acquire_mib", "acquire"),
                            ("peak_analyze_mib", "analyze")])

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

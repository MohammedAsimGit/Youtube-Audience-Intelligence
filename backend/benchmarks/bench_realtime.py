"""Sprint 8 realtime benchmark (§53.11).

Measures the REAL incremental-poll pipeline over deterministic synthetic
datasets at representative scales (100 / 500 / 1000 / 2500 / 5000 stored
comments). Test data is clearly synthetic (Sprint 2 mock-data policy).

Run from backend/:

    ./.venv/Scripts/python benchmarks/bench_realtime.py

Stages measured (wall time, time.perf_counter only - nothing extrapolated):

    steady    acquire_incremental with a fully-stored newest page (zero
              new): the steady-state poll. Also counts commentThreads.list
              calls - the §17 quota claim (1 unit per poll, flat vs scale).
    status    RealtimeService._build_status: the live aggregate reads that
              GET /realtime serves (status/sentiment/emotion counts +
              activity window probe).
    burst     acquire_incremental inserting 50 genuinely-new comments on
              top of the stored head (probe + validate + normalize +
              language detect + guarded upsert).
    tick      one FULL monitor cycle (poll_once): probe + insert-processing
              + VADER/NRC inference over the new rows + trend/activity
              snapshot - the honest end-to-end incremental cost. The
              insight warm is gated OUT here (threshold raised to 10000)
              so the cycle measures the Sprint 8 pipeline itself; warm-up
              cost is the Sprint 7 benchmark's subject.
    memory    tracemalloc peak across a second full tick (separate pass).

The dataset cap is raised to 10000 in the bench settings so the probe
path is measured at EVERY scale (in production a dataset at the cap skips
polling entirely - a documented §17 behavior, not a cost path).

Read-only stages (steady/status) repeat `--runs` times and report the MIN
mutating stages (burst/tick) run once per scale - each mutates the store,
exactly as production does.

LLM latency is NOT measured (the default insight provider is
deterministic); the insight warm only runs above its configured threshold,
which a 50-comment burst never reaches - so a tick's cost is acquisition +
inference + aggregates, not phrasing.

Results are printed and written to benchmarks/results/sprint8-realtime.json.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.connection import Database  # noqa: E402
from app.db.repository import DatasetRepository  # noqa: E402
from app.models.processing import ProcessingStatus  # noqa: E402
from app.schemas.youtube import YtVideoListResponse  # noqa: E402
from app.services.acquisition import VideoDataService  # noqa: E402
from app.services.cache import TTLCache  # noqa: E402
from app.services.dataset import DatasetService  # noqa: E402
from app.services.insights import InsightService  # noqa: E402
from app.services.ingestion import IngestionService  # noqa: E402
from app.services.realtime import RealtimeService  # noqa: E402
from app.services.sentiment import SentimentService  # noqa: E402
from app.services.topics import TopicService  # noqa: E402

from tests.conftest import (  # noqa: E402
    FakeYouTubeClient,
    comment_row,
    make_settings,
    seed_video,
    thread_payload,
    video_payload,
)

SCALES = (100, 500, 1000, 2500, 5000)
VIDEO_ID = "bench000000"  # exactly 11 chars (YouTube video-id rule)
BURST = 50  # genuinely-new comments per burst stage

# Hygienic synthetic English text (>= 8 words so language detection says
# `en` and the rows are analyzable - same fixture rule as the test suite).
NEW_TEXT = (
    "This is a genuinely new audience comment added during the benchmark "
    "run with enough english words to be detected and analyzed honestly"
)


def seed_dataset(scale: int) -> Database:
    """Real in-memory SQLite rows with synthetic VERDICTS (no VADER/NRC
    inference inside the seeding - same fixture rule as Sprint 7)."""
    db = Database("sqlite:///:memory:")
    repo = DatasetRepository(db)
    seed_video(repo, VIDEO_ID)
    rows = [
        comment_row(
            f"c{i}",
            video_id=VIDEO_ID,
            text="synthetic stored comment for benchmark scale measurement",
            processing_status=ProcessingStatus.PROCESSED.value,
        )
        for i in range(scale)
    ]
    repo.upsert_comments(rows, batch_size=500)
    conn = db.connection()
    with db.lock:
        # Demo shape: 68% positive / 29% neutral / 3% negative, cycled.
        for i in range(scale):
            bucket = i % 100
            if bucket < 68:
                label, score = "POSITIVE", 0.6
            elif bucket < 97:
                label, score = "NEUTRAL", 0.0
            else:
                label, score = "NEGATIVE", -0.5
            conn.execute(
                "UPDATE comments SET sentiment_label = ?, sentiment_score = ?, "
                "sentiment_confidence = 0.7, sentiment_model = 'vader-1.0', "
                "sentiment_processed_at = '2026-01-01T00:00:00+00:00', "
                "sentiment_intensity = 'LOW', emotion_label = 'TRUST', "
                "emotion_score = 0.5, emotion_model = 'nrclex-4.1' "
                "WHERE comment_id = ?",
                (label, score, f"c{i}"),
            )
        conn.commit()
    # Direct row seeding bypasses the L3 acquisition path that normally
    # activates a video - make it the single active working dataset.
    repo.switch_active_video(VIDEO_ID)
    return db


def newest_page(scale: int) -> list:
    """The newest stored threads (all KNOWN ids) as a raw API page."""
    return [thread_payload(f"c{scale - 1 - i}", NEW_TEXT) for i in range(100)]


def burst_page(tag: str, known_scale: int) -> list:
    """50 genuinely-new threads above 50 stored ones (newest-first)."""
    fresh = [thread_payload(f"n{tag}{i}", NEW_TEXT) for i in range(BURST)]
    known = [thread_payload(f"c{known_scale - 1 - i}", NEW_TEXT) for i in range(BURST)]
    return fresh + known


def build_stack(scale: int, db: Database):
    """Same wiring as create_app over the seeded store + fake client."""
    settings = make_settings(
        realtime_poll_interval_seconds=3600,  # clamped to 120s; no stray ticks
        realtime_insight_min_new_analyzed=10000,  # warm gated out (see module doc)
        comment_acquisition_max_comments=10000,  # probe measured at every scale
        max_comments_per_request=50,
    )
    repo = DatasetRepository(db)
    client = FakeYouTubeClient(
        video=YtVideoListResponse.model_validate(video_payload()),
        pages={None: (newest_page(scale), None)},
    )
    cache = TTLCache(
        ttl_seconds=settings.cache_ttl_seconds,
        max_entries=settings.cache_max_entries,
    )
    dataset = DatasetService(repo, settings)
    acquisition = VideoDataService(
        client=client,
        settings=settings,
        cache=cache,
        ingestion=IngestionService(repo, settings),
        dataset=dataset,
    )
    sentiment = SentimentService(repo, settings)
    topics = TopicService(repo, settings)
    insight = InsightService(repo, sentiment, topics, settings)
    realtime = RealtimeService(
        repository=repo,
        dataset=dataset,
        acquisition=acquisition,
        sentiment=sentiment,
        insight=insight,
        settings=settings,
        cache=cache,
    )
    return SimpleStack(
        repo=repo,
        dataset=dataset,
        acquisition=acquisition,
        realtime=realtime,
        client=client,
        sentiment=sentiment,
    )


class SimpleStack:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def best_ms(runs: int, fn) -> float:
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    return round(min(times), 3)


def bench_scale(scale: int, runs: int) -> dict:
    db = seed_dataset(scale)
    stack = build_stack(scale, db)
    generation = int(stack.repo.get_active_video()["dataset_generation"])

    # 1. Steady-state probe: fully-stored page -> boundary stop, no writes.
    calls_before = len(stack.client.comment_calls)
    steady_ms = best_ms(
        runs, lambda: stack.acquisition.acquire_incremental(VIDEO_ID, generation)
    )
    steady_calls = len(stack.client.comment_calls) - calls_before
    assert steady_calls == runs, "steady poll must cost exactly 1 page/run"

    # 2. Status build: the live aggregates behind GET /realtime.
    info = stack.dataset.get_dataset_info(VIDEO_ID)
    assert info is not None
    status_ms = best_ms(runs, lambda: stack.realtime._build_status(VIDEO_ID, info))

    # Arm the monitor (baseline snapshot + idle touch) for the tick stages.
    stack.realtime.observe(VIDEO_ID)

    # 3. Burst insert: 50 genuinely-new comments over the stored head.
    stack.client.pages = {None: (burst_page(str(scale), scale), None)}
    t0 = time.perf_counter()
    burst_out = stack.acquisition.acquire_incremental(VIDEO_ID, generation)
    burst_ms = round((time.perf_counter() - t0) * 1000, 3)
    assert burst_out.inserted == BURST, burst_out

    # 4. Full tick: probe (all-known page - the burst rows are stored but
    #    still READY) + VADER/NRC over the new rows + trend/activity.
    stack.client.pages = {
        None: ([thread_payload(f"n{scale}{i}", NEW_TEXT) for i in range(BURST)], None)
    }
    t0 = time.perf_counter()
    assert stack.realtime.poll_once(VIDEO_ID) is True
    tick_ms = round((time.perf_counter() - t0) * 1000, 3)

    # 5. Memory: a second full cycle (next 50 new) under tracemalloc.
    second = burst_page(f"m{scale}", scale)
    #    second burst ids differ from the first (tag prefix) -> inserts.
    stack.client.pages = {None: (second, None)}
    assert stack.acquisition.acquire_incremental(VIDEO_ID, generation).inserted == BURST
    tracemalloc.start()
    t0 = time.perf_counter()
    assert stack.realtime.poll_once(VIDEO_ID) is True
    tick2_ms = round((time.perf_counter() - t0) * 1000, 3)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    stack.realtime.shutdown()

    analyzed = sum(
        stack.repo.get_sentiment_counts(VIDEO_ID).get(label, 0)
        for label in ("POSITIVE", "NEUTRAL", "NEGATIVE")
    )
    assert analyzed == scale + 2 * BURST, analyzed

    return {
        "scale": scale,
        "steady_probe_ms": steady_ms,
        "steady_api_calls_per_poll": steady_calls // runs,
        "status_build_ms": status_ms,
        "burst_insert_ms": burst_ms,
        "tick_ms": tick_ms,
        "tick_second_ms": tick2_ms,
        "tick_memory_kb": round(peak / 1024, 1),
        "analyzed_after": analyzed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Sprint 8 realtime benchmark")
    parser.add_argument("--runs", type=int, default=20, help="repeats for read stages")
    parser.add_argument(
        "--scales", type=int, nargs="*", default=list(SCALES), help="dataset scales"
    )
    args = parser.parse_args()

    results = []
    header = (
        f"{'scale':>6} | {'steady':>9} | {'calls':>5} | {'status':>9} | "
        f"{'burst':>9} | {'tick':>9} | {'tick2':>9} | {'mem KB':>8}"
    )
    print(header)
    print("-" * len(header))
    for scale in args.scales:
        row = bench_scale(scale, args.runs)
        results.append(row)
        print(
            f"{row['scale']:>6} | {row['steady_probe_ms']:>8.3f}m | "
            f"{row['steady_api_calls_per_poll']:>5} | {row['status_build_ms']:>8.3f}m | "
            f"{row['burst_insert_ms']:>8.3f}m | {row['tick_ms']:>8.3f}m | "
            f"{row['tick_second_ms']:>8.3f}m | {row['tick_memory_kb']:>8.1f}"
        )

    out = Path(__file__).resolve().parent / "results" / "sprint8-realtime.json"
    out.write_text(
        json.dumps(
            {
                "benchmark": "sprint8-realtime",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "runs": args.runs,
                "burst_comments": BURST,
                "notes": (
                    "steady_probe = acquire_incremental over a fully-stored "
                    "newest page (boundary stop): 1 commentThreads.list unit "
                    "per poll, flat vs dataset scale. tick = full monitor "
                    "cycle incl. VADER/NRC over the 50 new rows."
                ),
                "results": results,
            },
            indent=2,
        )
    )
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()

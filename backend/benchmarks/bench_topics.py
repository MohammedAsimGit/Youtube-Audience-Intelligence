"""Sprint 6 topic-discovery benchmark (§26).

Measures the REAL topic pipeline over deterministic synthetic corpora at
representative dataset scales (100 / 500 / 1000 / 2500 / 5000 analyzed
comments). Test data is clearly synthetic and never presented as real
YouTube data (Sprint 2 mock-data policy).

Run from backend/:

    ./.venv/Scripts/python benchmarks/bench_topics.py

Stages measured (wall time, time.perf_counter only - nothing extrapolated):

    extract   phrase segmentation + document-frequency bookkeeping (the
              same operations discover_topics performs first; replayed
              here so the split can be reported without instrumenting
              production code)
    engine    full discover_topics (candidates + clustering + merge/
              absorption passes + labels) - the production entry point
    cluster   engine - extract (derived from the two measured values:
              ranking, greedy clustering, co-discussion merge, duplicate
              absorption, label selection)
    aggregate Pydantic response build over precomputed raw topics
              (percentages, dominant labels, ranked sections)
    db load   TopicService._load: keyset read of analyzed rows from a
              real in-memory SQLite store
    memory    tracemalloc peak across a full TopicService.get_analysis
              (separate pass - never mixed with timings)

Embedding time is reported as 0 by design: Sprint 6's approach is
pure-lexical (no embedding model exists in this project's dependency set -
see topics.py).

Results are printed and written to benchmarks/results/sprint6-topics.json.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.connection import Database  # noqa: E402
from app.db.repository import DatasetRepository  # noqa: E402
from app.services.topics import (  # noqa: E402
    AnalyzedComment,
    TopicService,
    _phrases,
    discover_topics,
)
from app.models.processing import ProcessingStatus  # noqa: E402

from tests.conftest import comment_row, make_settings, seed_video  # noqa: E402

SCALES = (100, 500, 1000, 2500, 5000)

# Five disjoint deterministic themes + generic noise (30%), cycled so every
# scale has the same shape. Vocabulary is hygienic: no token is shared
# across themes, exactly like the test suite's scale fixtures.
THEMES = (
    ("clear learning roadmap path", "POSITIVE", "TRUST"),
    ("setup problems npm install", "NEGATIVE", "FEAR"),
    ("react projects ideas portfolio", "NEUTRAL", "NEUTRAL"),
    ("career opportunities jobs", "POSITIVE", "ANTICIPATION"),
    ("explanation quality tutorial", "POSITIVE", "JOY"),
)
NOISE = "nice video good stuff great video"


def corpus(scale: int) -> list[AnalyzedComment]:
    comments: list[AnalyzedComment] = []
    noise_at = set(range(0, scale, 10))  # every 10th comment is generic noise
    index = 0
    while len(comments) < scale:
        if index in noise_at:
            comments.append(
                AnalyzedComment(text=NOISE, sentiment="NEUTRAL",
                                emotion="NEUTRAL", intensity="LOW")
            )
        else:
            text, sentiment, emotion = THEMES[index % len(THEMES)]
            intensity = "HIGH" if index % 3 == 0 else "LOW"
            comments.append(
                AnalyzedComment(text=text, sentiment=sentiment,
                                emotion=emotion, intensity=intensity)
            )
        index += 1
    return comments


def time_extract(comments: list[AnalyzedComment]) -> float:
    """Replay of discover_topics' first phase (df bookkeeping included)."""
    df: dict[str, int] = {}
    docs: dict[str, set] = {}
    started = time.perf_counter()
    for position, comment in enumerate(comments):
        for phrase in _phrases(comment.text):
            df[phrase] = df.get(phrase, 0) + 1
            docs.setdefault(phrase, set()).add(position)
    return time.perf_counter() - started


def bench_scale(scale: int, runs: int) -> dict:
    comments = corpus(scale)

    extract_s = min(time_extract(comments) for _ in range(runs))
    engine_s = min(
        (lambda t0: (discover_topics(comments, 8, 24), time.perf_counter() - t0)[1])(
            time.perf_counter()
        )
        for _ in range(runs)
    )
    raw = discover_topics(comments, 8, 24)
    video_id = "bench0000000"
    service = TopicService(DatasetRepository(Database("sqlite:///:memory:")),
                           make_settings())
    aggregate_s = min(
        (lambda t0: (service._build(video_id, scale, raw),
                     time.perf_counter() - t0)[1])(time.perf_counter())
        for _ in range(runs)
    )

    # DB load: real rows through the production keyset reader. Sentiment
    # columns are written directly (synthetic verdicts - the repository's
    # insert path only carries content columns; benchmark fixture, not app
    # code) so _load sees a fully analyzed dataset without paying for real
    # VADER/NRC inference inside a topic benchmark.
    db = Database("sqlite:///:memory:")
    repo = DatasetRepository(db)
    seed_video(repo, video_id)
    rows = [
        comment_row(
            f"c{i}",
            video_id=video_id,
            text=(THEMES[i % len(THEMES)][0] if i % 10 else NOISE),
            processing_status=ProcessingStatus.PROCESSED.value,
        )
        for i in range(scale)
    ]
    repo.upsert_comments(rows, batch_size=500)
    conn = db.connection()
    with db.lock:
        conn.execute(
            "UPDATE comments SET sentiment_label = 'POSITIVE', "
            "sentiment_score = 0.5, sentiment_confidence = 0.6, "
            "sentiment_model = 'vader-1.0', "
            "sentiment_processed_at = '2026-01-01T00:00:00+00:00', "
            "sentiment_intensity = 'LOW', emotion_label = 'TRUST', "
            "emotion_score = 0.5, emotion_model = 'nrclex-4.1'"
        )
        conn.commit()
    loader = TopicService(repo, make_settings())
    started = time.perf_counter()
    loaded = loader._load(video_id)
    db_load_s = time.perf_counter() - started
    assert len(loaded) == scale

    # Full service path incl. memo cold start (separate timing pass).
    started = time.perf_counter()
    loader.get_analysis(video_id)
    service_s = time.perf_counter() - started
    started = time.perf_counter()
    loader.get_analysis(video_id)  # memo hit
    memo_s = time.perf_counter() - started

    # Memory: cold service, full get_analysis under tracemalloc.
    cold = TopicService(repo, make_settings())
    tracemalloc.start()
    cold.get_analysis(video_id)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    db.close()

    return {
        "scale": scale,
        "extract_ms": round(extract_s * 1000, 2),
        "engine_ms": round(engine_s * 1000, 2),
        "cluster_ms": round(max(0.0, engine_s - extract_s) * 1000, 2),
        "aggregate_ms": round(aggregate_s * 1000, 2),
        "db_load_ms": round(db_load_s * 1000, 2),
        "service_total_ms": round(service_s * 1000, 2),
        "memo_hit_us": round(memo_s * 1_000_000, 1),
        "memory_peak_kb": round(peak / 1024, 1),
        "topics": len(raw),
        "embedding_ms": 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Sprint 6 topic benchmark")
    parser.add_argument("--runs", type=int, default=5,
                        help="timed runs per scale; the minimum is reported")
    args = parser.parse_args()

    rows = []
    header = (
        f"{'scale':>6} {'extract':>9} {'cluster':>9} {'aggregate':>10} "
        f"{'db load':>9} {'service':>9} {'memo':>9} {'peak KB':>9} {'topics':>7}"
    )
    print(header)
    print("-" * len(header))
    for scale in SCALES:
        result = bench_scale(scale, args.runs)
        rows.append(result)
        print(
            f"{result['scale']:>6} {result['extract_ms']:>8.2f}m "
            f"{result['cluster_ms']:>8.2f}m {result['aggregate_ms']:>9.2f}m "
            f"{result['db_load_ms']:>8.2f}m {result['service_total_ms']:>8.2f}m "
            f"{result['memo_hit_us']:>7.1f}u {result['memory_peak_kb']:>9.1f} "
            f"{result['topics']:>7}"
        )

    out = Path(__file__).resolve().parent / "results" / "sprint6-topics.json"
    out.write_text(
        json.dumps(
            {
                "benchmark": "sprint6-topics",
                "runs_per_scale": args.runs,
                "method": "min of timed runs (perf_counter)",
                "embedding": "not used (pure-lexical discovery)",
                "results": rows,
            },
            indent=2,
        )
    )
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()

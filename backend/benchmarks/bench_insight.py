"""Sprint 7 audience-insight benchmark (§36).

Measures the REAL insight pipeline over deterministic synthetic datasets
at representative scales (100 / 500 / 1000 / 2500 / 5000 analyzed
comments). Test data is clearly synthetic and never presented as real
YouTube data (Sprint 2 mock-data policy).

Run from backend/:

    ./.venv/Scripts/python benchmarks/bench_insight.py

Stages measured (wall time, time.perf_counter only - nothing extrapolated):

    evidence    build_evidence: SentimentAnalysisResponse + TopicAnalysisResponse
                -> AudienceEvidence snapshot (pure)
    candidates  select_candidates: rules/ranking over the evidence (pure)
    generate    DeterministicProvider.generate + validate_draft + build_cards
                (the whole phrasing layer the LLM path would replace)
    validate    validate_draft alone (schema + grounding + length gates)
    service     InsightService.get_insight with FRESH services (cold
                memos): sentiment aggregates + full topic discovery +
                evidence + phrasing - the honest first-GET cost
    memo        second get_insight (fingerprint memo hit)
    prompt      bytes of the compact evidence block an LLM would receive
                - proves §13/§37: bounded by topics, NOT comment count
    memory      tracemalloc peak across a cold get_insight (separate pass)

LLM latency is NOT measured: the default provider is deterministic (no
network), and provider HTTP time is configuration-dependent - what this
benchmark proves is that everything AROUND the provider call is
milliseconds and that the prompt stays small at any dataset scale.

Results are printed and written to benchmarks/results/sprint7-insight.json.
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
from app.models.processing import ProcessingStatus  # noqa: E402
from app.services.insights import (  # noqa: E402
    DeterministicProvider,
    InsightService,
    _evidence_prompt,
    build_cards,
    build_evidence,
    select_candidates,
    validate_draft,
)
from app.services.sentiment import SentimentService  # noqa: E402
from app.services.topics import TopicService  # noqa: E402

from tests.conftest import comment_row, make_settings, seed_video  # noqa: E402

SCALES = (100, 500, 1000, 2500, 5000)

# Five disjoint deterministic themes + generic noise (30%), cycled so every
# scale has the same shape as the Sprint 6 benchmark (hygienic vocabulary:
# no token shared across themes).
THEMES = (
    ("clear learning roadmap path", "POSITIVE", "TRUST"),
    ("setup problems npm install", "NEGATIVE", "FEAR"),
    ("react projects ideas portfolio", "NEUTRAL", "NEUTRAL"),
    ("career opportunities jobs", "POSITIVE", "ANTICIPATION"),
    ("explanation quality tutorial", "POSITIVE", "JOY"),
)
NOISE = "nice video good stuff great video"

VIDEO_ID = "bench0000000"


def seed_dataset(scale: int) -> Database:
    """Real in-memory SQLite rows with synthetic VERDICTS (no VADER/NRC
    inference inside an insight benchmark - same fixture rule as Sprint 6)."""
    db = Database("sqlite:///:memory:")
    repo = DatasetRepository(db)
    seed_video(repo, VIDEO_ID)
    rows = [
        comment_row(
            f"c{i}",
            video_id=VIDEO_ID,
            text=(THEMES[i % len(THEMES)][0] if i % 10 else NOISE),
            processing_status=ProcessingStatus.PROCESSED.value,
        )
        for i in range(scale)
    ]
    repo.upsert_comments(rows, batch_size=500)
    conn = db.connection()
    with db.lock:
        # Distribution: 68% positive / 29% neutral / 3% negative - the
        # documented demo shape, cycled deterministically by row index.
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
    return db


def bench_scale(scale: int, runs: int) -> dict:
    db = seed_dataset(scale)
    repo = DatasetRepository(db)
    settings = make_settings()
    sentiment_service = SentimentService(repo, settings)
    topics_service = TopicService(repo, settings)

    sentiment = sentiment_service.get_analysis(VIDEO_ID)
    topics = topics_service.get_analysis(VIDEO_ID)
    assert sentiment is not None and topics is not None
    version = f"{scale}:bench"

    evidence_s = min(
        (lambda t0: (build_evidence(VIDEO_ID, sentiment, topics, version),
                     time.perf_counter() - t0)[1])(time.perf_counter())
        for _ in range(runs)
    )
    evidence = build_evidence(VIDEO_ID, sentiment, topics, version)

    candidates_s = min(
        (lambda t0: (select_candidates(evidence, 8),
                     time.perf_counter() - t0)[1])(time.perf_counter())
        for _ in range(runs)
    )
    candidates = select_candidates(evidence, 8)

    provider = DeterministicProvider()
    generate_s = min(
        (lambda t0: (
            build_cards(evidence, candidates,
                        validate_draft(provider.generate(evidence, candidates),
                                       evidence)),
            time.perf_counter() - t0)[1])(time.perf_counter())
        for _ in range(runs)
    )
    payload = provider.generate(evidence, candidates)
    validate_s = min(
        (lambda t0: (validate_draft(payload, evidence),
                     time.perf_counter() - t0)[1])(time.perf_counter())
        for _ in range(runs)
    )
    prompt_bytes = len(_evidence_prompt(evidence, candidates).encode("utf-8"))

    # Full real GET path with FRESH services (cold memos: sentiment
    # aggregate read + full topic discovery + evidence + phrasing) - the
    # honest cost of a first GET /insight after a restart.
    fresh_settings = make_settings()
    cold = InsightService(
        repo,
        SentimentService(repo, fresh_settings),
        TopicService(repo, fresh_settings),
        fresh_settings,
    )
    started = time.perf_counter()
    response = cold.get_insight(VIDEO_ID)
    service_s = time.perf_counter() - started
    assert response is not None and response.status == "READY"
    started = time.perf_counter()
    cold.get_insight(VIDEO_ID)  # memo hit
    memo_s = time.perf_counter() - started

    tracemalloc.start()
    mem_settings = make_settings()
    fresh = InsightService(
        repo,
        SentimentService(repo, mem_settings),
        TopicService(repo, mem_settings),
        mem_settings,
    )
    fresh.get_insight(VIDEO_ID)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    db.close()

    return {
        "scale": scale,
        "evidence_ms": round(evidence_s * 1000, 2),
        "candidates_ms": round(candidates_s * 1000, 3),
        "generate_ms": round(generate_s * 1000, 2),
        "validate_ms": round(validate_s * 1000, 3),
        "service_total_ms": round(service_s * 1000, 2),
        "memo_hit_us": round(memo_s * 1_000_000, 1),
        "prompt_bytes": prompt_bytes,
        "memory_peak_kb": round(peak / 1024, 1),
        "cards": len(response.cards),
        "llm_prompt_chars": prompt_bytes,  # alias kept for readability
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Sprint 7 insight benchmark")
    parser.add_argument("--runs", type=int, default=5,
                        help="timed runs per scale; the minimum is reported")
    args = parser.parse_args()

    rows = []
    header = (
        f"{'scale':>6} {'evidence':>9} {'generate':>9} {'validate':>9} "
        f"{'service':>9} {'memo':>9} {'prompt':>8} {'peak KB':>9} {'cards':>6}"
    )
    print(header)
    print("-" * len(header))
    for scale in SCALES:
        result = bench_scale(scale, args.runs)
        rows.append(result)
        print(
            f"{result['scale']:>6} {result['evidence_ms']:>8.2f}m "
            f"{result['generate_ms']:>8.2f}m {result['validate_ms']:>8.3f}m "
            f"{result['service_total_ms']:>8.2f}m "
            f"{result['memo_hit_us']:>7.1f}u {result['prompt_bytes']:>7}B "
            f"{result['memory_peak_kb']:>9.1f} {result['cards']:>6}"
        )

    out = Path(__file__).resolve().parent / "results" / "sprint7-insight.json"
    out.write_text(
        json.dumps(
            {
                "benchmark": "sprint7-insight",
                "runs_per_scale": args.runs,
                "method": "min of timed runs (perf_counter)",
                "llm": "deterministic default provider (no network); provider "
                       "HTTP time is configuration-dependent and not measured",
                "results": rows,
            },
            indent=2,
        )
    )
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()

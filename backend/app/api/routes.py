"""API routes (contract: docs/13-backend-api-contract.md).

Sprint 3 additions are read-only statistics endpoints; the original
`GET /api/videos/{video_id}` contract is untouched (the Chrome extension
keeps working without changes). Route handlers never contain database
logic - they delegate to the service layer.
"""
import sqlite3

from fastapi import APIRouter, Request
from pydantic import AliasGenerator, BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.core.errors import StorageUnavailable, VideoNotFound
from app.core.logging import get_logger
from app.models.internal import (
    AnalysisJobCreatedResponse,
    AnalysisJobStatusResponse,
    RealtimeStatusResponse,
)
from app.services.acquisition import VideoDataService, validate_video_id
from app.services.dataset import DatasetService
from app.services.jobs import AnalysisJobService
from app.services.realtime import RealtimeService
from app.services.sentiment import SentimentService
from app.services.topics import TopicService
from app.services.insights import InsightService

logger = get_logger("api")
router = APIRouter()


class HealthResponse(BaseModel):
    model_config = ConfigDict(
        alias_generator=AliasGenerator(serialization_alias=to_camel),
        populate_by_name=True,
    )
    status: str = "ok"
    version: str = "0.5.0"


def get_service(request: Request) -> VideoDataService:
    return request.app.state.video_data_service


def get_dataset(request: Request) -> DatasetService:
    return request.app.state.dataset_service


def get_sentiment(request: Request) -> SentimentService:
    return request.app.state.sentiment_service


def get_topics(request: Request) -> TopicService:
    return request.app.state.topic_service


def get_insight(request: Request) -> InsightService:
    return request.app.state.insight_service


def get_jobs(request: Request) -> AnalysisJobService:
    return request.app.state.job_service


def get_realtime(request: Request) -> RealtimeService:
    return request.app.state.realtime_service


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness probe - works without an API key (no upstream calls)."""
    return HealthResponse()


@router.get("/api/videos/{video_id}")
def get_video_data(video_id: str, request: Request):
    """Acquire normalized metadata + comments for one video.

    Flow: validate id -> ACTIVE-VIDEO TRANSITION (Sprint 4.2: this is the
    extension's explicit activation point - a different id atomically
    replaces the working dataset) -> cache (memory) -> fresh dataset ->
    YouTube API -> ingestion (validate/normalize/dedup/persist, guarded by
    the activation generation) -> stable internal contract. Read-only
    endpoints never activate. 422 invalid id · 503 store unavailable.
    """
    service = get_service(request)
    try:
        return service.get_video_data(video_id)
    except sqlite3.Error as exc:
        logger.error(
            "acquisition failed on dataset store",
            extra={
                "video_id": video_id,
                "error": type(exc).__name__,
                "error_message": str(exc),
            },
        )
        raise StorageUnavailable("dataset store failed during activation") from exc


@router.post("/api/videos/{video_id}/analysis", status_code=202)
def start_analysis(video_id: str, request: Request) -> AnalysisJobCreatedResponse:
    """Start a background analysis job (Sprint 4.3 §9) - returns fast.

    Validates the id (422 BEFORE any lifecycle change), performs the
    active-video transition, deduplicates a running job for the same video
    (§21), persists QUEUED, spawns the worker, and responds 202 Accepted
    WITHOUT waiting for YouTube pages, storage, or sentiment batches (§40:
    no single long-running request owns the pipeline).

    422 invalid id · 503 store unavailable / service not configured.
    Observe progress via GET /api/videos/{video_id}/analysis/status.
    """
    validate_video_id(video_id)
    jobs = get_jobs(request)
    try:
        result = jobs.start(video_id)
    except sqlite3.Error as exc:
        logger.error(
            "analysis trigger failed on dataset store",
            extra={
                "video_id": video_id,
                "error": type(exc).__name__,
                "error_message": str(exc),
            },
        )
        raise StorageUnavailable("dataset store failed while starting analysis") from exc
    return AnalysisJobCreatedResponse(
        job_id=result.job_id, video_id=result.video_id, status=result.status
    )


@router.get("/api/videos/{video_id}/analysis/status")
def get_analysis_status(video_id: str, request: Request) -> AnalysisJobStatusResponse:
    """Live background-job progress for one video (Sprint 4.3 §10).

    Returns job id, status, phase, REAL collected/stored/analyzed/skipped/
    failed/pending counts, hasMore, error information and timestamps.
    422 invalid id · 404 no job has ever run for this video · 503 store
    unavailable. Never exposes secrets or raw comment text.
    """
    validate_video_id(video_id)
    jobs = get_jobs(request)
    try:
        status = jobs.get_status(video_id)
    except sqlite3.Error as exc:
        logger.error(
            "analysis status query failed",
            extra={
                "video_id": video_id,
                "error": type(exc).__name__,
                "error_message": str(exc),
            },
        )
        raise StorageUnavailable("dataset store failed while reading analysis status") from exc
    if status is None:
        raise VideoNotFound("no analysis job for this video")
    return status


@router.get("/api/videos/{video_id}/stats")
def get_video_stats(video_id: str, request: Request):
    """Dataset statistics for one video (acquisition facts only).

    422 invalid id · 404 no stored dataset · 503 store unavailable.
    No query parameters are accepted - nothing here can shape a raw query.
    """
    validate_video_id(video_id)
    dataset = get_dataset(request)
    try:
        stats = dataset.get_stats(video_id)
    except sqlite3.Error as exc:
        logger.error(
            "stats query failed",
            extra={
                "video_id": video_id,
                "error": type(exc).__name__,
                "error_message": str(exc),
            },
        )
        raise StorageUnavailable("dataset store failed while reading stats") from exc
    if stats is None:
        raise VideoNotFound("no stored dataset for this video")
    return stats


@router.get("/api/videos/{video_id}/sentiment")
def get_video_sentiment(video_id: str, request: Request):
    """Sentiment analysis state + aggregates for one video (Sprint 4).

    Backend-owned analysis: when the stored dataset has pending rows this
    request runs the sentiment engine (batched, state-machine transitions)
    before answering, so the extension only ever GETs results - it never runs
    a model. 422 invalid id · 404 no stored dataset · 503 store unavailable.
    No query parameters are accepted - nothing here can shape a raw query.
    """
    validate_video_id(video_id)
    sentiment = get_sentiment(request)
    try:
        analysis = sentiment.get_analysis(video_id)
    except sqlite3.Error as exc:
        logger.error(
            "sentiment query failed",
            extra={
                "video_id": video_id,
                "error": type(exc).__name__,
                "error_message": str(exc),
            },
        )
        raise StorageUnavailable("dataset store failed while reading sentiment") from exc
    if analysis is None:
        raise VideoNotFound("no stored dataset for this video")
    return analysis


@router.get("/api/videos/{video_id}/topics")
def get_video_topics(video_id: str, request: Request):
    """Topic / discussion intelligence for one video (Sprint 6, §28).

    Data-driven discovery over the already-analyzed comments: discovered
    themes with frequency/sentiment/emotion/intensity aggregates plus the
    Most Discussed / Most Appreciated / Pain Points / Mixed ranked views.
    Pure-function computation over stored rows - no model inference, no
    raw comment text, no query parameters. Answers INSUFFICIENT_DATA (with
    honest copy) instead of inventing themes below the configured minimum
    sample (§38).

    422 invalid id · 404 no stored dataset · 503 store unavailable.
    """
    validate_video_id(video_id)
    topics = get_topics(request)
    try:
        analysis = topics.get_analysis(video_id)
    except sqlite3.Error as exc:
        logger.error(
            "topic query failed",
            extra={
                "video_id": video_id,
                "error": type(exc).__name__,
                "error_message": str(exc),
            },
        )
        raise StorageUnavailable("dataset store failed while reading topics") from exc
    if analysis is None:
        raise VideoNotFound("no stored dataset for this video")
    return analysis


@router.get("/api/videos/{video_id}/insight")
def get_video_insight(video_id: str, request: Request):
    """Evidence-based audience insight for one video (Sprint 7, §15).

    Phrases the ALREADY computed sentiment + topic intelligence through
    the configured provider abstraction (deterministic default; optional
    LLM adapter with bounded retries and grounded, schema-validated
    output). `source` reports deterministic / llm / fallback honestly
    (§23) - deterministic text is never presented as AI-generated. The
    evidence is bounded aggregates only: no raw comment text leaves this
    endpoint and the prompt size does not scale with comment count (§13).

    422 invalid id · 404 no stored dataset · 503 store unavailable.
    """
    validate_video_id(video_id)
    insight = get_insight(request)
    try:
        analysis = insight.get_insight(video_id)
    except sqlite3.Error as exc:
        logger.error(
            "insight query failed",
            extra={
                "video_id": video_id,
                "error": type(exc).__name__,
                "error_message": str(exc),
            },
        )
        raise StorageUnavailable("dataset store failed while reading insight") from exc
    if analysis is None:
        raise VideoNotFound("no stored dataset for this video")
    return analysis


@router.get("/api/videos/{video_id}/realtime")
def get_video_realtime(video_id: str, request: Request) -> RealtimeStatusResponse:
    """Realtime audience-intelligence status + monitor touch (Sprint 8 §19).

    This GET is also the monitor's keep-alive: while the overlay polls it
    for the ACTIVE video, a background monitor thread checks YouTube for
    newly available comments on the configured interval (never per-request,
    §15: handlers stay non-blocking), processes only READY rows, and
    refreshes trend/activity/insight off the request path. The response
    carries LIVE counts, the monitor snapshot, `enabled`/`monitoring`, and
    a `version` marker for cheap change detection - never raw comment text.

    422 invalid id · 404 no stored dataset · 503 store unavailable.
    No query parameters are accepted - nothing here can shape a raw query.
    """
    validate_video_id(video_id)
    realtime = get_realtime(request)
    try:
        status = realtime.observe(video_id)
    except sqlite3.Error as exc:
        logger.error(
            "realtime status query failed",
            extra={
                "video_id": video_id,
                "error": type(exc).__name__,
                "error_message": str(exc),
            },
        )
        raise StorageUnavailable("dataset store failed while reading realtime status") from exc
    if status is None:
        raise VideoNotFound("no stored dataset for this video")
    return status

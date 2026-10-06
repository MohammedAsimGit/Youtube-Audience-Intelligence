"""FastAPI application factory.

Wiring: settings -> logging -> cache boundary -> YouTube client -> service ->
routes. `create_app` accepts injected dependencies so tests can run without a
real API key or network access.
"""
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.core.config import Settings, get_settings
from app.core.errors import AcquisitionError
from app.core.logging import configure_logging, get_logger
from app.db.connection import Database
from app.db.repository import DatasetRepository
from app.services.acquisition import VideoDataService
from app.services.cache import TTLCache
from app.services.dataset import DatasetService
from app.services.ingestion import IngestionService
from app.services.jobs import AnalysisJobService
from app.services.realtime import RealtimeService
from app.services.sentiment import SentimentService
from app.services.topics import TopicService
from app.services.insights import InsightService

logger = get_logger("main")

# User-facing copy per error code (§24 of the Sprint 2 brief). Internal
# details stay in server logs - never in responses.
_FRIENDLY_MESSAGES = {
    "invalid_video_id": "Invalid video ID.",
    "video_not_found": "This video was not found or is unavailable.",
    "comments_disabled": "Comments are disabled for this video.",
    "quota_exceeded": "The YouTube API quota is exhausted for now. Please try again later.",
    "upstream_timeout": "The request timed out. Please try again.",
    "upstream_unavailable": "YouTube is currently unreachable. Please try again later.",
    "upstream_data_invalid": "Received an unexpected response. Please try again later.",
    "server_not_configured": "The analysis service is not configured yet.",
    "storage_unavailable": "The dataset store is unavailable right now. Please try again later.",
    "acquisition_failed": (
        "We couldn't retrieve audience responses for this video. Please try again later."
    ),
}


def _error_payload(code: str) -> dict:
    return {
        "error": {
            "code": code,
            "message": _FRIENDLY_MESSAGES.get(
                code, _FRIENDLY_MESSAGES["acquisition_failed"]
            ),
        }
    }


def create_app(
    settings: Optional[Settings] = None,
    youtube_client: Any = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        jobs = getattr(app.state, "job_service", None)
        if jobs is not None:
            # Sprint 4.3 §32: jobs non-terminal at startup have no worker -
            # mark them STALE (retryable) BEFORE serving, so the extension
            # can never stay pinned at ACQUIRING after a restart.
            jobs.sweep_interrupted()
        yield
        # Sprint 8 §32: monitors are in-memory threads - stop them first so
        # no poll runs while the services they use are being torn down.
        realtime = getattr(app.state, "realtime_service", None)
        if realtime is not None:
            realtime.shutdown()
        jobs = getattr(app.state, "job_service", None)
        if jobs is not None:
            jobs.shutdown()
        close = getattr(app.state.youtube_client, "close", None)
        if callable(close):
            close()
        database = getattr(app.state, "database", None)
        if database is not None:
            database.close()

    app = FastAPI(
        title="Sentiment AI - Data Acquisition API",
        version="0.5.0",
        description=(
            "Sprint 4: acquisition + ingestion (validate, normalize, dedup, "
            "persist) with an analysis-ready dataset store, plus the backend-"
            "owned sentiment intelligence layer (VADER, batched, state-"
            "machine transitions) exposed at /api/videos/{video_id}/sentiment. "
            "Sprint 4.3: analysis runs as a background job - POST "
            "/api/videos/{video_id}/analysis returns 202 immediately and "
            "GET /api/videos/{video_id}/analysis/status reports real progress."
        ),
        lifespan=lifespan,
    )

    # CORS: exact allowed origins + extension-origin regex from settings
    # (never "*" as a final configuration; see config for rationale).
    # POST exists only for the Sprint 4.3 analysis trigger (§9).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_origin_regex=settings.cors_origin_regex,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    cache = TTLCache(
        ttl_seconds=settings.cache_ttl_seconds,
        max_entries=settings.cache_max_entries,
    )
    client = youtube_client
    if client is None:
        from app.clients.youtube import YouTubeClient

        client = YouTubeClient(
            api_key=settings.youtube_api_key,
            timeout_seconds=settings.youtube_request_timeout_seconds,
            max_attempts=settings.youtube_retry_attempts,
            backoff_seconds=settings.youtube_retry_backoff_seconds,
        )

    # Persistence: lazily-opened SQLite (schema on first use) -> repository
    # -> ingestion (write path) + dataset (read path) services.
    database = Database(settings.database_url)
    repository = DatasetRepository(database)
    ingestion = IngestionService(repository, settings)
    dataset = DatasetService(repository, settings)
    sentiment = SentimentService(repository, settings)
    # Sprint 6: topic/discussion intelligence - compute-on-read over the
    # same analyzed rows (no second pipeline, no new tables).
    topics = TopicService(repository, settings)
    # Sprint 7: evidence-based audience insight - phrases the sentiment +
    # topic responses through a provider abstraction (deterministic by
    # default; optional LLM adapter is configured, never hardcoded).
    insight = InsightService(repository, sentiment, topics, settings)

    app.state.youtube_client = client
    app.state.database = database
    app.state.cache = cache  # exposed for lifecycle tests (single-active L1)
    app.state.dataset_service = dataset
    app.state.sentiment_service = sentiment
    app.state.topic_service = topics
    app.state.insight_service = insight
    video_data_service = VideoDataService(
        client=client,
        settings=settings,
        cache=cache,
        ingestion=ingestion,
        dataset=dataset,
    )
    app.state.video_data_service = video_data_service
    # Sprint 4.3: background analysis jobs (trigger + worker + status).
    app.state.job_service = AnalysisJobService(
        repository=repository,
        dataset=dataset,
        acquisition=video_data_service,
        sentiment=sentiment,
        settings=settings,
        cache=cache,
        topics=topics,
        insight=insight,
    )
    # Sprint 8: realtime audience intelligence - one monitor thread for the
    # active video, started/kept alive by GET .../realtime touches, stopped
    # on video switch, idle disconnect, or shutdown (see services/realtime.py).
    app.state.realtime_service = RealtimeService(
        repository=repository,
        dataset=dataset,
        acquisition=video_data_service,
        sentiment=sentiment,
        insight=insight,
        settings=settings,
        cache=cache,
    )

    @app.exception_handler(AcquisitionError)
    async def acquisition_error_handler(
        _request: Request, exc: AcquisitionError
    ) -> JSONResponse:
        logger.error(
            "acquisition error",
            extra={"code": exc.code, "detail": str(exc)},
        )
        return JSONResponse(
            status_code=exc.http_status, content=_error_payload(exc.code)
        )

    app.include_router(router)
    return app


# Uvicorn entrypoint: `uvicorn app.main:app --reload` (run from backend/).
app = create_app()

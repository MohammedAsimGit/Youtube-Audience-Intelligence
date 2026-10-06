"""Environment-based configuration.

Secrets (YOUTUBE_API_KEY) live only here, read from `.env` / process env.
Nothing in this module may be imported by the Chrome extension - this file is
server-side by construction.
"""
import json
from functools import lru_cache
from typing import Annotated, List

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Secrets -----------------------------------------------------------------
    youtube_api_key: str = ""

    # Acquisition (Sprint 4.1: page size vs dataset limit are DISTINCT) -------
    # PAGE SIZE: max results requested per commentThreads.list call (1 quota
    # unit per page regardless of size; YouTube caps a page at 100 items).
    max_comments_per_request: int = 100
    # DATASET LIMIT: maximum comments intentionally acquired for ONE video in
    # ONE acquisition run. Pagination walks nextPageToken until YouTube runs
    # out, this limit is reached, or the page-safety bound below is hit - the
    # system never silently stops at a page boundary.
    comment_acquisition_max_comments: int = 5000
    youtube_request_timeout_seconds: float = 10.0
    # Sprint 4.3 §17: bounded retries with exponential backoff for
    # TRANSIENT failures only (network errors, timeouts, 5xx/429). Permanent
    # errors (quota, commentsDisabled, bad video id, invalid payload) are
    # never retried - retrying quota exhaustion would only burn units.
    # `attempts` counts the TOTAL tries (1 = no retry).
    youtube_retry_attempts: int = 3
    youtube_retry_backoff_seconds: float = 0.5
    # Pagination safety bound (independent of the dataset limit): a pathological
    # endless/repeating nextPageToken can never loop forever. Effective ceiling
    # per acquisition = min(COMMENT_ACQUISITION_MAX_COMMENTS,
    # MAX_COMMENTS_PER_REQUEST * MAX_API_PAGES); raise this for larger caps.
    max_api_pages: int = 100

    # Sprint 5.1 (§4): NLP inference batch - how many comments are
    # claimed -> classified -> persisted per analysis batch. Benchmark
    # (backend/benchmarks/bench_pipeline.py micro, 5000 comments):
    #   32 -> 549 c/s, 64 -> 641, 128 -> 695, 256 -> 675, 512 -> 697
    #   (Sprint 5.1 optimizations off) and
    #   32 -> 1039 c/s, 64 -> 1245, 128 -> 1284, 256 -> 1275, 512 -> 1226
    #   (optimizations on)  => throughput rises until ~128 and is flat
    #   within run-to-run noise after that; 32/64 are consistently the
    #   slowest, while larger batches only delay the first saved batch
    #   (1st-batch 0.09s at 128 vs 0.43s at 512). `0` (the default)
    #   follows COMMENT_BATCH_SIZE so ingestion and inference share one
    #   knob unless an operator explicitly pins inference batching.
    sentiment_inference_batch_size: int = 0
    # Sprint 5.2 (§9/§10): progressive-analysis cadence - the minimum
    # number of pending READY rows between INTERIM drains while
    # acquisition is still running. The FIRST interim drain always runs
    # immediately (time-to-first-insight stays one page), then drains
    # re-arm every ANALYSIS_PROGRESS_UPDATE_INTERVAL new comments. This
    # decouples NLP inference batching (SENTIMENT_INFERENCE_BATCH_SIZE,
    # above) from the cadence at which `analyzed` - and therefore the
    # UI's progressive aggregate - advances in truthful steps. The final
    # ANALYZING drain is unconditional: nothing is ever skipped.
    # Measured (bench paged-vs-gated, 5000 comments): one drain per
    # 100-comment page costs 47 ms/call (p50) with per-call fixed
    # overhead; gating to 500 raised drain throughput 1915 -> 2456 c/s.
    analysis_progress_update_interval: int = 500
    # Sprint 6 (§12): topic/discussion intelligence thresholds.
    # TOPIC_MIN_COMMENT_COUNT: below this many POLARITY-ANALYZED comments no
    # topic analysis runs at all - the API answers INSUFFICIENT_DATA instead
    # of inventing themes from a handful of comments (§38).
    # TOPIC_MIN_SUPPORT: minimum mentions before a discovered topic may enter
    # a ranked section (Most Discussed / Loved / Struggle With / Mixed).
    # Guards every small-sample trap: 5 mentions at 100% positive can never
    # outrank 250 mentions at 89% (§11/§19).
    # TOPIC_MAX_TOPICS: hard cap on persisted topics per video (payload stays
    # small; the lowest-signal clusters are dropped first, §22).
    topic_min_comment_count: int = 30
    topic_min_support: int = 8
    topic_max_topics: int = 24
    # Sprint 7 (§12/§16/§38): audience-insight LANGUAGE generation.
    # The insight layer is provider-independent: structured evidence ->
    # provider adapter -> validated phrasing, with the deterministic
    # generator (templates over the same evidence, no network) as the
    # guaranteed fallback so the product never depends on an external AI.
    # PROVIDER "deterministic" (default) never calls out; "openai_-
    # compatible" posts chat-completions to INSIGHT_API_BASE_URL using
    # INSIGHT_API_KEY (a secret - server-side only, never sent to the
    # extension, never logged). On timeout/invalid output the service
    # falls back and reports source=fallback honestly (§23/§45).
    insight_provider: str = "deterministic"
    insight_api_base_url: str = "https://api.openai.com/v1"
    insight_api_key: str = ""
    insight_model: str = "gpt-4o-mini"
    insight_timeout_seconds: float = 10.0
    # Total tries (1 = no retry); bounded so a failing provider can never
    # hold the job/GET long (§26 timeout safety).
    insight_retry_attempts: int = 2
    insight_max_output_tokens: int = 700
    # Sprint 8 (§8/§18/§20/§47): real-time incremental monitoring.
    # REALTIME_ENABLED gates the monitor loop entirely (off => the GET
    # /realtime endpoint still answers honestly with monitoring=false).
    # The POLL interval is clamped into [MIN, MAX] at use site so a single
    # misconfigured value can never spin the loop or starve it. The
    # TREND_MIN_CHANGE (percentage points on the NET score) is the noise
    # floor below which movement reports STABLE instead of flapping the UI.
    # INSIGHT_MIN_NEW_ANALYZED is the update threshold: the audience
    # insight regenerates only after this many NEW polarity verdicts since
    # the last regeneration - never after every comment (§17/§18).
    realtime_enabled: bool = True
    realtime_poll_interval_seconds: float = 30.0
    realtime_min_poll_interval_seconds: float = 15.0
    realtime_max_poll_interval_seconds: float = 120.0
    realtime_trend_min_change: float = 1.0
    realtime_insight_min_new_analyzed: int = 25
    # Storage / dataset cache (Sprint 3) ---------------------------------------
    # sqlite:/// relative to the backend/ working directory, or :memory:.
    database_url: str = "sqlite:///./data/sentiment.db"
    # Ingestion writes comments to the DB in batches of this many rows so a
    # 10k+ comment dataset never requires one giant statement or full reload.
    comment_batch_size: int = 500
    # Freshness policy: a stored dataset younger than this is served without
    # touching the YouTube API (0 quota). Single source of truth - the
    # service layer never hardcodes freshness anywhere else.
    dataset_cache_ttl_seconds: int = 3600

    # Caching (in-memory hot layer over the dataset store) ---------------------
    cache_ttl_seconds: int = 600
    cache_max_entries: int = 128

    # CORS --------------------------------------------------------------------
    # Exact origins (dev servers) + a regex for extension origins: Chrome sends
    # `Origin: chrome-extension://<32-char id>` on cross-origin fetches, and
    # Starlette matches `allow_origins` EXACTLY - a bare prefix would never
    # match, hence allow_origin_regex for the extension scheme. Never `*`.
    # NoDecode: CORS_ORIGINS is documented (.env.example) as a plain
    # comma-separated list, but pydantic-settings would otherwise demand a
    # JSON array and crash Settings() at import - see the validator below.
    cors_origins: Annotated[List[str], NoDecode] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://www.youtube.com",
    ]
    # Chrome extension IDs are 32 chars from the base16-alphabet a-p.
    cors_origin_regex: str = r"^chrome-extension://[a-p]{32}$"

    log_level: str = "INFO"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _cors_origins_from_env(cls, value: object) -> object:
        """Parse CORS_ORIGINS from either documented format.

        `.env.example` documents a comma-separated list; a JSON array also
        works. Values already typed as a list (programmatic construction,
        tests) pass through untouched.
        """
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            parsed = json.loads(text)
            if not isinstance(parsed, list):
                raise ValueError("CORS_ORIGINS must be a list")
            return parsed
        return [part.strip() for part in text.split(",") if part.strip()]

    @field_validator("max_comments_per_request")
    @classmethod
    def _bounded_comments(cls, value: int) -> int:
        if value < 1:
            raise ValueError("MAX_COMMENTS_PER_REQUEST must be >= 1")
        if value > 100:
            # commentThreads.list accepts at most 100 items per page; anything
            # larger would be silently clamped, so reject it loudly instead.
            raise ValueError("MAX_COMMENTS_PER_REQUEST must be <= 100 (YouTube page cap)")
        return value

    @field_validator("comment_acquisition_max_comments")
    @classmethod
    def _bounded_acquisition_limit(cls, value: int) -> int:
        if value < 1:
            raise ValueError("COMMENT_ACQUISITION_MAX_COMMENTS must be >= 1")
        return value

    @field_validator("comment_batch_size")
    @classmethod
    def _bounded_batch(cls, value: int) -> int:
        if value < 1:
            raise ValueError("COMMENT_BATCH_SIZE must be >= 1")
        return value

    @field_validator("sentiment_inference_batch_size")
    @classmethod
    def _bounded_inference_batch(cls, value: int) -> int:
        if value < 0:
            # 0 = follow COMMENT_BATCH_SIZE (documented default); negative
            # is never meaningful and is rejected instead of silently fixed.
            raise ValueError("SENTIMENT_INFERENCE_BATCH_SIZE must be >= 0")
        if value > 10000:
            # Not a tuning knob beyond this point: 10k outcomes in one
            # claim/save cycle already dwarfs any measured gain (§4: the
            # benchmark plateau is ~512) while inflating peak memory.
            raise ValueError("SENTIMENT_INFERENCE_BATCH_SIZE must be <= 10000")
        return value

    @field_validator("analysis_progress_update_interval")
    @classmethod
    def _bounded_progress_interval(cls, value: int) -> int:
        if value < 1:
            # 1 = drain after every page (pre-5.2 behavior); 0 or negative
            # would never re-arm the interim drain - rejected loudly.
            raise ValueError("ANALYSIS_PROGRESS_UPDATE_INTERVAL must be >= 1")
        if value > 10000:
            # Upper bound mirrors SENTIMENT_INFERENCE_BATCH_SIZE: beyond
            # this the interval stops being a progress cadence and starts
            # deferring analysis to the final drain (§10 still requires
            # progressive results for large datasets).
            raise ValueError("ANALYSIS_PROGRESS_UPDATE_INTERVAL must be <= 10000")
        return value

    @field_validator("max_api_pages")
    @classmethod
    def _bounded_pages(cls, value: int) -> int:
        if value < 1:
            raise ValueError("MAX_API_PAGES must be >= 1")
        return value

    @field_validator("youtube_retry_attempts")
    @classmethod
    def _bounded_retry_attempts(cls, value: int) -> int:
        if value < 1:
            raise ValueError("YOUTUBE_RETRY_ATTEMPTS must be >= 1")
        if value > 5:
            # A safety valve, not a tuning knob: 5 attempts already span
            # 7.5s of backoff at the 0.5s base - more would turn a transient
            # blip into a long-holding request (§17: never retry forever).
            raise ValueError("YOUTUBE_RETRY_ATTEMPTS must be <= 5")
        return value

    @field_validator("youtube_retry_backoff_seconds")
    @classmethod
    def _bounded_retry_backoff(cls, value: float) -> float:
        if value < 0:
            raise ValueError("YOUTUBE_RETRY_BACKOFF_SECONDS must be >= 0")
        if value > 30:
            raise ValueError("YOUTUBE_RETRY_BACKOFF_SECONDS must be <= 30")
        return value

    @field_validator("insight_provider")
    @classmethod
    def _bounded_insight_provider(cls, value: str) -> str:
        allowed = {"deterministic", "openai_compatible"}
        if value not in allowed:
            # Unknown providers must fail loudly instead of silently
            # producing unvalidated output paths.
            raise ValueError(
                f"INSIGHT_PROVIDER must be one of {sorted(allowed)}"
            )
        return value

    @field_validator("insight_timeout_seconds")
    @classmethod
    def _bounded_insight_timeout(cls, value: float) -> float:
        if value < 1:
            raise ValueError("INSIGHT_TIMEOUT_SECONDS must be >= 1")
        if value > 120:
            # The insight never blocks the analysis flow (§24/§26): even
            # the most patient setting stays well under a job phase.
            raise ValueError("INSIGHT_TIMEOUT_SECONDS must be <= 120")
        return value

    @field_validator("insight_retry_attempts")
    @classmethod
    def _bounded_insight_attempts(cls, value: int) -> int:
        if value < 1:
            raise ValueError("INSIGHT_RETRY_ATTEMPTS must be >= 1")
        if value > 5:
            # Bounded retries (§26): 5 attempts x 120s worst case would
            # already be pathological - refuse instead of allowing it.
            raise ValueError("INSIGHT_RETRY_ATTEMPTS must be <= 5")
        return value

    @field_validator("insight_max_output_tokens")
    @classmethod
    def _bounded_insight_tokens(cls, value: int) -> int:
        if value < 64:
            # Below the structured schema's floor the model physically
            # cannot return all fields - reject instead of guaranteeing
            # a fallback on every call.
            raise ValueError("INSIGHT_MAX_OUTPUT_TOKENS must be >= 64")
        if value > 4096:
            # Briefing, not essay (§17): the output schema is 6 short
            # strings - nothing legitimate needs more than this.
            raise ValueError("INSIGHT_MAX_OUTPUT_TOKENS must be <= 4096")
        return value

    @field_validator("topic_min_comment_count")
    @classmethod
    def _bounded_topic_min_comments(cls, value: int) -> int:
        if value < 1:
            raise ValueError("TOPIC_MIN_COMMENT_COUNT must be >= 1")
        if value > 10000:
            # Not a tuning knob beyond the full dataset cap: a threshold
            # above COMMENT_ACQUISITION_MAX_COMMENTS would disable topic
            # analysis entirely instead of configuring it.
            raise ValueError("TOPIC_MIN_COMMENT_COUNT must be <= 10000")
        return value

    @field_validator("topic_min_support")
    @classmethod
    def _bounded_topic_min_support(cls, value: int) -> int:
        if value < 1:
            raise ValueError("TOPIC_MIN_SUPPORT must be >= 1")
        if value > 1000:
            raise ValueError("TOPIC_MIN_SUPPORT must be <= 1000")
        return value

    @field_validator("topic_max_topics")
    @classmethod
    def _bounded_topic_max(cls, value: int) -> int:
        if value < 1:
            raise ValueError("TOPIC_MAX_TOPICS must be >= 1")
        if value > 100:
            # Payload/UX bound: beyond ~100 themes the discovery has stopped
            # finding signal (TOPIC_MIN_SUPPORT gates would have cut them)
            # and the overlay list becomes unreadable anyway.
            raise ValueError("TOPIC_MAX_TOPICS must be <= 100")
        return value

    @field_validator("dataset_cache_ttl_seconds")
    @classmethod
    def _bounded_freshness(cls, value: int) -> int:
        if value < 0:
            raise ValueError("DATASET_CACHE_TTL_SECONDS must be >= 0")
        return value

    @field_validator("realtime_poll_interval_seconds")
    @classmethod
    def _bounded_realtime_poll(cls, value: float) -> float:
        if value < 1:
            raise ValueError("REALTIME_POLL_INTERVAL_SECONDS must be >= 1")
        if value > 3600:
            raise ValueError("REALTIME_POLL_INTERVAL_SECONDS must be <= 3600")
        return value

    @field_validator("realtime_min_poll_interval_seconds")
    @classmethod
    def _bounded_realtime_min(cls, value: float) -> float:
        if value < 1:
            raise ValueError("REALTIME_MIN_POLL_INTERVAL_SECONDS must be >= 1")
        if value > 3600:
            raise ValueError("REALTIME_MIN_POLL_INTERVAL_SECONDS must be <= 3600")
        return value

    @field_validator("realtime_max_poll_interval_seconds")
    @classmethod
    def _bounded_realtime_max(cls, value: float) -> float:
        if value < 1:
            raise ValueError("REALTIME_MAX_POLL_INTERVAL_SECONDS must be >= 1")
        if value > 3600:
            raise ValueError("REALTIME_MAX_POLL_INTERVAL_SECONDS must be <= 3600")
        return value

    @field_validator("realtime_trend_min_change")
    @classmethod
    def _bounded_realtime_trend(cls, value: float) -> float:
        if value < 0:
            raise ValueError("REALTIME_TREND_MIN_CHANGE must be >= 0")
        if value > 100:
            # Percentage-point noise floor: beyond 100 the trend could never
            # fire at all - refuse instead of silently disabling movement.
            raise ValueError("REALTIME_TREND_MIN_CHANGE must be <= 100")
        return value

    @field_validator("realtime_insight_min_new_analyzed")
    @classmethod
    def _bounded_realtime_insight(cls, value: int) -> int:
        if value < 1:
            raise ValueError("REALTIME_INSIGHT_MIN_NEW_ANALYZED must be >= 1")
        if value > 10000:
            # Not a tuning knob beyond the dataset cap: a threshold above
            # COMMENT_ACQUISITION_MAX_COMMENTS would disable regeneration
            # entirely instead of configuring it.
            raise ValueError("REALTIME_INSIGHT_MIN_NEW_ANALYZED must be <= 10000")
        return value

    @model_validator(mode="after")
    def _realtime_bounds_sane(self) -> "Settings":
        # Cross-field rule: an inverted [min, max] window would make the
        # use-site clamp ambiguous - reject loudly instead.
        if self.realtime_min_poll_interval_seconds > self.realtime_max_poll_interval_seconds:
            raise ValueError(
                "REALTIME_MIN_POLL_INTERVAL_SECONDS must be <= "
                "REALTIME_MAX_POLL_INTERVAL_SECONDS"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

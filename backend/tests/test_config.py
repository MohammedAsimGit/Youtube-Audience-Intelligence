"""Settings / environment parsing regressions.

The documented `.env.example` format for CORS_ORIGINS is a plain
comma-separated string. pydantic-settings 2.x JSON-decodes complex fields
from dotenv sources, which crashed Settings() at import (and therefore the
entire test suite) until cors_origins gained NoDecode + a before-validator.
These tests pin both accepted formats.
"""
import json

import pytest
from pydantic import ValidationError

from app.core.config import Settings


class TestCorsOriginsEnvParsing:
    def test_comma_separated_documented_format_parses(self):
        settings = Settings(
            cors_origins="chrome-extension://,http://localhost:5173,http://127.0.0.1:5173"
        )
        assert settings.cors_origins == [
            "chrome-extension://",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]

    def test_json_array_format_still_parses(self):
        settings = Settings(cors_origins=json.dumps(["http://localhost:5173"]))
        assert settings.cors_origins == ["http://localhost:5173"]

    def test_programmatic_list_untouched(self):
        settings = Settings(cors_origins=["chrome-extension://"])
        assert settings.cors_origins == ["chrome-extension://"]

    def test_whitespace_and_empty_segments_dropped(self):
        settings = Settings(cors_origins=" a , , b ")
        assert settings.cors_origins == ["a", "b"]

    def test_empty_string_is_empty_list(self):
        assert Settings(cors_origins="").cors_origins == []

    def test_invalid_json_list_rejected(self):
        # Starts with '[' but is not valid JSON -> loud failure, not silence.
        with pytest.raises((ValidationError, ValueError)):
            Settings(cors_origins="[not-json")


class TestRealtimeSettings:
    def test_defaults_match_env_example(self):
        settings = Settings()
        assert settings.realtime_enabled is True
        assert settings.realtime_poll_interval_seconds == 30.0
        assert settings.realtime_min_poll_interval_seconds == 15.0
        assert settings.realtime_max_poll_interval_seconds == 120.0
        assert settings.realtime_trend_min_change == 1.0
        assert settings.realtime_insight_min_new_analyzed == 25

    def test_env_file_realtime_block_parses(self):
        # The real backend/.env carries the Sprint 8 block; Settings() must
        # load it without raising (this exact import path used to crash).
        settings = Settings()
        assert 1 <= settings.realtime_poll_interval_seconds <= 3600

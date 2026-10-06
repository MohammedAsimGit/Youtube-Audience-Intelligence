"""Unit tests for the cache boundary and ingestion-safe normalization."""
from app.services.cache import TTLCache
from app.utils.normalize import collapse_whitespace, parse_dt, parse_int


class TestTTLCache:
    def test_set_then_get(self):
        cache = TTLCache(ttl_seconds=60, max_entries=4)
        cache.set("k", "v")
        assert cache.get("k") == "v"

    def test_expiry(self):
        now = [0.0]
        cache = TTLCache(ttl_seconds=10, max_entries=4, clock=lambda: now[0])
        cache.set("k", "v")
        now[0] = 9.0
        assert cache.get("k") == "v"
        now[0] = 10.0
        assert cache.get("k") is None  # expired entry removed

    def test_lru_eviction_at_max_entries(self):
        cache = TTLCache(ttl_seconds=60, max_entries=2)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.get("a")  # refresh A
        cache.set("c", 3)  # evicts LRU (b)
        assert cache.get("b") is None
        assert cache.get("a") == 1
        assert cache.get("c") == 3
        assert len(cache) == 2

    def test_miss_returns_none(self):
        cache = TTLCache(ttl_seconds=60, max_entries=4)
        assert cache.get("missing") is None


class TestNormalize:
    def test_collapse_whitespace_unicode_safe(self):
        assert collapse_whitespace("  hello \n\n  world \t ") == "hello world"
        assert collapse_whitespace("\u00a0nbsp\u00a0") == "nbsp"  # NBSP handled

    def test_parse_int_from_youtube_string(self):
        assert parse_int("12345") == 12345
        assert parse_int(42) == 42
        assert parse_int("not-a-number") is None
        assert parse_int(None) is None
        assert parse_int(True) is None  # bool is not a counter

    def test_parse_dt_handles_zulu_and_garbage(self):
        parsed = parse_dt("2024-03-01T10:00:00Z")
        assert parsed is not None and parsed.year == 2024
        assert parse_dt("not-a-date") is None
        assert parse_dt(None) is None

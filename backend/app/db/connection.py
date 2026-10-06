"""SQLite connection management (stdlib ``sqlite3`` - zero extra drivers).

Concurrency model: ONE lazily-opened connection shared behind an RLock.
Rationale (docs/architecture/database.md):
- FastAPI runs sync endpoints in a threadpool -> the connection must be
  cross-thread (``check_same_thread=False``); SQLite serializes writers
  anyway, so a lock matches the real hardware and keeps dev-deploy simple.
- A single shared connection makes ``sqlite:///:memory:`` behave like a
  real database in tests (every query sees the same in-memory store).
- Lazy connection creation means merely importing the app (uvicorn
  entrypoint, test collection) never touches the filesystem; the schema is
  created idempotently on first use.
"""
import os
import sqlite3
import threading
from typing import Optional

from app.db.schema import SCHEMA_SQL, migrate

_MEMORY = ":memory:"
_SUPPORTED_PREFIX = "sqlite:///"


class UnsupportedDatabaseUrl(ValueError):
    """DATABASE_URL uses a scheme this build cannot open."""


def parse_sqlite_url(url: str) -> str:
    """``sqlite:///relative/path.db`` | ``sqlite:///:memory:`` -> sqlite path.

    Only SQLite is supported by this build; anything else raises with a clear
    message instead of silently connecting somewhere unexpected.
    """
    cleaned = (url or "").strip()
    if cleaned in ("sqlite://", "sqlite:///:memory:", "sqlite://:memory:"):
        return _MEMORY
    if cleaned.startswith(_SUPPORTED_PREFIX):
        path = cleaned[len(_SUPPORTED_PREFIX):]
        if path:
            return path
    raise UnsupportedDatabaseUrl(
        "DATABASE_URL must look like 'sqlite:///./data/sentiment.db' or "
        f"'sqlite:///:memory:' (got {url!r})"
    )


class Database:
    """Thread-safe lazily-initialized SQLite connection + schema bootstrap."""

    def __init__(self, url: str) -> None:
        self._path = parse_sqlite_url(url)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def path(self) -> str:
        return self._path

    def connection(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                if self._path != _MEMORY:
                    parent = os.path.dirname(os.path.abspath(self._path))
                    os.makedirs(parent, exist_ok=True)
                conn = sqlite3.connect(self._path, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys = ON")
                migrate(conn)  # additive columns for pre-Sprint-4 databases
                conn.executescript(SCHEMA_SQL)
                conn.commit()
                self._conn = conn
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

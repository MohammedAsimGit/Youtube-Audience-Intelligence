"""Sprint 4.4 tests: local backend launcher (native messaging host).

Covers §34 (idempotent start), §35 (health endpoint decides readiness, not
"port open"), §33 (strict closed protocol, invalid messages rejected),
§16 (never a second backend), and the Chrome native-messaging framing used
on the stdio channel. The host is stdlib-only and imported as a package so
these tests run anywhere (no Chrome, no registry, no network beyond loopback).
"""
import io
import json
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

# The launcher lives at the repo root (sibling of backend/); import it as a
# package without touching developer-specific paths.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from launcher import native_host  # noqa: E402


class FakeHealth:
    """Scripted health answers; records every probe origin."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def __call__(self, origin, timeout):
        self.calls += 1
        if len(self.answers) > 1:
            return self.answers.pop(0)
        return self.answers[0]


class FakeSpawn:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, host, port, **kwargs):
        self.calls.append((host, port, kwargs))
        if self.fail:
            raise OSError("cannot spawn")
        return object()


# ------------------------------------------------------------------ protocol
class TestProtocol:
    def test_unknown_action_is_rejected_never_executed(self):
        response = native_host.handle_message({"action": "rm -rf /"})
        assert response == {"ok": False, "reason": "unknown_action"}

    def test_non_object_message_is_rejected(self):
        assert native_host.handle_message(["ensure_backend"])["ok"] is False
        assert native_host.handle_message(None)["reason"] == "invalid_message"
        assert native_host.handle_message("ensure_backend")["ok"] is False

    def test_missing_action_is_rejected(self):
        assert native_host.handle_message({})["reason"] == "unknown_action"

    def test_invalid_port_is_rejected(self):
        response = native_host.handle_message(
            {"action": "ensure_backend", "port": "not-a-port"}
        )
        assert response == {"ok": False, "reason": "invalid_message"}

    def test_ensure_backend_action_wires_into_ensure(self, monkeypatch):
        seen = {}

        def fake_ensure(host, port, **kwargs):
            seen["host"] = host
            seen["port"] = port
            return {"ok": True, "outcome": "already_running", "port": port}

        monkeypatch.setattr(native_host, "ensure_backend", fake_ensure)
        monkeypatch.setenv("BACKEND_PORT", "9001")
        response = native_host.handle_message({"action": "ensure_backend"})
        assert response["ok"] is True
        assert seen["port"] == 9001  # §31: canonical port, env-overridable


# -------------------------------------------------------------- idempotency
class TestEnsureBackend:
    def test_reuses_a_healthy_backend_without_spawning(self):
        """§34: already running -> reuse, never start a second instance."""
        health = FakeHealth([True])
        spawn = FakeSpawn()
        result = native_host.ensure_backend(health=health, spawn=spawn)
        assert result == {"ok": True, "outcome": "already_running", "port": 8000}
        assert spawn.calls == []  # §16: no second backend, ever

    def test_starts_then_waits_for_health(self):
        health = FakeHealth([False, False, True])
        spawn = FakeSpawn()
        result = native_host.ensure_backend(
            health=health,
            spawn=spawn,
            port_probe=lambda h, p: False,
            poll_timeout=2.0,
            poll_interval=0.0,
        )
        assert result["ok"] is True
        assert result["outcome"] == "started"
        assert len(spawn.calls) == 1
        assert spawn.calls[0][0] == "127.0.0.1"
        assert spawn.calls[0][1] == 8000

    def test_port_open_but_unhealthy_fails_fast(self):
        """§35: 'port open' is NOT 'backend healthy' - fail, don't spawn."""
        health = FakeHealth([False])
        spawn = FakeSpawn()
        result = native_host.ensure_backend(
            health=health, spawn=spawn, port_probe=lambda h, p: True
        )
        assert result == {"ok": False, "reason": "port_in_use", "port": 8000}
        assert spawn.calls == []

    def test_bounded_wait_reports_timeout(self):
        health = FakeHealth([False])
        spawn = FakeSpawn()
        result = native_host.ensure_backend(
            health=health,
            spawn=spawn,
            port_probe=lambda h, p: False,
            poll_timeout=0.05,
            poll_interval=0.01,
        )
        assert result["ok"] is False
        assert result["reason"] == "timeout"
        assert len(spawn.calls) == 1  # one attempt, then bounded give-up

    def test_spawn_failure_is_reported_not_raised(self):
        health = FakeHealth([False])
        result = native_host.ensure_backend(
            health=health, spawn=FakeSpawn(fail=True), port_probe=lambda h, p: False
        )
        assert result == {"ok": False, "reason": "spawn_failed", "port": 8000}

    def test_repeated_calls_stay_idempotent(self):
        health = FakeHealth([True])
        spawn = FakeSpawn()
        first = native_host.ensure_backend(health=health, spawn=spawn)
        second = native_host.ensure_backend(health=health, spawn=spawn)
        assert first["outcome"] == second["outcome"] == "already_running"
        assert spawn.calls == []


# -------------------------------------------------------------- health probe
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/health":
            body = json.dumps({"status": "ok", "version": "0.4.4"}).encode()
            self.send_response(200)
        else:
            body = b"{}"
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep test output clean
        pass


class TestHealthEndpoint:
    @pytest.fixture()
    def server(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{httpd.server_address[1]}/health"
        httpd.shutdown()
        httpd.server_close()

    def test_real_health_endpoint_is_recognized(self, server):
        # Hits the SAME endpoint shape the FastAPI app exposes (§14).
        assert native_host.check_health(server, timeout=2.0) is True

    def test_unreachable_endpoint_is_not_healthy(self):
        assert native_host.check_health("http://127.0.0.1:9/health", timeout=0.3) is False

    def test_non_health_path_is_not_healthy(self, server):
        assert native_host.check_health(server.replace("/health", "/nope")) is False


# ------------------------------------------------------------------ framing
class TestNativeMessagingFraming:
    @staticmethod
    def _frame(payload: dict) -> bytes:
        body = json.dumps(payload).encode("utf-8")
        return struct.pack("<I", len(body)) + body

    def test_round_trip(self):
        stream = io.BytesIO(self._frame({"action": "ensure_backend"}))
        message = native_host._read_message(stream)
        assert message == {"action": "ensure_backend"}

        out = io.BytesIO()
        native_host._write_message(out, {"ok": True, "outcome": "started"})
        raw = out.getvalue()
        (length,) = struct.unpack("<I", raw[:4])
        assert json.loads(raw[4 : 4 + length]) == {"ok": True, "outcome": "started"}

    def test_truncated_stream_reads_none(self):
        assert native_host._read_message(io.BytesIO(b"\x05\x00")) is None
        assert native_host._read_message(io.BytesIO(b"")) is None

    def test_oversized_frame_is_rejected(self):
        huge = struct.pack("<I", native_host.MAX_MESSAGE_BYTES + 1)
        assert native_host._read_message(io.BytesIO(huge)) is None

    def test_malformed_json_reads_none(self):
        body = b"not-json"
        stream = io.BytesIO(struct.pack("<I", len(body)) + body)
        assert native_host._read_message(stream) is None

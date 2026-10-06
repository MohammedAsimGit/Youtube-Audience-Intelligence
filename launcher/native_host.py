"""Chrome Native Messaging host: ensure the local FastAPI backend is running.

Sprint 4.4 (§9–§16). This is the ONLY process-spawning component in the
system; the extension itself can never execute shell commands (browser
sandbox). The extension talks to this host through Chrome's supported
Native Messaging channel with a strict, closed protocol:

    extension -> {"action": "ensure_backend"}
    host     -> {"ok": true,  "outcome": "already_running" | "started", "port": 8000}
             | {"ok": false, "reason": "port_in_use" | "timeout" | "spawn_failed" | ...}

Design rules honored here:
- §34 idempotent: a healthy backend on the configured port is reused,
  never restarted, never duplicated.
- §35: readiness is decided by the real GET /health endpoint, not by
  "the port is open".
- §12: the backend starts DETACHED (background process, no terminal
  window); normal user operation needs no open shell.
- §31/§36: canonical 127.0.0.1:8000 (override via BACKEND_HOST /
  BACKEND_PORT env for BOTH this host and the extension origin); all
  paths are computed relative to this file - no developer-specific
  absolute paths anywhere.
- §33: only the fixed `ensure_backend` action is understood; messages
  are validated, arbitrary commands are rejected (never executed).
- Stdlib only; runs under the project venv python chosen by host.bat.
"""
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

HOST_NAME = "com.sentiment_ai.backend"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
MAX_MESSAGE_BYTES = 1024 * 1024  # Chrome native-messaging hard limit
DEFAULT_POLL_TIMEOUT = 20.0
DEFAULT_POLL_INTERVAL = 0.25

# Windows: start the backend fully detached (no console window, no
# terminal required - §12) and survive the native host's own exit.
if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    _DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
    )
else:  # pragma: no cover - POSIX dev fallback
    _DETACHED = 0

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
LOG_DIR = BACKEND_DIR / "data"


# --------------------------------------------------------------------- health
def check_health(origin: str, timeout: float = 2.0) -> bool:
    """True only when GET /health answers ok (§35: not just 'port open')."""
    try:
        with urllib.request.urlopen(origin, timeout=timeout) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
            return isinstance(payload, dict) and payload.get("status") == "ok"
    except (urllib.error.URLError, ValueError, OSError):
        return False


def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------- spawn
def spawn_backend(
    host: str,
    port: int,
    backend_dir: Path = BACKEND_DIR,
    log_path: Optional[Path] = None,
    python: str = sys.executable,
) -> "subprocess.Popen[bytes]":
    """Start ONE detached uvicorn process for the existing FastAPI app.

    The backend itself is untouched - this only launches it the same way a
    developer would (`python -m uvicorn app.main:app`), from the same
    working directory so `DATABASE_URL`/`.env` resolve identically.
    """
    if log_path is None:
        log_path = LOG_DIR / "launcher-uvicorn.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "ab")  # noqa: SIM115 - handle outlives this process
    try:
        return subprocess.Popen(
            [python, "-m", "uvicorn", "app.main:app", "--host", host, "--port", str(port)],
            cwd=str(backend_dir),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            creationflags=_DETACHED,
            close_fds=True,
        )
    finally:
        log_file.close()


def ensure_backend(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    health: Callable[[str, float], bool] = check_health,
    port_probe: Callable[[str, int], bool] = port_open,
    spawn: Callable[..., object] = spawn_backend,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    backend_dir: Path = BACKEND_DIR,
) -> dict:
    """Idempotent: reuse a healthy backend, start one only when needed."""
    origin = f"http://{host}:{port}/health"
    if health(origin, 2.0):
        return {"ok": True, "outcome": "already_running", "port": port}
    if port_probe(host, port):
        # Something is listening but does not answer /health - starting a
        # second backend would fail on the bind anyway (§34/§35).
        return {"ok": False, "reason": "port_in_use", "port": port}
    try:
        spawn(host, port, backend_dir=backend_dir)
    except (OSError, subprocess.SubprocessError):
        return {"ok": False, "reason": "spawn_failed", "port": port}

    deadline = time.monotonic() + poll_timeout
    while time.monotonic() < deadline:
        if health(origin, 1.0):
            return {"ok": True, "outcome": "started", "port": port}
        time.sleep(poll_interval)
    return {"ok": False, "reason": "timeout", "port": port}


# ------------------------------------------------------------------ protocol
def handle_message(message: object) -> dict:
    """Strict, closed command protocol (§33): no arbitrary commands."""
    if not isinstance(message, dict):
        return {"ok": False, "reason": "invalid_message"}
    action = message.get("action")
    if action == "ensure_backend":
        host = str(message.get("host") or os.environ.get("BACKEND_HOST") or DEFAULT_HOST)
        try:
            port = int(message.get("port") or os.environ.get("BACKEND_PORT") or DEFAULT_PORT)
        except (TypeError, ValueError):
            return {"ok": False, "reason": "invalid_message"}
        return ensure_backend(host, port)
    return {"ok": False, "reason": "unknown_action"}


# --------------------------------------------------------- native-messaging IO
def _read_message(stream) -> Optional[object]:
    """4-byte little-endian length prefix + UTF-8 JSON (Chrome framing)."""
    header = stream.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack("<I", header)
    if length > MAX_MESSAGE_BYTES:
        return None
    body = stream.read(length)
    if len(body) < length:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _write_message(stream, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    stream.write(struct.pack("<I", len(body)) + body)
    stream.flush()


def main() -> int:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        try:
            message = _read_message(stdin)
        except OSError:
            break
        if message is None:
            break  # Chrome closed the pipe (or malformed frame): exit cleanly
        response = handle_message(message)
        try:
            _write_message(stdout, response)
        except OSError:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

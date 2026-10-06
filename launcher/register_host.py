#!/usr/bin/env python3
"""One-time registration of the Sprint 4.4 native messaging host (Windows).

Writes (computed at runtime - no developer-specific paths live in the repo):

  1. launcher/native-host-manifest.json   (name/path/allowed_origins)
  2. HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts\\com.sentiment_ai.backend
     (default value = absolute path of the manifest)

Usage (from the repo root, once per machine):

    python launcher/register_host.py --extension-id <32-char-extension-id>

    # remove again:
    python launcher/register_host.py --unregister

The extension id comes from chrome://extensions after loading `dist/`
(unpacked ids are deterministic per install path). Only that exact origin
is allowed to talk to the host (§33).
"""
import argparse
import json
import re
import sys
from pathlib import Path

HOST_NAME = "com.sentiment_ai.backend"
LAUNCHER_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = LAUNCHER_DIR / "native-host-manifest.json"
EXTENSION_ID = re.compile(r"^[a-p]{32}$")  # Chrome extension ids: base16 a-p

REGISTRY_PATH = (
    r"Software\Google\Chrome\NativeMessagingHosts",
    HOST_NAME,
)


def build_manifest(extension_id: str) -> dict:
    # `path` may be relative to the manifest's directory on Windows, so the
    # manifest itself stays portable inside the repo checkout.
    return {
        "name": HOST_NAME,
        "description": "Sentiment AI local backend launcher (ensure_backend only)",
        "path": "host.bat",
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id}/"],
    }


def register(extension_id: str) -> None:
    import winreg  # Windows-only, imported lazily (clear error elsewhere)

    MANIFEST_PATH.write_text(json.dumps(build_manifest(extension_id), indent=2), encoding="utf-8")
    key_path = f"{REGISTRY_PATH[0]}\\{REGISTRY_PATH[1]}"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, str(MANIFEST_PATH))
    print(f"registered {HOST_NAME}")
    print(f"  manifest: {MANIFEST_PATH}")
    print(f"  allowed extension: chrome-extension://{extension_id}/")


def unregister() -> None:
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"{REGISTRY_PATH[0]}\{REGISTRY_PATH[1]}")
        print(f"unregistered {HOST_NAME}")
    except FileNotFoundError:
        print("nothing to unregister")
    if MANIFEST_PATH.exists():
        MANIFEST_PATH.unlink()
        print("manifest removed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extension-id", help="32-char Chrome extension id (a-p)")
    parser.add_argument("--unregister", action="store_true")
    args = parser.parse_args()

    if sys.platform != "win32":
        print("native host registration is Windows-only in this project", file=sys.stderr)
        return 1
    if args.unregister:
        unregister()
        return 0
    if not args.extension_id or not EXTENSION_ID.fullmatch(args.extension_id):
        parser.error("--extension-id must be 32 chars from a-p (see chrome://extensions)")
    register(args.extension_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

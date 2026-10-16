"""Open a dedicated Chrome window for the local Selenium tracker.

Examples (PowerShell):
    python open_chrome.py
    python open_chrome.py --url https://your-authorized-test-site.example

Chrome listens for DevTools connections on 127.0.0.1 only, using port 9223
by default. A separate browser profile is stored outside this project folder.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen


DEFAULT_PORT = 9223
DEFAULT_URL = "about:blank"


def find_chrome() -> Path | None:
    """Find Google Chrome at common Windows install locations."""
    program_files = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    program_files_x86 = Path(
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    )
    local_app_data = Path(
        os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    )

    candidates = [
        program_files / "Google" / "Chrome" / "Application" / "chrome.exe",
        program_files_x86 / "Google" / "Chrome" / "Application" / "chrome.exe",
        local_app_data / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]

    on_path = shutil.which("chrome.exe") or shutil.which("chrome")
    if on_path:
        candidates.append(Path(on_path))

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).casefold()
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate
    return None


def validate_url(value: str) -> str:
    """Allow a blank tab or an ordinary HTTP(S) page URL."""
    value = value.strip()
    if value == "about:blank":
        return value
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise argparse.ArgumentTypeError(
            "URL must be about:blank or start with http:// or https://"
        )
    if parsed.username or parsed.password:
        raise argparse.ArgumentTypeError("Do not put usernames or passwords in the URL.")
    return value


def debugger_is_available(port: int) -> bool:
    endpoint = f"http://127.0.0.1:{port}/json/version"
    try:
        with urlopen(endpoint, timeout=1.0) as response:
            return response.status == 200
    except Exception:
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch Chrome for the local Selenium tracker."
    )
    parser.add_argument(
        "--url",
        type=validate_url,
        default=DEFAULT_URL,
        help="Optional page to open (default: about:blank).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Chrome DevTools port on 127.0.0.1 (default: {DEFAULT_PORT}).",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return args


def main() -> int:
    if sys.platform != "win32":
        print("[ERROR] This launcher is currently configured for Windows.", file=sys.stderr)
        return 2

    args = parse_args()
    chrome = find_chrome()
    if chrome is None:
        print(
            "[ERROR] Google Chrome was not found in the standard Windows locations. "
            "Install Chrome or add chrome.exe to PATH.",
            file=sys.stderr,
        )
        return 2

    if debugger_is_available(args.port):
        print(f"[INFO] Chrome DevTools is already available on 127.0.0.1:{args.port}.")
        print("[INFO] Use the existing debugging Chrome window; no second window was started.")
        return 0

    local_app_data = Path(
        os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    )
    profile_dir = local_app_data / "selenium0-chrome-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    command = [
        str(chrome),
        f"--remote-debugging-port={args.port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        args.url,
    ]

    try:
        subprocess.Popen(command, close_fds=True)
    except OSError as exc:
        print(f"[ERROR] Chrome could not be launched: {exc}", file=sys.stderr)
        return 2

    print("[CHROME] Browser launch requested.")
    print(f"[CHROME] DevTools endpoint: http://127.0.0.1:{args.port}")
    print("[CHROME] Open your authorized test site in this browser window.")
    print("[CHROME] Sign in manually if the test site requires it.")
    print("[CHROME] Keep this Chrome window open while the tracker is running.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

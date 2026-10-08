#!/usr/bin/env python3
"""One command to run everything: `python start.py`.

Creates .venv if needed, installs requirements.txt (only when it changed), initialises SQLite, starts the mock
vendor-risk service and the copilot app as child processes, waits for both health checks and prints the URL.
Ctrl+C stops both. `--check` starts, verifies health and exits (used for smoke tests and CI).
Works on Windows, macOS and Linux with Python 3.11+. No API key needed: without one, starter requests replay
their recorded evaluation run and new requests get a deterministic-only decision handed to a human.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import venv
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def ensure_environment(skip_install: bool) -> None:
    """Make sure we run inside .venv with current requirements; re-launch this script there if not."""
    if sys.version_info < (3, 11):
        sys.exit(f"Python 3.11+ required (found {sys.version.split()[0]})")
    if not venv_python().exists():
        print("Creating virtual environment in .venv ...")
        venv.create(VENV, with_pip=True)
    stamp = VENV / ".requirements.sha256"
    digest = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    if not skip_install and (not stamp.exists() or stamp.read_text().strip() != digest):
        print("Installing requirements (first run only) ...")
        subprocess.check_call([str(venv_python()), "-m", "pip", "install", "-q", "--disable-pip-version-check",
                               "-r", str(REQUIREMENTS)])
        stamp.write_text(digest)
    if Path(sys.prefix).resolve() != VENV.resolve():
        sys.exit(subprocess.call([str(venv_python()), str(Path(__file__).resolve()), *sys.argv[1:], "--no-install"]))


def healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1.0) as resp:
            return resp.status == 200
    except OSError:
        return False


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def wait_healthy(url: str, proc: subprocess.Popen, name: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{name} exited during startup (code {proc.returncode}); see output above")
        if healthy(url):
            return
        time.sleep(0.25)
    raise RuntimeError(f"{name} did not become healthy within {timeout:.0f}s: {url}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the procurement copilot")
    parser.add_argument("--check", action="store_true", help="start, verify health, then stop")
    parser.add_argument("--no-install", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    ensure_environment(args.no_install)

    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    from src.config import get_settings  # noqa: E402  (imported after the venv is guaranteed; loads .env)
    from src.store import Store  # noqa: E402

    settings = get_settings()
    Store(settings.db_path)  # initialise SQLite
    vendor = urlparse(settings.vendor_service_url)
    vendor_port, app_port = vendor.port or 8001, settings.app_port
    procs: list[subprocess.Popen] = []

    def spawn(module: str, port: int) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1", "--port", str(port),
                                 "--log-level", "warning"], cwd=ROOT)
        procs.append(proc)
        return proc

    def stop(*_: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        vendor_health = f"http://127.0.0.1:{vendor_port}/health"
        if healthy(vendor_health):
            print(f"Vendor-risk service already running on :{vendor_port} - reusing it.")
        elif not port_free(vendor_port):
            raise RuntimeError(f"Port {vendor_port} is in use by another program (set VENDOR_SERVICE_URL)")
        else:
            wait_healthy(vendor_health, spawn("mock_api.app:app", vendor_port), "Vendor-risk service")
        if not port_free(app_port):
            raise RuntimeError(f"Port {app_port} is in use (set APP_PORT in .env)")
        wait_healthy(f"http://127.0.0.1:{app_port}/api/health", spawn("src.web.main:app", app_port), "Copilot app")

        mode = (f"live LLM: {settings.llm_model}" if settings.llm_configured else
                "no API key: starter requests replay recorded runs; new requests get deterministic checks only")
        print(f"\n  Procurement Copilot ready:  http://localhost:{app_port}\n  Vendor-risk service:        "
              f"http://127.0.0.1:{vendor_port}\n  Mode: {mode}\n  Press Ctrl+C to stop.\n")
        if args.check:
            return 0
        while True:
            time.sleep(1)
            for proc in procs:
                if proc.poll() is not None:
                    raise RuntimeError(f"a service exited with code {proc.returncode}")
    except KeyboardInterrupt:
        print("\nStopping ...")
        return 0
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())

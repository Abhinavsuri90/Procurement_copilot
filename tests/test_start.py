"""The one-command launcher starts both services, passes health checks and shuts down cleanly."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_start_check_mode_brings_up_both_services():
    env = {**os.environ, "APP_PORT": "18765", "VENDOR_SERVICE_URL": "http://127.0.0.1:18766",
           "DB_PATH": "var/test-start.db", "LLM_API_KEY": ""}
    out = subprocess.run([sys.executable, "start.py", "--check", "--no-install"], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=90)
    assert out.returncode == 0, out.stderr
    assert "http://localhost:18765" in out.stdout and "no API key" in out.stdout

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LITERATURE = ROOT / "scripts" / "literature.py"


def test_search_help_renders_successfully():
    result = subprocess.run(
        [sys.executable, str(LITERATURE), "search", "--help"],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "--async-search" in result.stdout
    assert "--enable-fallback" in result.stdout

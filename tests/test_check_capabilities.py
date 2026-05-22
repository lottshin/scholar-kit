import argparse
import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import literature  # noqa: E402


def run_check(monkeypatch, tmp_path, *, driver_ok=False, cnki_ok=True):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    monkeypatch.setattr(literature, "_check_browser", lambda _subprocess: (True, "Edge"))
    monkeypatch.setattr(literature, "_check_driver", lambda: (driver_ok, "driver missing"))
    monkeypatch.setattr(literature, "_check_cnki", lambda: (cnki_ok, "accessible" if cnki_ok else "blocked"))
    monkeypatch.setattr(literature, "_check_update", lambda: None)

    out = io.StringIO()
    with redirect_stdout(out):
        literature.cmd_check(argparse.Namespace(fix=False))
    return json.loads(out.getvalue())


def test_codex_sandbox_driver_failure_requests_escalation(monkeypatch, tmp_path):
    data = run_check(monkeypatch, tmp_path, driver_ok=False)

    caps = data["capabilities"]
    assert caps["cnki_feasible"] is False
    assert caps["needs_escalation"] is True
    assert caps["reason_code"] == "browser_or_driver_requires_host_permission"
    assert caps["agent_action"] == "rerun_same_command_with_escalated_permissions"
    assert "retry_command" in caps


def test_project_local_selenium_cache_is_preferred_in_codex(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    monkeypatch.delenv("SE_CACHE_PATH", raising=False)

    from cnki import driver

    driver._ensure_selenium_cache()

    expected = tmp_path / ".scholar-kit" / "selenium-cache"
    assert os.environ["SE_CACHE_PATH"] == str(expected)
    assert expected.is_dir()


def test_local_development_version_newer_than_remote_is_not_update(monkeypatch):
    monkeypatch.setattr(literature, "__version__", "1.12.1")
    monkeypatch.setattr(
        literature,
        "_fetch_json",
        lambda url, timeout=5: {"tag_name": "v1.12.0"},
    )

    update = literature._check_update()

    assert update == {
        "update_available": False,
        "current": "1.12.1",
        "latest": "1.12.0",
    }

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import literature  # noqa: E402
from workflows import render_workflow_argv  # noqa: E402


def test_render_workflow_argv_uses_positional_search_query():
    command_argvs = render_workflow_argv(
        "literature_review_classic",
        {"topic": "deep learning"},
    )

    assert command_argvs[0][:2] == ["search", "deep learning"]
    assert "--query" not in command_argvs[0]


def test_render_workflow_argv_maps_citation_identifier_to_positional_paper_id():
    command_argvs = render_workflow_argv(
        "citation_network_analysis",
        {"doi_or_url": "10.1000/example doi"},
    )

    assert command_argvs[0][:2] == ["citations", "10.1000/example doi"]
    assert "--identifier" not in command_argvs[0]


def test_cmd_workflows_executes_subcommand_argv_without_string_split(monkeypatch):
    calls = []
    run_kwargs = []

    def fake_run(argv, **kwargs):
        run_kwargs.append(kwargs)
        if argv[2:] == ["check"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"capabilities": {}}),
                stderr="",
            )

        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps({"status": "success"}),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    literature.cmd_workflows(
        argparse.Namespace(
            list=False,
            execute="literature_review_classic",
            variables=json.dumps({"topic": "deep learning"}),
            dry_run=False,
        )
    )

    assert calls[0][2:4] == ["search", "deep learning"]
    assert "--query" not in calls[0]
    assert all(kwargs["encoding"] == "utf-8" for kwargs in run_kwargs)
    assert all(kwargs["errors"] == "replace" for kwargs in run_kwargs)

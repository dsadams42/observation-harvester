from __future__ import annotations

import json
from pathlib import Path

from pdt_observer.cli import main
from pdt_observer.models import InvestigationResult


def test_cli_demo_output_is_valid_json(capsys) -> None:
    exit_code = main(["demo"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    result = InvestigationResult.model_validate(payload)

    assert exit_code == 0
    assert result.status == "accepted"
    assert result.count == 17
    assert captured.err == ""


def test_cli_validate_codex_run_output_is_valid_json(capsys) -> None:
    exit_code = main(["validate", "examples/milltown_codex_run.json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["valid"] is True
    assert payload["status"] == "accepted"
    assert payload["result"]["count"] == 17
    assert payload["errors"] == []
    assert captured.err == ""


def test_cli_summarize_codex_run(capsys) -> None:
    exit_code = main(["summarize", "examples/milltown_codex_run.json"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "accepted: 17 people at Blue Lantern" in captured.out
    assert "validation: valid" in captured.out


def test_cli_api_mode_requires_key(monkeypatch, capsys) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    exit_code = main(["investigate-api", "examples/milltown_task.json"])

    captured = capsys.readouterr()

    assert exit_code == 2
    assert "OPENAI_API_KEY is required" in captured.err
    assert captured.out == ""


def test_example_codex_run_file_exists() -> None:
    assert Path("examples/milltown_codex_run.json").is_file()

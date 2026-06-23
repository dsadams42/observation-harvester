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


def test_cli_batch_create_and_work_claim(tmp_path, capsys) -> None:
    exit_code = main(
        [
            "batch",
            "create",
            "--locality",
            "Milltown",
            "--country",
            "US",
            "--profiles",
            "public_venues",
            "--batch-id",
            "batch-test",
            "--workspace",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()
    batch = json.loads(captured.out)

    assert exit_code == 0
    assert batch["batch_id"] == "batch-test"
    assert len(batch["work_item_ids"]) == 5

    exit_code = main(
        [
            "work",
            "claim",
            "--profile",
            "restaurants_bars",
            "--claimed-by",
            "codex-restaurants",
            "--workspace",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()
    claimed = json.loads(captured.out)

    assert exit_code == 0
    assert claimed["profile_id"] == "restaurants_bars"
    assert claimed["status"] == "claimed"
    assert claimed["claimed_by"] == "codex-restaurants"


def test_cli_review_ingest_list_and_export(tmp_path, capsys) -> None:
    exit_code = main(
        [
            "review",
            "ingest",
            "examples/milltown_codex_run.json",
            "--workspace",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()
    ingested = json.loads(captured.out)

    assert exit_code == 0
    assert ingested["status"] == "accepted"
    assert ingested["validation_valid"] is True

    exit_code = main(
        ["review", "list", "--status", "accepted", "--workspace", str(tmp_path)]
    )
    captured = capsys.readouterr()
    items = json.loads(captured.out)

    assert exit_code == 0
    assert len(items) == 1
    assert items[0]["count"] == 17

    exit_code = main(["export", "--status", "accepted", "--workspace", str(tmp_path)])
    captured = capsys.readouterr()
    lines = [json.loads(line) for line in captured.out.splitlines()]

    assert exit_code == 0
    assert len(lines) == 1
    assert lines[0]["place_name"] == "Blue Lantern"

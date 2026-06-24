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
    assert payload["result"]["time_context"]["day_part"] == "night"
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
            "--target-accepted",
            "2",
            "--max-sources",
            "7",
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
    assert claimed["quota"]["target_accepted_count"] == 2
    assert claimed["quota"]["max_sources_examined"] == 7


def test_cli_work_status_and_record_source(tmp_path, capsys) -> None:
    main(
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
    capsys.readouterr()
    main(
        [
            "work",
            "claim",
            "--profile",
            "restaurants_bars",
            "--workspace",
            str(tmp_path),
        ]
    )
    capsys.readouterr()

    exit_code = main(
        [
            "work",
            "record-source",
            "--work-item-id",
            "batch-test-restaurants_bars",
            "--outcome",
            "empty",
            "--workspace",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert report["progress"]["sources_examined"] == 1
    assert report["progress"]["empty_sources"] == 1
    assert report["should_continue"] is True

    exit_code = main(
        [
            "work",
            "status",
            "--work-item-id",
            "batch-test-restaurants_bars",
            "--workspace",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()
    status = json.loads(captured.out)

    assert exit_code == 0
    assert status["remaining"]["sources_remaining"] == 39


def test_cli_record_run_completes_and_rejects_more_progress(tmp_path, capsys) -> None:
    main(
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
            "--target-accepted",
            "1",
            "--workspace",
            str(tmp_path),
        ]
    )
    capsys.readouterr()
    main(
        [
            "work",
            "claim",
            "--profile",
            "restaurants_bars",
            "--workspace",
            str(tmp_path),
        ]
    )
    capsys.readouterr()

    exit_code = main(
        [
            "work",
            "record-run",
            "--work-item-id",
            "batch-test-restaurants_bars",
            "--run-file",
            "examples/milltown_codex_run.json",
            "--workspace",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert report["status"] == "completed"
    assert report["stop_reason"] == "target_met"

    exit_code = main(
        [
            "work",
            "record-source",
            "--work-item-id",
            "batch-test-restaurants_bars",
            "--outcome",
            "examined",
            "--workspace",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "already completed" in captured.err


def test_cli_work_complete_marks_manual_stop(tmp_path, capsys) -> None:
    main(
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
    capsys.readouterr()
    main(
        [
            "work",
            "claim",
            "--profile",
            "restaurants_bars",
            "--workspace",
            str(tmp_path),
        ]
    )
    capsys.readouterr()

    exit_code = main(
        [
            "work",
            "complete",
            "--work-item-id",
            "batch-test-restaurants_bars",
            "--workspace",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert report["status"] == "completed"
    assert report["stop_reason"] == "manual_complete"
    assert report["should_continue"] is False


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
    assert lines[0]["time_context"]["observed_time_local"] == "21:10"

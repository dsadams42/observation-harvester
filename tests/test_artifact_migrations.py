from __future__ import annotations

import json
from pathlib import Path

from pdt_observer.artifact_migrations import (
    load_versioned_json,
    migrate_artifact_payload,
    migrate_workspace,
)


def test_migration_adds_schema_version_to_object_payload() -> None:
    payload, changed, message = migrate_artifact_payload(
        {"run_id": "example", "status": "completed"},
        artifact_type="harvest_runs",
    )

    assert changed is True
    assert payload["schema_version"] == 1
    assert "added schema_version" in message


def test_migration_leaves_array_artifacts_unchanged() -> None:
    payload, changed, message = migrate_artifact_payload(
        [{"lead_index": 0}],
        artifact_type="qaqc_runs",
    )

    assert changed is False
    assert payload == [{"lead_index": 0}]
    assert "array artifact left unchanged" in message


def test_workspace_migration_dry_run_and_write_backups(tmp_path: Path) -> None:
    manifest = tmp_path / "harvest_runs/example.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"run_id": "example", "status": "completed"}), encoding="utf-8")

    dry_run = migrate_workspace(tmp_path, dry_run=True)
    unchanged_payload = load_versioned_json(manifest)
    migrated = migrate_workspace(tmp_path, dry_run=False)
    migrated_payload = load_versioned_json(manifest)

    assert dry_run["changed_count"] == 1
    assert "schema_version" not in unchanged_payload
    assert migrated["changed_count"] == 1
    assert migrated_payload["schema_version"] == 1
    assert (tmp_path / "harvest_runs/example.json.bak").is_file()


def test_workspace_migration_reports_invalid_json_without_destroying_file(tmp_path: Path) -> None:
    broken = tmp_path / "sample_sets/broken.json"
    broken.parent.mkdir()
    broken.write_text("{", encoding="utf-8")

    result = migrate_workspace(tmp_path, dry_run=False)

    assert result["error_count"] == 1
    assert result["errors"][0]["path"] == str(broken)
    assert broken.read_text(encoding="utf-8") == "{"

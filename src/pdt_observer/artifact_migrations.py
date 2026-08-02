from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

VERSIONED_DIRECTORIES = {
    "harvest_runs": ("*.json",),
    "sample_sets": ("*.json",),
    "coverage_runs": ("*.json",),
    "curation_runs": ("*.json",),
    "geometry_reviews": ("*.json",),
    "geographer_runs": ("*.json",),
    "strategy_runs": ("*.json",),
    "agent_activity": ("*.json",),
    "agent_dialogue": ("*.json",),
    "work_items": ("*.json",),
    "batches": ("*.json",),
    "job_runs": ("*.job.json",),
}


def load_versioned_json(path: Path) -> dict[str, Any] | list[Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict | list):
        raise ValueError(f"artifact is not a JSON object or array: {path}")
    return payload


def artifact_type_for_path(path: Path) -> str:
    parts = path.parts
    for directory in VERSIONED_DIRECTORIES:
        if directory in parts:
            return directory
    return "unknown"


def migrate_artifact_payload(
    payload: dict[str, Any] | list[Any],
    *,
    artifact_type: str,
) -> tuple[dict[str, Any] | list[Any], bool, str]:
    if isinstance(payload, list):
        return payload, False, f"{artifact_type}: array artifact left unchanged"
    if payload.get("schema_version") == 1:
        return payload, False, f"{artifact_type}: already schema_version 1"
    upgraded = dict(payload)
    upgraded["schema_version"] = 1
    return upgraded, True, f"{artifact_type}: added schema_version 1"


def iter_artifact_paths(root: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for directory_name, patterns in VERSIONED_DIRECTORIES.items():
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for pattern in patterns:
            paths.extend(sorted(directory.glob(pattern)))
    return tuple(path for path in paths if path.is_file() and not path.name.endswith(".bak"))


def migrate_workspace(root: Path, *, dry_run: bool = True) -> dict[str, Any]:
    inspected = 0
    changed = 0
    unchanged = 0
    errors: list[dict[str, str]] = []
    changes: list[dict[str, str | bool]] = []
    for path in iter_artifact_paths(root):
        inspected += 1
        artifact_type = artifact_type_for_path(path)
        try:
            payload = load_versioned_json(path)
            migrated, did_change, message = migrate_artifact_payload(
                payload,
                artifact_type=artifact_type,
            )
            if did_change:
                changed += 1
                if not dry_run:
                    backup = path.with_suffix(path.suffix + ".bak")
                    if not backup.exists():
                        shutil.copy2(path, backup)
                    path.write_text(
                        json.dumps(migrated, indent=2, sort_keys=True),
                        encoding="utf-8",
                    )
            else:
                unchanged += 1
            changes.append(
                {
                    "path": str(path),
                    "changed": did_change,
                    "message": message,
                }
            )
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)})
    return {
        "dry_run": dry_run,
        "inspected_count": inspected,
        "changed_count": changed,
        "unchanged_count": unchanged,
        "error_count": len(errors),
        "changes": changes,
        "errors": errors,
    }

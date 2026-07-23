from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

from starlette.testclient import TestClient

from pdt_observer.app import create_app

LEAD_PAYLOAD = [
    {
        "is_valid_occupancy_report": True,
        "source_url": "https://example.test/story",
        "source_title": "Workers evacuated",
        "source_type": "news",
        "evidence_quote": "Officials said 12 workers were evacuated from the warehouse.",
        "incident_date": "2026-01-02",
        "incident_time": "03:30 PM",
        "occupancy_data": [{"count": 12, "group_type": "workers evacuated"}],
        "location": {
            "facility_name": "Example Warehouse",
            "specific_address_or_landmark": "Industrial Drive",
            "city_or_region": "Tennessee",
            "country": "US",
        },
        "confidence": "high",
        "is_facility_level": True,
        "is_regional_aggregate": False,
        "review_flags": [],
        "review_notes": None,
    }
]


def successful_runner(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    output_path = Path(command[command.index("-o") + 1])
    output_path.write_text(json.dumps(LEAD_PAYLOAD), encoding="utf-8")
    assert "Tennessee" in prompt
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def failing_runner(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 2, stdout="", stderr="codex failed")


def test_index_page_contains_local_app_controls(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner))

    response = client.get("/")

    assert response.status_code == 200
    assert "Observation Harvester" in response.text
    assert "Country" in response.text
    assert "Run Harvest" in response.text
    assert "Copy JSON" in response.text
    assert "Download CSV" in response.text


def test_profiles_endpoint_returns_builtin_profile_sets(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner))

    response = client.get("/api/profiles")

    assert response.status_code == 200
    payload = response.json()
    profile_ids = {item["profile_set_id"] for item in payload["profile_sets"]}
    assert {"commercial_business", "public_venues", "residential"} <= profile_ids
    commercial = next(
        item for item in payload["profile_sets"] if item["profile_set_id"] == "commercial_business"
    )
    assert any(
        profile["profile_id"] == "factories_warehouses"
        for profile in commercial["profiles"]
    )


def test_harvest_run_endpoint_returns_manifest_and_leads(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner))

    response = client.post(
        "/api/harvest/run",
        json={
            "country": "US",
            "locality": "Tennessee",
            "profiles": "commercial_business",
            "profile": "factories_warehouses",
            "target": 5,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["manifest"]["status"] == "completed"
    assert payload["summary"]["lead_count"] == 1
    assert payload["leads"][0]["source_type"] == "news"
    assert Path(payload["manifest"]["prompt_path"]).is_file()
    assert Path(payload["manifest"]["lead_path"]).is_file()


def test_harvest_batch_run_endpoint_returns_child_runs(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner))

    response = client.post(
        "/api/harvest/batch-run",
        json={
            "country": "US",
            "locality": "Tennessee",
            "profiles": "commercial_business",
            "target": 3,
        },
    )

    assert response.status_code == 200
    manifest = response.json()["manifest"]
    assert manifest["status"] == "completed"
    assert manifest["summary"]["run_count"] == 4
    assert len(manifest["child_run_ids"]) == 4


def test_run_detail_leads_export_and_promote_endpoints(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner))
    created = client.post(
        "/api/harvest/run",
        json={
            "country": "US",
            "locality": "Tennessee",
            "profiles": "commercial_business",
            "profile": "factories_warehouses",
            "target": 5,
        },
    ).json()
    run_id = created["manifest"]["run_id"]

    detail = client.get(f"/api/runs/{run_id}")
    leads = client.get(f"/api/runs/{run_id}/leads")
    csv_export = client.get(f"/api/runs/{run_id}/export.csv")
    jsonl_export = client.get(f"/api/runs/{run_id}/export.jsonl")
    promoted = client.post(f"/api/runs/{run_id}/promote", json={"index": 0})

    assert detail.status_code == 200
    assert leads.json()["leads"][0]["location"]["facility_name"] == "Example Warehouse"
    assert "Example Warehouse" in csv_export.text
    assert json.loads(jsonl_export.text)["source_url"] == "https://example.test/story"
    assert promoted.status_code == 200
    assert promoted.json()["run"]["candidate"]["result"]["status"] == "review"
    assert Path(promoted.json()["run_file"]).is_file()


def test_harvest_run_endpoint_returns_failed_manifest_for_codex_error(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=failing_runner))

    response = client.post(
        "/api/harvest/run",
        json={"country": "US", "profiles": "commercial_business", "target": 5},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["manifest"]["status"] == "failed"
    assert payload["manifest"]["error_message"] == "codex failed"
    assert payload["leads"] == []


def test_launcher_references_bootstrap_steps() -> None:
    launcher = Path("Observation Harvester.command").read_text(encoding="utf-8")

    assert ".venv" in launcher
    assert '.[app]' in launcher
    assert "command -v codex" in launcher
    assert "python -m pdt_observer app" in launcher

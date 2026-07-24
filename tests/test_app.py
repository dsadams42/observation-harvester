from __future__ import annotations

import json
import stat
import subprocess
import time
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
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner, background=False))

    response = client.get("/")

    assert response.status_code == 200
    assert "Observation Harvester" in response.text
    assert "Country" in response.text
    assert "Facility Type" in response.text
    assert "Subtype" in response.text
    assert "Campaign" in response.text
    assert "Regions or Localities" in response.text
    assert "Run Harvest" in response.text
    assert "Copy JSON" in response.text
    assert "Copy QAQC Prompt" in response.text
    assert "Download CSV" in response.text
    assert "Clear All" in response.text
    assert "Agent Activity" in response.text
    assert "Cancel Run" in response.text
    assert "Exit Application" in response.text
    assert "/api/runs/${runId}/log" in response.text
    assert "/api/runs/${state.currentRunId}/status" in response.text
    assert "/api/runs/${state.currentRunId}/cancel" in response.text
    assert "/api/runs/${state.currentRunId}/qaqc-prompt" in response.text
    assert "/api/runs/clear" in response.text
    assert "/api/app/exit" in response.text


def test_profiles_endpoint_returns_builtin_profile_sets(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner, background=False))

    response = client.get("/api/profiles")

    assert response.status_code == 200
    payload = response.json()
    profile_ids = {item["profile_set_id"] for item in payload["profile_sets"]}
    assert {
        "schools",
        "manufacturing",
        "restaurants",
        "commercial_business",
        "public_venues",
        "residential",
    } <= profile_ids
    schools = next(
        item for item in payload["profile_sets"] if item["profile_set_id"] == "schools"
    )
    assert any(
        profile["profile_id"] == "university_college"
        for profile in schools["profiles"]
    )


def test_harvest_run_endpoint_returns_manifest_and_leads(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner, background=False))

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
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner, background=False))

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


def test_harvest_campaign_run_endpoint_returns_child_runs(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner, background=False))

    response = client.post(
        "/api/harvest/campaign-run",
        json={
            "country": "US",
            "localities": ["Tennessee"],
            "facility_types": ["schools", "manufacturing"],
            "target": 3,
        },
    )
    runs = client.get("/api/runs")

    assert response.status_code == 200
    manifest = response.json()["manifest"]
    assert manifest["status"] == "completed"
    assert manifest["summary"] == {
        "planned_run_count": 2,
        "completed_count": 2,
        "failed_count": 0,
        "lead_count": 2,
    }
    assert len(manifest["child_run_ids"]) == 2
    assert any(item["manifest_type"] == "campaign" for item in runs.json()["runs"])


def test_run_detail_leads_export_and_promote_endpoints(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner, background=False))
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
    qaqc_prompt = client.get(f"/api/runs/{run_id}/qaqc-prompt")
    promoted = client.post(f"/api/runs/{run_id}/promote", json={"index": 0})

    assert detail.status_code == 200
    assert leads.json()["leads"][0]["location"]["facility_name"] == "Example Warehouse"
    assert "Example Warehouse" in csv_export.text
    assert json.loads(jsonl_export.text)["source_url"] == "https://example.test/story"
    assert qaqc_prompt.status_code == 200
    assert "Occupancy Lead QAQC Verification" in qaqc_prompt.text
    assert "https://example.test/story" in qaqc_prompt.text
    assert "Example Warehouse" in qaqc_prompt.text
    assert promoted.status_code == 200
    assert promoted.json()["run"]["candidate"]["result"]["status"] == "review"
    assert Path(promoted.json()["run_file"]).is_file()


def test_qaqc_prompt_endpoint_rejects_grouped_parent_manifest(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner, background=False))
    created = client.post(
        "/api/harvest/campaign-run",
        json={
            "country": "US",
            "localities": ["Tennessee"],
            "facility_types": ["schools", "manufacturing"],
            "target": 3,
        },
    ).json()
    campaign_id = created["manifest"]["campaign_id"]

    response = client.get(f"/api/runs/{campaign_id}/qaqc-prompt")

    assert response.status_code == 400
    assert "child harvest runs" in response.text


def test_clear_runs_endpoint_removes_history_without_promoted_runs_or_exports(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner, background=False))
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
    promoted = client.post(f"/api/runs/{run_id}/promote", json={"index": 0}).json()
    export_file = tmp_path / "exports/report.csv"
    export_file.parent.mkdir()
    export_file.write_text("kept", encoding="utf-8")

    response = client.post("/api/runs/clear")

    assert response.status_code == 200
    assert response.json()["cleared"] is True
    assert client.get("/api/runs").json()["runs"] == []
    assert not any((tmp_path / "harvest_runs").glob("*.json"))
    assert not any((tmp_path / "lead_runs").glob("*.json"))
    assert not any((tmp_path / "harvest_logs").glob("*.log"))
    assert not any((tmp_path / "work").glob("*.md"))
    assert Path(promoted["run_file"]).is_file()
    assert export_file.is_file()


def test_harvest_run_endpoint_returns_failed_manifest_for_codex_error(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=failing_runner, background=False))

    response = client.post(
        "/api/harvest/run",
        json={"country": "US", "profiles": "commercial_business", "target": 5},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["manifest"]["status"] == "failed"
    assert payload["manifest"]["error_message"] == "codex failed"
    assert payload["leads"] == []


def test_run_status_log_cancel_and_exit_endpoints(tmp_path: Path) -> None:
    fake_codex = tmp_path / "fake_codex.py"
    fake_codex.write_text(
        """#!/usr/bin/env python3
import pathlib
import sys
import time

output = pathlib.Path(sys.argv[sys.argv.index("-o") + 1])
sys.stdin.read()
time.sleep(10)
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text("[]")
""",
        encoding="utf-8",
    )
    fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)
    exit_called = False

    def shutdown_callback() -> None:
        nonlocal exit_called
        exit_called = True

    client = TestClient(
        create_app(
            workspace=tmp_path,
            codex_bin=str(fake_codex),
            shutdown_callback=shutdown_callback,
        )
    )

    created = client.post(
        "/api/harvest/run",
        json={"country": "US", "locality": "Tennessee", "profiles": "schools", "target": 5},
    )
    run_id = created.json()["manifest"]["run_id"]
    status = client.get(f"/api/runs/{run_id}/status")
    log = client.get(f"/api/runs/{run_id}/log")
    clear = client.post("/api/runs/clear")
    cancelled = client.post(f"/api/runs/{run_id}/cancel")

    for _ in range(40):
        final_status = client.get(f"/api/runs/{run_id}/status").json()
        if final_status["manifest"]["status"] == "cancelled":
            break
        time.sleep(0.05)

    exited = client.post("/api/app/exit")

    assert created.status_code == 200
    assert created.json()["manifest"]["status"] == "running"
    assert status.json()["active"] is True
    assert "Launching Codex command" in log.text
    assert clear.status_code == 409
    assert "Cannot clear history" in clear.json()["error"]
    assert cancelled.json()["cancelled"] is True
    assert final_status["manifest"]["status"] == "cancelled"
    assert exited.json()["shutting_down"] is True
    assert exit_called is True


def test_launcher_references_bootstrap_steps() -> None:
    launcher = Path("Observation Harvester.command").read_text(encoding="utf-8")

    assert ".venv" in launcher
    assert '.[app]' in launcher
    assert "command -v codex" in launcher
    assert "APP_PORT" in launcher
    assert "8771" in launcher
    assert "python -m pdt_observer app" in launcher

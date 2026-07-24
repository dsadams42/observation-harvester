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

QAQC_PAYLOAD = [
    {
        "lead_index": 0,
        "source_url": "https://example.test/story",
        "verification_status": "verified",
        "source_reachable": True,
        "facility_match": True,
        "location_match": True,
        "count_checks": [
            {
                "count": 12,
                "group_type": "workers evacuated",
                "reported_count_found": True,
                "quote_found": True,
                "supporting_quote": "Officials said 12 workers were evacuated.",
                "notes": None,
            }
        ],
        "supporting_quote": "Officials said 12 workers were evacuated.",
        "recommended_action": "keep",
        "review_notes": "Count, facility, and location are supported.",
    }
]

ADDRESS_PAYLOAD = [
    {
        "lead_index": 0,
        "item_id": "placeholder-0",
        "facility_name": "Example Warehouse",
        "formatted_address": "100 Industrial Drive, Nashville, TN, US",
        "address_line1": "100 Industrial Drive",
        "address_line2": None,
        "city_or_region": "Nashville",
        "state_or_province": "TN",
        "postal_code": None,
        "country": "US",
        "address_source_url": "https://example.test/warehouse",
        "address_evidence_quote": "Example Warehouse is located at 100 Industrial Drive.",
        "confidence": "high",
        "status": "found",
        "review_notes": "Official address page matches the harvested facility.",
    }
]


def successful_runner(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    output_path = Path(command[command.index("-o") + 1])
    if "Facility Address Enrichment" in prompt:
        item_id = "placeholder-0"
        try:
            records_text = prompt.split("## Input Records", 1)[1].strip()
            records = json.loads(records_text)
            item_id = records[0]["item_id"]
        except (IndexError, KeyError, ValueError, json.JSONDecodeError):
            pass
        payload = [{**ADDRESS_PAYLOAD[0], "item_id": item_id}]
    elif "Occupancy Lead QAQC Verification" in prompt:
        payload = QAQC_PAYLOAD
    else:
        payload = LEAD_PAYLOAD
    output_path.write_text(json.dumps(payload), encoding="utf-8")
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
    assert "Run QAQC" in response.text
    assert "Run Address Enrichment" in response.text
    assert "Download Verified CSV" in response.text
    assert "Geometry Review" in response.text
    assert "leaflet.draw" in response.text
    assert "Load Approved" in response.text
    assert "Manual Address Search" in response.text
    assert "Search Address" in response.text
    assert "Save Footprint" in response.text
    assert "Download Footprints GeoJSON" in response.text
    assert "Clear All" in response.text
    assert "Agent Activity" in response.text
    assert "Cancel Run" in response.text
    assert "Exit Application" in response.text
    assert "Theme" in response.text
    assert "observationHarvesterTheme" in response.text
    assert "data-theme" in response.text
    assert "/api/runs/${runId}/log" in response.text
    assert "/api/runs/${state.currentRunId}/status" in response.text
    assert "/api/runs/${state.currentRunId}/cancel" in response.text
    assert "/api/runs/${state.currentRunId}/qaqc-prompt" in response.text
    assert "/api/runs/${state.currentRunId}/qaqc-run" in response.text
    assert "/api/runs/${state.currentRunId}/qaqc-reviews" in response.text
    assert "/api/runs/${state.currentRunId}/address-run" in response.text
    assert "/api/runs/${state.currentRunId}/address-results" in response.text
    assert "/api/runs/${state.currentRunId}/geometry-items" in response.text
    assert "/api/geometry/geocode" in response.text
    assert "/api/runs/${state.currentRunId}/export.verified.${format}" in response.text
    assert "/api/runs/${state.currentRunId}/export.footprints.geojson" in response.text
    assert "QAQC still running" in response.text
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


def test_qaqc_run_endpoint_verifies_single_child_run(tmp_path: Path) -> None:
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

    qaqc = client.post(f"/api/runs/{run_id}/qaqc-run")
    reviews = client.get(f"/api/runs/{run_id}/qaqc-reviews")

    assert qaqc.status_code == 200
    assert qaqc.json()["qaqc"]["summary"]["completed_count"] == 1
    assert qaqc.json()["qaqc"]["summary"]["review_count"] == 1
    assert (tmp_path / f"qaqc_runs/{run_id}-qaqc.json").is_file()
    assert reviews.json()["review_count"] == 1
    assert reviews.json()["reviews"][0]["verification_status"] == "verified"


def test_address_run_endpoint_enriches_qaqc_approved_leads(tmp_path: Path) -> None:
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
    client.post(f"/api/runs/{run_id}/qaqc-run")

    address = client.post(f"/api/runs/{run_id}/address-run")
    results = client.get(f"/api/runs/{run_id}/address-results")
    verified = client.get(f"/api/runs/{run_id}/verified-leads")

    assert address.status_code == 200
    assert address.json()["address"]["summary"]["completed_count"] == 1
    assert address.json()["address"]["summary"]["result_count"] == 1
    assert (tmp_path / f"address_runs/{run_id}-address.json").is_file()
    assert results.json()["result_count"] == 1
    assert results.json()["results"][0]["status"] == "found"
    assert verified.json()["items"][0]["address_enrichment"]["formatted_address"].startswith("100")


def test_geometry_geocode_prefers_enriched_address(tmp_path: Path) -> None:
    def geocoder(query: str) -> dict[str, object]:
        assert query == "100 Industrial Drive, Nashville, TN, US"
        return {
            "display_name": query,
            "latitude": 36.0,
            "longitude": -86.0,
            "provider": "mock",
            "query": query,
        }

    client = TestClient(
        create_app(
            workspace=tmp_path,
            runner=successful_runner,
            geocoder=geocoder,
            background=False,
        )
    )
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
    client.post(f"/api/runs/{run_id}/qaqc-run")
    client.post(f"/api/runs/{run_id}/address-run")
    item = client.get(f"/api/runs/{run_id}/geometry-items").json()["items"][0]

    geocoded = client.post(
        "/api/geometry/geocode",
        json={"item_id": item["item_id"], "query": item["geocode_query"]},
    )

    assert item["geocode_query"] == "100 Industrial Drive, Nashville, TN, US"
    assert item["address_status"] == "found"
    assert geocoded.json()["geometry"]["point"]["latitude"] == 36.0


def test_geometry_review_endpoints_and_verified_exports(tmp_path: Path) -> None:
    def geocoder(query: str) -> dict[str, object]:
        assert "Example Warehouse" in query
        return {
            "display_name": "Example Warehouse, Tennessee",
            "latitude": 36.0,
            "longitude": -86.0,
            "provider": "mock",
            "query": query,
        }

    client = TestClient(
        create_app(
            workspace=tmp_path,
            runner=successful_runner,
            geocoder=geocoder,
            background=False,
        )
    )
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
    client.post(f"/api/runs/{run_id}/qaqc-run")

    verified = client.get(f"/api/runs/{run_id}/verified-leads")
    geometry_items = client.get(f"/api/runs/{run_id}/geometry-items")
    item = geometry_items.json()["items"][0]
    geocoded = client.post(
        "/api/geometry/geocode",
        json={"item_id": item["item_id"], "query": item["geocode_query"]},
    )
    polygon = {
        "type": "Polygon",
        "coordinates": [
            [[-86.0, 36.0], [-86.0, 36.001], [-85.999, 36.001], [-85.999, 36.0], [-86.0, 36.0]]
        ],
    }
    saved = client.post(
        f"/api/geometry/items/{item['item_id']}",
        json={
            "item_id": item["item_id"],
            "geocode_query": item["geocode_query"],
            "point": {"latitude": 36.0, "longitude": -86.0, "source": "user"},
            "polygon_geojson": polygon,
            "geometry_status": "footprint_drawn",
            "geocode_result": geocoded.json()["geocode_result"],
            "review_notes": "Traced from imagery.",
        },
    )
    verified_json = client.get(f"/api/runs/{run_id}/export.verified.json")
    verified_csv = client.get(f"/api/runs/{run_id}/export.verified.csv")
    footprints = client.get(f"/api/runs/{run_id}/export.footprints.geojson")

    assert verified.status_code == 200
    assert verified.json()["item_count"] == 1
    assert geometry_items.json()["items"][0]["geometry_status"] == "needs_review"
    assert geocoded.json()["geometry"]["point"]["latitude"] == 36.0
    assert saved.json()["geometry"]["geometry_status"] == "footprint_drawn"
    assert saved.json()["geometry"]["area_m2"] > 0
    assert "Example Warehouse" in verified_json.text
    assert "footprint_drawn" in verified_csv.text
    assert footprints.json()["features"][0]["geometry"]["type"] == "Polygon"


def test_verified_export_requires_qaqc(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner, background=False))
    created = client.post(
        "/api/harvest/run",
        json={"country": "US", "locality": "Tennessee", "profiles": "schools", "target": 5},
    ).json()
    run_id = created["manifest"]["run_id"]

    response = client.get(f"/api/runs/{run_id}/export.verified.json")

    assert response.status_code == 409
    assert "QAQC review not found" in response.json()["error"]


def test_qaqc_run_endpoint_fans_out_for_campaign_parent(tmp_path: Path) -> None:
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

    qaqc = client.post(f"/api/runs/{campaign_id}/qaqc-run")
    reviews = client.get(f"/api/runs/{campaign_id}/qaqc-reviews")

    assert qaqc.status_code == 200
    assert qaqc.json()["qaqc"]["summary"]["planned_count"] == 2
    assert qaqc.json()["qaqc"]["summary"]["completed_count"] == 2
    assert qaqc.json()["qaqc"]["summary"]["review_count"] == 2
    assert reviews.json()["review_count"] == 2
    assert len(reviews.json()["child_reviews"]) == 2

    verified = client.get(f"/api/runs/{campaign_id}/verified-leads")

    assert verified.status_code == 200
    assert verified.json()["item_count"] == 2


def test_address_run_endpoint_fans_out_for_campaign_parent(tmp_path: Path) -> None:
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
    client.post(f"/api/runs/{campaign_id}/qaqc-run")

    address = client.post(f"/api/runs/{campaign_id}/address-run")
    results = client.get(f"/api/runs/{campaign_id}/address-results")

    assert address.status_code == 200
    assert address.json()["address"]["summary"]["planned_count"] == 2
    assert address.json()["address"]["summary"]["completed_count"] == 2
    assert address.json()["address"]["summary"]["result_count"] == 2
    assert results.json()["result_count"] == 2
    assert len(results.json()["child_results"]) == 2


def test_run_log_includes_qaqc_child_subprocess_logs(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner, background=False))
    created = client.post(
        "/api/harvest/campaign-run",
        json={
            "country": "US",
            "localities": ["Tennessee"],
            "facility_types": ["schools"],
            "target": 3,
        },
    ).json()
    campaign_id = created["manifest"]["campaign_id"]
    child_run_id = created["manifest"]["child_run_ids"][0]
    child_qaqc_log = tmp_path / "harvest_logs" / f"{child_run_id}-qaqc.log"
    child_qaqc_log.write_text("[test] child QAQC subprocess detail\n", encoding="utf-8")

    response = client.get(f"/api/runs/{campaign_id}/log")

    assert response.status_code == 200
    assert "QAQC subprocess log" in response.text
    assert "child QAQC subprocess detail" in response.text


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
    client.post(f"/api/runs/{run_id}/qaqc-run")
    client.post(f"/api/runs/{run_id}/address-run")
    geometry_file = tmp_path / f"geometry_reviews/{run_id}.json"
    geometry_file.parent.mkdir()
    geometry_file.write_text("[]", encoding="utf-8")
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
    assert not any((tmp_path / "qaqc_runs").glob("*.json"))
    assert not any((tmp_path / "address_runs").glob("*.json"))
    assert not any((tmp_path / "work").glob("*.md"))
    assert geometry_file.is_file()
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

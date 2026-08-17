from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from pdt_observer.app import ActiveCodexRegistry, create_app
from pdt_observer.jobs import create_job, mark_job_running
from pdt_observer.models import JobType


def _write_fake_codex(tmp_path: Path, script: str) -> Path:
    fake_codex = tmp_path / "fake_codex.py"
    fake_codex.write_text(script, encoding="utf-8")
    if os.name == "nt":
        launcher = tmp_path / "fake_codex.cmd"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "%~dp0{fake_codex.name}" %*\r\n',
            encoding="utf-8",
        )
        return launcher
    fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)
    return fake_codex


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

COVERAGE_PAYLOAD = {
    "coverage_id": "placeholder-coverage",
    "sample_set_id": "placeholder-sample",
    "dispersion_status": "clustered",
    "counts_by_locality": {"Tennessee": 1},
    "counts_by_city_or_region": {"Tennessee": 1},
    "counts_by_facility_type": {"schools": 1},
    "out_of_scope_flags": [],
    "duplicate_or_cluster_flags": [
        {
            "item_id": None,
            "flag_type": "clustered",
            "reason": "Current verified records are concentrated in one region.",
        }
    ],
    "narrative_notes": "Run a targeted western Tennessee gap-fill pass.",
    "recommended_child_jobs": [
        {
            "country": "US",
            "locality": "Western Tennessee",
            "facility_type": "schools",
            "target": 2,
            "reason": "Western Tennessee is underrepresented.",
        }
    ],
}


MULTI_COUNT_LEAD_PAYLOAD = [
    {
        **LEAD_PAYLOAD[0],
        "occupancy_data": [
            {"count": 12, "group_type": "workers evacuated"},
            {"count": 4, "group_type": "security staff"},
        ],
    }
]


def successful_runner(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    output_path = Path(command[command.index("-o") + 1])
    if "Sample Set Coverage Steering" in prompt:
        payload = {
            **COVERAGE_PAYLOAD,
            "coverage_id": output_path.stem,
            "sample_set_id": output_path.stem.split("-coverage", 1)[0],
        }
    elif "Facility Address Enrichment" in prompt:
        item_id = "placeholder-0"
        try:
            marker = "## Input Records" if "## Input Records" in prompt else "## Input"
            records_text = prompt.split(marker, 1)[1].strip()
            records = json.loads(records_text)
            item_id = records[0]["item_id"]
        except (IndexError, KeyError, ValueError, json.JSONDecodeError):
            pass
        payload = [{**ADDRESS_PAYLOAD[0], "item_id": item_id}]
    elif "QAQC Verification" in prompt:
        payload = QAQC_PAYLOAD
    else:
        payload = LEAD_PAYLOAD
    output_path.write_text(json.dumps(payload), encoding="utf-8")
    assert "Tennessee" in prompt or "Coverage Steering" in prompt
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def no_gap_runner(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    if "Sample Set Coverage Steering" not in prompt:
        return successful_runner(command, prompt, cwd)
    output_path = Path(command[command.index("-o") + 1])
    payload = {
        **COVERAGE_PAYLOAD,
        "coverage_id": output_path.stem,
        "sample_set_id": output_path.stem.split("-coverage", 1)[0],
        "dispersion_status": "balanced",
        "duplicate_or_cluster_flags": [],
        "narrative_notes": "Coverage is sufficient for this review dataset.",
        "recommended_child_jobs": [],
    }
    output_path.write_text(json.dumps(payload), encoding="utf-8")
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def partial_gap_runner(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    output_path = Path(command[command.index("-o") + 1])
    if "Sample Set Coverage Steering" in prompt:
        payload = {
            **COVERAGE_PAYLOAD,
            "coverage_id": output_path.stem,
            "sample_set_id": output_path.stem.split("-coverage", 1)[0],
            "recommended_child_jobs": [
                {
                    "country": "US",
                    "locality": "Western Tennessee",
                    "facility_type": "schools",
                    "target": 2,
                    "reason": "Western Tennessee is underrepresented.",
                },
                {
                    "country": "US",
                    "locality": "Eastern Tennessee",
                    "facility_type": "schools",
                    "target": 2,
                    "reason": "Eastern Tennessee is underrepresented.",
                },
            ],
        }
    elif "Minimal Geographic Vernacular Review" in prompt:
        payload = {
            "search_languages": ["English"],
            "administrative_terms": [],
            "public_safety_terms": [],
            "facility_terms": [],
            "query_adjustments": [],
            "source_urls": [],
            "commentary": "No special local terms needed.",
            "rationale": "English source terms are sufficient.",
        }
    elif "western-tennessee" in output_path.name:
        return subprocess.CompletedProcess(command, 2, stdout="", stderr="harvest failed")
    else:
        return successful_runner(command, prompt, cwd)
    output_path.write_text(json.dumps(payload), encoding="utf-8")
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def multi_count_runner(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    output_path = Path(command[command.index("-o") + 1])
    output_path.write_text(json.dumps(MULTI_COUNT_LEAD_PAYLOAD), encoding="utf-8")
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def failing_runner(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 2, stdout="", stderr="codex failed")


def raising_runner(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    raise RuntimeError("runner exploded")


def blocking_successful_runner(
    release: threading.Event,
) -> Callable[[Sequence[str], str, Path], subprocess.CompletedProcess[str]]:
    def runner(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        release.wait(timeout=5)
        return successful_runner(command, prompt, cwd)

    return runner


class FunctionSpatialGeocoder:
    def __init__(
        self,
        geocode: Callable[[str], dict[str, object] | None],
        reverse: Callable[[float, float], dict[str, object] | None] | None = None,
    ) -> None:
        self._geocode = geocode
        self._reverse = reverse

    def geocode(self, query: str) -> dict[str, object] | None:
        return self._geocode(query)

    def reverse(self, latitude: float, longitude: float) -> dict[str, object] | None:
        if self._reverse is None:
            return None
        return self._reverse(latitude, longitude)


def test_active_codex_registry_sends_prompts_as_utf8(tmp_path: Path) -> None:
    prompt = "Georgia’s GEMA/HS technical college search"
    registry = ActiveCodexRegistry(tmp_path)

    with patch("pdt_observer.app_runtime.subprocess.Popen") as popen:
        process = popen.return_value
        process.communicate.return_value = ("", "")
        process.returncode = 0

        result = registry.runner(
            ["codex", "exec", "-o", str(tmp_path / "georgia.json")],
            prompt,
            tmp_path,
        )

    assert result.returncode == 0
    assert popen.call_args.kwargs["encoding"] == "utf-8"
    assert popen.call_args.kwargs["errors"] == "replace"
    process.communicate.assert_called_once_with(input=prompt)


def test_harvest_run_rejects_duplicate_active_submission(tmp_path: Path) -> None:
    release = threading.Event()
    app = create_app(workspace=tmp_path, runner=blocking_successful_runner(release))
    with TestClient(app) as client:
        first = client.post(
            "/api/harvest/run",
            json={
                "country": "US",
                "locality": "Tennessee",
                "profiles": "schools",
                "target": 5,
                "run_id": "duplicate-run",
            },
        )
        second = client.post(
            "/api/harvest/run",
            json={
                "country": "US",
                "locality": "Tennessee",
                "profiles": "schools",
                "target": 5,
                "run_id": "duplicate-run",
            },
        )
        release.set()

    assert first.status_code == 200
    assert first.json()["job"]["active"] is True
    assert second.status_code == 409
    assert second.json()["error"] == "Run already has active work: duplicate-run"


def test_campaign_run_rejects_duplicate_active_submission(tmp_path: Path) -> None:
    release = threading.Event()
    app = create_app(workspace=tmp_path, runner=blocking_successful_runner(release))
    with TestClient(app) as client:
        payload = {
            "country": "US",
            "localities": ["Tennessee"],
            "facility_types": ["schools"],
            "target": 3,
            "campaign_id": "duplicate-campaign",
        }
        first = client.post("/api/harvest/campaign-run", json=payload)
        second = client.post("/api/harvest/campaign-run", json=payload)
        release.set()

    assert first.status_code == 200
    assert first.json()["job"]["active"] is True
    assert second.status_code == 409
    assert second.json()["error"] == "Run already has active work: duplicate-campaign"


def geographer_and_harvest_runner(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    output_path = Path(command[command.index("-o") + 1])
    if "Minimal Geographic Vernacular Review" in prompt:
        output_path.write_text(
            json.dumps(
                {
                    "country_code": "US",
                    "country_aliases": ["Estados Unidos"],
                    "search_languages": ["English"],
                    "administrative_terms": [],
                    "address_terms": [],
                    "public_safety_terms": [
                        {
                            "standard_term": "state police",
                            "local_term": "Tennessee Highway Patrol",
                            "language": "English",
                            "usage_note": "Use for state-level incident searches.",
                        }
                    ],
                    "facility_terms": [],
                    "query_adjustments": ["Tennessee Highway Patrol school evacuation"],
                    "source_urls": ["https://example.test/agency"],
                    "commentary": (
                        "I found a locally relevant public-safety name, so I added it to searches."
                    ),
                    "rationale": (
                        "The local agency name may reveal reports missed by generic terms."
                    ),
                }
            ),
            encoding="utf-8",
        )
    elif "QAQC Verification" in prompt:
        output_path.write_text(json.dumps(QAQC_PAYLOAD), encoding="utf-8")
    else:
        output_path.write_text(json.dumps(LEAD_PAYLOAD), encoding="utf-8")
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def test_index_page_contains_local_app_controls(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner, background=False))

    response = client.get("/")
    html = response.text

    assert response.status_code == 200
    assert "<title>OASIS</title>" in html
    assert "Observation Acquisition and Spatial Information Synthesis" in html
    assert 'src="/assets/oasis-logo.jpg"' in html
    assert "Country" in html
    assert "Land Use" in html
    assert "Facility Class" in html
    assert "Campaign" in html
    assert "Regions or Localities" in html
    assert "Agentic Workbench" in html
    assert "Geometry Studio" in html
    assert "Tabular Data" in html
    assert 'href="/assets/app.css"' in html
    assert 'src="/assets/app.js"' in html

    logo = client.get("/assets/oasis-logo.jpg")
    assert logo.status_code == 200
    assert logo.headers["content-type"] == "image/jpeg"
    assert logo.content.startswith(b"\xff\xd8\xff")
    css = client.get("/assets/app.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert ".workspace-tabs" in css.text
    js = client.get("/assets/app.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    assert "function runHarvest" in js.text
    assert 'role="tablist"' in html
    assert 'role="tabpanel"' in html
    assert 'aria-labelledby="workbenchTab"' in html
    assert 'aria-labelledby="geometryTab"' in html
    assert 'aria-labelledby="tableTab"' in html
    assert 'aria-live="polite"' in html
    assert 'aria-pressed="true"' in html
    assert 'data-workspace="workbench"' in html
    assert 'data-workspace="geometry"' in html
    assert 'data-workspace="table"' in html
    assert "setWorkspaceTab" in js.text
    assert "bindTabKeyboard(workspaceTabs, setWorkspaceTab)" in js.text
    assert "setControlEnabled" in js.text
    assert "updateActionAvailability" in js.text
    assert "setStatusMessage" in js.text
    assert "setPressedGroup" in js.text
    assert "countValueColumns" in js.text
    assert "Count Relationship" in js.text
    assert "defaultWorkflowStages" in js.text
    assert "Start Full Pipeline" in js.text
    assert "Run Full Pipeline" in html
    assert '<option value="hybrid">Hybrid</option>' not in html
    assert "run the two modes separately" in html
    assert "run two separate harvests" in Path("README.md").read_text(encoding="utf-8")
    assert "Review dataset ready - approval required" in js.text
    assert "Approve Dataset &amp; Check Coverage" in html
    assert "approval with no exclusions is valid" in html.lower()
    assert "Run Harvest Only" in html
    assert "Advanced manual stages" in html
    assert html.index("Run Full Pipeline") < html.index("Advanced manual stages")
    assert "Copy JSON" in html
    assert "Copy QAQC Prompt" in html
    assert "Run QAQC" in html
    assert 'id="runQaqcButton" class="secondary" type="button" disabled' in html
    assert "Run Address Enrichment" in html
    assert 'id="runAddressButton" class="secondary" type="button" disabled' in html
    assert "Download Verified CSV" in html
    assert "Coordinate Assignment Required" in html
    assert 'id="interventionList"' in html
    assert 'id="geocodedQueueTab"' in html
    assert 'id="manualQueueTab"' in html
    assert "Geocoded" in html
    assert "Needs Manual Geocoding" in html
    assert "geometryItemsForActiveTab" in js.text
    assert "needsManualGeocoding" in js.text
    assert "setGeometryListTab('geocoded')" in js.text
    assert "setGeometryListTab('manual')" in js.text
    assert "Geocode QAQC-Approved Observations" in html
    assert "Project Workflow" in html
    assert "Recommended next:" in js.text
    assert "/workflow-status" in js.text
    assert "Geocoding ${index + 1}/${pending.length}" in js.text
    assert "Review Dataset / Coverage" in html
    assert html.index("Geometry Studio") < html.index("Review Dataset / Coverage")
    assert "Assemble Review Dataset" in html
    assert "Check Coverage" in html
    assert "Run Coverage Gap Follow-ups" in html
    assert "Run QAQC Missing" in html
    assert "Run Address Missing" in html
    assert "leaflet.draw" in html
    assert "Load QAQC-Approved Observations" in html
    assert "Load Review Dataset Observations" in html
    assert "Show Sample Extent" in html
    assert "Zoom To Extent" in html
    assert "Clear Extent" in html
    assert "geometryExtentSummary" in html
    assert "overviewPointLayer" in js.text
    assert "overviewFootprintLayer" in js.text
    assert "overviewExtentLayer" in js.text
    assert "renderSampleExtent" in js.text
    assert "geometryRoundLabel" in js.text
    assert "selectGeometryItem(item.item_id)" in js.text
    assert "Corrected Address or Place" in html
    assert "Search Corrected Address" in html
    assert "Save Footprint" in html
    assert "Download Footprints GeoJSON" in html
    assert "Download Admin-Scoped JSON" in html
    assert "Download Admin-Scoped CSV" in html
    assert "Download Sample Admin-Scoped JSON" in html
    assert "Clear Generated Runs" in html
    assert "Clear All" not in html
    assert "Agent Activity" in html
    assert "Full Pipeline Transcript" in html
    assert "Download Transcript (.txt)" in html
    assert "Resolve Selected Coordinate" in html
    assert "Search Corrected Address" in html
    assert "Research This Facility" in html
    assert "Search Google Maps" in html
    assert "Accept this location" in js.text
    assert "Paste Google Maps Coordinates" in html
    assert "Preview Coordinate" in html
    assert "/api/geometry/coordinate-preview" in js.text
    assert "human_pasted_coordinate" in js.text
    assert "renderCandidateOptions" in js.text
    assert "/api/geometry/research" in js.text
    assert "Place Point on Map" in html
    assert "Save Coordinate" in html
    assert "renderGeocodingProgress" in js.text
    assert "allow_address_retry: true" in js.text
    assert 'id="dialogueOutput"' in html
    assert "/api/geographer/plan" in js.text
    assert "'dialogue'" in js.text
    assert "'transcript.txt'" in js.text
    assert "Cancel Run" in html
    assert "Exit OASIS" in html
    assert "closes this tab when the browser allows it" in html
    assert html.index("Exit OASIS") < html.index("Agent transcript and activity")
    assert "Theme" in html
    assert "observationHarvesterTheme" in js.text
    assert "data-theme" in css.text
    assert "--primary-bg: #2d6578" in css.text
    assert ":focus-visible" in css.text
    assert ".friendly-empty" in css.text
    assert ".control-help" in css.text
    assert "Raw outputs and prompts" in html
    assert "Agent transcript and activity" in html
    assert "Fix Missing Pipeline Stages" in html
    assert "Start with <strong>Run Full Pipeline</strong>" in html
    assert "Load approved observations after QAQC and address enrichment" in html
    assert "Select or assemble a review dataset" in html
    assert 'id="tableCopyButton" class="secondary" type="button" disabled' in html
    assert 'id="saveCoordinateButton" type="button" disabled' in html
    assert "/api/runs/${runId}/log" in js.text
    assert "/api/runs/${state.currentRunId}/status" in js.text
    assert "/api/runs/${state.currentRunId}/cancel" in js.text
    assert "/api/runs/${state.currentRunId}/qaqc-prompt" in js.text
    assert "/api/runs/${state.currentRunId}/qaqc-run" in js.text
    assert "/api/runs/${state.currentRunId}/qaqc-reviews" in js.text
    assert "/api/runs/${state.currentRunId}/address-run" in js.text
    assert "/api/runs/${state.currentRunId}/address-results" in js.text
    assert "/api/runs/${state.currentRunId}/geometry-items" in js.text
    assert "/api/runs/${state.currentRunId}/table?mode=${state.tableMode}" in js.text
    assert "/api/samples/${state.currentSampleSetId}/table?mode=verified" in js.text
    assert "Verified Only" in html
    assert "All Leads" in html
    assert "Copy Visible Rows" in html
    assert "downloadVisibleTableCsv" in js.text
    assert "openTableRowInGeometry" in js.text
    assert "function geometryLocation(item)" in js.text
    assert "item.lead.location" not in js.text
    assert "/api/samples/from-run" in js.text
    assert "/api/samples/${state.currentSampleSetId}/coverage-run" in js.text
    assert "/api/samples/${state.currentSampleSetId}/gap-fill-run" in js.text
    assert "/api/samples/${state.currentSampleSetId}/qaqc-missing" in js.text
    assert "/api/samples/${state.currentSampleSetId}/address-missing" in js.text
    assert "/api/samples/${state.currentSampleSetId}/geometry-items" in js.text
    assert "/api/geometry/geocode" in js.text
    assert "/api/runs/${state.currentRunId}/export.verified.${format}" in js.text
    assert "/api/runs/${state.currentRunId}/export.admin_scoped.${format}" in js.text
    assert "/api/samples/${state.currentSampleSetId}/export.admin_scoped.${format}" in js.text
    assert "/api/runs/${state.currentRunId}/export.footprints.geojson" in js.text
    assert "QAQC still running" in js.text
    assert "/api/runs/clear" in js.text
    assert "/api/app/exit" in js.text
    assert "window.close()" in js.text
    assert "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" in html
    assert "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" in html
    assert "https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.css" in html
    assert "https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.js" in html


def test_profiles_endpoint_returns_builtin_profile_sets(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner, background=False))

    response = client.get("/api/profiles")

    assert response.status_code == 200
    payload = response.json()
    profile_ids = [item["profile_set_id"] for item in payload["profile_sets"]]
    assert profile_ids == [
        "residential",
        "institutions_public_service",
        "retail_service",
        "commercial",
        "transportation",
        "military_facility",
        "recreation_entertainment",
        "agriculture",
    ]
    public_service = next(
        item
        for item in payload["profile_sets"]
        if item["profile_set_id"] == "institutions_public_service"
    )
    assert public_service["land_use"] == "Institutions/Public Service"
    assert any(
        profile["profile_id"] == "school_d_12"
        for profile in public_service["profiles"]
    )
    assert any(
        strategy["strategy_id"] == "incident_evacuation"
        for strategy in payload["strategies"]
    )
    school = next(
        profile
        for profile in public_service["profiles"]
        if profile["profile_id"] == "school_d_12"
    )
    assert school["strategy_plan"]["recommendations"]
    assert school["land_use"] == "Institutions/Public Service"
    assert school["facility_class"] == "School (D-12)"
    assert school["pdt_subtype"] == "School (D-12)"
    assert school["component_count_fields"] == [
        "Students",
        "staff",
        "faculty",
        "shift schooling",
    ]


def test_geographer_plan_flows_into_harvest_prompt_and_dialogue(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            workspace=tmp_path,
            runner=geographer_and_harvest_runner,
            background=False,
        )
    )
    planned = client.post(
        "/api/geographer/plan",
        json={
            "country": "US",
            "locality": "Tennessee",
            "profiles": "schools",
            "profile": "primary_secondary_education",
            "mode": "single",
        },
    )

    assert planned.status_code == 200
    plan_payload = planned.json()
    assert plan_payload["plan"]["status"] == "completed"
    assert "Geographer Agent:" in plan_payload["dialogue"]

    harvested = client.post(
        "/api/harvest/run",
        json={
            "country": "US",
            "locality": "Tennessee",
            "profiles": "schools",
            "profile": "primary_secondary_education",
            "target": 5,
            "run_id": plan_payload["run_id"],
            "geographer_plan_path": plan_payload["plan_path"],
        },
    )

    assert harvested.status_code == 200
    manifest = harvested.json()["manifest"]
    assert manifest["geographer_plan_path"] == plan_payload["plan_path"]
    prompt = Path(manifest["prompt_path"]).read_text(encoding="utf-8")
    assert "Geographer Vernacular Adjustments" in prompt
    assert "Tennessee Highway Patrol" in prompt
    dialogue = client.get(f"/api/runs/{plan_payload['run_id']}/dialogue")
    assert "Harvester Agent: I completed the search" in dialogue.text


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


def test_harvest_run_endpoint_accepts_count_method_override(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner, background=False))

    response = client.post(
        "/api/harvest/run",
        json={
            "country": "US",
            "locality": "Tennessee",
            "profiles": "recreation_entertainment",
            "profile": "theaters",
            "target": 5,
            "count_method_override": "population_subcomponent",
        },
    )

    assert response.status_code == 200
    manifest = response.json()["manifest"]
    assert manifest["count_method_override"] == "population_subcomponent"
    prompt = Path(manifest["prompt_path"]).read_text(encoding="utf-8")
    assert "Count method: population_subcomponent" in prompt

    hybrid_response = client.post(
        "/api/harvest/run",
        json={
            "country": "US",
            "locality": "Tennessee",
            "profiles": "retail_service",
            "profile": "hotels_motels",
            "target": 5,
            "run_id": "legacy-hybrid-compatibility",
            "count_method_override": "hybrid",
        },
    )
    assert hybrid_response.status_code == 200
    assert hybrid_response.json()["manifest"]["count_method_override"] == "hybrid"


def test_workflow_status_recommends_next_artifact_backed_stage(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner, background=False))
    harvested = client.post(
        "/api/harvest/run",
        json={
            "country": "US",
            "locality": "Tennessee",
            "profiles": "commercial_business",
            "profile": "factories_warehouses",
            "target": 1,
        },
    ).json()
    run_id = harvested["manifest"]["run_id"]

    workflow = client.get(f"/api/runs/{run_id}/workflow-status")

    assert workflow.status_code == 200
    payload = workflow.json()
    stages = {stage["id"]: stage for stage in payload["stages"]}
    assert stages["harvest"]["status"] == "complete"
    assert stages["harvest"]["current"] == 1
    assert stages["harvest"]["total"] == 1
    assert stages["harvest"]["display_mode"] == "progress"
    assert stages["qaqc"]["status"] == "ready"
    assert stages["qaqc"]["display_mode"] == "progress"
    assert payload["next_action"]["id"] == "run_qaqc"

    assert client.post(f"/api/runs/{run_id}/qaqc-run").status_code == 200
    updated = client.get(f"/api/runs/{run_id}/workflow-status").json()
    updated_stages = {stage["id"]: stage for stage in updated["stages"]}
    assert updated_stages["qaqc"]["status"] == "complete"
    assert updated["next_action"]["id"] == "run_address"


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
    assert manifest["summary"]["run_count"] == 6
    assert len(manifest["child_run_ids"]) == 6


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
        "budget_observation_count": 2,
    }
    assert len(manifest["child_run_ids"]) == 2
    assert any(item["manifest_type"] == "campaign" for item in runs.json()["runs"])


def test_geographer_plan_flows_into_campaign_children_and_dialogue(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            workspace=tmp_path,
            runner=geographer_and_harvest_runner,
            background=False,
        )
    )
    planned = client.post(
        "/api/geographer/plan",
        json={
            "country": "US",
            "localities": ["Tennessee", "Kentucky"],
            "facility_types": ["schools", "manufacturing"],
            "mode": "campaign",
        },
    )
    assert planned.status_code == 200
    plan_payload = planned.json()
    assert plan_payload["plan"]["localities"] == ["Tennessee", "Kentucky"]
    assert plan_payload["plan"]["facility_types"] == ["schools", "manufacturing"]

    harvested = client.post(
        "/api/harvest/campaign-run",
        json={
            "country": "US",
            "localities": ["Tennessee", "Kentucky"],
            "facility_types": ["schools", "manufacturing"],
            "target": 3,
            "campaign_id": plan_payload["run_id"],
            "geographer_plan_path": plan_payload["plan_path"],
        },
    )

    assert harvested.status_code == 200
    manifest = harvested.json()["manifest"]
    assert manifest["geographer_plan_path"] == plan_payload["plan_path"]
    for child_path in manifest["child_manifest_paths"]:
        child_manifest = json.loads(Path(child_path).read_text(encoding="utf-8"))
        assert child_manifest["geographer_plan_path"] == plan_payload["plan_path"]
        prompt = Path(child_manifest["prompt_path"]).read_text(encoding="utf-8")
        assert "Tennessee Highway Patrol" in prompt
    dialogue = client.get(f"/api/runs/{plan_payload['run_id']}/dialogue")
    assert "Geographer Agent:" in dialogue.text
    assert dialogue.text.count("Harvester Agent: I completed the search") == 4


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
    assert "Requested geographic scope" in qaqc_prompt.text
    assert "Tennessee" in qaqc_prompt.text
    assert "hard acceptance boundary" in qaqc_prompt.text
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


def test_sample_transcript_combines_pipeline_stages_and_downloads(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner, background=False))
    created = client.post(
        "/api/harvest/run",
        json={
            "country": "US",
            "locality": "Tennessee",
            "profiles": "commercial_business",
            "profile": "factories_warehouses",
            "target": 1,
        },
    ).json()
    run_id = created["manifest"]["run_id"]
    client.post(f"/api/runs/{run_id}/qaqc-run")
    client.post(f"/api/runs/{run_id}/address-run")
    sample = client.post("/api/samples/from-run", json={"run_id": run_id}).json()["sample_set"]
    sample_set_id = sample["sample_set_id"]
    client.post(f"/api/samples/{sample_set_id}/curation/approve")
    client.post(f"/api/samples/{sample_set_id}/coverage-run")

    transcript = client.get(f"/api/samples/{sample_set_id}/dialogue")
    download = client.get(f"/api/samples/{sample_set_id}/transcript.txt")

    assert transcript.status_code == 200
    assert "OASIS PIPELINE TRANSCRIPT" in transcript.text
    assert "=== INITIAL HARVEST ===" in transcript.text
    assert "=== EVIDENCE QAQC ===" in transcript.text
    assert "=== ADDRESS ENRICHMENT ===" in transcript.text
    assert "=== COVERAGE ANALYSIS ===" in transcript.text
    assert download.text == transcript.text
    assert "attachment;" in download.headers["content-disposition"]
    assert download.headers["content-disposition"].endswith('pipeline-transcript.txt"')


def test_geocode_can_retry_address_research_after_spatial_failure(tmp_path: Path) -> None:
    def correction_runner(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        if "Spatial Correction" not in prompt:
            return successful_runner(command, prompt, cwd)
        output_path = Path(command[command.index("-o") + 1])
        records = json.loads(prompt.split("## Input Records", 1)[1].strip())
        output_path.write_text(
            json.dumps(
                [
                    {
                        **ADDRESS_PAYLOAD[0],
                        "item_id": records[0]["item_id"],
                        "formatted_address": "200 Corrected Plant Road, Nashville, TN, US",
                        "address_line1": "200 Corrected Plant Road",
                        "address_source_url": "https://example.test/official-facility",
                        "review_notes": (
                            "An official facility page corrected the directory street name."
                        ),
                    }
                ]
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def geocoder(query: str) -> dict[str, object] | None:
        if "200 Corrected Plant Road" not in query:
            return {
                "display_name": "Unrelated Depot, 900 Wrong Lane, Nashville, Tennessee",
                "latitude": 36.2,
                "longitude": -86.8,
                "provider": "mock",
                "query": query,
                "name": "Unrelated Depot",
                "type": "industrial",
                "address": {
                    "house_number": "900",
                    "road": "Wrong Lane",
                    "state": "Tennessee",
                    "country_code": "us",
                },
            }
        return {
            "display_name": query,
            "latitude": 36.1,
            "longitude": -86.7,
            "provider": "mock",
            "query": query,
            "type": "industrial",
            "address": {"state": "Tennessee", "country_code": "us"},
        }

    client = TestClient(
        create_app(
            workspace=tmp_path,
            runner=correction_runner,
            geocoder=FunctionSpatialGeocoder(geocoder),
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
            "target": 1,
        },
    ).json()
    run_id = created["manifest"]["run_id"]
    client.post(f"/api/runs/{run_id}/qaqc-run")
    client.post(f"/api/runs/{run_id}/address-run")
    item = client.get(f"/api/runs/{run_id}/geometry-items").json()["items"][0]

    response = client.post(
        "/api/geometry/geocode",
        json={
            "item_id": item["item_id"],
            "query": item["geocode_query"],
            "allow_address_retry": True,
            "conversation_id": run_id,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["address_retry"]["status"] == "corrected"
    assert payload["geometry"]["point"]["latitude"] == 36.1
    assert payload["geometry"]["geocode_query"].startswith("200 Corrected Plant Road")
    address = client.get(f"/api/runs/{run_id}/address-results").json()["results"][0]
    assert address["formatted_address"].startswith("200 Corrected Plant Road")
    transcript = client.get(f"/api/runs/{run_id}/dialogue").text
    assert "=== ADDRESS-SPATIAL CORRECTION ===" in transcript
    assert "=== AUTOMATED GEOCODING ===" in transcript


def test_geocode_skips_address_retry_for_plausible_same_country_candidate(
    tmp_path: Path,
) -> None:
    correction_prompts: list[str] = []

    def runner(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        if "Spatial Correction" in prompt:
            correction_prompts.append(prompt)
        return successful_runner(command, prompt, cwd)

    def geocoder(query: str) -> dict[str, object]:
        return {
            "display_name": "Unmapped Depot, United States",
            "latitude": 36.2,
            "longitude": -86.8,
            "provider": "mock",
            "query": query,
            "name": "Unmapped Depot",
            "type": "industrial",
            "address": {"country_code": "us"},
        }

    client = TestClient(
        create_app(
            workspace=tmp_path,
            runner=runner,
            geocoder=FunctionSpatialGeocoder(geocoder),
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
            "target": 1,
        },
    ).json()
    run_id = created["manifest"]["run_id"]
    client.post(f"/api/runs/{run_id}/qaqc-run")
    client.post(f"/api/runs/{run_id}/address-run")
    item = client.get(f"/api/runs/{run_id}/geometry-items").json()["items"][0]

    response = client.post(
        "/api/geometry/geocode",
        json={
            "item_id": item["item_id"],
            "query": item["geocode_query"],
            "allow_address_retry": True,
            "conversation_id": run_id,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["address_retry"] is None
    assert correction_prompts == []
    assert payload["geocode_result"] is None
    assert payload["spatial_validation"]["status"] == "requires_human"
    assert payload["spatial_validation"]["candidate_options"][0]["confidence"] == "possible"


def test_geometry_geocode_prefers_enriched_address(tmp_path: Path) -> None:
    def geocoder(query: str) -> dict[str, object]:
        assert query == "100 Industrial Drive, Nashville, TN, US"
        return {
            "display_name": query,
            "latitude": 36.0,
            "longitude": -86.0,
            "provider": "mock",
            "query": query,
            "type": "industrial",
            "address": {"state": "Tennessee", "country_code": "us"},
        }

    client = TestClient(
        create_app(
            workspace=tmp_path,
            runner=successful_runner,
            geocoder=FunctionSpatialGeocoder(geocoder),
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


def test_geometry_geocode_uses_geographer_country_aliases(tmp_path: Path) -> None:
    def geocoder(query: str) -> dict[str, object]:
        return {
            "display_name": "Example Warehouse, Nashville, Tennessee, Estados Unidos",
            "latitude": 36.0,
            "longitude": -86.0,
            "provider": "mock",
            "query": query,
            "type": "industrial",
            "address": {
                "city": "Nashville",
                "state": "Tennessee",
                "country": "Estados Unidos",
            },
        }

    client = TestClient(
        create_app(
            workspace=tmp_path,
            runner=geographer_and_harvest_runner,
            geocoder=FunctionSpatialGeocoder(geocoder),
            background=False,
        )
    )
    planned = client.post(
        "/api/geographer/plan",
        json={
            "country": "US",
            "locality": "Tennessee",
            "profiles": "commercial_business",
            "profile": "factories_warehouses",
            "mode": "single",
        },
    ).json()
    harvested = client.post(
        "/api/harvest/run",
        json={
            "country": "US",
            "locality": "Tennessee",
            "profiles": "commercial_business",
            "profile": "factories_warehouses",
            "target": 1,
            "run_id": planned["run_id"],
            "geographer_plan_path": planned["plan_path"],
        },
    ).json()
    run_id = harvested["manifest"]["run_id"]
    client.post(f"/api/runs/{run_id}/qaqc-run")
    item = client.get(f"/api/runs/{run_id}/geometry-items").json()["items"][0]

    response = client.post(
        "/api/geometry/geocode",
        json={"item_id": item["item_id"], "query": item["geocode_query"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["spatial_validation"]["status"] == "accepted"
    assert payload["geometry"]["point"]["latitude"] == 36.0


def test_geometry_geocode_routes_same_country_uncertain_candidate_to_human_queue(
    tmp_path: Path,
) -> None:
    def geocoder(query: str) -> dict[str, object]:
        return {
            "display_name": "Unrelated Depot, Birmingham, Alabama, United States",
            "latitude": 33.52,
            "longitude": -86.80,
            "provider": "mock",
            "query": query,
            "type": "industrial",
            "address": {
                "city": "Birmingham",
                "state": "Alabama",
                "country_code": "us",
            },
        }

    client = TestClient(
        create_app(
            workspace=tmp_path,
            runner=successful_runner,
            geocoder=FunctionSpatialGeocoder(geocoder),
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
    item = client.get(f"/api/runs/{run_id}/geometry-items").json()["items"][0]

    response = client.post(
        "/api/geometry/geocode",
        json={"item_id": item["item_id"], "query": item["geocode_query"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["geocode_result"] is None
    assert payload["spatial_validation"]["status"] == "requires_human"
    assert payload["spatial_validation"]["candidate_options"][0]["confidence"] == (
        "possible"
    )
    assert payload["spatial_validation"]["candidate_options"][0]["latitude"] == 33.52
    assert payload["geometry"]["point"] is None
    assert payload["geometry"]["spatial_validation"]["requires_human_intervention"] is True


def test_geometry_research_reruns_address_agent_for_one_selected_item(
    tmp_path: Path,
) -> None:
    def geocoder(query: str) -> dict[str, object]:
        return {
            "display_name": query,
            "latitude": 36.11,
            "longitude": -86.71,
            "provider": "mock",
            "query": query,
            "name": "Example Warehouse",
            "type": "industrial",
            "address": {
                "house_number": "100",
                "road": "Industrial Drive",
                "state": "Tennessee",
                "country_code": "us",
            },
        }

    client = TestClient(
        create_app(
            workspace=tmp_path,
            runner=successful_runner,
            geocoder=FunctionSpatialGeocoder(geocoder),
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
            "target": 1,
        },
    ).json()
    run_id = created["manifest"]["run_id"]
    client.post(f"/api/runs/{run_id}/qaqc-run")
    client.post(f"/api/runs/{run_id}/address-run")
    item = client.get(f"/api/runs/{run_id}/geometry-items").json()["items"][0]

    response = client.post(
        "/api/geometry/research",
        json={"item_id": item["item_id"], "conversation_id": run_id},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["address_retry"]["status"] == "corrected"
    assert payload["research_resolved"] is True
    assert payload["geometry"]["point"]["latitude"] == 36.11
    assert payload["spatial_validation"]["manual_research"] is True
    transcript = client.get(f"/api/runs/{run_id}/dialogue").text
    assert "focused research" in transcript


def test_coordinate_preview_parses_and_spatially_checks_google_maps_coordinates(
    tmp_path: Path,
) -> None:
    class PreviewGeocoder:
        def geocode(self, query: str) -> dict[str, object] | None:
            return None

        def reverse(self, latitude: float, longitude: float) -> dict[str, object]:
            assert latitude == 36.11
            assert longitude == -86.71
            return {
                "display_name": (
                    "Example Warehouse, 100 Industrial Drive, Nashville, Tennessee, US"
                ),
                "latitude": latitude,
                "longitude": longitude,
                "provider": "mock-reverse",
                "query": "reverse",
                "name": "Example Warehouse",
                "type": "industrial",
                "address": {
                    "house_number": "100",
                    "road": "Industrial Drive",
                    "city": "Nashville",
                    "state": "Tennessee",
                    "country_code": "us",
                },
            }

    client = TestClient(
        create_app(
            workspace=tmp_path,
            runner=successful_runner,
            geocoder=PreviewGeocoder(),
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
            "target": 1,
        },
    ).json()
    run_id = created["manifest"]["run_id"]
    client.post(f"/api/runs/{run_id}/qaqc-run")
    client.post(f"/api/runs/{run_id}/address-run")
    item = client.get(f"/api/runs/{run_id}/geometry-items").json()["items"][0]

    response = client.post(
        "/api/geometry/coordinate-preview",
        json={
            "item_id": item["item_id"],
            "coordinate_text": "https://www.google.com/maps/@36.1100,-86.7100,18z",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["point"] == {
        "latitude": 36.11,
        "longitude": -86.71,
        "source": "google-maps-human",
    }
    assert payload["normalized"] == "36.1100000, -86.7100000"
    assert payload["spatial_validation"]["status"] == "in_scope"
    assert payload["spatial_validation"]["warning"] is False
    unchanged = client.get(f"/api/runs/{run_id}/geometry-items").json()["items"][0]
    assert unchanged["geometry"] is None


def test_geometry_geocode_all_reports_successes_and_not_found(tmp_path: Path) -> None:
    calls: list[str] = []

    def geocoder(query: str) -> dict[str, object] | None:
        calls.append(query)
        if len(calls) >= 2:
            return None
        return {
            "display_name": query,
            "latitude": 36.0,
            "longitude": -86.0,
            "provider": "mock",
            "query": query,
            "type": "industrial",
            "address": {"state": "Tennessee", "country_code": "us"},
        }

    client = TestClient(
        create_app(
            workspace=tmp_path,
            runner=successful_runner,
            geocoder=FunctionSpatialGeocoder(geocoder),
            background=False,
        )
    )
    created = client.post(
        "/api/harvest/campaign-run",
        json={
            "country": "US",
            "localities": ["Tennessee"],
            "facility_types": ["schools", "manufacturing"],
            "target": 2,
        },
    ).json()
    campaign_id = created["manifest"]["campaign_id"]
    client.post(f"/api/runs/{campaign_id}/qaqc-run")
    items = client.get(f"/api/runs/{campaign_id}/geometry-items").json()["items"]

    response = client.post(
        "/api/geometry/geocode-all",
        json={
            "items": [
                {"item_id": item["item_id"], "query": item["geocode_query"]}
                for item in items
            ]
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["requested_count"] == 2
    assert payload["geocoded_count"] == 1
    assert payload["not_found_count"] == 1
    assert payload["error_count"] == 0
    assert len(payload["items"]) == 2
    refreshed = client.get(f"/api/runs/{campaign_id}/geometry-items").json()["items"]
    assert sum(item["geometry_status"] == "point_confirmed" for item in refreshed) == 1


def test_geometry_review_endpoints_and_verified_exports(tmp_path: Path) -> None:
    def geocoder(query: str) -> dict[str, object]:
        assert "Example Warehouse" in query
        return {
            "display_name": "Example Warehouse, Tennessee",
            "latitude": 36.0,
            "longitude": -86.0,
            "provider": "mock",
            "query": query,
            "type": "industrial",
            "address": {"state": "Tennessee", "country_code": "us"},
        }

    client = TestClient(
        create_app(
            workspace=tmp_path,
            runner=successful_runner,
            geocoder=FunctionSpatialGeocoder(geocoder),
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
    admin_scoped_json = client.get(f"/api/runs/{run_id}/export.admin_scoped.json")
    admin_scoped_csv = client.get(f"/api/runs/{run_id}/export.admin_scoped.csv")
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
    admin_scoped_after_point = client.get(f"/api/runs/{run_id}/export.admin_scoped.json")
    footprints = client.get(f"/api/runs/{run_id}/export.footprints.geojson")

    assert verified.status_code == 200
    assert verified.json()["item_count"] == 1
    assert geometry_items.json()["items"][0]["geometry_status"] == "needs_review"
    assert admin_scoped_json.status_code == 200
    assert admin_scoped_json.json()[0]["spatial_certainty"] == "admin_scoped"
    assert admin_scoped_json.json()[0]["admin_level"] == "locality"
    assert admin_scoped_json.json()[0]["admin_name"] == "Tennessee"
    assert "admin_scoped" in admin_scoped_csv.text
    assert geocoded.json()["geometry"]["point"]["latitude"] == 36.0
    assert saved.json()["geometry"]["geometry_status"] == "footprint_drawn"
    assert saved.json()["geometry"]["area_m2"] > 0
    assert [geometry["type"] for geometry in saved.json()["geometry"]["geometries"]] == [
        "Point",
        "Polygon",
    ]
    verified_record = verified_json.json()[0]
    assert [geometry["type"] for geometry in verified_record["geometries"]] == [
        "Point",
        "Polygon",
    ]
    assert verified_record["area_m2"] > 0
    assert "Example Warehouse" in verified_json.text
    assert "footprint_drawn" in verified_csv.text
    assert admin_scoped_after_point.json() == []
    assert footprints.json()["features"][0]["geometry"]["type"] == "Polygon"


def test_run_table_endpoint_pivots_occupancy_counts_wide(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=multi_count_runner, background=False))
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

    response = client.get(f"/api/runs/{run_id}/table?mode=all")

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "all"
    assert payload["context_type"] == "run"
    assert payload["row_count"] == 1
    row = payload["rows"][0]
    assert row["item_id"] == f"{run_id}-0"
    assert row["row_id"] == f"{run_id}-0"
    assert row["count"] == 16
    assert row["count_index"] == ""
    assert row["group_type"] == "workers evacuated, security staff"
    assert row["count_values"] == {
        "count_security_staff": 4,
        "count_workers_evacuated": 12,
    }
    assert row["count_security_staff"] == 4
    assert row["count_workers_evacuated"] == 12
    assert row["count_relationship"] == "additive_subgroups"
    assert row["qaqc_status"] == ""


def test_run_table_verified_includes_review_address_and_geometry_fields(
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
    item = client.get(f"/api/runs/{run_id}/geometry-items").json()["items"][0]
    saved = client.post(
        f"/api/geometry/items/{item['item_id']}",
        json={
            "item_id": item["item_id"],
            "geocode_query": item["geocode_query"],
            "point": {"latitude": 36.0, "longitude": -86.0, "source": "user"},
            "geometry_status": "point_confirmed",
            "review_notes": "Coordinate confirmed.",
        },
    )

    response = client.get(f"/api/runs/{run_id}/table?mode=verified")

    assert saved.status_code == 200
    assert response.status_code == 200
    row = response.json()["rows"][0]
    assert row["qaqc_status"] == "verified"
    assert row["recommended_action"] == "keep"
    assert row["address_status"] == "found"
    assert row["enriched_address"] == "100 Industrial Drive, Nashville, TN, US"
    assert row["geometry_status"] == "point_confirmed"
    assert row["source_url"] == "https://example.test/story"
    assert row["review_notes"] == "Count, facility, and location are supported."


def test_run_table_and_exports_include_verified_component_rows(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner, background=False))
    created = client.post(
        "/api/harvest/run",
        json={
            "country": "US",
            "locality": "Tennessee",
            "profiles": "schools",
            "profile": "primary_secondary_education",
            "target": 1,
        },
    ).json()
    run_id = created["manifest"]["run_id"]
    lead_path = tmp_path / f"lead_runs/{run_id}.json"
    qaqc_path = tmp_path / f"qaqc_runs/{run_id}-qaqc.json"
    lead_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "occupancy_leads": [],
                "component_leads": [
                    {
                        "is_valid_component_report": True,
                        "source_url": "https://example.test/school",
                        "source_title": "School profile",
                        "source_type": "official",
                        "evidence_quote": "Enrollment was 512 students in SY 2025.",
                        "component_data": [
                            {
                                "component_type": "Students",
                                "value": 512,
                                "unit": "people",
                                "time_basis": "school_year",
                                "geography_level": "facility",
                                "period_label": "SY 2025",
                            },
                            {
                                "component_type": "Staff",
                                "value": 44,
                                "unit": "people",
                                "time_basis": "school_year",
                                "geography_level": "facility",
                                "period_label": "SY 2025",
                            }
                        ],
                        "location": {
                            "facility_name": "Example School",
                            "specific_address_or_landmark": "10 Main Street",
                            "city_or_region": "Tennessee",
                            "country": "US",
                        },
                        "geography_name": "Example School",
                        "country": "US",
                        "strategy_id": "official_facility_statistics",
                        "count_semantics": "component_input",
                        "representativeness": "component_input",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    qaqc_path.parent.mkdir(exist_ok=True)
    qaqc_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "occupancy_reviews": [],
                "component_reviews": [
                    {
                        "lead_index": 0,
                        "source_url": "https://example.test/school",
                        "verification_status": "verified",
                        "source_reachable": True,
                        "evidence_role_match": True,
                        "component_type_match": True,
                        "geography_level_match": True,
                        "component_checks": [
                            {
                                "component_type": "Students",
                                "value": 512,
                                "unit": "people",
                                "reported_value_found": True,
                                "quote_found": True,
                            },
                            {
                                "component_type": "Staff",
                                "value": 44,
                                "unit": "people",
                                "reported_value_found": True,
                                "quote_found": True,
                            }
                        ],
                        "recommended_action": "keep",
                        "review_notes": "Component input is supported.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    address = client.post(f"/api/runs/{run_id}/address-run")
    table = client.get(f"/api/runs/{run_id}/table?mode=verified")
    component_json = client.get(f"/api/runs/{run_id}/export.components.json")
    component_csv = client.get(f"/api/runs/{run_id}/export.components.csv")

    assert address.status_code == 200
    assert address.json()["address"]["summary"]["result_count"] == 1
    assert table.status_code == 200
    assert table.json()["row_count"] == 1
    row = table.json()["rows"][0]
    assert row["evidence_role"] == "component_input"
    assert row["component_type"] == "Students, Staff"
    assert row["component_values"]["Students"] == "512 people (school_year, facility, SY 2025)"
    assert row["component_values"]["Staff"] == "44 people (school_year, facility, SY 2025)"
    assert row["address_status"] == "found"
    assert row["enriched_address"].startswith("100")
    assert row["geometry_status"] == "not_applicable"
    assert component_json.status_code == 200
    assert component_json.json()[0]["component_lead"]["geography_name"] == "Example School"
    assert "Students" in component_csv.text


def test_component_bundle_address_reconciliation_and_verified_table(
    tmp_path: Path,
) -> None:
    def skipped_address_runner(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        output_path = Path(command[command.index("-o") + 1])
        if "Facility Address Enrichment" in prompt:
            output_path.write_text("[]", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return successful_runner(command, prompt, cwd)

    client = TestClient(
        create_app(workspace=tmp_path, runner=skipped_address_runner, background=False)
    )
    created = client.post(
        "/api/harvest/run",
        json={
            "country": "US",
            "locality": "Tennessee",
            "profiles": "schools",
            "profile": "primary_secondary_education",
            "target": 1,
        },
    ).json()
    run_id = created["manifest"]["run_id"]
    lead_path = tmp_path / f"lead_runs/{run_id}.json"
    qaqc_path = tmp_path / f"qaqc_runs/{run_id}-qaqc.json"
    lead_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "occupancy_leads": [],
                "component_leads": [
                    {
                        "is_valid_component_report": True,
                        "source_url": "https://example.test/school-enrollment",
                        "source_title": "School enrollment",
                        "source_type": "official",
                        "evidence_quote": "Enrollment was 512 students in SY 2025.",
                        "component_data": [
                            {
                                "component_type": "Students",
                                "value": 512,
                                "unit": "people",
                                "time_basis": "school_year",
                                "geography_level": "facility",
                                "period_label": "SY 2025",
                            }
                        ],
                        "location": {
                            "facility_name": "Example School",
                            "specific_address_or_landmark": "10 Main Street",
                            "city_or_region": "Tennessee",
                            "country": "US",
                        },
                        "geography_name": "Example School",
                        "country": "US",
                        "strategy_id": "official_facility_statistics",
                        "count_semantics": "component_input",
                        "representativeness": "component_input",
                    },
                    {
                        "is_valid_component_report": True,
                        "source_url": "https://example.test/school-staff",
                        "source_title": "School staffing",
                        "source_type": "official",
                        "evidence_quote": "The school employed 44 staff in SY 2025.",
                        "component_data": [
                            {
                                "component_type": "Staff",
                                "value": 44,
                                "unit": "people",
                                "time_basis": "school_year",
                                "geography_level": "facility",
                                "period_label": "SY 2025",
                            }
                        ],
                        "location": {
                            "facility_name": "Example School",
                            "specific_address_or_landmark": "10 Main Street",
                            "city_or_region": "Tennessee",
                            "country": "US",
                        },
                        "geography_name": "Example School",
                        "country": "US",
                        "strategy_id": "official_facility_statistics",
                        "count_semantics": "component_input",
                        "representativeness": "component_input",
                    },
                ],
                "component_bundles": [
                    {
                        "geography_name": "Example School",
                        "country": "US",
                        "location": {
                            "facility_name": "Example School",
                            "specific_address_or_landmark": "10 Main Street",
                            "city_or_region": "Tennessee",
                            "country": "US",
                        },
                        "target_component_fields": ["Students", "Staff"],
                        "found_component_types": ["Students", "Staff"],
                        "missing_component_types": [],
                        "source_lead_indexes": [0, 1],
                        "follow_up_searches_attempted": [
                            '"Example School" staff',
                        ],
                        "completion_status": "complete",
                        "counts_toward_target": True,
                        "confidence": "high",
                        "completion_notes": "Enrollment and staff were both found.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    qaqc_path.parent.mkdir(exist_ok=True)
    qaqc_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "occupancy_reviews": [],
                "component_reviews": [
                    {
                        "lead_index": 0,
                        "source_url": "https://example.test/school-enrollment",
                        "verification_status": "verified",
                        "source_reachable": True,
                        "evidence_role_match": True,
                        "component_type_match": True,
                        "geography_level_match": True,
                        "recommended_action": "keep",
                        "review_notes": "Student component is supported.",
                    },
                    {
                        "lead_index": 1,
                        "source_url": "https://example.test/school-staff",
                        "verification_status": "verified",
                        "source_reachable": True,
                        "evidence_role_match": True,
                        "component_type_match": True,
                        "geography_level_match": True,
                        "recommended_action": "keep",
                        "review_notes": "Staff component is supported.",
                    },
                ],
                "component_bundle_reviews": [
                    {
                        "bundle_index": 0,
                        "item_id": f"{run_id}-component-bundle-0",
                        "geography_name": "Example School",
                        "verification_status": "verified",
                        "source_lead_indexes_valid": True,
                        "same_facility_or_geography": True,
                        "component_fields_match": True,
                        "completion_status_match": True,
                        "counts_toward_target_approved": True,
                        "found_component_types": ["Students", "Staff"],
                        "missing_component_types": [],
                        "source_lead_indexes": [0, 1],
                        "recommended_action": "keep",
                        "review_notes": (
                            "Bundle is a complete facility-level component observation."
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    address = client.post(f"/api/runs/{run_id}/address-run")
    address_results = client.get(f"/api/runs/{run_id}/address-results")
    reviews = client.get(f"/api/runs/{run_id}/qaqc-reviews")
    table = client.get(f"/api/runs/{run_id}/table?mode=verified")
    workflow = client.get(f"/api/runs/{run_id}/workflow-status")

    assert address.status_code == 200
    assert address.json()["address"]["summary"]["expected_count"] == 1
    assert address.json()["address"]["summary"]["missing_count"] == 1
    assert address.json()["address"]["summary"]["missing_item_ids"] == [
        f"{run_id}-component-bundle-0"
    ]
    assert address_results.json()["reconciliation"]["expected_count"] == 1
    assert address_results.json()["reconciliation"]["missing_item_ids"] == []
    saved_address = address_results.json()["results"][0]
    assert saved_address["item_id"] == f"{run_id}-component-bundle-0"
    assert saved_address["status"] == "needs_review"
    assert reviews.json()["component_bundle_reviews"][0]["item_id"] == (
        f"{run_id}-component-bundle-0"
    )
    row = table.json()["rows"][0]
    assert row["evidence_role"] == "component_bundle"
    assert row["item_id"] == f"{run_id}-component-bundle-0"
    assert row["component_values"]["Students"] == "512 people (school_year, facility, SY 2025)"
    assert row["component_values"]["Staff"] == "44 people (school_year, facility, SY 2025)"
    assert row["address_status"] == "needs_review"
    assert row["bundle_qaqc_status"] == "verified"
    qaqc_stage = next(stage for stage in workflow.json()["stages"] if stage["id"] == "qaqc")
    assert qaqc_stage["current"] == 1
    assert qaqc_stage["total"] == 1
    assert "target observation(s) reviewed" in qaqc_stage["detail"]
    assert "supporting component evidence record(s)" in qaqc_stage["detail"]
    assert qaqc_stage["metrics"]["raw_evidence_record_count"] == 3
    assert qaqc_stage["metrics"]["raw_review_count"] == 3
    assert qaqc_stage["metrics"]["supporting_component_review_count"] == 2


def test_partial_component_bundles_are_addressable_but_not_verified_rows(
    tmp_path: Path,
) -> None:
    def held_bundle_runner(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        output_path = Path(command[command.index("-o") + 1])
        if "QAQC Verification" in prompt:
            child_run_id = output_path.name.removesuffix("-qaqc.json")
            output_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "occupancy_reviews": [],
                        "component_reviews": [
                            {
                                "lead_index": 0,
                                "source_url": "https://example.test/school-enrollment",
                                "verification_status": "verified",
                                "source_reachable": True,
                                "evidence_role_match": True,
                                "component_type_match": True,
                                "geography_level_match": True,
                                "recommended_action": "keep",
                                "review_notes": "Student component is supported.",
                            }
                        ],
                        "component_bundle_reviews": [
                            {
                                "bundle_index": 0,
                                "item_id": f"{child_run_id}-component-bundle-0",
                                "geography_name": "Example School",
                                "verification_status": "ambiguous",
                                "source_lead_indexes_valid": True,
                                "same_facility_or_geography": True,
                                "component_fields_match": True,
                                "completion_status_match": True,
                                "counts_toward_target_approved": False,
                                "found_component_types": ["Students"],
                                "missing_component_types": ["Staff"],
                                "source_lead_indexes": [0],
                                "recommended_action": "review",
                                "review_notes": "Bundle is partial because staff is missing.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return successful_runner(command, prompt, cwd)

    client = TestClient(create_app(workspace=tmp_path, runner=held_bundle_runner, background=False))
    created = client.post(
        "/api/harvest/run",
        json={
            "country": "US",
            "locality": "Tennessee",
            "profiles": "schools",
            "profile": "primary_secondary_education",
            "target": 1,
        },
    ).json()
    run_id = created["manifest"]["run_id"]
    lead_path = tmp_path / f"lead_runs/{run_id}.json"
    qaqc_path = tmp_path / f"qaqc_runs/{run_id}-qaqc.json"
    lead_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "occupancy_leads": [],
                "component_leads": [
                    {
                        "is_valid_component_report": True,
                        "source_url": "https://example.test/school-enrollment",
                        "source_title": "School enrollment",
                        "source_type": "official",
                        "evidence_quote": "Enrollment was 512 students in SY 2025.",
                        "component_data": [
                            {
                                "component_type": "Students",
                                "value": 512,
                                "unit": "people",
                                "time_basis": "school_year",
                                "geography_level": "facility",
                                "period_label": "SY 2025",
                            }
                        ],
                        "location": {
                            "facility_name": "Example School",
                            "specific_address_or_landmark": "10 Main Street",
                            "city_or_region": "Tennessee",
                            "country": "US",
                        },
                        "geography_name": "Example School",
                        "country": "US",
                        "strategy_id": "official_facility_statistics",
                        "count_semantics": "component_input",
                        "representativeness": "component_input",
                    }
                ],
                "component_bundles": [
                    {
                        "geography_name": "Example School",
                        "country": "US",
                        "location": {
                            "facility_name": "Example School",
                            "specific_address_or_landmark": "10 Main Street",
                            "city_or_region": "Tennessee",
                            "country": "US",
                        },
                        "target_component_fields": ["Students", "Staff"],
                        "found_component_types": ["Students"],
                        "missing_component_types": ["Staff"],
                        "source_lead_indexes": [0],
                        "follow_up_searches_attempted": [],
                        "completion_status": "partial",
                        "counts_toward_target": False,
                        "confidence": "medium",
                        "completion_notes": "Staff component is missing.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    qaqc_path.parent.mkdir(exist_ok=True)
    qaqc_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "occupancy_reviews": [],
                "component_reviews": [
                    {
                        "lead_index": 0,
                        "source_url": "https://example.test/school-enrollment",
                        "verification_status": "verified",
                        "source_reachable": True,
                        "evidence_role_match": True,
                        "component_type_match": True,
                        "geography_level_match": True,
                        "recommended_action": "keep",
                        "review_notes": "Student component is supported.",
                    }
                ],
                "component_bundle_reviews": [
                    {
                        "bundle_index": 0,
                        "item_id": f"{run_id}-component-bundle-0",
                        "geography_name": "Example School",
                        "verification_status": "ambiguous",
                        "source_lead_indexes_valid": True,
                        "same_facility_or_geography": True,
                        "component_fields_match": True,
                        "completion_status_match": True,
                        "counts_toward_target_approved": False,
                        "found_component_types": ["Students"],
                        "missing_component_types": ["Staff"],
                        "source_lead_indexes": [0],
                        "recommended_action": "review",
                        "review_notes": "Bundle is partial because staff is missing.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    qaqc = client.post(f"/api/runs/{run_id}/qaqc-run")
    address = client.post(f"/api/runs/{run_id}/address-run")
    workflow = client.get(f"/api/runs/{run_id}/workflow-status").json()
    verified_table = client.get(f"/api/runs/{run_id}/table?mode=verified").json()
    all_table = client.get(f"/api/runs/{run_id}/table?mode=all").json()
    component_export = client.get(f"/api/runs/{run_id}/export.components.json").json()
    transcript = client.get(f"/api/runs/{run_id}/dialogue").text
    geometry = client.get(f"/api/runs/{run_id}/geometry-items").json()
    sample = client.post("/api/samples/from-run", json={"run_id": run_id})

    assert qaqc.status_code == 200
    assert address.status_code == 200
    assert address.json()["address"]["summary"]["expected_count"] == 1
    assert verified_table["rows"] == []
    assert all_table["rows"][0]["item_id"] == f"{run_id}-component-bundle-0"
    assert all_table["rows"][0]["bundle_readiness"] == "partial_component_bundle"
    assert all_table["rows"][0]["bundle_review_required"] is True
    assert all_table["rows"][0]["address_status"] == "found"
    assert geometry["item_count"] == 1
    assert geometry["items"][0]["bundle_readiness"] == "partial_component_bundle"
    assert len(component_export) == 1
    assert component_export[0]["item_id"] == f"{run_id}-component-0"
    address_stage = next(stage for stage in workflow["stages"] if stage["id"] == "address")
    assert address_stage["status"] == "complete"
    assert "1 partial candidate bundle(s)" in address_stage["detail"]
    assert address_stage["metrics"]["partial_component_bundle_count"] == 1
    assert address_stage["metrics"]["held_component_bundle_count"] == 0
    sample_stage = next(stage for stage in workflow["stages"] if stage["id"] == "sample")
    assert sample_stage["status"] == "ready"
    assert sample.status_code == 200
    sample_id = sample.json()["sample_set"]["sample_set_id"]
    sample_table = client.get(f"/api/samples/{sample_id}/table").json()
    sample_workflow = client.get(f"/api/samples/{sample_id}/workflow-status").json()
    sample_export = client.get(f"/api/samples/{sample_id}/export.verified.json")
    sample_admin_export = client.get(f"/api/samples/{sample_id}/export.admin_scoped.json")
    assert sample_table["row_count"] == 1
    assert sample_table["rows"][0]["bundle_readiness"] == "partial_component_bundle"
    assert sample_table["rows"][0]["model_ready"] is False
    assert sample_table["rows"][0]["bundle_review_required"] is True
    assert sample_export.status_code == 200
    assert sample_export.json() == []
    assert sample_admin_export.status_code == 200
    assert sample_admin_export.json()[0]["spatial_certainty"] == "admin_scoped"
    assert sample_admin_export.json()[0]["facility_name"] == "Example School"
    export_stage = next(stage for stage in sample_workflow["stages"] if stage["id"] == "export")
    assert export_stage["status"] == "blocked"
    assert "1 partial candidate is addressable" in transcript
    assert "partial candidate" in transcript
    assert "Common missing bundle fields" in transcript


def test_sample_table_endpoint_aggregates_verified_rows_across_rounds(
    tmp_path: Path,
) -> None:
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
    client.post(f"/api/runs/{campaign_id}/qaqc-run")
    client.post(f"/api/runs/{campaign_id}/address-run")
    sample = client.post(
        "/api/samples/from-run",
        json={"run_id": campaign_id, "sample_set_id": "tn-schools-sample"},
    )

    response = client.get("/api/samples/tn-schools-sample/table?mode=verified")

    assert sample.status_code == 200
    assert response.status_code == 200
    payload = response.json()
    assert payload["context_type"] == "sample"
    assert payload["row_count"] == 1
    row = payload["rows"][0]
    assert row["sample_set_id"] == "tn-schools-sample"
    assert row["sample_round"] == 1
    assert row["run_id"] == child_run_id
    assert row["facility_type"] == "schools"


def test_verified_table_requires_qaqc(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner, background=False))
    created = client.post(
        "/api/harvest/run",
        json={"country": "US", "locality": "Tennessee", "profiles": "schools", "target": 5},
    ).json()
    run_id = created["manifest"]["run_id"]

    response = client.get(f"/api/runs/{run_id}/table?mode=verified")

    assert response.status_code == 409
    assert "QAQC review not found" in response.json()["error"]


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


def test_sample_set_coverage_and_gap_fill_api_flow(tmp_path: Path) -> None:
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
    client.post(f"/api/runs/{campaign_id}/qaqc-run")
    client.post(f"/api/runs/{campaign_id}/address-run")

    sample = client.post(
        "/api/samples/from-run",
        json={"run_id": campaign_id, "sample_set_id": "tn-schools-sample"},
    )
    summary = client.get("/api/samples/tn-schools-sample/coverage-summary")
    approval = client.post("/api/samples/tn-schools-sample/curation/approve")
    coverage = client.post("/api/samples/tn-schools-sample/coverage-run")
    workflow = client.get("/api/samples/tn-schools-sample/workflow-status")
    coverage_results = client.get("/api/samples/tn-schools-sample/coverage-results")
    gap_fill = client.post("/api/samples/tn-schools-sample/gap-fill-run", json={})
    missing_qaqc = client.post("/api/samples/tn-schools-sample/qaqc-missing")
    missing_address = client.post("/api/samples/tn-schools-sample/address-missing")
    geometry_items = client.get("/api/samples/tn-schools-sample/geometry-items")
    exported = client.get("/api/samples/tn-schools-sample/export.verified.csv")

    assert sample.status_code == 200
    assert sample.json()["sample_set"]["combined_child_run_ids"] == created["manifest"][
        "child_run_ids"
    ]
    assert summary.json()["summary"]["approved_count"] == 1
    assert approval.json()["curation"]["approval_status"] == "approved"
    assert approval.json()["curation"]["excluded_count"] == 0
    assert coverage.status_code == 200
    assert coverage.json()["coverage"]["review"]["dispersion_status"] == "clustered"
    workflow_stages = {stage["id"]: stage for stage in workflow.json()["stages"]}
    assert workflow_stages["sample"]["label"] == "Assemble Review Dataset"
    assert workflow_stages["sample"]["display_mode"] == "gate"
    assert workflow_stages["curation"]["label"] == "Approve / Exclude Observations"
    assert workflow_stages["curation"]["display_mode"] == "gate"
    assert workflow_stages["coverage"]["label"] == "Check Coverage"
    assert workflow_stages["coverage"]["display_mode"] == "gate"
    assert "Coverage gaps found" in workflow_stages["coverage"]["detail"]
    assert workflow_stages["gap_fill"]["label"] == "Run Coverage Gap Follow-ups"
    assert workflow_stages["gap_fill"]["display_mode"] == "job_progress"
    assert workflow_stages["gap_fill"]["action_id"] == "run_gap_fill"
    assert workflow_stages["gap_fill"]["action_label"] == "Run Coverage Gap Follow-ups"
    assert coverage_results.json()["review"]["recommended_child_jobs"][0]["locality"] == (
        "Western Tennessee"
    )
    assert gap_fill.json()["sample_set"]["rounds"][1]["role"] == "gap_fill"
    assert len(gap_fill.json()["sample_set"]["combined_child_run_ids"]) == 2
    assert missing_qaqc.json()["qaqc"]["child_run_ids"]
    assert missing_address.json()["address"]["child_run_ids"]
    assert geometry_items.json()["item_count"] == 2
    assert "sample_round" in exported.text


def test_workflow_status_explains_failed_targeted_follow_ups(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=partial_gap_runner, background=False))
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
    client.post(f"/api/runs/{campaign_id}/qaqc-run")
    client.post(f"/api/runs/{campaign_id}/address-run")
    client.post(
        "/api/samples/from-run",
        json={"run_id": campaign_id, "sample_set_id": "tn-schools-partial-gap"},
    )
    client.post("/api/samples/tn-schools-partial-gap/curation/approve")
    client.post("/api/samples/tn-schools-partial-gap/coverage-run")
    client.post("/api/samples/tn-schools-partial-gap/gap-fill-run", json={})

    workflow = client.get("/api/samples/tn-schools-partial-gap/workflow-status")
    stages = {stage["id"]: stage for stage in workflow.json()["stages"]}
    gap_stage = stages["gap_fill"]

    assert gap_stage["status"] == "attention"
    assert gap_stage["current"] == 1
    assert gap_stage["total"] == 2
    assert "1/2 coverage gap follow-up job(s) succeeded; 1 need repair or retry." in (
        gap_stage["detail"]
    )
    assert "Western Tennessee" in gap_stage["detail"]
    assert gap_stage["action_label"] == "Retry Coverage Gap Follow-ups"
    assert gap_stage["metrics"]["failed_count"] == 1
    assert gap_stage["metrics"]["failed_child_runs"][0]["error_message"] == "harvest failed"


def test_workflow_status_marks_targeted_follow_ups_not_needed(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=no_gap_runner, background=False))
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
    client.post(f"/api/runs/{campaign_id}/qaqc-run")
    client.post(f"/api/runs/{campaign_id}/address-run")
    sample = client.post(
        "/api/samples/from-run",
        json={"run_id": campaign_id, "sample_set_id": "tn-schools-balanced"},
    )
    client.post("/api/samples/tn-schools-balanced/curation/approve")
    coverage = client.post("/api/samples/tn-schools-balanced/coverage-run")

    workflow = client.get("/api/samples/tn-schools-balanced/workflow-status")
    stages = {stage["id"]: stage for stage in workflow.json()["stages"]}

    assert sample.status_code == 200
    assert coverage.status_code == 200
    assert stages["coverage"]["detail"] == (
        "Coverage sufficient. No coverage gap follow-ups recommended."
    )
    assert stages["coverage"]["display_mode"] == "gate"
    assert stages["gap_fill"]["status"] == "complete"
    assert stages["gap_fill"]["detail"] == (
        "Not needed. No coverage gap follow-ups are currently recommended."
    )
    assert stages["gap_fill"]["display_mode"] == "gate"
    assert stages["gap_fill"]["action_id"] is None


def test_sample_curation_supports_zero_feedback_exclusion_and_restore(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner, background=False))
    created = client.post(
        "/api/harvest/run",
        json={
            "country": "US",
            "locality": "Tennessee",
            "profiles": "commercial_business",
            "profile": "factories_warehouses",
            "target": 1,
        },
    ).json()
    run_id = created["manifest"]["run_id"]
    client.post(f"/api/runs/{run_id}/qaqc-run")
    sample = client.post(
        "/api/samples/from-run",
        json={"run_id": run_id, "sample_set_id": "curation-sample"},
    )
    table = client.get("/api/samples/curation-sample/table?mode=verified").json()
    item_id = table["rows"][0]["item_id"]

    blocked = client.post("/api/samples/curation-sample/coverage-run")
    approved = client.post("/api/samples/curation-sample/curation/approve")

    assert sample.status_code == 200
    assert blocked.status_code == 409
    assert approved.json()["curation"]["approval_status"] == "approved"
    assert approved.json()["curation"]["excluded_count"] == 0

    excluded = client.post(
        "/api/samples/curation-sample/curation/exclude",
        json={
            "item_ids": [item_id],
            "reason_code": "wrong_facility",
            "reason_note": "The source describes a warehouse rather than the target facility.",
        },
    )
    curated_table = client.get("/api/samples/curation-sample/table?mode=verified").json()
    curated_export = client.get("/api/samples/curation-sample/export.verified.csv")

    assert excluded.json()["curation"]["approval_status"] == "stale"
    assert curated_table["rows"][0]["excluded_from_dataset"] is True
    assert curated_table["rows"][0]["exclusion_reason_code"] == "wrong_facility"
    assert "Example Factory" not in curated_export.text
    assert client.post("/api/samples/curation-sample/coverage-run").status_code == 409

    restored = client.post(
        "/api/samples/curation-sample/curation/restore",
        json={"item_ids": [item_id]},
    )
    reapproved = client.post("/api/samples/curation-sample/curation/approve")

    assert restored.json()["curation"]["excluded_count"] == 0
    assert reapproved.json()["curation"]["approval_status"] == "approved"


def test_sample_address_missing_skips_children_that_need_qaqc_first(tmp_path: Path) -> None:
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
    sample = client.post(
        "/api/samples/from-run",
        json={"run_id": campaign_id, "sample_set_id": "tn-schools-sample"},
    )

    response = client.post("/api/samples/tn-schools-sample/address-missing")

    assert sample.status_code == 200
    assert response.status_code == 200
    assert response.json()["address"]["child_run_ids"] == []
    assert response.json()["address"]["skipped_needs_qaqc"] == created["manifest"][
        "child_run_ids"
    ]


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
    sample_file = tmp_path / "sample_sets/sample.json"
    coverage_file = tmp_path / "coverage_runs/sample-coverage.json"
    sample_file.parent.mkdir()
    coverage_file.parent.mkdir()
    sample_file.write_text("{}", encoding="utf-8")
    coverage_file.write_text("{}", encoding="utf-8")

    response = client.post("/api/runs/clear")

    assert response.status_code == 200
    assert response.json()["cleared"] is True
    assert client.get("/api/runs").json()["runs"] == []
    assert not any((tmp_path / "harvest_runs").glob("*.json"))
    assert not any((tmp_path / "lead_runs").glob("*.json"))
    assert not any((tmp_path / "harvest_logs").glob("*.log"))
    assert not any((tmp_path / "qaqc_runs").glob("*.json"))
    assert not any((tmp_path / "address_runs").glob("*.json"))
    assert not any((tmp_path / "sample_sets").glob("*.json"))
    assert not any((tmp_path / "coverage_runs").glob("*.json"))
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
    fake_codex = _write_fake_codex(
        tmp_path,
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
    )
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
    assert created.json()["manifest"]["status"] == "queued"
    assert created.json()["job"]["status"] == "queued"
    assert status.json()["active"] is True
    assert "Manifest prepared" in log.text or "Launching Codex command" in log.text
    assert clear.status_code == 409
    assert "Cannot clear history" in clear.json()["error"]
    assert cancelled.json()["cancelled"] is True
    assert final_status["manifest"]["status"] == "cancelled"
    assert exited.json()["shutting_down"] is True
    assert exit_called is True


def test_background_harvest_exception_writes_failed_job_and_manifest(tmp_path: Path) -> None:
    client = TestClient(create_app(workspace=tmp_path, runner=raising_runner))

    created = client.post(
        "/api/harvest/run",
        json={"country": "US", "locality": "Tennessee", "profiles": "schools", "target": 5},
    )
    run_id = created.json()["manifest"]["run_id"]

    for _ in range(40):
        status = client.get(f"/api/runs/{run_id}/status").json()
        if status["manifest"]["status"] == "failed":
            break
        time.sleep(0.05)

    assert created.status_code == 200
    assert status["manifest"]["status"] == "failed"
    assert status["manifest"]["error_message"] == "runner exploded"
    assert status["job"]["status"] == "failed"
    assert (tmp_path / f"harvest_runs/{run_id}.json").is_file()
    assert "runner exploded" in (tmp_path / f"harvest_logs/{run_id}.log").read_text(
        encoding="utf-8"
    )


def test_running_job_from_prior_session_is_visible_but_inactive(tmp_path: Path) -> None:
    job = create_job(
        tmp_path,
        job_id="prior-session-run",
        job_type=JobType.HARVEST,
        log_path=str(tmp_path / "harvest_logs/prior-session-run.log"),
    )
    mark_job_running(tmp_path, job.job_id)
    client = TestClient(create_app(workspace=tmp_path, runner=successful_runner))

    runs = client.get("/api/runs")
    status = client.get("/api/runs/prior-session-run/status")

    assert runs.status_code == 200
    assert runs.json()["runs"][0]["run_id"] == "prior-session-run"
    assert status.status_code == 200
    assert status.json()["manifest"]["status"] == "running"
    assert status.json()["active"] is False
    assert status.json()["job"]["active"] is False


def test_launcher_references_bootstrap_steps() -> None:
    mac_launcher = Path("OASIS.command").read_text(encoding="utf-8")
    windows_launcher = Path("OASIS.bat").read_text(encoding="utf-8")

    assert ".venv" in mac_launcher
    assert "ensurepip --upgrade" in mac_launcher
    assert '.[app]' in mac_launcher
    assert "command -v codex" in mac_launcher
    assert "APP_PORT" in mac_launcher
    assert "OASIS_PORT" in mac_launcher
    assert "8771" in mac_launcher
    assert "python -m pdt_observer app" in mac_launcher

    assert ".venv\\Scripts\\python.exe" in windows_launcher
    assert 'set "WORKSPACE_DIR=%APP_DIR:~0,-1%"' in windows_launcher
    assert '--workspace "%WORKSPACE_DIR%"' in windows_launcher
    assert "ensurepip --upgrade" in windows_launcher
    assert '.[app]' in windows_launcher
    assert "OASIS_CODEX_BIN" in windows_launcher
    assert "OBSERVATION_HARVESTER_CODEX_BIN" in windows_launcher
    assert "where codex.exe" in windows_launcher
    assert '--codex-bin "%CODEX_BIN%"' in windows_launcher
    assert "OBSERVATION_HARVESTER_PORT" in windows_launcher
    assert "OASIS_PORT" in windows_launcher
    assert "8771" in windows_launcher
    assert '".venv\\Scripts\\python.exe" -m pdt_observer app' in windows_launcher

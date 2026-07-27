from __future__ import annotations

import json
import stat
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from pdt_observer.app import ActiveCodexRegistry, create_app

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
    assert "Tennessee" in prompt or "Coverage Steering" in prompt
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def failing_runner(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 2, stdout="", stderr="codex failed")


def test_active_codex_registry_sends_prompts_as_utf8(tmp_path: Path) -> None:
    prompt = "Georgia’s GEMA/HS technical college search"
    registry = ActiveCodexRegistry(tmp_path)

    with patch("pdt_observer.app.subprocess.Popen") as popen:
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
                    "search_languages": ["English"],
                    "administrative_terms": [],
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
    assert "Facility Type" in html
    assert "Subtype" in html
    assert "Campaign" in html
    assert "Regions or Localities" in html
    assert "Agentic Workbench" in html
    assert "Geometry Studio" in html

    logo = client.get("/assets/oasis-logo.jpg")
    assert logo.status_code == 200
    assert logo.headers["content-type"] == "image/jpeg"
    assert logo.content.startswith(b"\xff\xd8\xff")
    assert 'role="tablist"' in html
    assert 'data-workspace="workbench"' in html
    assert 'data-workspace="geometry"' in html
    assert "setWorkspaceTab" in html
    assert "Run Full Pipeline" in html
    assert "Coverage ready - human review required" in html
    assert "paused before gap fill" in html
    assert "Run Harvest" in html
    assert "Copy JSON" in html
    assert "Copy QAQC Prompt" in html
    assert "Run QAQC" in html
    assert "Run Address Enrichment" in html
    assert "Download Verified CSV" in html
    assert "Coordinate Assignment Required" in html
    assert 'id="interventionList"' in html
    assert 'id="geocodedQueueTab"' in html
    assert 'id="manualQueueTab"' in html
    assert "Geocoded" in html
    assert "Needs Manual Geocoding" in html
    assert "geometryItemsForActiveTab" in html
    assert "needsManualGeocoding" in html
    assert "setGeometryListTab('geocoded')" in html
    assert "setGeometryListTab('manual')" in html
    assert "Geocode All" in html
    assert "Project Workflow" in html
    assert "Recommended next:" in html
    assert "/workflow-status" in html
    assert "Geocoding ${index + 1}/${pending.length}" in html
    assert "Sample Set / Coverage" in html
    assert html.index("Geometry Studio") < html.index("Sample Set / Coverage")
    assert "Create Sample Set" in html
    assert "Analyze Coverage" in html
    assert "Run Gap Fill" in html
    assert "Run QAQC Missing" in html
    assert "Run Address Missing" in html
    assert "leaflet.draw" in html
    assert "Load Approved" in html
    assert "Load Augmented Sample" in html
    assert "Show Sample Extent" in html
    assert "Zoom To Extent" in html
    assert "Clear Extent" in html
    assert "geometryExtentSummary" in html
    assert "overviewPointLayer" in html
    assert "overviewFootprintLayer" in html
    assert "overviewExtentLayer" in html
    assert "renderSampleExtent" in html
    assert "geometryRoundLabel" in html
    assert "selectGeometryItem(item.item_id)" in html
    assert "Corrected Address or Place" in html
    assert "Search Corrected Address" in html
    assert "Save Footprint" in html
    assert "Download Footprints GeoJSON" in html
    assert "Clear All" in html
    assert "Agent Activity" in html
    assert "Full Pipeline Transcript" in html
    assert "Download Transcript (.txt)" in html
    assert "Resolve Selected Coordinate" in html
    assert "Search Corrected Address" in html
    assert "Research This Facility" in html
    assert "Search Google Maps" in html
    assert "Accept this location" in html
    assert "Paste Google Maps Coordinates" in html
    assert "Preview Coordinate" in html
    assert "/api/geometry/coordinate-preview" in html
    assert "human_pasted_coordinate" in html
    assert "renderCandidateOptions" in html
    assert "/api/geometry/research" in html
    assert "Place Point on Map" in html
    assert "Save Coordinate" in html
    assert "renderGeocodingProgress" in html
    assert "allow_address_retry: true" in html
    assert 'id="dialogueOutput"' in html
    assert "/api/geographer/plan" in html
    assert "'dialogue'" in html
    assert "'transcript.txt'" in html
    assert "Cancel Run" in html
    assert "Exit Application" in html
    assert "Theme" in html
    assert "observationHarvesterTheme" in html
    assert "data-theme" in html
    assert "/api/runs/${runId}/log" in html
    assert "/api/runs/${state.currentRunId}/status" in html
    assert "/api/runs/${state.currentRunId}/cancel" in html
    assert "/api/runs/${state.currentRunId}/qaqc-prompt" in html
    assert "/api/runs/${state.currentRunId}/qaqc-run" in html
    assert "/api/runs/${state.currentRunId}/qaqc-reviews" in html
    assert "/api/runs/${state.currentRunId}/address-run" in html
    assert "/api/runs/${state.currentRunId}/address-results" in html
    assert "/api/runs/${state.currentRunId}/geometry-items" in html
    assert "/api/samples/from-run" in html
    assert "/api/samples/${state.currentSampleSetId}/coverage-run" in html
    assert "/api/samples/${state.currentSampleSetId}/gap-fill-run" in html
    assert "/api/samples/${state.currentSampleSetId}/qaqc-missing" in html
    assert "/api/samples/${state.currentSampleSetId}/address-missing" in html
    assert "/api/samples/${state.currentSampleSetId}/geometry-items" in html
    assert "/api/geometry/geocode" in html
    assert "/api/runs/${state.currentRunId}/export.verified.${format}" in html
    assert "/api/runs/${state.currentRunId}/export.footprints.geojson" in html
    assert "QAQC still running" in html
    assert "/api/runs/clear" in html
    assert "/api/app/exit" in html


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
    assert any(
        strategy["strategy_id"] == "incident_evacuation"
        for strategy in payload["strategies"]
    )
    university = next(
        profile
        for profile in schools["profiles"]
        if profile["profile_id"] == "university_college"
    )
    assert university["strategy_plan"]["recommendations"]


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
    assert stages["qaqc"]["status"] == "ready"
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


def test_geometry_geocode_routes_out_of_scope_candidate_to_human_queue(
    tmp_path: Path,
) -> None:
    def geocoder(query: str) -> dict[str, object]:
        return {
            "display_name": "Example Warehouse, Birmingham, Alabama, United States",
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
    item = client.get(f"/api/runs/{run_id}/geometry-items").json()["items"][0]

    response = client.post(
        "/api/geometry/geocode",
        json={"item_id": item["item_id"], "query": item["geocode_query"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["geocode_result"] is None
    assert payload["spatial_validation"]["status"] == "out_of_scope"
    assert payload["spatial_validation"]["candidate_options"][0]["confidence"] == (
        "conflicting"
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
        def __call__(self, query: str) -> dict[str, object] | None:
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
            geocoder=geocoder,
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
    coverage = client.post("/api/samples/tn-schools-sample/coverage-run")
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
    assert coverage.status_code == 200
    assert coverage.json()["coverage"]["review"]["dispersion_status"] == "clustered"
    assert coverage_results.json()["review"]["recommended_child_jobs"][0]["locality"] == (
        "Western Tennessee"
    )
    assert gap_fill.json()["sample_set"]["rounds"][1]["role"] == "gap_fill"
    assert len(gap_fill.json()["sample_set"]["combined_child_run_ids"]) == 2
    assert missing_qaqc.json()["qaqc"]["child_run_ids"]
    assert missing_address.json()["address"]["child_run_ids"]
    assert geometry_items.json()["item_count"] == 2
    assert "sample_round" in exported.text


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
    mac_launcher = Path("OASIS.command").read_text(encoding="utf-8")
    windows_launcher = Path("OASIS.bat").read_text(encoding="utf-8")
    legacy_mac_launcher = Path("Observation Harvester.command").read_text(encoding="utf-8")
    legacy_windows_launcher = Path("Observation Harvester.bat").read_text(encoding="utf-8")

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
    assert "OASIS.command" in legacy_mac_launcher
    assert "OASIS.bat" in legacy_windows_launcher

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

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


def test_cli_artifacts_inspect_and_migrate(tmp_path, capsys) -> None:
    manifest = tmp_path / "harvest_runs/example.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"run_id": "example", "status": "completed"}), encoding="utf-8")

    inspect_code = main(["artifacts", "inspect", "--workspace", str(tmp_path)])
    inspect_output = json.loads(capsys.readouterr().out)
    migrate_code = main(["artifacts", "migrate", "--workspace", str(tmp_path)])
    migrate_output = json.loads(capsys.readouterr().out)

    assert inspect_code == 0
    assert inspect_output["dry_run"] is True
    assert inspect_output["changed_count"] == 1
    assert migrate_code == 0
    assert migrate_output["dry_run"] is False
    assert json.loads(manifest.read_text(encoding="utf-8"))["schema_version"] == 1


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
            "--subtype",
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


def test_cli_work_prompt_renders_profile_specific_guidance(tmp_path, capsys) -> None:
    main(
        [
            "batch",
            "create",
            "--locality",
            "Makati",
            "--country",
            "PH",
            "--profiles",
            "commercial_business",
            "--batch-id",
            "ph-makati",
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
            "offices_bpo_call_centers",
            "--claimed-by",
            "codex-bpo",
            "--workspace",
            str(tmp_path),
        ]
    )
    capsys.readouterr()

    exit_code = main(
        [
            "work",
            "prompt",
            "--work-item-id",
            "ph-makati-offices_bpo_call_centers",
            "--workspace",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Profile-Driven Occupancy Harvest Prompt" in captured.out
    assert "Country: Philippines (`PH`)" in captured.out
    assert "barangay" in captured.out
    assert "call center agents were evacuated" in captured.out
    assert '"Makati" Philippines "employees were inside" office' in captured.out
    assert "Accepted observations require exact source URL" in captured.out


def test_cli_harvest_prepare_renders_broad_lead_prompt(tmp_path, capsys) -> None:
    output = tmp_path / "ph-commercial-leads.md"

    exit_code = main(
        [
            "harvest",
            "prepare",
            "--country",
            "PH",
            "--profiles",
            "commercial_business",
            "--target",
            "20",
            "--output",
            str(output),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert output.is_file()
    assert "Broad Occupancy Lead Harvest" in captured.out
    assert "Target: 20 lead records." in captured.out
    assert "Country: Philippines (`PH`)." in captured.out
    assert '"city_or_region": "String"' in captured.out
    assert "DO NOT" not in captured.err


def test_cli_harvest_prepare_can_focus_one_profile(tmp_path, capsys) -> None:
    output = tmp_path / "tn-factories.md"

    exit_code = main(
        [
            "harvest",
            "prepare",
            "--country",
            "US",
            "--locality",
            "Tennessee",
            "--profiles",
            "commercial_business",
            "--profile",
            "factories_warehouses",
            "--target",
            "5",
            "--output",
            str(output),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert output.is_file()
    assert "Scope: Focus on Tennessee, United States" in captured.out
    assert "- Factories and warehouses" in captured.out
    assert "- Malls, retail, and markets" not in captured.out
    assert "workers were trapped" in captured.out
    assert "shopping center" not in captured.out


def test_cli_harvest_prepare_accepts_facility_type_and_subtype_aliases(tmp_path, capsys) -> None:
    output = tmp_path / "tn-university.md"

    exit_code = main(
        [
            "harvest",
            "prepare",
            "--country",
            "US",
            "--locality",
            "Tennessee",
            "--facility-type",
            "schools",
            "--subtype",
            "university_college",
            "--target",
            "5",
            "--output",
            str(output),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert output.is_file()
    assert "Facility type: Schools (`schools`)." in captured.out
    assert "- University and college" in captured.out
    assert "- Primary and secondary education" not in captured.out
    assert "campus population" in captured.out


def test_cli_leads_validate_and_summarize(capsys) -> None:
    exit_code = main(["leads", "validate", "examples/ph_commercial_leads.json"])
    captured = capsys.readouterr()
    validated = json.loads(captured.out)

    assert exit_code == 0
    assert validated == {"valid": True, "lead_count": 1}

    exit_code = main(["leads", "summarize", "examples/ph_commercial_leads.json"])
    captured = capsys.readouterr()
    summary = json.loads(captured.out)

    assert exit_code == 0
    assert summary["lead_count"] == 1
    assert summary["occupancy_count_rows"] == 2
    assert summary["countries"] == ["PH"]


def test_cli_harvest_run_invokes_codex_and_writes_manifest(tmp_path, capsys) -> None:
    fake_codex = tmp_path / "fake_codex.py"
    fake_codex.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys

output = pathlib.Path(sys.argv[sys.argv.index("-o") + 1])
sys.stdin.read()
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps([
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
      "country": "US"
    },
    "confidence": "high",
    "is_facility_level": True,
    "is_regional_aggregate": False,
    "review_flags": [],
    "review_notes": None
  }
]))
""",
        encoding="utf-8",
    )
    fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)

    exit_code = main(
        [
            "harvest",
            "run",
            "--country",
            "US",
            "--locality",
            "Tennessee",
            "--profiles",
            "commercial_business",
            "--profile",
            "factories_warehouses",
            "--target",
            "5",
            "--workspace",
            str(tmp_path),
            "--run-id",
            "us-tn-factories",
            "--codex-bin",
            str(fake_codex),
        ]
    )
    captured = capsys.readouterr()
    manifest = json.loads(captured.out)

    assert exit_code == 0
    assert manifest["status"] == "completed"
    assert manifest["summary"]["lead_count"] == 1
    assert (tmp_path / "work/us-tn-factories.md").is_file()
    assert (tmp_path / "lead_runs/us-tn-factories.json").is_file()
    assert (tmp_path / "harvest_runs/us-tn-factories.json").is_file()


def test_cli_harvest_campaign_run_invokes_codex_for_each_child(tmp_path, capsys) -> None:
    fake_codex = tmp_path / "fake_codex.py"
    fake_codex.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys

output = pathlib.Path(sys.argv[sys.argv.index("-o") + 1])
sys.stdin.read()
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps([
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
      "country": "US"
    },
    "confidence": "high",
    "is_facility_level": True,
    "is_regional_aggregate": False,
    "review_flags": [],
    "review_notes": None
  }
]))
""",
        encoding="utf-8",
    )
    fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)

    exit_code = main(
        [
            "harvest",
            "campaign-run",
            "--country",
            "US",
            "--locality",
            "Tennessee",
            "--locality",
            "Kentucky",
            "--facility-type",
            "schools",
            "--facility-type",
            "manufacturing",
            "--target",
            "3",
            "--workspace",
            str(tmp_path),
            "--campaign-id",
            "us-south-campaign",
            "--codex-bin",
            str(fake_codex),
        ]
    )
    captured = capsys.readouterr()
    manifest = json.loads(captured.out)

    assert exit_code == 0
    assert manifest["status"] == "completed"
    assert manifest["summary"] == {
        "planned_run_count": 4,
        "completed_count": 4,
        "failed_count": 0,
        "lead_count": 4,
    }
    assert manifest["child_run_ids"] == [
        "us-south-campaign-tennessee-schools",
        "us-south-campaign-tennessee-manufacturing",
        "us-south-campaign-kentucky-schools",
        "us-south-campaign-kentucky-manufacturing",
    ]
    assert (tmp_path / "harvest_runs/us-south-campaign.campaign.json").is_file()


def test_cli_harvest_campaign_run_requires_facility_type(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["harvest", "campaign-run", "--country", "US"])
    captured = capsys.readouterr()

    assert exc.value.code == 2
    assert "--facility-type" in captured.err


def test_cli_leads_export_csv_and_jsonl(tmp_path, capsys) -> None:
    lead_file = tmp_path / "leads.json"
    lead_file.write_text(
        json.dumps(
            [
                {
                    "is_valid_occupancy_report": True,
                    "source_url": "https://example.test/story",
                    "source_title": "Workers evacuated",
                    "source_type": "news",
                    "evidence_quote": "Officials said 12 workers were evacuated.",
                    "incident_date": "2026-01-02",
                    "incident_time": "03:30 PM",
                    "occupancy_data": [{"count": 12, "group_type": "workers"}],
                    "location": {
                        "facility_name": "Example Warehouse",
                        "specific_address_or_landmark": "Industrial Drive",
                        "city_or_region": "Tennessee",
                        "country": "US",
                    },
                    "confidence": "high",
                    "is_facility_level": True,
                    "is_regional_aggregate": False,
                    "review_flags": ["needs_geocode"],
                    "review_notes": "Review geocode.",
                }
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(["leads", "export", str(lead_file), "--format", "csv"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "lead_index,source_url" in captured.out
    assert "Example Warehouse" in captured.out
    assert "needs_geocode" in captured.out

    exit_code = main(["leads", "export", str(lead_file), "--format", "jsonl"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert json.loads(captured.out)["source_type"] == "news"


def test_cli_leads_promote_writes_draft_investigation_run(tmp_path, capsys) -> None:
    lead_file = tmp_path / "leads.json"
    output = tmp_path / "runs/promoted.json"
    lead_file.write_text(
        json.dumps(
            [
                {
                    "is_valid_occupancy_report": True,
                    "source_url": "https://example.test/story",
                    "source_title": "Workers evacuated",
                    "source_type": "news",
                    "evidence_quote": "Officials said 12 workers were evacuated.",
                    "incident_date": "2026-01-02",
                    "incident_time": "03:30 PM",
                    "occupancy_data": [{"count": 12, "group_type": "workers"}],
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
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "leads",
            "promote",
            str(lead_file),
            "--index",
            "0",
            "--output",
            str(output),
            "--task-id",
            "promoted-example",
        ]
    )
    captured = capsys.readouterr()
    promoted = json.loads(captured.out)

    assert exit_code == 0
    assert output.is_file()
    assert promoted["candidate"]["result"]["status"] == "review"
    assert promoted["candidate"]["result"]["count"] == 12
    assert promoted["source_bundle"]["documents"][0]["source_url"] == "https://example.test/story"


def test_cli_leads_qaqc_prompt_writes_verification_prompt(tmp_path, capsys) -> None:
    lead_file = tmp_path / "lead_runs/leads.json"
    output = tmp_path / "work/leads-qaqc.md"
    lead_file.parent.mkdir()
    lead_file.write_text(
        json.dumps(
            [
                {
                    "is_valid_occupancy_report": True,
                    "source_url": "https://example.test/story",
                    "source_title": "Workers evacuated",
                    "source_type": "news",
                    "evidence_quote": "Officials said 12 workers were evacuated.",
                    "incident_date": "2026-01-02",
                    "incident_time": "03:30 PM",
                    "occupancy_data": [{"count": 12, "group_type": "workers"}],
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
        ),
        encoding="utf-8",
    )

    exit_code = main(["leads", "qaqc-prompt", str(lead_file), "--output", str(output)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert output.is_file()
    assert "Occupancy Lead QAQC Verification" in captured.out
    assert "https://example.test/story" in captured.out
    assert "Example Warehouse" in captured.out
    assert "`verified`: source is reachable" in captured.out
    assert "`count_not_found`" in captured.out


def test_cli_leads_qaqc_validate_accepts_valid_review(tmp_path, capsys) -> None:
    qaqc_file = tmp_path / "qaqc_runs/leads-qaqc.json"
    qaqc_file.parent.mkdir()
    qaqc_file.write_text(
        json.dumps(
            [
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
                            "group_type": "workers",
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
        ),
        encoding="utf-8",
    )

    exit_code = main(["leads", "qaqc-validate", str(qaqc_file)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert json.loads(captured.out) == {"valid": True, "review_count": 1}


def test_cli_leads_qaqc_validate_rejects_invalid_status(tmp_path, capsys) -> None:
    qaqc_file = tmp_path / "bad-qaqc.json"
    qaqc_file.write_text(
        json.dumps(
            [
                {
                    "lead_index": 0,
                    "source_url": "https://example.test/story",
                    "verification_status": "mostly_ok",
                    "source_reachable": True,
                    "facility_match": True,
                    "location_match": True,
                    "count_checks": [],
                    "supporting_quote": None,
                    "recommended_action": "keep",
                    "review_notes": "Bad status should fail.",
                }
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(["leads", "qaqc-validate", str(qaqc_file)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "mostly_ok" in captured.err


def test_cli_leads_address_prompt_writes_enrichment_prompt(tmp_path, capsys) -> None:
    lead_file = tmp_path / "lead_runs/leads.json"
    qaqc_file = tmp_path / "qaqc_runs/leads-qaqc.json"
    output = tmp_path / "work/leads-address.md"
    lead_file.parent.mkdir()
    qaqc_file.parent.mkdir()
    lead_file.write_text(
        json.dumps(
            [
                {
                    "is_valid_occupancy_report": True,
                    "source_url": "https://example.test/story",
                    "source_title": "Workers evacuated",
                    "source_type": "news",
                    "evidence_quote": "Officials said 12 workers were evacuated.",
                    "incident_date": "2026-01-02",
                    "incident_time": "03:30 PM",
                    "occupancy_data": [{"count": 12, "group_type": "workers"}],
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
        ),
        encoding="utf-8",
    )
    qaqc_file.write_text(
        json.dumps(
            [
                {
                    "lead_index": 0,
                    "source_url": "https://example.test/story",
                    "verification_status": "verified",
                    "source_reachable": True,
                    "facility_match": True,
                    "location_match": True,
                    "count_checks": [],
                    "supporting_quote": "Officials said 12 workers were evacuated.",
                    "recommended_action": "keep",
                    "review_notes": "Count, facility, and location are supported.",
                }
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "leads",
            "address-prompt",
            str(lead_file),
            "--qaqc",
            str(qaqc_file),
            "--output",
            str(output),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert output.is_file()
    assert "Facility Address Enrichment" in captured.out
    assert "Example Warehouse" in captured.out
    assert "https://example.test/story" in captured.out
    assert "Do not invent an address" in captured.out
    assert '"status": "found"' in captured.out


def test_cli_leads_address_validate_accepts_valid_result(tmp_path, capsys) -> None:
    address_file = tmp_path / "address_runs/leads-address.json"
    address_file.parent.mkdir()
    address_file.write_text(
        json.dumps(
            [
                {
                    "lead_index": 0,
                    "item_id": "leads-0",
                    "facility_name": "Example Warehouse",
                    "formatted_address": "100 Industrial Drive, Nashville, TN, US",
                    "address_line1": "100 Industrial Drive",
                    "address_line2": None,
                    "city_or_region": "Nashville",
                    "state_or_province": "TN",
                    "postal_code": None,
                    "country": "US",
                    "address_source_url": "https://example.test/warehouse",
                    "address_evidence_quote": (
                        "Example Warehouse is located at 100 Industrial Drive."
                    ),
                    "confidence": "high",
                    "status": "found",
                    "review_notes": "Official address page matches the facility.",
                }
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(["leads", "address-validate", str(address_file)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert json.loads(captured.out) == {"valid": True, "result_count": 1}


def test_cli_leads_address_validate_rejects_invalid_status(tmp_path, capsys) -> None:
    address_file = tmp_path / "bad-address.json"
    address_file.write_text(
        json.dumps(
            [
                {
                    "lead_index": 0,
                    "item_id": "leads-0",
                    "facility_name": "Example Warehouse",
                    "formatted_address": None,
                    "address_line1": None,
                    "address_line2": None,
                    "city_or_region": "Nashville",
                    "state_or_province": "TN",
                    "postal_code": None,
                    "country": "US",
                    "address_source_url": None,
                    "address_evidence_quote": None,
                    "confidence": "unknown",
                    "status": "sort_of",
                    "review_notes": "Bad status should fail.",
                }
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(["leads", "address-validate", str(address_file)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "sort_of" in captured.err


def test_cli_samples_create_prompt_validate_and_gap_fill(tmp_path, capsys) -> None:
    manifest_file = tmp_path / "harvest_runs/us-tn-schools.json"
    manifest_file.parent.mkdir()
    manifest_file.write_text(
        json.dumps(
            {
                "run_id": "us-tn-schools",
                "status": "completed",
                "country": "US",
                "locality": "Tennessee",
                "profile_set": "schools",
                "profile_id": None,
                "target": 2,
                "prompt_path": "work/us-tn-schools.md",
                "lead_path": "lead_runs/us-tn-schools.json",
                "started_at": "2026-07-24T00:00:00Z",
                "completed_at": "2026-07-24T00:01:00Z",
                "codex_command": [],
                "exit_code": 0,
                "validation_valid": True,
                "summary": {"lead_count": 0},
                "error_message": None,
                "log_path": None,
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "samples",
            "create-from-run",
            "us-tn-schools",
            "--sample-set-id",
            "us-tn-sample",
            "--workspace",
            str(tmp_path),
        ]
    )
    created = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert created["sample_set_id"] == "us-tn-sample"
    assert created["combined_child_run_ids"] == ["us-tn-schools"]

    prompt_file = tmp_path / "work/coverage.md"
    exit_code = main(
        [
            "samples",
            "coverage-prompt",
            "us-tn-sample",
            "--coverage-id",
            "us-tn-sample-coverage",
            "--output",
            str(prompt_file),
            "--workspace",
            str(tmp_path),
        ]
    )
    prompt = capsys.readouterr().out

    assert exit_code == 0
    assert prompt_file.is_file()
    assert "Sample Set Coverage Steering" in prompt
    assert "recommended_child_jobs" in prompt

    from pdt_observer.curation import approve_curation

    curation = approve_curation(tmp_path, "us-tn-sample", item_ids=())
    assert curation.approval is not None
    coverage_file = tmp_path / "coverage_runs/us-tn-sample-coverage.json"
    coverage_file.parent.mkdir()
    coverage_file.write_text(
        json.dumps(
            {
                "coverage_id": "us-tn-sample-coverage",
                "sample_set_id": "us-tn-sample",
                "dispersion_status": "insufficient_data",
                "counts_by_locality": {},
                "counts_by_city_or_region": {},
                "counts_by_facility_type": {},
                "out_of_scope_flags": [],
                "duplicate_or_cluster_flags": [],
                "narrative_notes": "No verified records yet.",
                "recommended_child_jobs": [],
                "curation_snapshot_id": curation.approval.snapshot_id,
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(["samples", "coverage-validate", str(coverage_file)])
    validated = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert validated == {"valid": True, "recommended_job_count": 0}

    exit_code = main(
        [
            "samples",
            "gap-fill-run",
            "us-tn-sample",
            "--coverage",
            str(coverage_file),
            "--workspace",
            str(tmp_path),
        ]
    )
    updated = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert len(updated["rounds"]) == 2
    assert updated["rounds"][1]["role"] == "gap_fill"


def test_cli_work_claim_by_locality_and_exact_id(tmp_path, capsys) -> None:
    main(
        [
            "batch",
            "create",
            "--locality",
            "Manila",
            "--country",
            "PH",
            "--profiles",
            "public_venues",
            "--batch-id",
            "ph-manila",
            "--workspace",
            str(tmp_path),
        ]
    )
    capsys.readouterr()
    main(
        [
            "batch",
            "create",
            "--locality",
            "Cebu City",
            "--country",
            "PH",
            "--profiles",
            "public_venues",
            "--batch-id",
            "ph-cebu-city",
            "--workspace",
            str(tmp_path),
        ]
    )
    capsys.readouterr()

    exit_code = main(
        [
            "work",
            "claim",
            "--profile",
            "restaurants_bars",
            "--locality",
            "Cebu City",
            "--country",
            "PH",
            "--claimed-by",
            "codex-cebu",
            "--workspace",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()
    claimed = json.loads(captured.out)

    assert exit_code == 0
    assert claimed["work_item_id"] == "ph-cebu-city-restaurants_bars"
    assert claimed["claimed_by"] == "codex-cebu"

    exit_code = main(
        [
            "work",
            "claim",
            "--work-item-id",
            "ph-manila-schools_childcare",
            "--claimed-by",
            "codex-schools",
            "--workspace",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()
    exact = json.loads(captured.out)

    assert exit_code == 0
    assert exact["work_item_id"] == "ph-manila-schools_childcare"
    assert exact["profile_id"] == "schools_childcare"


def test_cli_work_claim_requires_profile_or_exact_id(tmp_path, capsys) -> None:
    exit_code = main(["work", "claim", "--workspace", str(tmp_path)])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert "provide profile_id or work_item_id" in captured.err


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

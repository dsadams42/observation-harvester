from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Sequence
from pathlib import Path

from pdt_observer.harvest import run_harvest, run_harvest_batch, run_harvest_campaign
from pdt_observer.models import HarvestRunStatus

LEAD_PAYLOAD = [
    {
        "is_valid_occupancy_report": True,
        "source_url": "https://example.test/story",
        "source_title": "Factory fire evacuates workers",
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
    assert cwd.is_dir()
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def failing_runner(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 2, stdout="", stderr="codex failed")


def cancelled_runner(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, -15, stdout="", stderr="terminated")


def campaign_runner(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    output_path = Path(command[command.index("-o") + 1])
    output_path.write_text(json.dumps(LEAD_PAYLOAD), encoding="utf-8")
    assert "Facility type:" in prompt
    assert cwd.is_dir()
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def partly_failing_campaign_runner(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    output_path = Path(command[command.index("-o") + 1])
    if "manufacturing" in output_path.name:
        return subprocess.CompletedProcess(command, 2, stdout="", stderr="codex failed")
    output_path.write_text(json.dumps(LEAD_PAYLOAD), encoding="utf-8")
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def test_run_harvest_writes_prompt_leads_and_completed_manifest(tmp_path: Path) -> None:
    manifest = run_harvest(
        root=tmp_path,
        country="US",
        locality="Tennessee",
        profile_set_name="commercial_business",
        profile_id="factories_warehouses",
        target=5,
        run_id="us-tn-factories",
        codex_bin="codex-test",
        runner=successful_runner,
    )

    assert manifest.status == HarvestRunStatus.COMPLETED
    assert manifest.exit_code == 0
    assert manifest.validation_valid is True
    assert manifest.summary is not None
    assert manifest.summary["lead_count"] == 1
    assert manifest.strategy_plan is not None
    assert [
        recommendation.strategy_id.value
        for recommendation in manifest.strategy_plan.recommendations
    ][:3] == [
        "incident_evacuation",
        "enforcement_inspection",
        "shift_operational_presence",
    ]
    assert Path(manifest.prompt_path).is_file()
    assert Path(manifest.lead_path).is_file()
    assert manifest.log_path is not None
    assert Path(manifest.log_path).is_file()
    assert "Harvest completed." in Path(manifest.log_path).read_text(encoding="utf-8")
    saved = json.loads((tmp_path / "harvest_runs/us-tn-factories.json").read_text())
    assert saved["codex_command"][0] == "codex-test"
    assert saved["profile_id"] == "factories_warehouses"
    assert saved["strategy_plan"]["planner"] == "deterministic_strategy_planner_v1"


def test_run_harvest_failed_codex_run_writes_failed_manifest(tmp_path: Path) -> None:
    manifest = run_harvest(
        root=tmp_path,
        country="US",
        profile_set_name="commercial_business",
        target=5,
        run_id="us-countrywide-commercial",
        runner=failing_runner,
    )

    assert manifest.status == HarvestRunStatus.FAILED
    assert manifest.exit_code == 2
    assert manifest.validation_valid is False
    assert manifest.error_message == "codex failed"
    assert manifest.log_path is not None
    assert "codex failed" in Path(manifest.log_path).read_text(encoding="utf-8")
    saved = json.loads((tmp_path / "harvest_runs/us-countrywide-commercial.json").read_text())
    assert saved["status"] == "failed"


def test_run_harvest_cancelled_codex_run_writes_cancelled_manifest(tmp_path: Path) -> None:
    manifest = run_harvest(
        root=tmp_path,
        country="US",
        profile_set_name="schools",
        target=5,
        run_id="us-countrywide-schools",
        runner=cancelled_runner,
    )

    assert manifest.status == HarvestRunStatus.CANCELLED
    assert manifest.exit_code == -15
    assert manifest.validation_valid is False
    assert manifest.error_message == "Harvest cancelled by user."
    assert manifest.log_path is not None
    assert "Harvest cancelled." in Path(manifest.log_path).read_text(encoding="utf-8")


def test_run_harvest_batch_runs_each_enabled_profile(tmp_path: Path) -> None:
    manifest = run_harvest_batch(
        root=tmp_path,
        country="US",
        locality="Tennessee",
        profile_set_name="commercial_business",
        target=3,
        batch_id="us-tn-commercial",
        runner=successful_runner,
    )

    assert manifest.status == HarvestRunStatus.COMPLETED
    assert manifest.summary == {
        "run_count": 4,
        "completed_count": 4,
        "failed_count": 0,
        "lead_count": 4,
    }
    assert manifest.child_run_ids == (
        "us-tn-commercial-malls_retail_markets",
        "us-tn-commercial-offices_bpo_call_centers",
        "us-tn-commercial-factories_warehouses",
        "us-tn-commercial-hotels_restaurants",
    )
    assert manifest.log_path is not None
    assert Path(manifest.log_path).is_file()
    assert (tmp_path / "harvest_runs/us-tn-commercial.batch.json").is_file()


def test_run_harvest_campaign_runs_countrywide_facility_types(tmp_path: Path) -> None:
    manifest = run_harvest_campaign(
        root=tmp_path,
        country="US",
        facility_types=("schools", "manufacturing", "restaurants"),
        target=3,
        campaign_id="us-countrywide-campaign",
        runner=campaign_runner,
    )

    assert manifest.status == HarvestRunStatus.COMPLETED
    assert manifest.localities == ()
    assert manifest.facility_types == ("schools", "manufacturing", "restaurants")
    assert manifest.summary == {
        "planned_run_count": 3,
        "completed_count": 3,
        "failed_count": 0,
        "lead_count": 3,
    }
    assert manifest.child_run_ids == (
        "us-countrywide-campaign-countrywide-schools",
        "us-countrywide-campaign-countrywide-manufacturing",
        "us-countrywide-campaign-countrywide-restaurants",
    )
    assert manifest.log_path is not None
    assert Path(manifest.log_path).is_file()
    assert (tmp_path / "harvest_runs/us-countrywide-campaign.campaign.json").is_file()


def test_run_harvest_campaign_runs_each_locality_facility_pair(tmp_path: Path) -> None:
    manifest = run_harvest_campaign(
        root=tmp_path,
        country="US",
        localities=("Tennessee", "Kentucky"),
        facility_types=("schools", "manufacturing", "restaurants"),
        target=2,
        campaign_id="us-south-campaign",
        runner=campaign_runner,
    )

    assert manifest.status == HarvestRunStatus.COMPLETED
    assert manifest.summary is not None
    assert manifest.summary["planned_run_count"] == 6
    assert manifest.summary["lead_count"] == 6
    assert manifest.child_run_ids == (
        "us-south-campaign-tennessee-schools",
        "us-south-campaign-tennessee-manufacturing",
        "us-south-campaign-tennessee-restaurants",
        "us-south-campaign-kentucky-schools",
        "us-south-campaign-kentucky-manufacturing",
        "us-south-campaign-kentucky-restaurants",
    )


def test_run_harvest_campaign_executes_child_agents_concurrently(tmp_path: Path) -> None:
    rendezvous = threading.Barrier(2)

    def concurrent_runner(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        rendezvous.wait(timeout=2)
        output_path = Path(command[command.index("-o") + 1])
        output_path.write_text(json.dumps(LEAD_PAYLOAD), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    manifest = run_harvest_campaign(
        root=tmp_path,
        country="US",
        localities=("Tennessee",),
        facility_types=("schools", "manufacturing"),
        target=2,
        campaign_id="us-tn-parallel-campaign",
        runner=concurrent_runner,
        max_concurrent_jobs=2,
    )

    assert manifest.status == HarvestRunStatus.COMPLETED
    assert manifest.child_run_ids == (
        "us-tn-parallel-campaign-tennessee-schools",
        "us-tn-parallel-campaign-tennessee-manufacturing",
    )


def test_run_harvest_campaign_marks_failed_if_one_child_fails(tmp_path: Path) -> None:
    manifest = run_harvest_campaign(
        root=tmp_path,
        country="US",
        localities=("Tennessee",),
        facility_types=("schools", "manufacturing", "restaurants"),
        target=2,
        campaign_id="us-tn-campaign",
        runner=partly_failing_campaign_runner,
    )

    assert manifest.status == HarvestRunStatus.FAILED
    assert manifest.error_message == "One or more child harvest runs failed or were cancelled."
    assert manifest.summary == {
        "planned_run_count": 3,
        "completed_count": 2,
        "failed_count": 1,
        "lead_count": 2,
    }

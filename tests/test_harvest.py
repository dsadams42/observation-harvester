from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

from pdt_observer.harvest import run_harvest, run_harvest_batch
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
    assert Path(manifest.prompt_path).is_file()
    assert Path(manifest.lead_path).is_file()
    saved = json.loads((tmp_path / "harvest_runs/us-tn-factories.json").read_text())
    assert saved["codex_command"][0] == "codex-test"
    assert saved["profile_id"] == "factories_warehouses"


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
    saved = json.loads((tmp_path / "harvest_runs/us-countrywide-commercial.json").read_text())
    assert saved["status"] == "failed"


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
    assert (tmp_path / "harvest_runs/us-tn-commercial.batch.json").is_file()

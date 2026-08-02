from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Sequence
from pathlib import Path

from pdt_observer.curation import approve_curation, set_exclusions
from pdt_observer.dialogue import load_dialogue, render_dialogue
from pdt_observer.geometry import GeometryPoint, GeometryStatus, geometry_item_from_payload
from pdt_observer.harvest import run_harvest_campaign
from pdt_observer.models import (
    CoverageFlag,
    CoverageSteeringReview,
    CurationReasonCode,
    HarvestRunStatus,
    RecommendedGapFillJob,
    SampleSetManifest,
    SampleSetRound,
    SampleSetRoundRole,
)
from pdt_observer.samples import (
    compute_coverage_summary,
    create_sample_set_from_run,
    render_coverage_steering_prompt,
    run_gap_fill,
    sample_records,
    save_sample_set,
)

LEAD_PAYLOAD = [
    {
        "is_valid_occupancy_report": True,
        "source_url": "https://example.test/story",
        "source_title": "Students evacuated",
        "source_type": "news",
        "evidence_quote": "Officials said 12 students were evacuated from the school.",
        "incident_date": "2026-01-02",
        "incident_time": "03:30 PM",
        "occupancy_data": [{"count": 12, "group_type": "students evacuated"}],
        "location": {
            "facility_name": "Example School",
            "specific_address_or_landmark": "Main Street",
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
        "count_checks": [],
        "supporting_quote": "Officials said 12 students were evacuated.",
        "recommended_action": "keep",
        "review_notes": "Supported.",
    }
]


def successful_runner(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    output_path = Path(command[command.index("-o") + 1])
    output_path.write_text(json.dumps(LEAD_PAYLOAD), encoding="utf-8")
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def _write_qaqc(root: Path, child_run_id: str) -> None:
    path = root / "qaqc_runs" / f"{child_run_id}-qaqc.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(QAQC_PAYLOAD), encoding="utf-8")


def test_sample_set_aggregates_campaign_child_runs_and_coverage_summary(
    tmp_path: Path,
) -> None:
    campaign = run_harvest_campaign(
        root=tmp_path,
        country="US",
        localities=("Tennessee",),
        facility_types=("schools", "manufacturing"),
        target=2,
        campaign_id="us-tn-campaign",
        runner=successful_runner,
    )
    for child_run_id in campaign.child_run_ids:
        _write_qaqc(tmp_path, child_run_id)
    item = geometry_item_from_payload(
        item_id=f"{campaign.child_run_ids[0]}-0",
        geocode_query="Example School, Tennessee, US",
        point=GeometryPoint(latitude=36.0, longitude=-86.0, source="user"),
        polygon_geojson=None,
        geometry_status=GeometryStatus.POINT_CONFIRMED,
    )
    from pdt_observer.geometry import save_geometry_review_item

    save_geometry_review_item(tmp_path, item)

    sample_set = create_sample_set_from_run(
        root=tmp_path,
        run_id=campaign.campaign_id,
        sample_set_id="us-tn-sample",
    )
    records = sample_records(tmp_path, sample_set)
    summary = compute_coverage_summary(tmp_path, sample_set)
    prompt = render_coverage_steering_prompt(
        sample_set=sample_set,
        coverage_id="us-tn-sample-coverage",
        summary=summary,
        records=records,
    )

    assert sample_set.combined_child_run_ids == campaign.child_run_ids
    assert sample_set.rounds[0].role == SampleSetRoundRole.INITIAL
    assert len(records) == 2
    assert summary["approved_count"] == 2
    assert summary["geocoded_count"] == 1
    assert "Sample Set Coverage Steering" in prompt
    assert "recommended_child_jobs" in prompt

    review = CoverageSteeringReview(
        coverage_id="us-tn-sample-coverage-filter",
        sample_set_id=sample_set.sample_set_id,
        narrative_notes="One approved item was found outside the requested scope.",
        out_of_scope_flags=(
            CoverageFlag(
                item_id=str(records[0]["item_id"]),
                flag_type="out_of_scope",
                reason="Facility is outside Tennessee.",
            ),
        ),
    )
    coverage_path = tmp_path / "coverage_runs/us-tn-sample-coverage-filter.json"
    coverage_path.parent.mkdir(exist_ok=True)
    coverage_path.write_text(review.model_dump_json(), encoding="utf-8")

    agent_flagged_records = sample_records(tmp_path, sample_set)

    assert len(agent_flagged_records) == 2

    set_exclusions(
        tmp_path,
        sample_set.sample_set_id,
        item_ids=(str(records[0]["item_id"]),),
        reason_code=CurationReasonCode.OUTSIDE_GEOGRAPHIC_SCOPE,
        reason_note="Human review confirmed that this facility is outside Tennessee.",
    )
    curated_records = sample_records(tmp_path, sample_set)

    assert len(curated_records) == 1
    assert curated_records[0]["item_id"] != records[0]["item_id"]


def test_gap_fill_appends_second_round_without_overwriting_initial_round(
    tmp_path: Path,
) -> None:
    sample_set = SampleSetManifest(
        sample_set_id="us-tn-sample",
        country="US",
        requested_localities=("Tennessee",),
        facility_types=("schools",),
        target=2,
        rounds=(
            SampleSetRound(
                round_number=1,
                role=SampleSetRoundRole.INITIAL,
                source_run_ids=("initial-run",),
                child_run_ids=("initial-run",),
                status=HarvestRunStatus.COMPLETED,
            ),
        ),
        combined_child_run_ids=("initial-run",),
        created_at="2026-07-24T00:00:00Z",
        updated_at="2026-07-24T00:00:00Z",
    )
    save_sample_set(tmp_path, sample_set)
    curation = approve_curation(tmp_path, sample_set.sample_set_id, item_ids=())
    assert curation.approval is not None
    review = CoverageSteeringReview(
        coverage_id="us-tn-sample-coverage",
        sample_set_id="us-tn-sample",
        dispersion_status="imbalanced",
        narrative_notes="Western Tennessee is underrepresented.",
        curation_snapshot_id=curation.approval.snapshot_id,
        recommended_child_jobs=(
            RecommendedGapFillJob(
                country="US",
                locality="Western Tennessee",
                facility_type="schools",
                target=2,
                reason="Pad western Tennessee.",
            ),
        ),
    )
    coverage_file = tmp_path / "coverage_runs/us-tn-sample-coverage.json"
    coverage_file.parent.mkdir()
    coverage_file.write_text(review.model_dump_json(), encoding="utf-8")

    updated = run_gap_fill(
        root=tmp_path,
        sample_set_id="us-tn-sample",
        coverage_path=coverage_file,
        runner=successful_runner,
    )

    assert len(updated.rounds) == 2
    assert updated.rounds[1].role == SampleSetRoundRole.GAP_FILL
    assert updated.combined_child_run_ids[0] == "initial-run"
    assert updated.combined_child_run_ids[1].startswith("us-tn-sample-r2-gap")


def test_gap_fill_runs_job_teams_concurrently_with_geographer_reviews(
    tmp_path: Path,
) -> None:
    sample_set = SampleSetManifest(
        sample_set_id="us-gap-parallel",
        country="US",
        requested_localities=("Western Tennessee", "Eastern Tennessee"),
        facility_types=("schools",),
        target=2,
        rounds=(
            SampleSetRound(
                round_number=1,
                role=SampleSetRoundRole.INITIAL,
                source_run_ids=("initial-run",),
                child_run_ids=("initial-run",),
                status=HarvestRunStatus.COMPLETED,
            ),
        ),
        combined_child_run_ids=("initial-run",),
        created_at="2026-07-24T00:00:00Z",
        updated_at="2026-07-24T00:00:00Z",
    )
    save_sample_set(tmp_path, sample_set)
    curation = approve_curation(tmp_path, sample_set.sample_set_id, item_ids=())
    assert curation.approval is not None
    review = CoverageSteeringReview(
        coverage_id="us-gap-parallel-coverage",
        sample_set_id="us-gap-parallel",
        dispersion_status="imbalanced",
        narrative_notes="Both ends of the state need targeted collection.",
        curation_snapshot_id=curation.approval.snapshot_id,
        recommended_child_jobs=(
            RecommendedGapFillJob(
                country="US",
                locality="Western Tennessee",
                facility_type="schools",
                target=2,
                reason="Western Tennessee is underrepresented.",
            ),
            RecommendedGapFillJob(
                country="US",
                locality="Eastern Tennessee",
                facility_type="schools",
                target=2,
                reason="Eastern Tennessee is underrepresented.",
            ),
        ),
    )
    coverage_file = tmp_path / "coverage_runs/us-gap-parallel-coverage.json"
    coverage_file.parent.mkdir()
    coverage_file.write_text(review.model_dump_json(), encoding="utf-8")
    geographer_rendezvous = threading.Barrier(2)

    def parallel_job_runner(
        command: Sequence[str],
        prompt: str,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        output_path = Path(command[command.index("-o") + 1])
        if "Minimal Geographic Vernacular Review" in prompt:
            geographer_rendezvous.wait(timeout=2)
            payload: object = {
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
                "commentary": "I added the locally relevant state agency name.",
                "rationale": "The agency name can reveal locality-specific reports.",
            }
        else:
            assert "Geographer Vernacular Adjustments" in prompt
            assert "Tennessee Highway Patrol" in prompt
            payload = LEAD_PAYLOAD
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    updated = run_gap_fill(
        root=tmp_path,
        sample_set_id=sample_set.sample_set_id,
        coverage_path=coverage_file,
        runner=parallel_job_runner,
        max_concurrent_jobs=2,
    )

    gap_round = updated.rounds[-1]
    assert gap_round.status == HarvestRunStatus.COMPLETED
    assert len(gap_round.child_run_ids) == 2
    assert gap_round.summary is not None
    for child_summary in gap_round.summary["child_summaries"]:
        geographer_path = Path(str(child_summary["geographer_plan_path"]))
        assert geographer_path.is_file()
    dialogue = render_dialogue(load_dialogue(tmp_path, sample_set.sample_set_id))
    assert dialogue.count("Geographer Agent:") == 2
    assert "Gap-Fill Coordinator:" in dialogue

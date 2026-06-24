from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from pdt_observer.models import (
    BuildingTypeProfile,
    InvestigationResult,
    InvestigationRun,
    OccupancyLead,
    ResultStatus,
    WorkItem,
)


def test_accepted_result_cannot_omit_required_fields() -> None:
    with pytest.raises(ValidationError):
        InvestigationResult(status=ResultStatus.ACCEPTED, reason="Incomplete accepted result.")


def test_codex_run_model_loads_example() -> None:
    run = InvestigationRun.model_validate_json(
        Path("examples/milltown_codex_run.json").read_text(encoding="utf-8")
    )

    assert run.task.locality == "Milltown"
    assert run.candidate.produced_by == "codex"
    assert run.candidate.result.count == 17
    assert run.candidate.result.observed_time_text == "approximately 9:10 p.m."
    assert run.candidate.result.time_context is not None
    assert run.candidate.result.time_context.observed_time_local == "21:10"
    assert run.candidate.result.time_context.day_part == "night"
    assert run.source_bundle.documents
    assert run.source_bundle.places


def test_occupancy_lead_model_loads_example_array() -> None:
    payload = json.loads(Path("examples/ph_commercial_leads.json").read_text(encoding="utf-8"))
    lead = OccupancyLead.model_validate(payload[0])

    assert lead.is_valid_occupancy_report
    assert lead.occupancy_data[0].count == 83
    assert lead.location.country == "PH"


def test_work_item_defaults_are_backward_compatible() -> None:
    item = WorkItem.model_validate(
        {
            "work_item_id": "legacy-restaurants",
            "batch_id": "legacy",
            "locality": "Milltown",
            "country": "US",
            "profile_id": "restaurants_bars",
            "created_at": "2026-06-23T00:00:00Z",
            "updated_at": "2026-06-23T00:00:00Z",
        }
    )

    assert item.quota.target_accepted_count == 5
    assert item.quota.max_sources_examined == 40
    assert item.progress.accepted_count == 0
    assert item.progress.run_files == ()


def test_building_type_profile_source_type_defaults_are_backward_compatible() -> None:
    profile = BuildingTypeProfile.model_validate(
        {
            "profile_id": "legacy",
            "label": "Legacy",
            "source_search_prompt": "Find useful sources.",
        }
    )

    assert profile.preferred_source_types == ()
    assert profile.context_only_source_types == ()

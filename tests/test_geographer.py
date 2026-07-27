from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from pdt_observer.dialogue import load_dialogue, render_dialogue
from pdt_observer.geographer import (
    geographer_prompt_guidance,
    load_geographer_plan,
    render_geographer_prompt,
    run_geographer,
)
from pdt_observer.models import GeographerPlanStatus

PROPOSAL = {
    "search_languages": ["English", "Cebuano"],
    "administrative_terms": [
        {
            "standard_term": "district",
            "local_term": "barangay",
            "language": "Filipino",
            "usage_note": "Use with the locality and facility alias.",
        }
    ],
    "public_safety_terms": [
        {
            "standard_term": "fire department",
            "local_term": "BFP",
            "language": "English",
            "usage_note": "Use the agency abbreviation in incident searches.",
        }
    ],
    "facility_terms": [],
    "query_adjustments": ["BFP Cebu restaurant evacuated"],
    "source_urls": ["https://example.test/local-context"],
    "commentary": "I found that BFP and barangay are useful local search terms, so I added them.",
    "rationale": "Local agency and administrative vocabulary should expose regional reporting.",
}


def proposal_runner(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    output_path = Path(command[command.index("-o") + 1])
    output_path.write_text(json.dumps(PROPOSAL), encoding="utf-8")
    assert "Minimal Geographic Vernacular Review" in prompt
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def invalid_runner(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    output_path = Path(command[command.index("-o") + 1])
    output_path.write_text("not json", encoding="utf-8")
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def test_geographer_prompt_is_bounded_to_vernacular_review() -> None:
    prompt = render_geographer_prompt(
        country="PH",
        locality="Cebu City",
        profile_set_name="restaurants",
        profile_id="full_service_restaurants",
    )

    assert "Do not harvest occupancy observations" in prompt
    assert "Names or abbreviations used for police, fire" in prompt
    assert "full-service restaurants" in prompt.casefold()


def test_geographer_prompt_can_cover_a_multi_locality_campaign() -> None:
    prompt = render_geographer_prompt(
        country="PH",
        locality=None,
        profile_set_name="campaign",
        localities=("Cebu City", "Davao City"),
        facility_types=("schools", "restaurants"),
    )

    assert "campaign localities: Cebu City, Davao City" in prompt
    assert "primary and secondary education" in prompt.casefold()
    assert "full-service restaurants" in prompt.casefold()
    assert "Locality-specific differences" in prompt


def test_run_geographer_writes_plan_and_colleague_dialogue(tmp_path: Path) -> None:
    plan = run_geographer(
        root=tmp_path,
        plan_id="ph-cebu-restaurants",
        country="PH",
        locality="Cebu City",
        profile_set_name="restaurants",
        profile_id="full_service_restaurants",
        codex_bin="codex-test",
        runner=proposal_runner,
    )

    assert plan.status == GeographerPlanStatus.COMPLETED
    assert plan.proposal.public_safety_terms[0].local_term == "BFP"
    assert Path(plan.artifact_path).is_file()
    assert load_geographer_plan(tmp_path, plan.artifact_path) == plan
    dialogue = render_dialogue(load_dialogue(tmp_path, plan.plan_id))
    assert "Geographer Agent: I found that BFP" in dialogue
    assert "Why: Local agency" in dialogue
    assert "BFP" in geographer_prompt_guidance(plan)


def test_run_geographer_falls_back_when_agent_output_is_invalid(tmp_path: Path) -> None:
    plan = run_geographer(
        root=tmp_path,
        plan_id="us-tennessee-schools",
        country="US",
        locality="Tennessee",
        profile_set_name="schools",
        profile_id=None,
        codex_bin="codex-test",
        runner=invalid_runner,
    )

    assert plan.status == GeographerPlanStatus.FALLBACK
    assert plan.proposal.query_adjustments == ()
    assert plan.error_message


def test_geographer_plan_loader_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="inside the application workspace"):
        load_geographer_plan(tmp_path, str(tmp_path.parent / "plan.json"))

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from pdt_observer.leads import render_lead_harvest_prompt
from pdt_observer.models import StrategyScoutPlan
from pdt_observer.profiles import get_profile_set
from pdt_observer.strategies import build_strategy_plan
from pdt_observer.strategy_scout import effective_strategy_plan, render_strategy_scout_prompt


def _valid_scout_payload() -> dict[str, object]:
    return {
        "run_id": "us-tn-restaurants",
        "country": "US",
        "locality": "Tennessee",
        "profile_set": "restaurants",
        "profile_id": "full_service_restaurants",
        "recommended_strategy_order": [
            "official_event_attendance",
            "enforcement_inspection",
            "incident_evacuation",
        ],
        "recommendations": [
            {
                "strategy_id": "official_event_attendance",
                "emphasis": "primary",
                "rationale": "Local event listings may have bounded attendance.",
                "query_patterns": ["restaurant event attendance Tennessee"],
                "expected_traps": ["tickets sold"],
            },
            {
                "strategy_id": "enforcement_inspection",
                "emphasis": "secondary",
                "rationale": "Code inspections may count people present.",
                "query_patterns": ["restaurant people found inside Tennessee"],
                "expected_traps": ["capacity"],
            },
            {
                "strategy_id": "incident_evacuation",
                "emphasis": "secondary",
                "rationale": "Fire reports may have evacuated patrons.",
                "query_patterns": ["restaurant patrons evacuated Tennessee"],
                "expected_traps": ["injury counts"],
            },
        ],
        "local_source_ideas": ["fire marshal reports"],
        "overall_rationale": "Try event attendance before inspection and incident queries.",
        "confidence": "medium",
    }


def test_strategy_scout_output_validates_and_reorders_existing_plan() -> None:
    deterministic = build_strategy_plan(
        get_profile_set("restaurants"),
        profile_id="full_service_restaurants",
    )
    scout = StrategyScoutPlan.model_validate(_valid_scout_payload())

    effective = effective_strategy_plan(deterministic, scout)

    assert effective.planner == "strategy_scout_guided_v1"
    assert [item.strategy_id.value for item in effective.recommendations][:3] == [
        "official_event_attendance",
        "enforcement_inspection",
        "incident_evacuation",
    ]


def test_strategy_scout_rejects_unknown_strategy_ids() -> None:
    payload = _valid_scout_payload()
    payload["recommended_strategy_order"] = ["invented_strategy"]

    with pytest.raises(ValidationError):
        StrategyScoutPlan.model_validate(payload)


def test_strategy_scout_cannot_add_strategy_outside_deterministic_plan() -> None:
    payload = _valid_scout_payload()
    payload["recommended_strategy_order"] = ["research_measured_occupancy"]
    payload["recommendations"] = [
        {
            "strategy_id": "research_measured_occupancy",
            "emphasis": "primary",
            "rationale": "Try measured occupancy.",
            "query_patterns": ["occupancy study"],
            "expected_traps": ["modeled occupancy"],
        }
    ]
    scout = StrategyScoutPlan.model_validate(payload)
    deterministic = build_strategy_plan(
        get_profile_set("restaurants"),
        profile_id="full_service_restaurants",
    )

    with pytest.raises(ValueError, match="outside the deterministic plan"):
        effective_strategy_plan(deterministic, scout)


def test_strategy_scout_prompt_is_bounded_to_strategy_selection() -> None:
    profile_set = get_profile_set("restaurants")
    plan = build_strategy_plan(profile_set, profile_id="full_service_restaurants")

    prompt = render_strategy_scout_prompt(
        run_id="us-tn-restaurants",
        country="US",
        locality="Tennessee",
        profile_set=profile_set,
        profile_id="full_service_restaurants",
        strategy_plan=plan,
        geographer_plan=None,
    )

    assert "Do not harvest observations" in prompt
    assert "Do not invent evidence standards" in prompt
    assert "Land use:" in prompt


def test_harvest_prompt_is_bounded_occupancy_first_and_requests_activity() -> None:
    profile_set = get_profile_set("restaurants")
    deterministic = build_strategy_plan(profile_set, profile_id="full_service_restaurants")
    scout = StrategyScoutPlan.model_validate(_valid_scout_payload())
    effective = effective_strategy_plan(deterministic, scout)

    prompt = render_lead_harvest_prompt(
        country="US",
        locality="Tennessee",
        profile_set_name="restaurants",
        profile_id="full_service_restaurants",
        target=3,
        strategy_plan=effective,
        strategy_scout_plan=scout,
        run_id="us-tn-restaurants",
        activity_path=Path("agent_activity/us.harvester.json"),
    )

    assert "bounded time, incident, event, shift" in prompt
    assert "not the only acceptable pathway" in prompt
    assert "Strategy Scout Guidance" in prompt
    assert "Public Harvester Activity Report" in prompt

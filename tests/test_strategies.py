from __future__ import annotations

from pdt_observer.models import CountMethod, EvidenceStrategyType
from pdt_observer.profiles import get_profile_set
from pdt_observer.strategies import (
    build_strategy_plan,
    get_strategy,
    render_strategy_queries,
)


def _ids(profile_set_name: str, profile_id: str | None = None) -> tuple[str, ...]:
    plan = build_strategy_plan(
        get_profile_set(profile_set_name),
        profile_id=profile_id,
    )
    return tuple(item.strategy_id.value for item in plan.recommendations)


def test_manufacturing_plan_prioritizes_component_input_strategies() -> None:
    assert _ids("commercial", "light_manufacturing") == (
        "official_facility_statistics",
        "dataset_row_extraction",
        "corporate_location_list_discovery",
        "facility_universe_discovery",
        "regional_component_allocation",
        "operational_schedule_factors",
    )


def test_temporary_use_is_recommended_only_for_profiles_with_intermittent_venues() -> None:
    assert "temporary_use_occupancy" in _ids("recreation_entertainment", "theater")
    assert "temporary_use_occupancy" not in _ids(
        "institutions_public_service",
        "school_d_12",
    )
    assert "temporary_use_occupancy" not in _ids(
        "retail_service",
        "restaurant",
    )


def test_count_method_override_switches_strategy_family() -> None:
    direct_plan = build_strategy_plan(
        get_profile_set("commercial"),
        profile_id="light_manufacturing",
        count_method_override=CountMethod.DIRECT_COUNT,
    )
    component_plan = build_strategy_plan(
        get_profile_set("recreation_entertainment"),
        profile_id="theater",
        count_method_override=CountMethod.POPULATION_SUBCOMPONENT,
    )

    assert direct_plan.recommendations[0].strategy_id == EvidenceStrategyType.INCIDENT_EVACUATION
    assert (
        component_plan.recommendations[0].strategy_id
        == EvidenceStrategyType.OFFICIAL_FACILITY_STATISTICS
    )


def test_strategy_plan_contains_auditable_reasons() -> None:
    plan = build_strategy_plan(
        get_profile_set("recreation_entertainment"),
        profile_id="theater",
    )
    temporary = next(
        item
        for item in plan.recommendations
        if item.strategy_id == EvidenceStrategyType.TEMPORARY_USE_OCCUPANCY
    )

    assert "arenas" in temporary.reason
    assert plan.planner == "deterministic_strategy_planner_v1"


def test_profile_level_strategy_preferences_override_family_defaults() -> None:
    religious = _ids("institutions_public_service", "religious")
    power_plants = _ids("commercial", "powerplants")
    theaters = _ids("recreation_entertainment", "theater")

    assert religious[:2] == (
        "incident_evacuation",
        "routine_dated_attendance",
    )
    assert power_plants[:2] == (
        "official_facility_statistics",
        "dataset_row_extraction",
    )
    assert theaters[:2] == (
        "official_event_attendance",
        "temporary_use_occupancy",
    )


def test_strategy_queries_mix_component_evidence_pathways() -> None:
    plan = build_strategy_plan(
        get_profile_set("commercial"),
        profile_id="heavy_manufacturing",
    )
    queries = render_strategy_queries(
        plan,
        locality="Pittsburgh",
        country="United States",
        aliases=("steel mill",),
        positive_phrases=("workers were evacuated",),
    )

    assert '"Pittsburgh" United States steel mill official statistics' in queries
    assert any("dataset CSV" in query for query in queries)
    assert any("company locations employees" in query for query in queries)
    assert any("directory facilities" in query for query in queries)
    assert any("regional employment allocation" in query for query in queries)
    assert any("shifts employees" in query for query in queries)


def test_strategy_registry_preserves_semantics_and_representativeness() -> None:
    strategy = get_strategy("official_event_attendance")

    assert "attended" in strategy.accepted_count_semantics
    assert strategy.default_representativeness == "event_specific"

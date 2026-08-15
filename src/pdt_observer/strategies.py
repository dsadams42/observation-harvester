from __future__ import annotations

from collections.abc import Iterable

from pdt_observer.models import (
    BuildingProfileSet,
    BuildingTypeProfile,
    CountMethod,
    EvidenceStrategy,
    EvidenceStrategyType,
    StrategyPlan,
    StrategyRecommendation,
)
from pdt_observer.profiles import apply_count_method_override

STRATEGIES: dict[EvidenceStrategyType, EvidenceStrategy] = {
    EvidenceStrategyType.INCIDENT_EVACUATION: EvidenceStrategy(
        strategy_id=EvidenceStrategyType.INCIDENT_EVACUATION,
        label="Incident and evacuation",
        objective=(
            "Find facility-specific counts of people inside, evacuated, trapped, rescued, or "
            "accounted for during a bounded incident."
        ),
        query_templates=(
            '"{locality}" "{phrase}" {alias}',
            '"{locality}" {alias} fire evacuated people',
            '"{locality}" "inside the {alias} when"',
        ),
        preferred_source_types=(
            "news report",
            "official emergency or public-safety report",
            "official facility incident statement",
        ),
        accepted_count_semantics=(
            "confirmed_inside",
            "evacuated",
            "trapped",
            "rescued",
            "accounted_for",
        ),
        negative_traps=(
            "injury or casualty count without presence evidence",
            "regional evacuation total",
            "facility capacity",
        ),
        default_representativeness="incident_specific",
    ),
    EvidenceStrategyType.ENFORCEMENT_INSPECTION: EvidenceStrategy(
        strategy_id=EvidenceStrategyType.ENFORCEMENT_INSPECTION,
        label="Enforcement and inspection",
        objective=(
            "Find people counted inside a named facility during an inspection, raid, code "
            "enforcement action, or regulatory visit."
        ),
        query_templates=(
            '"{locality}" {alias} "people found inside"',
            '"{locality}" {alias} overcrowding citation',
            '"{locality}" {alias} inspection people present',
        ),
        preferred_source_types=(
            "official police or regulator report",
            "fire marshal or code-enforcement notice",
            "news report quoting the responsible authority",
        ),
        accepted_count_semantics=("counted_inside", "found_present", "inspected_present"),
        negative_traps=(
            "licensed capacity without an observed count",
            "arrest total without facility-presence confirmation",
            "staff or responders silently added to patrons",
        ),
        default_representativeness="atypical_or_unknown",
    ),
    EvidenceStrategyType.OFFICIAL_EVENT_ATTENDANCE: EvidenceStrategy(
        strategy_id=EvidenceStrategyType.OFFICIAL_EVENT_ATTENDANCE,
        label="Official event attendance",
        objective=(
            "Find an official or well-attributed attendance count for a bounded event held in one "
            "named building or venue."
        ),
        query_templates=(
            '"{locality}" {alias} "people attended"',
            '"{locality}" {alias} event attendance',
            '"{locality}" {alias} attendees official',
        ),
        preferred_source_types=(
            "official venue or organizer announcement",
            "government or institutional event report",
            "news report quoting an official attendance count",
        ),
        accepted_count_semantics=("attended", "checked_in", "ticket_scanned"),
        negative_traps=(
            "tickets sold without attendance confirmation",
            "multi-building campus event total",
            "maximum venue capacity",
        ),
        default_representativeness="event_specific",
    ),
    EvidenceStrategyType.ROUTINE_DATED_ATTENDANCE: EvidenceStrategy(
        strategy_id=EvidenceStrategyType.ROUTINE_DATED_ATTENDANCE,
        label="Routine dated attendance",
        objective=(
            "Find a count explicitly tied to people physically present during a particular date, "
            "session, or bounded operating period."
        ),
        query_templates=(
            '"{locality}" {alias} "people present"',
            '"{locality}" {alias} daily attendance',
            '"{locality}" {alias} attendance report filetype:pdf',
        ),
        preferred_source_types=(
            "official daily or session attendance record",
            "institutional operational report",
            "government inspection or reporting document",
        ),
        accepted_count_semantics=("present", "attended", "checked_in"),
        negative_traps=(
            "enrollment",
            "registered membership",
            "annual visitors",
            "employees assigned rather than present",
        ),
        default_representativeness="routine_period",
    ),
    EvidenceStrategyType.SHIFT_OPERATIONAL_PRESENCE: EvidenceStrategy(
        strategy_id=EvidenceStrategyType.SHIFT_OPERATIONAL_PRESENCE,
        label="Shift and operational presence",
        objective=(
            "Find workers or staff explicitly reported as physically present on a named shift or "
            "during a bounded operating period."
        ),
        query_templates=(
            '"{locality}" {alias} "workers on shift"',
            '"{locality}" {alias} "employees on duty"',
            '"{locality}" {alias} "working at the time"',
        ),
        preferred_source_types=(
            "official workplace investigation",
            "labor or safety regulator report",
            "company statement or news report with a shift-specific count",
        ),
        accepted_count_semantics=("on_shift", "on_duty", "working_at_time"),
        negative_traps=(
            "total workforce",
            "scheduled workers without presence confirmation",
            "site-wide count spanning multiple buildings",
        ),
        default_representativeness="operational_period",
    ),
    EvidenceStrategyType.LEGAL_INVESTIGATIVE_RECORDS: EvidenceStrategy(
        strategy_id=EvidenceStrategyType.LEGAL_INVESTIGATIVE_RECORDS,
        label="Legal and investigative records",
        objective=(
            "Find facility-specific headcounts in court, inquiry, accident-investigation, or "
            "regulatory documents."
        ),
        query_templates=(
            'filetype:pdf "{locality}" {alias} occupants',
            'filetype:pdf "{locality}" {alias} workers present',
            '"{locality}" {alias} investigation report evacuated',
        ),
        preferred_source_types=(
            "court or inquiry record",
            "occupational-safety investigation",
            "fire, environmental, or disaster investigation report",
        ),
        accepted_count_semantics=("confirmed_inside", "present", "on_shift", "evacuated"),
        negative_traps=(
            "workforce background statistics",
            "counts covering an entire site",
            "secondary summary that conflicts with the primary report",
        ),
        default_representativeness="event_or_investigation_specific",
    ),
    EvidenceStrategyType.TEMPORARY_USE_OCCUPANCY: EvidenceStrategy(
        strategy_id=EvidenceStrategyType.TEMPORARY_USE_OCCUPANCY,
        label="Temporary or event-driven use",
        objective=(
            "Find attendance during a temporary use that represents the meaningful occupied state "
            "of an arena, hall, theater, event venue, shelter, or similar intermittently used "
            "space."
        ),
        query_templates=(
            '"{locality}" {alias} event attendance',
            '"{locality}" {alias} "people attended"',
            '"{locality}" {alias} evacuees sheltered',
        ),
        preferred_source_types=(
            "official venue, organizer, or government announcement",
            "ticket-scan or check-in report",
            "news report quoting a bounded attendance count",
        ),
        accepted_count_semantics=("attended", "checked_in", "temporarily_sheltered"),
        negative_traps=(
            "ordinary capacity",
            "tickets sold without arrivals",
            "event totals spanning outdoor areas or multiple buildings",
        ),
        default_representativeness="temporary_use",
    ),
    EvidenceStrategyType.RESEARCH_MEASURED_OCCUPANCY: EvidenceStrategy(
        strategy_id=EvidenceStrategyType.RESEARCH_MEASURED_OCCUPANCY,
        label="Research and measured occupancy",
        objective=(
            "Find a documented headcount or sensor-derived occupancy measurement with a stated "
            "time window and spatial scope."
        ),
        query_templates=(
            '"{locality}" {alias} occupancy study',
            'filetype:pdf "{locality}" {alias} measured occupancy',
            '"{locality}" {alias} people count study',
        ),
        preferred_source_types=(
            "peer-reviewed study",
            "government or institutional measurement report",
            "documented post-occupancy or building-performance study",
        ),
        accepted_count_semantics=("direct_headcount", "sensor_measured", "sampled_present"),
        negative_traps=(
            "modeled occupancy without observed measurements",
            "average entries presented as simultaneous occupancy",
            "anonymized building that cannot be georeferenced",
        ),
        default_representativeness="study_specific",
    ),
    EvidenceStrategyType.OFFICIAL_FACILITY_STATISTICS: EvidenceStrategy(
        strategy_id=EvidenceStrategyType.OFFICIAL_FACILITY_STATISTICS,
        label="Official facility statistics",
        objective=(
            "Find source-backed facility-level component inputs such as enrollment, staff, "
            "beds, rooms, capacity, occupied beds, employees, units, or other facility facts."
        ),
        query_templates=(
            '"{locality}" {alias} official statistics',
            '"{locality}" {alias} enrollment staff beds rooms',
            '"{locality}" {alias} annual report statistics',
        ),
        preferred_source_types=(
            "official facility page",
            "government registry",
            "institutional annual report",
            "regulatory facility statistics",
        ),
        accepted_count_semantics=("component_input", "facility_statistic", "source_backed_input"),
        negative_traps=(
            "component value converted into final occupancy",
            "undated value without period label",
            "multi-facility total mislabeled as one facility",
        ),
        default_representativeness="component_input",
    ),
    EvidenceStrategyType.REGIONAL_DEMOGRAPHIC_STATISTICS: EvidenceStrategy(
        strategy_id=EvidenceStrategyType.REGIONAL_DEMOGRAPHIC_STATISTICS,
        label="Regional demographic statistics",
        objective=(
            "Find locality, region, or country-level component inputs such as household size, "
            "school attendance, unemployment, age/sex groups, occupancy rates, or census rates."
        ),
        query_templates=(
            '"{locality}" census household size',
            '"{locality}" school attendance rate',
            '"{locality}" unemployment rate age sex population',
        ),
        preferred_source_types=(
            "national statistics office",
            "census table",
            "official survey table",
            "government open-data portal",
        ),
        accepted_count_semantics=("regional_component_input", "demographic_rate", "census_input"),
        negative_traps=(
            "regional statistic treated as facility-specific",
            "unlabeled geographic scope",
            "derived final occupancy estimate",
        ),
        default_representativeness="regional_component_input",
    ),
    EvidenceStrategyType.OPERATIONAL_SCHEDULE_FACTORS: EvidenceStrategy(
        strategy_id=EvidenceStrategyType.OPERATIONAL_SCHEDULE_FACTORS,
        label="Operational schedule factors",
        objective=(
            "Find component inputs about shifts, day/night staffing fractions, operating hours, "
            "days open, school shifts, or staff presence factors."
        ),
        query_templates=(
            '"{locality}" {alias} shifts employees',
            '"{locality}" {alias} operating hours staff',
            '"{locality}" {alias} day night staff',
        ),
        preferred_source_types=(
            "official operating schedule",
            "staffing report",
            "regulatory filing",
            "institutional operations report",
        ),
        accepted_count_semantics=("schedule_factor", "staffing_factor", "operational_input"),
        negative_traps=(
            "scheduled staffing treated as observed occupancy",
            "ordinary hours without a usable component value",
            "unattributed percentage or factor",
        ),
        default_representativeness="operational_component_input",
    ),
    EvidenceStrategyType.VISITOR_TRAFFIC_VOLUME: EvidenceStrategy(
        strategy_id=EvidenceStrategyType.VISITOR_TRAFFIC_VOLUME,
        label="Visitor and traffic volume statistics",
        objective=(
            "Find component inputs such as annual visitors, daily visitors, passenger traffic, "
            "freight, ship passengers, or visit duration for facilities where volume informs "
            "population estimates."
        ),
        query_templates=(
            '"{locality}" {alias} annual visitors',
            '"{locality}" {alias} passenger traffic employees',
            '"{locality}" {alias} daily visitors average visit time',
        ),
        preferred_source_types=(
            "official visitor statistics",
            "transport authority report",
            "tourism or cultural annual report",
            "facility traffic report",
        ),
        accepted_count_semantics=("visitor_volume_input", "traffic_volume_input", "annual_flow"),
        negative_traps=(
            "annual flow treated as simultaneous occupancy",
            "ticket sales converted into attendance without source support",
            "unscoped multi-site total",
        ),
        default_representativeness="volume_component_input",
    ),
}


_PROFILE_SET_PRIORITIES: dict[str, tuple[EvidenceStrategyType, ...]] = {
    "schools": (
        EvidenceStrategyType.INCIDENT_EVACUATION,
        EvidenceStrategyType.ROUTINE_DATED_ATTENDANCE,
        EvidenceStrategyType.OFFICIAL_EVENT_ATTENDANCE,
        EvidenceStrategyType.LEGAL_INVESTIGATIVE_RECORDS,
    ),
    "manufacturing": (
        EvidenceStrategyType.INCIDENT_EVACUATION,
        EvidenceStrategyType.SHIFT_OPERATIONAL_PRESENCE,
        EvidenceStrategyType.LEGAL_INVESTIGATIVE_RECORDS,
        EvidenceStrategyType.ENFORCEMENT_INSPECTION,
    ),
    "restaurants": (
        EvidenceStrategyType.ENFORCEMENT_INSPECTION,
        EvidenceStrategyType.INCIDENT_EVACUATION,
        EvidenceStrategyType.OFFICIAL_EVENT_ATTENDANCE,
        EvidenceStrategyType.ROUTINE_DATED_ATTENDANCE,
    ),
    "commercial_business": (
        EvidenceStrategyType.INCIDENT_EVACUATION,
        EvidenceStrategyType.ENFORCEMENT_INSPECTION,
        EvidenceStrategyType.SHIFT_OPERATIONAL_PRESENCE,
        EvidenceStrategyType.LEGAL_INVESTIGATIVE_RECORDS,
    ),
    "residential": (
        EvidenceStrategyType.INCIDENT_EVACUATION,
        EvidenceStrategyType.ENFORCEMENT_INSPECTION,
        EvidenceStrategyType.LEGAL_INVESTIGATIVE_RECORDS,
        EvidenceStrategyType.RESEARCH_MEASURED_OCCUPANCY,
    ),
    "public_venues": (
        EvidenceStrategyType.INCIDENT_EVACUATION,
        EvidenceStrategyType.ENFORCEMENT_INSPECTION,
        EvidenceStrategyType.OFFICIAL_EVENT_ATTENDANCE,
        EvidenceStrategyType.ROUTINE_DATED_ATTENDANCE,
    ),
}

_TEMPORARY_USE_ALIASES = {
    "arena",
    "venue",
    "event venue",
    "hall",
    "theater",
    "stadium",
    "shelter",
}


def get_strategy(strategy_id: EvidenceStrategyType | str) -> EvidenceStrategy:
    return STRATEGIES[EvidenceStrategyType(strategy_id)]


def _profiles_support_temporary_use(profiles: Iterable[BuildingTypeProfile]) -> bool:
    return any(
        alias.casefold() in _TEMPORARY_USE_ALIASES
        for profile in profiles
        for alias in profile.venue_aliases
    )


def _recommendation_reason(
    strategy_id: EvidenceStrategyType,
    profile_set: BuildingProfileSet,
    profiles: tuple[BuildingTypeProfile, ...],
) -> str:
    labels = ", ".join(profile.label for profile in profiles)
    if strategy_id == EvidenceStrategyType.TEMPORARY_USE_OCCUPANCY:
        return (
            "The selected subtype includes intermittently occupied arenas, halls, theaters, "
            "event venues, or shelters where event-time use is analytically meaningful."
        )
    if strategy_id in {
        EvidenceStrategyType.OFFICIAL_FACILITY_STATISTICS,
        EvidenceStrategyType.REGIONAL_DEMOGRAPHIC_STATISTICS,
        EvidenceStrategyType.OPERATIONAL_SCHEDULE_FACTORS,
        EvidenceStrategyType.VISITOR_TRAFFIC_VOLUME,
    }:
        component_fields = sorted(
            {
                field
                for profile in profiles
                for field in profile.component_count_fields + profile.regional_stat_fields
            }
        )
        field_text = ", ".join(component_fields) if component_fields else "configured inputs"
        return (
            f"{STRATEGIES[strategy_id].label} is a component-input pathway for "
            f"{profile_set.label} within the selected scope ({labels}); target fields: "
            f"{field_text}."
        )
    return (
        f"{STRATEGIES[strategy_id].label} is a productive evidence pathway for "
        f"{profile_set.label} within the selected scope ({labels})."
    )


def build_strategy_plan(
    profile_set: BuildingProfileSet,
    *,
    profile_id: str | None = None,
    count_method_override: CountMethod | None = None,
) -> StrategyPlan:
    profile_set = apply_count_method_override(profile_set, count_method_override)
    profiles = tuple(
        profile
        for profile in profile_set.profiles
        if profile.enabled and (profile_id is None or profile.profile_id == profile_id)
    )
    if not profiles:
        raise ValueError(
            f"no enabled profile {profile_id!r} in profile set {profile_set.profile_set_id!r}"
        )

    direct_strategy_ids: list[EvidenceStrategyType] = []
    for profile in sorted(profiles, key=lambda item: item.priority):
        direct_strategy_ids.extend(profile.preferred_strategy_ids)
    if not direct_strategy_ids:
        direct_strategy_ids = list(
            _PROFILE_SET_PRIORITIES.get(
                profile_set.profile_set_id,
                (
                    EvidenceStrategyType.INCIDENT_EVACUATION,
                    EvidenceStrategyType.ENFORCEMENT_INSPECTION,
                    EvidenceStrategyType.LEGAL_INVESTIGATIVE_RECORDS,
                    EvidenceStrategyType.RESEARCH_MEASURED_OCCUPANCY,
                ),
            )
        )
    if _profiles_support_temporary_use(profiles):
        insertion_index = min(2, len(direct_strategy_ids))
        direct_strategy_ids.insert(insertion_index, EvidenceStrategyType.TEMPORARY_USE_OCCUPANCY)

    component_strategy_ids = [
        EvidenceStrategyType.OFFICIAL_FACILITY_STATISTICS,
        EvidenceStrategyType.OPERATIONAL_SCHEDULE_FACTORS,
    ]
    if any(profile.regional_stat_fields for profile in profiles):
        component_strategy_ids.append(EvidenceStrategyType.REGIONAL_DEMOGRAPHIC_STATISTICS)
    if any(
        any(
            marker in field.casefold()
            for marker in ("visitor", "passenger", "traffic", "freight", "visit time")
        )
        for profile in profiles
        for field in profile.component_count_fields
    ):
        component_strategy_ids.append(EvidenceStrategyType.VISITOR_TRAFFIC_VOLUME)

    methods = {profile.count_method for profile in profiles}
    if methods == {CountMethod.POPULATION_SUBCOMPONENT}:
        strategy_ids = component_strategy_ids
    elif CountMethod.HYBRID in methods or CountMethod.POPULATION_SUBCOMPONENT in methods:
        strategy_ids = component_strategy_ids + direct_strategy_ids
    else:
        strategy_ids = direct_strategy_ids

    unique_strategy_ids = tuple(dict.fromkeys(strategy_ids))
    return StrategyPlan(
        recommendations=tuple(
            StrategyRecommendation(
                strategy_id=strategy_id,
                priority=index * 10,
                reason=_recommendation_reason(strategy_id, profile_set, profiles),
            )
            for index, strategy_id in enumerate(unique_strategy_ids, start=1)
        )
    )


def strategy_plan_definitions(plan: StrategyPlan) -> tuple[EvidenceStrategy, ...]:
    return tuple(get_strategy(item.strategy_id) for item in plan.recommendations)


def render_strategy_queries(
    plan: StrategyPlan,
    *,
    locality: str,
    country: str | None = None,
    aliases: tuple[str, ...],
    positive_phrases: tuple[str, ...],
    maximum: int = 16,
) -> tuple[str, ...]:
    rendered: list[str] = []
    resolved_aliases = aliases[:3] or ("facility",)
    resolved_phrases = positive_phrases[:3] or ("people were inside",)

    def render_query(template: str, alias: str, phrase: str) -> str:
        query = template.format(locality=locality, alias=alias, phrase=phrase)
        if country:
            query = query.replace(f'"{locality}"', f'"{locality}" {country}', 1)
        return query

    # Give every recommended pathway at least one concrete starting query before adding variants.
    for index, recommendation in enumerate(plan.recommendations):
        strategy = get_strategy(recommendation.strategy_id)
        query = render_query(
            strategy.query_templates[0],
            resolved_aliases[index % len(resolved_aliases)],
            resolved_phrases[index % len(resolved_phrases)],
        )
        if query not in rendered:
            rendered.append(query)
        if len(rendered) >= maximum:
            return tuple(rendered)

    for recommendation in plan.recommendations:
        strategy = get_strategy(recommendation.strategy_id)
        for template in strategy.query_templates:
            for alias in resolved_aliases:
                phrase = resolved_phrases[len(rendered) % len(resolved_phrases)]
                query = render_query(template, alias, phrase)
                if query not in rendered:
                    rendered.append(query)
                if len(rendered) >= maximum:
                    return tuple(rendered)
    return tuple(rendered)

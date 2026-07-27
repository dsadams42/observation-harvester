from __future__ import annotations

from collections.abc import Iterable

from pdt_observer.models import (
    BuildingProfileSet,
    BuildingTypeProfile,
    EvidenceStrategy,
    EvidenceStrategyType,
    StrategyPlan,
    StrategyRecommendation,
)

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
    return (
        f"{STRATEGIES[strategy_id].label} is a productive evidence pathway for "
        f"{profile_set.label} within the selected scope ({labels})."
    )


def build_strategy_plan(
    profile_set: BuildingProfileSet,
    *,
    profile_id: str | None = None,
) -> StrategyPlan:
    profiles = tuple(
        profile
        for profile in profile_set.profiles
        if profile.enabled and (profile_id is None or profile.profile_id == profile_id)
    )
    if not profiles:
        raise ValueError(
            f"no enabled profile {profile_id!r} in profile set {profile_set.profile_set_id!r}"
        )

    strategy_ids = list(
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
        insertion_index = min(2, len(strategy_ids))
        strategy_ids.insert(insertion_index, EvidenceStrategyType.TEMPORARY_USE_OCCUPANCY)

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

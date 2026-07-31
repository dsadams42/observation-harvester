from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from pydantic import TypeAdapter

from pdt_observer.activity import render_activity_prompt_instructions
from pdt_observer.geographer import geographer_prompt_guidance
from pdt_observer.models import (
    BuildingProfileSet,
    CandidateObservation,
    Evidence,
    GeographerPlan,
    InvestigationResult,
    InvestigationRun,
    InvestigationTask,
    LeadQaqcReview,
    ObservationType,
    OccupancyLead,
    ResultStatus,
    SourceBundle,
    SourceDocument,
    StrategyPlan,
    StrategyScoutPlan,
)
from pdt_observer.profiles import get_profile_set, narrow_profile_set
from pdt_observer.prompting import country_search_context
from pdt_observer.strategies import (
    build_strategy_plan,
    get_strategy,
    render_strategy_queries,
)
from pdt_observer.workflow import slugify

LEAD_LIST_ADAPTER: TypeAdapter[tuple[OccupancyLead, ...]] = TypeAdapter(
    tuple[OccupancyLead, ...]
)
QAQC_REVIEW_LIST_ADAPTER: TypeAdapter[tuple[LeadQaqcReview, ...]] = TypeAdapter(
    tuple[LeadQaqcReview, ...]
)


def load_leads(path: Path) -> tuple[OccupancyLead, ...]:
    return LEAD_LIST_ADAPTER.validate_json(path.read_text(encoding="utf-8"))


def load_qaqc_reviews(path: Path) -> tuple[LeadQaqcReview, ...]:
    return QAQC_REVIEW_LIST_ADAPTER.validate_json(path.read_text(encoding="utf-8"))


def leads_to_json(leads: tuple[OccupancyLead, ...]) -> str:
    payload = [lead.model_dump(mode="json") for lead in leads]
    return json.dumps(payload, indent=2)


def qaqc_reviews_to_json(reviews: tuple[LeadQaqcReview, ...]) -> str:
    payload = [review.model_dump(mode="json") for review in reviews]
    return json.dumps(payload, indent=2)


def summarize_leads(leads: tuple[OccupancyLead, ...]) -> dict[str, object]:
    valid = [lead for lead in leads if lead.is_valid_occupancy_report]
    counts = sum(len(lead.occupancy_data) for lead in valid)
    countries = sorted({lead.location.country for lead in valid})
    cities = sorted({lead.location.city_or_region for lead in valid})
    facility_level_count = sum(1 for lead in valid if lead.is_facility_level is True)
    aggregate_count = sum(1 for lead in valid if lead.is_regional_aggregate is True)
    counts_by_strategy: dict[str, int] = {}
    for lead in valid:
        strategy_id = lead.strategy_id.value if lead.strategy_id is not None else "unattributed"
        counts_by_strategy[strategy_id] = counts_by_strategy.get(strategy_id, 0) + 1
    return {
        "lead_count": len(leads),
        "valid_occupancy_reports": len(valid),
        "occupancy_count_rows": counts,
        "countries": countries,
        "cities_or_regions": cities,
        "facility_level_count": facility_level_count,
        "regional_aggregate_count": aggregate_count,
        "counts_by_strategy": counts_by_strategy,
    }


def leads_to_jsonl(leads: tuple[OccupancyLead, ...]) -> str:
    lines = [json.dumps(lead.model_dump(mode="json"), sort_keys=True) for lead in leads]
    return "\n".join(lines) + ("\n" if lines else "")


def leads_to_csv(leads: tuple[OccupancyLead, ...]) -> str:
    fieldnames = (
        "lead_index",
        "source_url",
        "source_title",
        "source_type",
        "evidence_quote",
        "incident_date",
        "incident_time",
        "count",
        "group_type",
        "facility_name",
        "specific_address_or_landmark",
        "city_or_region",
        "country",
        "confidence",
        "is_facility_level",
        "is_regional_aggregate",
        "review_flags",
        "review_notes",
        "strategy_id",
        "count_semantics",
        "representativeness",
    )
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for lead_index, lead in enumerate(leads):
        for datum in lead.occupancy_data:
            writer.writerow(
                {
                    "lead_index": lead_index,
                    "source_url": lead.source_url,
                    "source_title": lead.source_title,
                    "source_type": lead.source_type.value,
                    "evidence_quote": lead.evidence_quote or "",
                    "incident_date": lead.incident_date,
                    "incident_time": lead.incident_time,
                    "count": datum.count,
                    "group_type": datum.group_type,
                    "facility_name": lead.location.facility_name,
                    "specific_address_or_landmark": lead.location.specific_address_or_landmark,
                    "city_or_region": lead.location.city_or_region,
                    "country": lead.location.country,
                    "confidence": lead.confidence.value,
                    "is_facility_level": (
                        "" if lead.is_facility_level is None else lead.is_facility_level
                    ),
                    "is_regional_aggregate": (
                        "" if lead.is_regional_aggregate is None else lead.is_regional_aggregate
                    ),
                    "review_flags": ";".join(lead.review_flags),
                    "review_notes": lead.review_notes or "",
                    "strategy_id": lead.strategy_id.value if lead.strategy_id is not None else "",
                    "count_semantics": lead.count_semantics or "",
                    "representativeness": lead.representativeness or "",
                }
            )
    return output.getvalue()


def export_leads(leads: tuple[OccupancyLead, ...], *, output_format: str) -> str:
    if output_format == "jsonl":
        return leads_to_jsonl(leads)
    if output_format == "csv":
        return leads_to_csv(leads)
    raise ValueError("lead export format must be csv or jsonl")


def promote_lead_to_run(
    lead: OccupancyLead,
    *,
    task_id: str | None = None,
) -> InvestigationRun:
    resolved_task_id = task_id or slugify(
        f"{lead.location.country}-{lead.location.city_or_region}-{lead.location.facility_name}"
    )
    document_id = f"{resolved_task_id}-source"
    has_source = lead.source_url != "Not provided"
    has_quote = lead.evidence_quote is not None and bool(lead.evidence_quote.strip())
    evidence = (
        Evidence(
            document_id=document_id,
            source_url=lead.source_url,
            supporting_quote=lead.evidence_quote or "",
        )
        if has_source and has_quote
        else None
    )
    documents = (
        (
            SourceDocument(
                document_id=document_id,
                title=lead.source_title,
                source_url=lead.source_url,
                locality=lead.location.city_or_region,
                country=lead.location.country,
                text=lead.evidence_quote or "",
                tags=("lead-promotion",),
            ),
        )
        if has_source and has_quote
        else ()
    )
    first_count = lead.occupancy_data[0].count if lead.occupancy_data else None
    group_labels = ", ".join(datum.group_type for datum in lead.occupancy_data)
    result = InvestigationResult(
        status=ResultStatus.REVIEW,
        count=first_count,
        observation_type=ObservationType.PEOPLE_PRESENT,
        place_name=lead.location.facility_name,
        evidence=evidence,
        reason=(
            "Draft promoted from broad lead harvest; needs exact source text and georeference "
            f"review before acceptance. Lead groups: {group_labels}."
        ),
    )
    return InvestigationRun(
        task=InvestigationTask(
            task_id=resolved_task_id,
            locality=lead.location.city_or_region,
            country=lead.location.country,
            observation_type=ObservationType.PEOPLE_PRESENT,
        ),
        source_bundle=SourceBundle(documents=documents, places=()),
        candidate=CandidateObservation(
            result=result,
            produced_by="lead-promotion",
            strategy_id=lead.strategy_id,
            count_semantics=lead.count_semantics,
            representativeness=lead.representativeness,
        ),
    )


def render_lead_qaqc_prompt(
    leads: tuple[OccupancyLead, ...],
    *,
    source_label: str = "lead JSON",
    expected_country: str | None = None,
    expected_locality: str | None = None,
) -> str:
    lead_payload = json.dumps([lead.model_dump(mode="json") for lead in leads], indent=2)
    scope_text = (
        f"\nRequested geographic scope: country `{expected_country or 'unspecified'}`; "
        f"locality `{expected_locality or 'unspecified'}`.\n"
    )
    return f"""# Occupancy Lead QAQC Verification

You are a careful QAQC verification agent for harvested occupancy leads. Your job is to inspect
the source URL for each lead, verify whether the source supports the reported occupancy values,
and return review JSON only.

Input source: {source_label}
{scope_text}

## Verification Tasks

For each lead in the input array:
- Open the `source_url` when it is a usable URL. If the URL is missing, broken, paywalled, or
  unavailable, use `source_unreachable` and recommend `retry` or `review`.
- Search within the page/source text for each reported `occupancy_data[].count`.
- Confirm whether the count is tied to the reported facility and incident, not merely a capacity,
  enrollment, workforce size, regional disaster total, or unrelated statistic.
- Check whether the source supports the lead's `strategy_id` and `count_semantics`. For example,
  tickets sold do not prove event attendance, scheduled workers do not prove on-shift presence,
  and entries accumulated over a day do not prove simultaneous occupancy.
- Set `strategy_match` to true only when the source supports that evidence pathway and count
  meaning. Use null when an older lead has no strategy attribution.
- Confirm whether the facility name and city/region/country match the harvested lead.
- Enforce the requested geographic scope as a hard acceptance boundary. If the facility is
  outside the requested country or locality, set `location_match` to false, do not use `keep`,
  and reject the lead even when its source and occupancy count are otherwise valid. Regional
  locality descriptions may name a subregion, but they never authorize crossing the enclosing
  state, province, or country boundary.
- Capture an exact supporting quote when the source text supports the count. Keep quotes short
  and limited to the sentence or phrase needed to support the count.
- Do not invent support. If the source does not clearly support the lead, mark it for review or
  rejection.

## Status And Action Rules

Set `verification_status` to one of:
- `verified`: source is reachable and supports the count, facility, and location.
- `ambiguous`: source partially supports the lead, but one important detail is unclear.
- `count_not_found`: source is reachable, but the reported count is not found.
- `facility_mismatch`: count appears, but it is tied to a different facility/location.
- `source_unreachable`: source cannot be reached or inspected.
- `reject`: source clearly disproves the lead or the lead is not an occupancy observation.

Set `recommended_action` to one of:
- `keep`: verified and suitable for promotion.
- `review`: needs human review before promotion.
- `reject`: should not be promoted.
- `retry`: source needs to be replaced or rechecked.

## Output Format

Return strictly a single valid JSON array. Do not wrap the JSON in markdown or prose. Use this
exact schema:

[
  {{
    "lead_index": 0,
    "source_url": "String",
    "verification_status": "verified",
    "source_reachable": true,
    "facility_match": true,
    "location_match": true,
    "strategy_match": true,
    "count_checks": [
      {{
        "count": 0,
        "group_type": "String",
        "reported_count_found": true,
        "quote_found": true,
        "supporting_quote": "Exact quote or null",
        "notes": "String or null"
      }}
    ],
    "supporting_quote": "Best exact quote supporting the lead, or null",
    "recommended_action": "keep",
    "review_notes": "Short explanation of the verification decision"
  }}
]

## Leads To Verify

{lead_payload}
"""


def _unique_profile_values(profile_set: BuildingProfileSet, attr: str) -> tuple[str, ...]:
    values: list[str] = []
    for profile in profile_set.profiles:
        for value in getattr(profile, attr):
            if value not in values:
                values.append(value)
    return tuple(values)


def _bullet_list(values: tuple[str, ...]) -> str:
    if not values:
        return "- None"
    return "\n".join(f"- {value}" for value in values)


def _profile_scope_guidance(profile_set: BuildingProfileSet) -> str:
    if profile_set.profile_set_id == "schools":
        return (
            "Include bounded counts for school buildings and campus facilities. Do not treat "
            "ordinary enrollment, attendance, campus population, or seating capacity as occupancy "
            "observations unless the count is tied to a bounded date, session, event, incident, "
            "evacuation, or measured period."
        )
    if profile_set.profile_set_id == "manufacturing":
        return (
            "Include bounded counts for factories, plants, mills, workshops, and industrial "
            "production facilities. Workers on shift, evacuated workers, trapped employees, and "
            "rescued crew members are acceptable occupancy proxies. Do not treat workforce size "
            "or production capacity as occupancy observations."
        )
    if profile_set.profile_set_id == "restaurants":
        return (
            "Include bounded counts for restaurants, cafes, quick-service outlets, bars, and "
            "nightlife venues. Customers, patrons, diners, employees, attendees, and evacuated "
            "people are acceptable occupancy groups when tied to a bounded service period, event, "
            "inspection, or incident."
        )
    if profile_set.profile_set_id == "commercial_business":
        return (
            "Do not extract residential buildings or outdoor public open spaces unless the source "
            "ties the count to a named commercial/business facility."
        )
    if profile_set.profile_set_id == "residential":
        return (
            "Do not extract commercial-only facilities, workplaces, or outdoor public open spaces "
            "unless the source ties the count to a named residential facility, home, settlement, "
            "or residential portion of a mixed-use building."
        )
    return (
        "Do not extract records outside this facility type unless the source ties the count to a "
        "matching named facility."
    )


def _profile_occurrence_guidance(profile_set: BuildingProfileSet) -> str:
    lines: list[str] = []
    for profile in profile_set.profiles:
        if not profile.enabled:
            continue
        details: list[str] = []
        if profile.pdt_subtype:
            details.append(f"PDT subtype: {profile.pdt_subtype}")
        if profile.area_defined:
            details.append(f"Area scope: {profile.area_defined}")
        if profile.occupancy_groups:
            details.append(f"Expected groups: {', '.join(profile.occupancy_groups)}")
        if profile.day_occurrence:
            details.append(f"Day/open pattern: {profile.day_occurrence}")
        if profile.night_occurrence:
            details.append(f"Night/closed pattern: {profile.night_occurrence}")
        if profile.episodic_occurrence:
            details.append(f"Episodic patterns: {', '.join(profile.episodic_occurrence)}")
        if profile.contextual_count_fields:
            details.append(
                "Context-only counts: " + ", ".join(profile.contextual_count_fields)
            )
        if details:
            lines.append(f"- {profile.label}: {'; '.join(details)}.")
    if not lines:
        return "- No PDT occurrence hints are configured for this profile set."
    return "\n".join(lines)


def _strategy_scout_guidance(strategy_scout_plan: StrategyScoutPlan | None) -> str:
    if strategy_scout_plan is None:
        return (
            "No Strategy Scout sidecar is available for this run. Use the deterministic strategy "
            "plan below, sample more than one evidence pathway when practical, and lean into the "
            "strategy that produces source-backed facility-level counts."
        )

    recommendations: list[str] = []
    for item in strategy_scout_plan.recommendations:
        queries = "; ".join(item.query_patterns[:3]) or "No query pattern supplied"
        traps = "; ".join(item.expected_traps[:3]) or "No additional trap supplied"
        recommendations.append(
            f"- {item.strategy_id.value} ({item.emphasis.value}): {item.rationale} "
            f"Query ideas: {queries}. Expected traps: {traps}."
        )

    return f"""The Strategy Scout reviewed this facility/geography before harvest.

Recommended strategy order:
{_bullet_list(tuple(item.value for item in strategy_scout_plan.recommended_strategy_order))}

Scout rationale:
{strategy_scout_plan.overall_rationale}

Strategy notes:
{chr(10).join(recommendations) or "- None"}

Local source ideas:
{_bullet_list(strategy_scout_plan.local_source_ideas)}
"""


def render_lead_harvest_prompt(
    *,
    country: str,
    profile_set_name: str,
    target: int,
    locality: str | None = None,
    profile_id: str | None = None,
    geographer_plan: GeographerPlan | None = None,
    strategy_plan: StrategyPlan | None = None,
    strategy_scout_plan: StrategyScoutPlan | None = None,
    run_id: str | None = None,
    activity_path: Path | None = None,
) -> str:
    profile_set = get_profile_set(profile_set_name)
    if profile_id is not None:
        profile_set = narrow_profile_set(profile_set, profile_id)
    strategy_plan = strategy_plan or build_strategy_plan(profile_set, profile_id=profile_id)
    country_context = country_search_context(country)
    country_name = country_context["name"]
    locality_scope = (
        f"Focus on {locality}, {country_name}, but include nearby/clearly related records only "
        "when the source explicitly supports the location."
        if locality is not None
        else f"Search across {country_name}."
    )
    facility_labels = tuple(profile.label for profile in profile_set.profiles if profile.enabled)
    aliases = _unique_profile_values(profile_set, "venue_aliases")
    positive_patterns = _unique_profile_values(profile_set, "positive_evidence_patterns")
    negative_patterns = _unique_profile_values(profile_set, "negative_evidence_patterns")
    preferred_sources = _unique_profile_values(profile_set, "preferred_source_types")
    context_only_sources = _unique_profile_values(profile_set, "context_only_source_types")
    scope_guidance = _profile_scope_guidance(profile_set)
    occurrence_guidance = _profile_occurrence_guidance(profile_set)
    strategy_sections: list[str] = []
    for recommendation in strategy_plan.recommendations:
        strategy = get_strategy(recommendation.strategy_id)
        strategy_sections.append(
            f"### {recommendation.priority}. {strategy.label} "
            f"(`{strategy.strategy_id.value}`)\n\n"
            f"{recommendation.reason}\n\n"
            f"Objective: {strategy.objective}\n\n"
            f"Accepted count semantics: {', '.join(strategy.accepted_count_semantics)}.\n\n"
            f"Important traps:\n{_bullet_list(strategy.negative_traps)}"
        )
    strategy_guidance = "\n\n".join(strategy_sections)
    scout_guidance = _strategy_scout_guidance(strategy_scout_plan)
    strategy_queries = _bullet_list(
        render_strategy_queries(
            strategy_plan,
            locality=locality or country_name,
            country=country_name if locality is not None else None,
            aliases=aliases,
            positive_phrases=positive_patterns,
        )
    )
    vernacular_guidance = geographer_prompt_guidance(geographer_plan)
    activity_guidance = (
        render_activity_prompt_instructions(run_id=run_id, activity_path=activity_path)
        if run_id is not None and activity_path is not None
        else ""
    )

    return f"""# Broad Occupancy Lead Harvest

You are a specialized geospatial data extraction engine. Your objective is to search online public
sources, inspect unstructured source text, and extract source-backed facility-level lead records
for counts of people physically present during a bounded time, incident, event, shift, inspection,
transfer, shelter activation, operating state, or measured period.

Target: {target} lead records.
Country: {country_name} (`{country}`).
Scope: {locality_scope}
Facility type: {profile_set.label} (`{profile_set.profile_set_id}`).

## Inclusion Filter

Only extract records for facilities matching this facility type or selected subtype:
{_bullet_list(facility_labels)}

Facility aliases and examples:
{_bullet_list(aliases)}

{scope_guidance}

## Occupancy Extraction

Identify any specific, historical headcount of people physically present in, at, evacuated from,
trapped in, rescued from, transferred from, checked in to, attending, on duty at, sheltered in, or
measured within a matching facility during a bounded date, time, event, shift, inspection,
incident, operating period, or study window. Incidents such as fires, earthquakes, chemical leaks,
code violations, overcrowding, police operations, raids, hostage events, and public-safety
responses are one high-value evidence pathway, not the only acceptable pathway.

Facility-specific occupancy and occurrence hints:
{occurrence_guidance}

High-value evidence phrases:
{_bullet_list(positive_patterns)}

Avoid treating these alone as occupancy observations:
{_bullet_list(negative_patterns)}

If the source breaks down subgroups, capture each subgroup separately. If only a total is given,
use a generic group type such as residents, occupants, tenants, families, patrons, employees,
workers, guests, shoppers, or people based on context. Evacuated residents, trapped occupants,
displaced families, rescued guests, and similar incident-tied groups are acceptable occupancy
proxies.

Use conventional occurrence hints to search and interpret likely subgroups, but do not treat
capacity, enrollment, bed counts, workforce size, annual visitors, or other contextual counts as
direct observed occupancy unless the source explicitly ties the count to people present during a
bounded date, time, incident, event, shift, inspection, or measured period.

## Orchestrator Strategy Plan

The job-building orchestrator recommends these ordered evidence strategies. Start with the
Strategy Scout or deterministic plan, sample several strategies when practical, then lean into the
productive pathway while preserving strategy attribution on each lead. These recommendations are
facility-aware: temporary-use occupancy is included only when the selected scope contains
intermittently occupied arenas, halls, theaters, event venues, or shelters.

## Strategy Scout Guidance

{scout_guidance}

{strategy_guidance}

Suggested strategy-aware searches:

{strategy_queries}

{vernacular_guidance}

{activity_guidance}

Do not discard a lead because minor metadata is missing. Use "Unknown" or "Not provided" for
missing metadata, and add a short `review_notes` value when the lead needs human review.

## Country And Source Context

Administrative terms to look for:
{_bullet_list(country_context["admin_terms"])}

Useful locality examples:
{_bullet_list(country_context["locality_examples"])}

Source/search terms that may help:
{_bullet_list(country_context["source_terms"])}

Preferred sources:
{_bullet_list(preferred_sources)}

Context-only sources:
{_bullet_list(context_only_sources)}

Context-only sources can provide leads, but the final lead should point to the strongest available
source URL.

## Output Format

Return strictly a single valid JSON array. Do not wrap the JSON in markdown or prose. Use this
exact schema. Use raw URLs, not Markdown links, in `source_url`.

Set `source_type` to one of: news, official, wire, encyclopedia, social, directory, unknown.
Set `confidence` to one of: high, medium, low, unknown.
Set `strategy_id` to the strategy that produced the lead. Set `count_semantics` to a concise value
such as `confirmed_inside`, `evacuated`, `counted_inside`, `attended`, `on_shift`, or
`sensor_measured`. Set `representativeness` to a concise value such as `incident_specific`,
`event_specific`, `routine_period`, `operational_period`, `temporary_use`, or `unknown`.
Set `is_facility_level` to true only when the count is tied to a specific facility or named
residential place. Set `is_regional_aggregate` to true for broad city/province/region/country
disaster totals. Add short machine-readable `review_flags` such as "missing_quote",
"context_only_source", "regional_aggregate", "needs_source_upgrade", or "duplicate_incident".

[
  {{
    "is_valid_occupancy_report": true,
    "source_url": "String or 'Not provided'",
    "source_title": "String or ''",
    "source_type": "news | official | wire | encyclopedia | social | directory | unknown",
    "evidence_quote": "Exact source quote containing the count, or null",
    "incident_date": "YYYY-MM-DD or 'Unknown'",
    "incident_time": "HH:MM AM/PM or 'Unknown'",
    "occupancy_data": [
      {{
        "count": 0,
        "group_type": "String"
      }}
    ],
    "location": {{
      "facility_name": "String",
      "specific_address_or_landmark": "String or 'Unknown'",
      "city_or_region": "String",
      "country": "{country}"
    }},
    "confidence": "high | medium | low | unknown",
    "is_facility_level": true,
    "is_regional_aggregate": false,
    "review_flags": ["String"],
    "review_notes": "String or null",
    "strategy_id": "One orchestrator-recommended strategy ID",
    "count_semantics": "String or null",
    "representativeness": "String or null"
  }}
]
"""

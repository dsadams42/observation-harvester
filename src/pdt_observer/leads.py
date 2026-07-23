from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from pydantic import TypeAdapter

from pdt_observer.models import (
    BuildingProfileSet,
    CandidateObservation,
    Evidence,
    InvestigationResult,
    InvestigationRun,
    InvestigationTask,
    ObservationType,
    OccupancyLead,
    ResultStatus,
    SourceBundle,
    SourceDocument,
)
from pdt_observer.profiles import get_profile_set, narrow_profile_set
from pdt_observer.prompting import country_search_context
from pdt_observer.workflow import slugify

LEAD_LIST_ADAPTER: TypeAdapter[tuple[OccupancyLead, ...]] = TypeAdapter(
    tuple[OccupancyLead, ...]
)


def load_leads(path: Path) -> tuple[OccupancyLead, ...]:
    return LEAD_LIST_ADAPTER.validate_json(path.read_text(encoding="utf-8"))


def leads_to_json(leads: tuple[OccupancyLead, ...]) -> str:
    payload = [lead.model_dump(mode="json") for lead in leads]
    return json.dumps(payload, indent=2)


def summarize_leads(leads: tuple[OccupancyLead, ...]) -> dict[str, object]:
    valid = [lead for lead in leads if lead.is_valid_occupancy_report]
    counts = sum(len(lead.occupancy_data) for lead in valid)
    countries = sorted({lead.location.country for lead in valid})
    cities = sorted({lead.location.city_or_region for lead in valid})
    facility_level_count = sum(1 for lead in valid if lead.is_facility_level is True)
    aggregate_count = sum(1 for lead in valid if lead.is_regional_aggregate is True)
    return {
        "lead_count": len(leads),
        "valid_occupancy_reports": len(valid),
        "occupancy_count_rows": counts,
        "countries": countries,
        "cities_or_regions": cities,
        "facility_level_count": facility_level_count,
        "regional_aggregate_count": aggregate_count,
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
        candidate=CandidateObservation(result=result, produced_by="lead-promotion"),
    )


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
        "Do not extract records outside this profile set unless the source ties the count to a "
        "matching named facility."
    )


def render_lead_harvest_prompt(
    *,
    country: str,
    profile_set_name: str,
    target: int,
    locality: str | None = None,
    profile_id: str | None = None,
) -> str:
    profile_set = get_profile_set(profile_set_name)
    if profile_id is not None:
        profile_set = narrow_profile_set(profile_set, profile_id)
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

    return f"""# Broad Occupancy Lead Harvest

You are a specialized geospatial data extraction engine. Your objective is to search online news
and public incident sources, inspect unstructured article text, and extract real-time occupancy
lead records for facilities matching the selected profile set.

Target: {target} lead records.
Country: {country_name} (`{country}`).
Scope: {locality_scope}
Profile set: {profile_set.label} (`{profile_set.profile_set_id}`).

## Inclusion Filter

Only extract records for facilities matching this profile set:
{_bullet_list(facility_labels)}

Facility aliases and examples:
{_bullet_list(aliases)}

{scope_guidance}

## Occupancy Extraction

Identify any specific, historical headcount of people physically present inside, trapped in,
rescued from, or evacuated from a matching facility during an incident such as fire, earthquake,
chemical leak, code violation, overcrowding, police operation, raid, hostage event, or public
safety response.

High-value evidence phrases:
{_bullet_list(positive_patterns)}

Avoid treating these alone as occupancy observations:
{_bullet_list(negative_patterns)}

If the source breaks down subgroups, capture each subgroup separately. If only a total is given,
use a generic group type such as residents, occupants, tenants, families, patrons, employees,
workers, guests, shoppers, or people based on context. Evacuated residents, trapped occupants,
displaced families, rescued guests, and similar incident-tied groups are acceptable occupancy
proxies.

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
    "review_notes": "String or null"
  }}
]
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from pdt_observer.models import BuildingProfileSet, OccupancyLead
from pdt_observer.profiles import get_profile_set
from pdt_observer.prompting import country_search_context

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
    return {
        "lead_count": len(leads),
        "valid_occupancy_reports": len(valid),
        "occupancy_count_rows": counts,
        "countries": countries,
        "cities_or_regions": cities,
    }


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


def render_lead_harvest_prompt(
    *,
    country: str,
    profile_set_name: str,
    target: int,
    locality: str | None = None,
) -> str:
    profile_set = get_profile_set(profile_set_name)
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

    return f"""# Broad Occupancy Lead Harvest

You are a specialized geospatial data extraction engine. Your objective is to search online news
and public incident sources, inspect unstructured article text, and extract real-time occupancy
lead records for commercial, retail, hospitality, workplace, and business facilities.

Target: {target} lead records.
Country: {country_name} (`{country}`).
Scope: {locality_scope}
Profile set: {profile_set.label} (`{profile_set.profile_set_id}`).

## Inclusion Filter

Only extract records for facilities matching this profile set:
{_bullet_list(facility_labels)}

Facility aliases and examples:
{_bullet_list(aliases)}

Do not extract residential buildings or outdoor public open spaces unless the source ties the
count to a named commercial/business facility.

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
use a generic group type such as patrons, employees, workers, guests, shoppers, occupants, or
people based on context. Evacuated employees, trapped workers, rescued guests, and similar
incident-tied groups are acceptable occupancy proxies.

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

[
  {{
    "is_valid_occupancy_report": true,
    "source_url": "String or 'Not provided'",
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
    "review_notes": "String or null"
  }}
]
"""

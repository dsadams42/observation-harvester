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
    ComponentBundleQaqcReview,
    ComponentBundleStatus,
    ComponentQaqcReview,
    CountMethod,
    Evidence,
    GeographerPlan,
    HarvestEvidenceSet,
    HarvestQaqcReviewSet,
    InvestigationResult,
    InvestigationRun,
    InvestigationTask,
    LeadQaqcReview,
    ObservationType,
    OccupancyLead,
    PopulationComponentLead,
    ResultStatus,
    SourceBundle,
    SourceDocument,
    StrategyPlan,
    StrategyScoutPlan,
)
from pdt_observer.profiles import resolve_profile_set
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
COMPONENT_LEAD_LIST_ADAPTER: TypeAdapter[tuple[PopulationComponentLead, ...]] = TypeAdapter(
    tuple[PopulationComponentLead, ...]
)
EVIDENCE_SET_ADAPTER: TypeAdapter[HarvestEvidenceSet] = TypeAdapter(HarvestEvidenceSet)
QAQC_REVIEW_LIST_ADAPTER: TypeAdapter[tuple[LeadQaqcReview, ...]] = TypeAdapter(
    tuple[LeadQaqcReview, ...]
)
COMPONENT_QAQC_REVIEW_LIST_ADAPTER: TypeAdapter[tuple[ComponentQaqcReview, ...]] = TypeAdapter(
    tuple[ComponentQaqcReview, ...]
)
COMPONENT_BUNDLE_QAQC_REVIEW_LIST_ADAPTER: TypeAdapter[
    tuple[ComponentBundleQaqcReview, ...]
] = TypeAdapter(tuple[ComponentBundleQaqcReview, ...])
QAQC_REVIEW_SET_ADAPTER: TypeAdapter[HarvestQaqcReviewSet] = TypeAdapter(HarvestQaqcReviewSet)


def load_evidence_set(path: Path) -> HarvestEvidenceSet:
    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)
    if isinstance(payload, list):
        return HarvestEvidenceSet(
            occupancy_leads=LEAD_LIST_ADAPTER.validate_python(payload),
            component_leads=(),
        )
    return EVIDENCE_SET_ADAPTER.validate_python(payload)


def load_leads(path: Path) -> tuple[OccupancyLead, ...]:
    return load_evidence_set(path).occupancy_leads


def load_component_leads(path: Path) -> tuple[PopulationComponentLead, ...]:
    return load_evidence_set(path).component_leads


def load_qaqc_review_set(path: Path) -> HarvestQaqcReviewSet:
    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)
    if isinstance(payload, list):
        if payload and "verification_status" not in payload[0]:
            raise ValueError(
                "QAQC review file looks like a harvest lead file; expected review objects"
            )
        return HarvestQaqcReviewSet(
            occupancy_reviews=QAQC_REVIEW_LIST_ADAPTER.validate_python(payload),
            component_reviews=(),
            component_bundle_reviews=(),
        )
    return QAQC_REVIEW_SET_ADAPTER.validate_python(payload)


def load_qaqc_reviews(path: Path) -> tuple[LeadQaqcReview, ...]:
    return load_qaqc_review_set(path).occupancy_reviews


def load_component_qaqc_reviews(path: Path) -> tuple[ComponentQaqcReview, ...]:
    return load_qaqc_review_set(path).component_reviews


def load_component_bundle_qaqc_reviews(path: Path) -> tuple[ComponentBundleQaqcReview, ...]:
    return load_qaqc_review_set(path).component_bundle_reviews


def leads_to_json(leads: tuple[OccupancyLead, ...]) -> str:
    payload = [lead.model_dump(mode="json") for lead in leads]
    return json.dumps(payload, indent=2)


def evidence_set_to_json(evidence_set: HarvestEvidenceSet) -> str:
    return json.dumps(evidence_set.model_dump(mode="json"), indent=2)


def qaqc_reviews_to_json(reviews: tuple[LeadQaqcReview, ...]) -> str:
    payload = [review.model_dump(mode="json") for review in reviews]
    return json.dumps(payload, indent=2)


def qaqc_review_set_to_json(review_set: HarvestQaqcReviewSet) -> str:
    return json.dumps(review_set.model_dump(mode="json"), indent=2)


def summarize_evidence_set(evidence_set: HarvestEvidenceSet) -> dict[str, object]:
    leads = evidence_set.occupancy_leads
    valid = [lead for lead in leads if lead.is_valid_occupancy_report]
    counts = sum(len(lead.occupancy_data) for lead in valid)
    component_leads = evidence_set.component_leads
    valid_components = [lead for lead in component_leads if lead.is_valid_component_report]
    component_values = sum(len(lead.component_data) for lead in valid_components)
    component_bundles = evidence_set.component_bundles
    countable_component_bundles = [
        bundle
        for bundle in component_bundles
        if bundle.counts_toward_target
        and bundle.completion_status
        in {ComponentBundleStatus.COMPLETE, ComponentBundleStatus.MOSTLY_COMPLETE}
    ]
    countries = sorted({lead.location.country for lead in valid})
    cities = sorted({lead.location.city_or_region for lead in valid})
    component_countries = sorted({lead.country for lead in valid_components})
    component_geographies = sorted({lead.geography_name for lead in valid_components})
    facility_level_count = sum(1 for lead in valid if lead.is_facility_level is True)
    aggregate_count = sum(1 for lead in valid if lead.is_regional_aggregate is True)
    counts_by_strategy: dict[str, int] = {}
    for lead in valid:
        strategy_id = lead.strategy_id.value if lead.strategy_id is not None else "unattributed"
        counts_by_strategy[strategy_id] = counts_by_strategy.get(strategy_id, 0) + 1
    component_counts_by_strategy: dict[str, int] = {}
    component_counts_by_type: dict[str, int] = {}
    component_counts_by_geography_level: dict[str, int] = {}
    component_bundles_by_status: dict[str, int] = {}
    for bundle in component_bundles:
        status = bundle.completion_status.value
        component_bundles_by_status[status] = component_bundles_by_status.get(status, 0) + 1
    for component_lead in valid_components:
        strategy_id = (
            component_lead.strategy_id.value
            if component_lead.strategy_id is not None
            else "unattributed"
        )
        component_counts_by_strategy[strategy_id] = component_counts_by_strategy.get(
            strategy_id, 0
        ) + 1
        for datum in component_lead.component_data:
            component_counts_by_type[datum.component_type] = (
                component_counts_by_type.get(datum.component_type, 0) + 1
            )
            level = datum.geography_level.value
            component_counts_by_geography_level[level] = (
                component_counts_by_geography_level.get(level, 0) + 1
            )
    return {
        "lead_count": len(leads),
        "valid_occupancy_reports": len(valid),
        "occupancy_count_rows": counts,
        "component_lead_count": len(component_leads),
        "valid_component_reports": len(valid_components),
        "component_value_rows": component_values,
        "component_bundle_count": len(component_bundles),
        "countable_component_observations": len(countable_component_bundles),
        "budget_observation_count": len(valid) + len(countable_component_bundles),
        "countries": countries,
        "cities_or_regions": cities,
        "component_countries": component_countries,
        "component_geographies": component_geographies,
        "facility_level_count": facility_level_count,
        "regional_aggregate_count": aggregate_count,
        "counts_by_role": {
            "direct_occupancy": len(valid),
            "component_input": len(valid_components),
        },
        "counts_by_strategy": counts_by_strategy,
        "component_counts_by_strategy": component_counts_by_strategy,
        "component_counts_by_type": component_counts_by_type,
        "component_counts_by_geography_level": component_counts_by_geography_level,
        "component_bundles_by_status": component_bundles_by_status,
    }


def summarize_leads(leads: tuple[OccupancyLead, ...]) -> dict[str, object]:
    return summarize_evidence_set(HarvestEvidenceSet(occupancy_leads=leads))


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


def evidence_set_to_jsonl(evidence_set: HarvestEvidenceSet) -> str:
    payloads: list[dict[str, object]] = []
    payloads.extend(lead.model_dump(mode="json") for lead in evidence_set.occupancy_leads)
    payloads.extend(lead.model_dump(mode="json") for lead in evidence_set.component_leads)
    for bundle in evidence_set.component_bundles:
        payload = bundle.model_dump(mode="json")
        payload["record_type"] = "component_bundle"
        payloads.append(payload)
    lines = [json.dumps(payload, sort_keys=True) for payload in payloads]
    return "\n".join(lines) + ("\n" if lines else "")


def component_leads_to_csv(component_leads: tuple[PopulationComponentLead, ...]) -> str:
    fieldnames = (
        "lead_index",
        "source_url",
        "source_title",
        "source_type",
        "evidence_quote",
        "component_type",
        "value",
        "unit",
        "time_basis",
        "geography_level",
        "period_label",
        "facility_name",
        "specific_address_or_landmark",
        "geography_name",
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
    for lead_index, lead in enumerate(component_leads):
        for datum in lead.component_data:
            location = lead.location
            writer.writerow(
                {
                    "lead_index": lead_index,
                    "source_url": lead.source_url,
                    "source_title": lead.source_title,
                    "source_type": lead.source_type.value,
                    "evidence_quote": lead.evidence_quote,
                    "component_type": datum.component_type,
                    "value": datum.value,
                    "unit": datum.unit,
                    "time_basis": datum.time_basis.value,
                    "geography_level": datum.geography_level.value,
                    "period_label": datum.period_label or "",
                    "facility_name": location.facility_name if location is not None else "",
                    "specific_address_or_landmark": (
                        location.specific_address_or_landmark if location is not None else ""
                    ),
                    "geography_name": lead.geography_name,
                    "country": lead.country,
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


def export_evidence_set(evidence_set: HarvestEvidenceSet, *, output_format: str) -> str:
    if output_format == "jsonl":
        return evidence_set_to_jsonl(evidence_set)
    if output_format == "csv":
        occupancy_csv = leads_to_csv(evidence_set.occupancy_leads)
        component_csv = component_leads_to_csv(evidence_set.component_leads)
        if not evidence_set.component_leads:
            return occupancy_csv
        return occupancy_csv + "\n# Component evidence\n" + component_csv
    raise ValueError("evidence export format must be csv or jsonl")


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
    evidence: HarvestEvidenceSet | tuple[OccupancyLead, ...],
    *,
    source_label: str = "lead JSON",
    expected_country: str | None = None,
    expected_locality: str | None = None,
) -> str:
    evidence_set = (
        evidence
        if isinstance(evidence, HarvestEvidenceSet)
        else HarvestEvidenceSet(occupancy_leads=evidence)
    )
    lead_payload = json.dumps(evidence_set.model_dump(mode="json"), indent=2)
    scope_text = (
        f"\nRequested geographic scope: country `{expected_country or 'unspecified'}`; "
        f"locality `{expected_locality or 'unspecified'}`.\n"
    )
    return f"""# Occupancy Lead QAQC Verification / Harvest Evidence QAQC Verification

You are a careful QAQC verification agent for harvested OASIS evidence. Your job is to inspect
the source URL for each lead, verify whether the source supports the reported values under the
lead's intended evidence role, and return review JSON only.

Input source: {source_label}
{scope_text}

## Verification Tasks

For each direct occupancy lead in `occupancy_leads`:
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

For each component lead in `component_leads`:
- Verify each `component_data[]` value against the source quote and page text.
- Confirm that the value is valid as a component input, not as a direct occupancy observation.
- Verify `component_type`, `value`, `unit`, `time_basis`, and `geography_level` when the source
  provides enough evidence.
- Facility-level component evidence should match the named facility. Locality, region, and
  country-level component evidence may be valid when its `geography_level` and `geography_name`
  accurately describe the source scope.
- Do not reject a component input merely because it is not a direct people-present count.
- Do reject or review a component lead when it silently converts a component input into a final
  occupancy estimate.

For each facility bundle in `component_bundles`:
- Verify that `source_lead_indexes` point to component leads for the same facility or stated
  geography.
- Check whether `found_component_types` and `missing_component_types` honestly summarize the
  source-backed component data and configured targets.
- `counts_toward_target` is acceptable only when `completion_status` is `complete` or
  `mostly_complete`.
- Partial and seed-only bundles may remain in the artifact as useful notes, but they should not
  be counted as completed/model-ready observations.
- A partial bundle can still be an addressable supervisor-review candidate when it has a specific
  facility identity plus at least one source-backed population-bearing component such as students,
  staff, employees, beds, rooms, residents, visitors, or annual/daily attendance.
- Treat a facility-level component bundle as the observation unit. The individual component leads
  are supporting evidence fields attached to that facility observation.
- For facility-level bundles, verify that the bundle identity is specific enough to support one
  facility address later. For locality, region, or country bundles, do not pretend a specific
  store/building/campus address is available.
- Return one `component_bundle_reviews[]` item per component bundle. Do not put bundle-level
  decisions into `component_reviews[]`.

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

Return strictly a single valid JSON object. Do not wrap the JSON in markdown or prose. Use this
exact schema:

{{
  "schema_version": 1,
  "occupancy_reviews": [
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
  ],
  "component_reviews": [
    {{
      "lead_index": 0,
      "source_url": "String",
      "verification_status": "verified",
      "source_reachable": true,
      "evidence_role_match": true,
      "component_type_match": true,
      "geography_level_match": true,
      "location_match": true,
      "strategy_match": true,
      "component_checks": [
        {{
          "component_type": "String",
          "value": 0,
          "unit": "String",
          "reported_value_found": true,
          "quote_found": true,
          "component_type_match": true,
          "time_basis_match": true,
          "geography_level_match": true,
          "supporting_quote": "Exact quote or null",
          "notes": "String or null"
        }}
      ],
      "supporting_quote": "Best exact quote supporting the component values, or null",
      "recommended_action": "keep",
      "review_notes": "Short explanation of the verification decision"
    }}
  ],
  "component_bundle_reviews": [
    {{
      "bundle_index": 0,
      "item_id": "child-run-id-component-bundle-0",
      "geography_name": "String",
      "verification_status": "verified",
      "source_lead_indexes_valid": true,
      "same_facility_or_geography": true,
      "component_fields_match": true,
      "completion_status_match": true,
      "counts_toward_target_approved": true,
      "found_component_types": ["String"],
      "missing_component_types": ["String"],
      "source_lead_indexes": [0],
      "supporting_quote": "Short bundle-level quote or null",
      "recommended_action": "keep",
      "review_notes": "Short explanation of the bundle-level verification decision"
    }}
  ]
}}

## Evidence To Verify

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
        details.append(f"Count method: {profile.count_method.value}")
        if profile.component_count_fields:
            details.append(f"Component inputs: {', '.join(profile.component_count_fields)}")
        if profile.regional_stat_fields:
            details.append(f"Regional/country inputs: {', '.join(profile.regional_stat_fields)}")
        if profile.component_source_guidance:
            details.append(f"Component guidance: {profile.component_source_guidance}")
        if profile.contextual_count_fields:
            label = (
                "Context-only counts"
                if profile.count_method == CountMethod.DIRECT_COUNT
                else "Legacy context fields"
            )
            details.append(
                label + ": " + ", ".join(profile.contextual_count_fields)
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
    count_method_override: CountMethod | None = None,
    geographer_plan: GeographerPlan | None = None,
    strategy_plan: StrategyPlan | None = None,
    strategy_scout_plan: StrategyScoutPlan | None = None,
    run_id: str | None = None,
    activity_path: Path | None = None,
    curation_guidance: str = "",
) -> str:
    profile_set = resolve_profile_set(
        profile_set_name,
        profile_id=profile_id,
        count_method_override=count_method_override,
    )
    strategy_plan = strategy_plan or build_strategy_plan(profile_set)
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
    methods = {profile.count_method for profile in profile_set.profiles if profile.enabled}
    component_only = methods == {CountMethod.POPULATION_SUBCOMPONENT}
    hybrid = CountMethod.HYBRID in methods or (
        CountMethod.POPULATION_SUBCOMPONENT in methods and CountMethod.DIRECT_COUNT in methods
    )
    component_targets = tuple(
        dict.fromkeys(
            _unique_profile_values(profile_set, "component_count_fields")
            + _unique_profile_values(profile_set, "regional_stat_fields")
        )
    )
    target_rule = (
        f"Target: {target} completed or mostly complete facility component observation bundles. "
        "Raw component source hits and same-facility deepening searches do not count toward this "
        "target until they are consolidated into a `component_bundles[]` item with "
        "`counts_toward_target: true`."
        if component_only
        else (
            f"Target: {target} completed observations. Direct occupancy leads count as "
            "observations; component source hits count only after consolidation into complete or "
            "mostly complete facility bundles."
            if hybrid
            else f"Target: {target} lead records."
        )
    )
    harvest_objective = (
        "extract source-backed population component inputs for the selected facility scope. "
        "Do not calculate final occupancy estimates in this phase."
        if component_only
        else (
            "extract both source-backed direct occupancy observations and population component "
            "inputs for the selected facility scope. Keep the evidence roles separate and do "
            "not calculate final occupancy estimates."
            if hybrid
            else (
                "extract source-backed facility-level lead records for counts of people "
                "physically present during a bounded time, incident, event, shift, inspection, "
                "transfer, shelter activation, operating state, or measured period."
            )
        )
    )
    context_rule = (
        "For component-input profiles, capacity, enrollment, bed counts, workforce size, rooms, "
        "annual visitors, schedules, rates, and regional statistics may be valid component "
        "evidence when they match configured component fields. Preserve them as component inputs, "
        "not direct occupancy observations or derived totals."
        if component_only
        else (
            "For hybrid profiles, collect direct people-present observations and component inputs "
            "in their separate arrays. A component value can be valid evidence without being a "
            "direct occupancy observation."
            if hybrid
            else (
                "For direct-count profiles, do not treat capacity, enrollment, bed counts, "
                "workforce size, annual visitors, or other contextual counts as direct observed "
                "occupancy unless the source explicitly ties the count to people present during "
                "a bounded date, time, incident, event, shift, inspection, or measured period."
            )
        )
    )
    component_deepening_guidance = (
        f"""
## Component Facility Deepening Loop

For component-input work, do not treat the first source hit for a facility as a completed
observation unless it already covers most configured component targets.

Configured component targets:
{_bullet_list(component_targets)}

When you find a facility-level component hit:
- Treat the facility as a seed and normalize its name, locality, country, and address if available.
- Compare found component types with the configured component targets.
- Run additional same-facility searches for missing fields using the exact facility name plus terms
  such as annual report, statistics, staff, FTE, hours, days open, attendance, visitors, rooms,
  beds, enrollment, occupancy rate, schedule, or the missing component field names.
- These follow-up searches are part of deepening the same candidate and do not count against the
  target budget by themselves.
- Add each source-backed value as a `component_leads[]` item, preserving its own URL and quote.
- Add one `component_bundles[]` item per seeded facility or geography, listing source lead indexes,
  found fields, missing fields, and follow-up searches attempted.
- Set `completion_status` to `complete` only when all or nearly all target fields are found.
- Set `completion_status` to `mostly_complete` when the bundle contains enough high-value fields
  to be useful despite one or more missing low-priority fields.
- Set `completion_status` to `partial` or `seed_only` for incomplete seeds. These must have
  `counts_toward_target: false`.
- Preserve partial bundles with a specific facility identity and at least one source-backed
  population-bearing component as supervisor-review candidates. Do not convert them into final
  occupancy estimates and do not mark them countable/model-ready until QAQC approves that status.
- Only `complete` and `mostly_complete` bundles may use `counts_toward_target: true`.

Do not calculate final occupancy estimates from the bundle. The bundle is a source-backed input
package, not a derived population total.
"""
        if component_only or hybrid
        else ""
    )

    return f"""# Broad Occupancy Lead Harvest

You are a specialized geospatial data extraction engine. Your objective is to search online public
sources, inspect unstructured source text, and {harvest_objective}

{target_rule}
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

Use conventional occurrence hints to search and interpret likely subgroups and component variables.
{context_rule}

Counts must be judged against their intended evidence role. A value can be invalid as
`direct_occupancy` but valid as `component_input`. Never convert component inputs into final
occupancy estimates in this workflow.

{component_deepening_guidance}

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

{curation_guidance}

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

Return strictly a single valid `HarvestEvidenceSet` JSON object. Do not wrap the JSON in markdown
or prose. Use this exact schema. Use raw URLs, not Markdown links, in `source_url`.

Set `source_type` to one of: news, official, wire, encyclopedia, social, directory, unknown.
Set `confidence` to one of: high, medium, low, unknown.
Set component `time_basis` to one of: instant, shift, daily, event, annual, school_year,
census_year, operating_period, current_static, unknown. Use `current_static` for facility facts
such as seating capacity, rooms, beds, or named static capacities when no date period is supplied.
Use `event` for an event-specific component input such as event capacity or staff for a sold-out
event.
Set `strategy_id` to the strategy that produced the lead. Set `count_semantics` to a concise value
such as `confirmed_inside`, `evacuated`, `counted_inside`, `attended`, `on_shift`, or
`sensor_measured`. Set `representativeness` to a concise value such as `incident_specific`,
`event_specific`, `routine_period`, `operational_period`, `temporary_use`, or `unknown`.
Set `is_facility_level` to true only when the count is tied to a specific facility or named
residential place. Set `is_regional_aggregate` to true for broad city/province/region/country
disaster totals. Add short machine-readable `review_flags` such as "missing_quote",
"context_only_source", "regional_aggregate", "needs_source_upgrade", or "duplicate_incident".

{{
  "schema_version": 1,
  "occupancy_leads": [
    {{
      "evidence_role": "direct_occupancy",
      "is_valid_occupancy_report": true,
      "source_url": "String or 'Not provided'",
      "source_title": "String or ''",
      "source_type": "news | official | wire | encyclopedia | social | directory | unknown",
      "evidence_quote": "Exact source quote containing the count, or null",
      "incident_date": "YYYY-MM-DD or 'Unknown'",
      "incident_time": "HH:MM AM/PM or 'Unknown'",
      "occupancy_data": [
        {{"count": 0, "group_type": "String"}}
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
  ],
  "component_leads": [
    {{
      "evidence_role": "component_input",
      "is_valid_component_report": true,
      "source_url": "String or 'Not provided'",
      "source_title": "String or ''",
      "source_type": "news | official | wire | encyclopedia | social | directory | unknown",
      "evidence_quote": "Exact source quote containing the component value",
      "component_data": [
        {{
          "component_type": "String such as students, staff, beds, annual visitors",
          "value": 0,
          "unit": "String such as people, beds, rooms, percent, visits/year",
          "time_basis": "Allowed TimeBasis value",
          "geography_level": "facility | locality | region | country",
          "period_label": "String or null"
        }}
      ],
      "location": {{
        "facility_name": "String",
        "specific_address_or_landmark": "String or 'Unknown'",
        "city_or_region": "String",
        "country": "{country}"
      }},
      "geography_name": "Facility, locality, region, or country name",
      "country": "{country}",
      "confidence": "high | medium | low | unknown",
      "is_facility_level": true,
      "is_regional_aggregate": false,
      "review_flags": ["String"],
      "review_notes": "String or null",
      "strategy_id": "One orchestrator-recommended strategy ID",
      "count_semantics": "component_input",
      "representativeness": "component_input"
    }}
  ],
  "component_bundles": [
    {{
      "evidence_role": "component_input",
      "geography_name": "Facility, locality, region, or country name",
      "country": "{country}",
      "location": {{
        "facility_name": "String",
        "specific_address_or_landmark": "String or 'Unknown'",
        "city_or_region": "String",
        "country": "{country}"
      }},
      "target_component_fields": ["Configured component field names searched for this bundle"],
      "found_component_types": ["Component types found across referenced source leads"],
      "missing_component_types": ["Configured component fields not found after follow-up search"],
      "source_lead_indexes": [0],
      "follow_up_searches_attempted": [
        "Exact same-facility query used to deepen a seed, such as facility name plus missing field"
      ],
      "completion_status": "complete | mostly_complete | partial | seed_only",
      "counts_toward_target": true,
      "confidence": "high | medium | low | unknown",
      "completion_notes": "Bundle completion rationale"
    }}
  ]
}}
"""

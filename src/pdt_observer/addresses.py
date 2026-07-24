from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from pdt_observer.geometry import approved_records_for_child, item_id_for_lead
from pdt_observer.leads import load_leads, load_qaqc_reviews
from pdt_observer.models import (
    AddressEnrichmentResult,
    HarvestRunManifest,
    LeadQaqcRecommendedAction,
    LeadQaqcVerificationStatus,
)

ADDRESS_RESULT_LIST_ADAPTER: TypeAdapter[tuple[AddressEnrichmentResult, ...]] = TypeAdapter(
    tuple[AddressEnrichmentResult, ...]
)


def address_output_path(root: Path, child_run_id: str) -> Path:
    return root / "address_runs" / f"{child_run_id}-address.json"


def address_prompt_path(root: Path, child_run_id: str) -> Path:
    return root / "work" / f"{child_run_id}-address.md"


def load_address_results(path: Path) -> tuple[AddressEnrichmentResult, ...]:
    return ADDRESS_RESULT_LIST_ADAPTER.validate_json(path.read_text(encoding="utf-8"))


def address_results_to_json(results: tuple[AddressEnrichmentResult, ...]) -> str:
    return json.dumps([result.model_dump(mode="json") for result in results], indent=2)


def approved_address_inputs(
    *,
    root: Path,
    manifest: HarvestRunManifest,
) -> tuple[dict[str, Any], ...]:
    return approved_address_inputs_from_files(
        lead_path=Path(manifest.lead_path),
        qaqc_path=root / "qaqc_runs" / f"{manifest.run_id}-qaqc.json",
        child_run_id=manifest.run_id,
    )


def approved_address_inputs_from_files(
    *,
    lead_path: Path,
    qaqc_path: Path,
    child_run_id: str | None = None,
) -> tuple[dict[str, Any], ...]:
    leads = load_leads(lead_path)
    reviews = load_qaqc_reviews(qaqc_path)
    run_id = child_run_id or lead_path.stem
    inputs: list[dict[str, Any]] = []
    for review in reviews:
        if review.lead_index >= len(leads):
            continue
        if review.verification_status != LeadQaqcVerificationStatus.VERIFIED:
            continue
        if review.recommended_action != LeadQaqcRecommendedAction.KEEP:
            continue
        lead = leads[review.lead_index]
        inputs.append(
            {
                "lead_index": review.lead_index,
                "item_id": item_id_for_lead(run_id, review.lead_index),
                "source_url": lead.source_url,
                "source_title": lead.source_title,
                "facility_name": lead.location.facility_name,
                "reported_address_or_landmark": lead.location.specific_address_or_landmark,
                "city_or_region": lead.location.city_or_region,
                "country": lead.location.country,
                "qaqc_supporting_quote": review.supporting_quote,
                "qaqc_review_notes": review.review_notes,
            }
        )
    return tuple(inputs)


def render_address_enrichment_prompt(
    records: tuple[dict[str, Any], ...],
    *,
    source_label: str = "QAQC-approved lead JSON",
) -> str:
    payload = json.dumps(list(records), indent=2)
    return f"""# Facility Address Enrichment

You are a careful facility-address enrichment agent. Your job is to find a reliable street,
campus, or site address for each QAQC-approved occupancy lead, then return address review JSON
only.

Input source: {source_label}

## Enrichment Tasks

For each input record:
- Start with the original `source_url` when it is usable.
- If the source does not provide a precise address, search official facility pages, government
  directories, school/company pages, reputable business directories, or trusted news context.
- Confirm the address belongs to the same facility, city/region, and country as the lead.
- Prefer a specific street/campus/site address over a broad city, district, or province.
- Capture a short exact supporting quote or address snippet when found.
- Do not invent an address. If multiple plausible addresses exist, mark the result `ambiguous`.

## Status Rules

Set `status` to one of:
- `found`: a specific address was found and matches the facility/location.
- `ambiguous`: multiple plausible addresses or unresolved facility-name ambiguity.
- `not_found`: no reliable address could be found.
- `needs_review`: partial address or weak source that needs human review.

Set `confidence` to one of: `high`, `medium`, `low`, `unknown`.

## Output Format

Return strictly a single valid JSON array. Do not wrap the JSON in markdown or prose. Use this
exact schema:

[
  {{
    "lead_index": 0,
    "item_id": "child-run-id-0",
    "facility_name": "String",
    "formatted_address": "String or null",
    "address_line1": "String or null",
    "address_line2": "String or null",
    "city_or_region": "String or null",
    "state_or_province": "String or null",
    "postal_code": "String or null",
    "country": "String or null",
    "address_source_url": "String or null",
    "address_evidence_quote": "Exact quote or null",
    "confidence": "high",
    "status": "found",
    "review_notes": "String"
  }}
]

## Input Records

{payload}
"""


def address_results_payload(root: Path, child_run_ids: tuple[str, ...]) -> dict[str, Any]:
    child_results: list[dict[str, Any]] = []
    all_results: list[dict[str, Any]] = []
    for child_run_id in child_run_ids:
        output_path = address_output_path(root, child_run_id)
        if not output_path.is_file():
            continue
        results = load_address_results(output_path)
        result_payload = [result.model_dump(mode="json") for result in results]
        child_results.append(
            {
                "run_id": child_run_id,
                "address_path": str(output_path),
                "result_count": len(results),
                "results": result_payload,
            }
        )
        all_results.extend(result_payload)
    return {
        "result_count": len(all_results),
        "child_results": child_results,
        "results": all_results,
    }


def merge_address_results(
    root: Path,
    records: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    by_child: dict[str, dict[str, AddressEnrichmentResult]] = {}
    merged: list[dict[str, Any]] = []
    for record in records:
        child_run_id = str(record["child_run_id"])
        if child_run_id not in by_child:
            path = address_output_path(root, child_run_id)
            by_child[child_run_id] = (
                {result.item_id: result for result in load_address_results(path)}
                if path.is_file()
                else {}
            )
        address = by_child[child_run_id].get(str(record["item_id"]))
        payload = dict(record)
        payload["address_enrichment"] = (
            address.model_dump(mode="json") if address is not None else None
        )
        payload["address_status"] = address.status.value if address is not None else "not_run"
        if address is not None and address.formatted_address:
            payload["geocode_query"] = address.formatted_address
        merged.append(payload)
    return tuple(merged)


def approved_records_with_addresses(
    root: Path,
    manifest: HarvestRunManifest,
) -> tuple[dict[str, Any], ...]:
    return merge_address_results(root, approved_records_for_child(root, manifest))

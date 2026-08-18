from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from pdt_observer.geometry import spatially_validate_geocode_result


def _normalized_address_words(value: object) -> set[str]:
    return {
        word
        for word in re.findall(r"[^\W_]+", str(value or "").casefold(), flags=re.UNICODE)
        if len(word) >= 3 and not word.isdigit()
    }


def _record_facility_name(record: dict[str, Any]) -> str:
    lead = record.get("lead")
    location = lead.get("location") if isinstance(lead, dict) else None
    if isinstance(location, dict):
        return str(location.get("facility_name") or "")
    bundle = record.get("component_bundle")
    bundle_location = bundle.get("location") if isinstance(bundle, dict) else None
    if isinstance(bundle_location, dict):
        geography_name = bundle.get("geography_name") if isinstance(bundle, dict) else ""
        return str(bundle_location.get("facility_name") or geography_name or "")
    allocated = record.get("allocated_component_lead")
    allocated_location = (
        allocated.get("facility_location") if isinstance(allocated, dict) else None
    )
    if isinstance(allocated_location, dict):
        return str(allocated_location.get("facility_name") or "")
    return ""


def _record_expected_postal_code(record: dict[str, Any]) -> str:
    address = record.get("address_enrichment")
    return str(address.get("postal_code") or "") if isinstance(address, dict) else ""


def _record_address_status(record: dict[str, Any]) -> str:
    address = record.get("address_enrichment")
    if isinstance(address, dict):
        return str(address.get("status") or record.get("address_status") or "")
    return str(record.get("address_status") or "")


def _address_candidate_mismatch(
    candidate: dict[str, Any],
    record: dict[str, Any],
) -> str | None:
    enriched = record.get("address_enrichment")
    if not isinstance(enriched, dict):
        return None
    candidate_address = candidate.get("address")
    if not isinstance(candidate_address, dict):
        return None
    expected_postal = re.sub(r"\W", "", str(enriched.get("postal_code") or "").casefold())
    candidate_postal = re.sub(
        r"\W",
        "",
        str(candidate_address.get("postcode") or "").casefold(),
    )
    if expected_postal and candidate_postal and expected_postal != candidate_postal:
        return (
            f"Geocoder postal code {candidate_postal} does not match researched "
            f"postal code {expected_postal}."
        )
    expected_line = str(enriched.get("address_line1") or "")
    expected_number_match = re.match(r"\s*(\d+[A-Za-z]?)\b", expected_line)
    expected_number = expected_number_match.group(1).casefold() if expected_number_match else ""
    candidate_number = str(candidate_address.get("house_number") or "").casefold()
    if expected_number and candidate_number and expected_number != candidate_number:
        return (
            f"Geocoder house number {candidate_number} does not match researched "
            f"house number {expected_number}."
        )
    return None


def _address_candidate_warnings(
    candidate: dict[str, Any],
    record: dict[str, Any],
) -> tuple[str, ...]:
    enriched = record.get("address_enrichment")
    if not isinstance(enriched, dict):
        return ()
    candidate_address = candidate.get("address")
    if not isinstance(candidate_address, dict):
        return ()
    warnings: list[str] = []
    expected_line = str(enriched.get("address_line1") or "")
    expected_road_words = _normalized_address_words(
        re.sub(r"^\s*\d+[A-Za-z]?\s*", "", expected_line)
    )
    candidate_road_words = _normalized_address_words(
        candidate_address.get("road")
        or candidate_address.get("pedestrian")
        or candidate_address.get("industrial")
    )
    if (
        expected_road_words
        and candidate_road_words
        and not expected_road_words.intersection(candidate_road_words)
    ):
        warnings.append("Geocoder street name differs from the researched address.")
    candidate_name_words = _normalized_address_words(candidate.get("name"))
    facility_words = _normalized_address_words(_record_facility_name(record))
    if (
        candidate_name_words
        and facility_words
        and not candidate_name_words.intersection(facility_words)
    ):
        warnings.append("Named geocoder feature differs from the researched facility identity.")
    return tuple(warnings)


def _ranked_candidate_options(
    result: dict[str, Any] | None,
    *,
    record: dict[str, Any],
    expected_country: str,
    expected_locality: str | None,
    expected_country_aliases: dict[str, str],
    expected_locality_aliases: tuple[str, ...],
    query: str,
) -> list[dict[str, Any]]:
    if result is None:
        return []
    raw_candidates = result.get("candidates")
    candidates = (
        [candidate for candidate in raw_candidates if isinstance(candidate, dict)]
        if isinstance(raw_candidates, list)
        else [result]
    )
    options: list[dict[str, Any]] = []
    expected_facility_name = _record_facility_name(record)
    expected_postal_code = _record_expected_postal_code(record)
    facility_words = _normalized_address_words(expected_facility_name)
    for candidate in candidates:
        latitude = candidate.get("latitude")
        longitude = candidate.get("longitude")
        if latitude is None or longitude is None:
            continue
        _, validation = spatially_validate_geocode_result(
            {"candidates": [candidate]},
            expected_country=expected_country,
            expected_locality=expected_locality,
            expected_country_aliases=expected_country_aliases,
            expected_locality_aliases=expected_locality_aliases,
            expected_postal_code=expected_postal_code,
            expected_facility_name=expected_facility_name,
        )
        raw_assessments = validation.get("assessments")
        assessment: dict[str, object] = (
            raw_assessments[0]
            if isinstance(raw_assessments, list)
            and raw_assessments
            and isinstance(raw_assessments[0], dict)
            else {}
        )
        scope_status = str(assessment.get("status") or validation["status"])
        scope_reason = str(assessment.get("reason") or validation["reason"])
        mismatch = _address_candidate_mismatch(candidate, record)
        warnings = _address_candidate_warnings(candidate, record)
        candidate_words = _normalized_address_words(
            candidate.get("name") or candidate.get("display_name")
        )
        facility_match = bool(
            facility_words
            and candidate_words
            and facility_words.intersection(candidate_words)
        )
        score: float = {
            "accepted": 60,
            "requires_human": 35,
            "out_of_scope": -100,
        }.get(scope_status, 0)
        match_summary = [scope_reason]
        if mismatch is None:
            score += 25
            match_summary.append("No conflict with the researched address was detected.")
        else:
            score -= 30
            match_summary.append(mismatch)
        for warning in warnings:
            score -= 8
            match_summary.append(warning)
        if facility_match:
            score += 15
            match_summary.append("The candidate name overlaps the facility name.")
        raw_support_signals = assessment.get("support_signals")
        support_signals = tuple(
            str(signal) for signal in raw_support_signals
        ) if isinstance(raw_support_signals, list) else ()
        if support_signals:
            score += min(len(support_signals), 2) * 10
            match_summary.append(f"Support signals: {', '.join(support_signals)}.")
        importance = candidate.get("importance")
        if isinstance(importance, int | float):
            score += min(max(float(importance), 0.0), 1.0) * 10
        confidence = (
            "likely"
            if score >= 75 and scope_status == "accepted" and mismatch is None
            else "possible"
            if score >= 25 and scope_status != "out_of_scope"
            else "conflicting"
        )
        geocode_result = {
            key: value
            for key, value in candidate.items()
            if key not in {"candidates", "cache_version"}
        }
        options.append(
            {
                "display_name": str(candidate.get("display_name") or ""),
                "latitude": float(latitude),
                "longitude": float(longitude),
                "provider": str(candidate.get("provider") or "geocoder"),
                "query": query,
                "category": candidate.get("category"),
                "type": candidate.get("type"),
                "name": candidate.get("name"),
                "address": candidate.get("address", {}),
                "scope_status": scope_status,
                "scope_reason": scope_reason,
                "address_mismatch": mismatch,
                "address_warnings": list(warnings),
                "hard_conflict": mismatch is not None,
                "facility_name_match": facility_match,
                "support_signals": list(support_signals),
                "score": round(score, 1),
                "confidence": confidence,
                "match_summary": match_summary,
                "geocode_result": geocode_result,
            }
        )
    return options


def _merge_ranked_candidate_options(
    existing: list[dict[str, Any]],
    additions: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_location: dict[tuple[float, float], dict[str, Any]] = {
        (
            round(float(option["latitude"]), 6),
            round(float(option["longitude"]), 6),
        ): option
        for option in existing
    }
    for option in additions:
        key = (
            round(float(option["latitude"]), 6),
            round(float(option["longitude"]), 6),
        )
        current = by_location.get(key)
        if current is None or float(option["score"]) > float(current["score"]):
            by_location[key] = dict(option)
    return sorted(
        by_location.values(),
        key=lambda option: (-float(option["score"]), str(option["display_name"])),
    )[:5]


def _should_retry_address_after_geocode(
    spatial_validation: dict[str, object],
    geometry_record: dict[str, Any],
) -> bool:
    raw_candidate_count = spatial_validation.get("candidate_count")
    candidate_count = raw_candidate_count if isinstance(raw_candidate_count, int) else 0
    address_status = _record_address_status(geometry_record)
    if bool(spatial_validation.get("hard_conflict")):
        return True
    if spatial_validation.get("status") in {"address_mismatch", "out_of_scope"}:
        return True
    if candidate_count == 0:
        return True
    return address_status in {"", "not_run", "not_found", "ambiguous", "needs_review"}

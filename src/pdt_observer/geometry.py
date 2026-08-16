from __future__ import annotations

import csv
import io
import json
import math
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from pdt_observer.countries import country_code_for
from pdt_observer.leads import load_leads, load_qaqc_reviews
from pdt_observer.models import (
    GeometryPoint,
    GeometryReviewItem,
    GeometryStatus,
    HarvestRunManifest,
    LeadQaqcRecommendedAction,
    LeadQaqcVerificationStatus,
)
from pdt_observer.storage import write_json_file

GEOMETRY_LIST_ADAPTER: TypeAdapter[tuple[GeometryReviewItem, ...]] = TypeAdapter(
    tuple[GeometryReviewItem, ...]
)

_LOCALITY_STOPWORDS = {
    "and",
    "area",
    "district",
    "excluding",
    "greater",
    "metropolitan",
    "north",
    "northeast",
    "northwest",
    "province",
    "region",
    "regional",
    "south",
    "southeast",
    "southwest",
    "state",
    "the",
    "west",
    "east",
}
_CENTROID_RESULT_TYPES = {
    "administrative",
    "city",
    "country",
    "county",
    "municipality",
    "postcode",
    "province",
    "region",
    "state",
    "town",
    "village",
}
def geometry_review_path(root: Path, child_run_id: str) -> Path:
    return root / "geometry_reviews" / f"{child_run_id}.json"


def qaqc_output_path(root: Path, child_run_id: str) -> Path:
    return root / "qaqc_runs" / f"{child_run_id}-qaqc.json"


def load_geometry_reviews(root: Path, child_run_id: str) -> tuple[GeometryReviewItem, ...]:
    path = geometry_review_path(root, child_run_id)
    if not path.is_file():
        return ()
    return GEOMETRY_LIST_ADAPTER.validate_json(path.read_text(encoding="utf-8"))


def save_geometry_review_item(root: Path, item: GeometryReviewItem) -> GeometryReviewItem:
    existing = {
        candidate.item_id: candidate for candidate in load_geometry_reviews(root, item.child_run_id)
    }
    existing[item.item_id] = item
    path = geometry_review_path(root, item.child_run_id)
    payload = [candidate.model_dump(mode="json") for candidate in existing.values()]
    write_json_file(path, payload)
    return item


def item_id_for_lead(child_run_id: str, lead_index: int) -> str:
    return f"{child_run_id}-{lead_index}"


def parse_geometry_item_id(item_id: str) -> tuple[str, int]:
    child_run_id, lead_index_text = item_id.rsplit("-", 1)
    return child_run_id, int(lead_index_text)


def geocode_query_for_lead(lead: Any) -> str:
    parts = [
        lead.location.facility_name,
        lead.location.specific_address_or_landmark,
        lead.location.city_or_region,
        lead.location.country,
    ]
    return ", ".join(part for part in parts if part and part != "Unknown")


def _percent_decode(value: str) -> str:
    def decode(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 16))

    return re.sub(r"%([0-9A-Fa-f]{2})", decode, value)


def parse_coordinate_text(value: str) -> tuple[float, float, bool]:
    text = _percent_decode(value.strip())
    if not text:
        raise ValueError("Paste a latitude and longitude or a Google Maps URL.")
    patterns = (
        r"@([+-]?\d+(?:\.\d+)?),\s*([+-]?\d+(?:\.\d+)?)",
        r"[?&](?:query|q|ll)=([+-]?\d+(?:\.\d+)?),\s*([+-]?\d+(?:\.\d+)?)",
    )
    latitude: float | None = None
    longitude: float | None = None
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match is not None:
            latitude = float(match.group(1))
            longitude = float(match.group(2))
            break
    if latitude is None or longitude is None:
        directional = re.search(
            r"([+-]?\d+(?:\.\d+)?)\s*°?\s*([NS])\s*[,;]?\s*"
            r"([+-]?\d+(?:\.\d+)?)\s*°?\s*([EW])",
            text,
            flags=re.IGNORECASE,
        )
        if directional is not None:
            latitude = abs(float(directional.group(1))) * (
                -1 if directional.group(2).casefold() == "s" else 1
            )
            longitude = abs(float(directional.group(3))) * (
                -1 if directional.group(4).casefold() == "w" else 1
            )
    if latitude is None or longitude is None:
        decimal = re.fullmatch(
            r"\s*([+-]?\d+(?:\.\d+)?)\s*[,;]\s*"
            r"([+-]?\d+(?:\.\d+)?)\s*",
            text,
        )
        if decimal is not None:
            latitude = float(decimal.group(1))
            longitude = float(decimal.group(2))
    if latitude is None or longitude is None:
        raise ValueError(
            "Coordinates were not recognized. Use latitude, longitude; "
            "directional coordinates; or a Google Maps URL."
        )
    reversed_order = False
    if abs(latitude) > 90 and abs(longitude) <= 90:
        latitude, longitude = longitude, latitude
        reversed_order = True
    if not -90 <= latitude <= 90:
        raise ValueError("Latitude must be between -90 and 90.")
    if not -180 <= longitude <= 180:
        raise ValueError("Longitude must be between -180 and 180.")
    return latitude, longitude, reversed_order


def approved_records_for_child(
    root: Path,
    manifest: HarvestRunManifest,
) -> tuple[dict[str, Any], ...]:
    qaqc_path = qaqc_output_path(root, manifest.run_id)
    if not qaqc_path.is_file():
        raise FileNotFoundError(f"QAQC review not found for run: {manifest.run_id}")
    leads = load_leads(Path(manifest.lead_path))
    reviews = load_qaqc_reviews(qaqc_path)
    approved: list[dict[str, Any]] = []
    for review in reviews:
        if review.lead_index >= len(leads):
            continue
        if review.verification_status != LeadQaqcVerificationStatus.VERIFIED:
            continue
        if review.recommended_action != LeadQaqcRecommendedAction.KEEP:
            continue
        lead = leads[review.lead_index]
        approved.append(
            {
                "item_id": item_id_for_lead(manifest.run_id, review.lead_index),
                "child_run_id": manifest.run_id,
                "lead_index": review.lead_index,
                "geocode_query": geocode_query_for_lead(lead),
                "lead": lead.model_dump(mode="json"),
                "qaqc_review": review.model_dump(mode="json"),
            }
        )
    return tuple(approved)


def merge_geometry_items(
    root: Path,
    records: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    by_child: dict[str, dict[str, GeometryReviewItem]] = {}
    payloads: list[dict[str, Any]] = []
    for record in records:
        child_run_id = str(record["child_run_id"])
        if child_run_id not in by_child:
            by_child[child_run_id] = {
                item.item_id: item for item in load_geometry_reviews(root, child_run_id)
            }
        saved = by_child[child_run_id].get(str(record["item_id"]))
        payload = dict(record)
        payload["geometry"] = saved.model_dump(mode="json") if saved is not None else None
        payload["geometries"] = (
            list(saved.geometries or geometry_set(saved.point, saved.polygon_geojson))
            if saved is not None
            else []
        )
        payload["geometry_status"] = (
            saved.geometry_status.value if saved is not None else GeometryStatus.NEEDS_REVIEW.value
        )
        payload["area_m2"] = saved.area_m2 if saved is not None else None
        payloads.append(payload)
    return tuple(payloads)


def polygon_area_m2(polygon_geojson: dict[str, Any]) -> float:
    coordinates = polygon_geojson.get("coordinates")
    if not isinstance(coordinates, list) or not coordinates:
        return 0.0
    ring = coordinates[0]
    if not isinstance(ring, list) or len(ring) < 4:
        return 0.0
    latitudes = [
        float(point[1])
        for point in ring
        if isinstance(point, list | tuple) and len(point) >= 2
    ]
    if not latitudes:
        return 0.0
    mean_lat = math.radians(sum(latitudes) / len(latitudes))
    meters_per_degree_lat = 111_320.0
    meters_per_degree_lon = meters_per_degree_lat * math.cos(mean_lat)
    projected: list[tuple[float, float]] = []
    for point in ring:
        if not isinstance(point, list | tuple) or len(point) < 2:
            continue
        projected.append(
            (
                float(point[0]) * meters_per_degree_lon,
                float(point[1]) * meters_per_degree_lat,
            )
        )
    if len(projected) < 4:
        return 0.0
    area = 0.0
    for index, current in enumerate(projected):
        next_point = projected[(index + 1) % len(projected)]
        area += current[0] * next_point[1] - next_point[0] * current[1]
    return abs(area) / 2.0


def geometry_set(
    point: GeometryPoint | None,
    polygon_geojson: dict[str, Any] | None,
) -> tuple[dict[str, object], ...]:
    geometries: list[dict[str, object]] = []
    if point is not None:
        geometries.append(
            {
                "type": "Point",
                "coordinates": [point.longitude, point.latitude],
                "source": point.source,
            }
        )
    if polygon_geojson is not None:
        geometries.append(dict(polygon_geojson))
    return tuple(geometries)


def verified_json(records: Sequence[dict[str, Any]]) -> str:
    return json.dumps(list(records), indent=2)


def verified_csv(records: Sequence[dict[str, Any]]) -> str:
    output = io.StringIO()
    fieldnames = (
        "item_id",
        "sample_set_id",
        "sample_round",
        "child_run_id",
        "lead_index",
        "facility_type",
        "source_url",
        "facility_name",
        "city_or_region",
        "country",
        "counts",
        "qaqc_status",
        "address_status",
        "enriched_address",
        "address_confidence",
        "address_source_url",
        "geometry_status",
        "area_m2",
    )
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for record in records:
        lead = record["lead"]
        review = record["qaqc_review"]
        address = record.get("address_enrichment") or {}
        counts = "; ".join(
            f"{datum['count']} {datum['group_type']}" for datum in lead["occupancy_data"]
        )
        writer.writerow(
            {
                "item_id": record["item_id"],
                "sample_set_id": record.get("sample_set_id", ""),
                "sample_round": record.get("sample_round", ""),
                "child_run_id": record["child_run_id"],
                "lead_index": record["lead_index"],
                "facility_type": record.get("facility_type", ""),
                "source_url": lead["source_url"],
                "facility_name": lead["location"]["facility_name"],
                "city_or_region": lead["location"]["city_or_region"],
                "country": lead["location"]["country"],
                "counts": counts,
                "qaqc_status": review["verification_status"],
                "address_status": record.get("address_status", "not_run"),
                "enriched_address": address.get("formatted_address", ""),
                "address_confidence": address.get("confidence", ""),
                "address_source_url": address.get("address_source_url", ""),
                "geometry_status": record.get("geometry_status", GeometryStatus.NEEDS_REVIEW.value),
                "area_m2": record.get("area_m2") or "",
            }
        )
    return output.getvalue()


def footprints_geojson(records: Sequence[dict[str, Any]]) -> str:
    features: list[dict[str, Any]] = []
    for record in records:
        geometry = record.get("geometry")
        if not isinstance(geometry, dict):
            continue
        polygon = geometry.get("polygon_geojson")
        if not isinstance(polygon, dict):
            continue
        lead = record["lead"]
        address = record.get("address_enrichment") or {}
        features.append(
            {
                "type": "Feature",
                "geometry": polygon,
                "properties": {
                    "item_id": record["item_id"],
                    "sample_set_id": record.get("sample_set_id"),
                    "sample_round": record.get("sample_round"),
                    "child_run_id": record["child_run_id"],
                    "lead_index": record["lead_index"],
                    "facility_type": record.get("facility_type"),
                    "facility_name": lead["location"]["facility_name"],
                    "source_url": lead["source_url"],
                    "address_status": record.get("address_status", "not_run"),
                    "enriched_address": address.get("formatted_address"),
                    "address_source_url": address.get("address_source_url"),
                    "area_m2": geometry.get("area_m2"),
                },
            }
        )
    return json.dumps({"type": "FeatureCollection", "features": features}, indent=2)


def _normalized_words(value: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[^\W\d_]+", value.casefold(), flags=re.UNICODE)
        if len(word) >= 3
    }


def _normalized_postal_code(value: object) -> str:
    return re.sub(r"\W", "", str(value or "").casefold())


def _candidate_support_signals(
    candidate: dict[str, Any],
    *,
    expected_locality: str | None,
    expected_locality_aliases: tuple[str, ...] = (),
    expected_postal_code: str | None,
    expected_facility_name: str | None,
) -> tuple[tuple[str, ...], str | None]:
    address = candidate.get("address")
    address = address if isinstance(address, dict) else {}
    signals: list[str] = []
    locality_miss_reason: str | None = None
    locality_values = (expected_locality or "", *expected_locality_aliases)
    if any(value.strip() for value in locality_values):
        locality_words: set[str] = set()
        for value in locality_values:
            locality_words.update(_normalized_words(value) - _LOCALITY_STOPWORDS)
        administrative_text = " ".join(
            [
                str(candidate.get("display_name") or ""),
                *(str(value) for value in address.values()),
            ]
        )
        administrative_words = _normalized_words(administrative_text)
        if locality_words and locality_words.intersection(administrative_words):
            signals.append("locality_match")
        elif locality_words:
            locality_miss_reason = (
                f"Result does not visibly match requested locality {expected_locality}."
            )
    expected_postal = _normalized_postal_code(expected_postal_code)
    candidate_postal = _normalized_postal_code(address.get("postcode"))
    if expected_postal and candidate_postal and expected_postal == candidate_postal:
        signals.append("postal_code_match")
    facility_words = _normalized_words(str(expected_facility_name or "")) - _LOCALITY_STOPWORDS
    candidate_words = _normalized_words(
        " ".join(
            str(candidate.get(key) or "")
            for key in ("name", "display_name", "category", "type")
        )
    )
    if facility_words and candidate_words and facility_words.intersection(candidate_words):
        signals.append("facility_name_match")
    return tuple(dict.fromkeys(signals)), locality_miss_reason


def _candidate_scope_status(
    candidate: dict[str, Any],
    *,
    expected_country: str,
    expected_locality: str | None,
    expected_country_aliases: dict[str, str] | None = None,
    expected_locality_aliases: tuple[str, ...] = (),
    expected_postal_code: str | None = None,
    expected_facility_name: str | None = None,
) -> tuple[str, str, tuple[str, ...]]:
    address = candidate.get("address")
    address = address if isinstance(address, dict) else {}
    country_code = str(address.get("country_code") or "").casefold()
    country_name = str(address.get("country") or "").casefold()
    expected_country_code = country_code_for(
        expected_country, extra_aliases=expected_country_aliases
    )
    candidate_country_code = country_code_for(
        country_code, extra_aliases=expected_country_aliases
    ) or country_code_for(
        country_name, extra_aliases=expected_country_aliases
    )
    if expected_country_code and candidate_country_code:
        if candidate_country_code != expected_country_code:
            return (
                "out_of_scope",
                f"Result country code is {candidate_country_code}, not {expected_country}.",
                (),
            )
    else:
        expected_country_folded = expected_country.casefold().strip()
        if (
            len(expected_country_folded) > 2
            and country_name
            and expected_country_folded not in country_name
            and country_name not in expected_country_folded
        ):
            return "out_of_scope", f"Result country is {country_name}, not {expected_country}.", ()

    result_type = str(candidate.get("type") or "").casefold()
    if result_type in _CENTROID_RESULT_TYPES:
        return (
            "requires_human",
            f"Result type {result_type} is an administrative centroid, not a facility coordinate.",
            (),
        )
    if not address:
        return (
            "requires_human",
            "Provider returned no structured administrative components for validation.",
            (),
        )
    support_signals, locality_miss_reason = _candidate_support_signals(
        candidate,
        expected_locality=expected_locality,
        expected_locality_aliases=expected_locality_aliases,
        expected_postal_code=expected_postal_code,
        expected_facility_name=expected_facility_name,
    )
    country_signal = "country_code_match" if candidate_country_code else "country_name_match"
    if support_signals:
        return (
            "accepted",
            f"{country_signal}, {', '.join(support_signals)}, and result type passed validation.",
            support_signals,
        )
    return (
        "requires_human",
        locality_miss_reason
        or "Candidate is in the expected country but lacks a locality, postal, or facility match.",
        (),
    )


def spatially_validate_geocode_result(
    result: dict[str, Any] | None,
    *,
    expected_country: str,
    expected_locality: str | None,
    expected_country_aliases: dict[str, str] | None = None,
    expected_locality_aliases: tuple[str, ...] = (),
    expected_postal_code: str | None = None,
    expected_facility_name: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, object]]:
    if result is None:
        return None, {
            "status": "no_match",
            "requires_human_intervention": True,
            "reason": "The geocoder returned no candidates.",
            "candidate_count": 0,
        }
    raw_candidates = result.get("candidates")
    candidates = (
        [candidate for candidate in raw_candidates if isinstance(candidate, dict)]
        if isinstance(raw_candidates, list)
        else [result]
    )
    assessments: list[dict[str, object]] = []
    accepted: dict[str, Any] | None = None
    for candidate in candidates:
        status, reason, support_signals = _candidate_scope_status(
            candidate,
            expected_country=expected_country,
            expected_locality=expected_locality,
            expected_country_aliases=expected_country_aliases,
            expected_locality_aliases=expected_locality_aliases,
            expected_postal_code=expected_postal_code,
            expected_facility_name=expected_facility_name,
        )
        assessments.append(
            {
                "display_name": str(candidate.get("display_name") or ""),
                "status": status,
                "reason": reason,
                "support_signals": list(support_signals),
                "latitude": candidate.get("latitude"),
                "longitude": candidate.get("longitude"),
            }
        )
        if status == "accepted" and accepted is None:
            accepted = candidate
    if accepted is not None:
        return accepted, {
            "status": "accepted",
            "requires_human_intervention": False,
            "reason": "A geocoder candidate passed spatial validation.",
            "candidate_count": len(candidates),
            "assessments": assessments,
        }
    statuses = {str(assessment["status"]) for assessment in assessments}
    status = "out_of_scope" if statuses == {"out_of_scope"} else "requires_human"
    return None, {
        "status": status,
        "requires_human_intervention": True,
        "reason": (
            "All candidates were outside the requested geographic scope."
            if status == "out_of_scope"
            else "No candidate was specific and complete enough for automatic acceptance."
        ),
        "candidate_count": len(candidates),
        "assessments": assessments,
    }


def geometry_item_from_payload(
    *,
    item_id: str,
    geocode_query: str,
    point: GeometryPoint | None,
    polygon_geojson: dict[str, Any] | None,
    geometry_status: GeometryStatus,
    geocode_result: dict[str, Any] | None = None,
    spatial_validation: dict[str, Any] | None = None,
    review_notes: str | None = None,
) -> GeometryReviewItem:
    child_run_id, lead_index = parse_geometry_item_id(item_id)
    area = polygon_area_m2(polygon_geojson) if polygon_geojson is not None else None
    return GeometryReviewItem(
        item_id=item_id,
        child_run_id=child_run_id,
        lead_index=lead_index,
        geocode_query=geocode_query,
        geocode_result=geocode_result,
        point=point,
        polygon_geojson=polygon_geojson,
        geometries=geometry_set(point, polygon_geojson),
        area_m2=area,
        spatial_validation=spatial_validation,
        geometry_status=geometry_status,
        review_notes=review_notes,
    )

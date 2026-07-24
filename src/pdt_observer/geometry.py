from __future__ import annotations

import csv
import io
import json
import math
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from pdt_observer.leads import load_leads, load_qaqc_reviews
from pdt_observer.models import (
    GeometryPoint,
    GeometryReviewItem,
    GeometryStatus,
    HarvestRunManifest,
    LeadQaqcRecommendedAction,
    LeadQaqcVerificationStatus,
)

GEOMETRY_LIST_ADAPTER: TypeAdapter[tuple[GeometryReviewItem, ...]] = TypeAdapter(
    tuple[GeometryReviewItem, ...]
)

Geocoder = Callable[[str], dict[str, Any] | None]


def geometry_review_path(root: Path, child_run_id: str) -> Path:
    return root / "geometry_reviews" / f"{child_run_id}.json"


def geocode_cache_path(root: Path) -> Path:
    return root / "geocode_cache" / "nominatim.json"


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
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [candidate.model_dump(mode="json") for candidate in existing.values()]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
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


def verified_json(records: Sequence[dict[str, Any]]) -> str:
    return json.dumps(list(records), indent=2)


def verified_csv(records: Sequence[dict[str, Any]]) -> str:
    output = io.StringIO()
    fieldnames = (
        "item_id",
        "child_run_id",
        "lead_index",
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
                "child_run_id": record["child_run_id"],
                "lead_index": record["lead_index"],
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
                    "child_run_id": record["child_run_id"],
                    "lead_index": record["lead_index"],
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


class NominatimGeocoder:
    def __init__(self, root: Path, *, min_interval_seconds: float = 1.0) -> None:
        self.root = root
        self.min_interval_seconds = min_interval_seconds
        self._lock = threading.Lock()
        self._last_request_at = 0.0

    def _load_cache(self) -> dict[str, dict[str, Any] | None]:
        path = geocode_cache_path(self.root)
        if not path.is_file():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}

    def _write_cache(self, cache: dict[str, dict[str, Any] | None]) -> None:
        path = geocode_cache_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")

    def __call__(self, query: str) -> dict[str, Any] | None:
        with self._lock:
            cache = self._load_cache()
            if query in cache:
                return cache[query]
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.min_interval_seconds:
                time.sleep(self.min_interval_seconds - elapsed)
            params = urllib.parse.urlencode({"q": query, "format": "jsonv2", "limit": "1"})
            request = urllib.request.Request(
                f"https://nominatim.openstreetmap.org/search?{params}",
                headers={
                    "User-Agent": "pdt-observer-local-app/0.1 (user-triggered geometry review)"
                },
            )
            self._last_request_at = time.monotonic()
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            first = payload[0] if isinstance(payload, list) and payload else None
            result = (
                {
                    "display_name": first.get("display_name", ""),
                    "latitude": float(first["lat"]),
                    "longitude": float(first["lon"]),
                    "provider": "nominatim",
                    "query": query,
                }
                if isinstance(first, dict) and "lat" in first and "lon" in first
                else None
            )
            cache[query] = result
            self._write_cache(cache)
            return result


def geometry_item_from_payload(
    *,
    item_id: str,
    geocode_query: str,
    point: GeometryPoint | None,
    polygon_geojson: dict[str, Any] | None,
    geometry_status: GeometryStatus,
    geocode_result: dict[str, Any] | None = None,
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
        area_m2=area,
        geometry_status=geometry_status,
        review_notes=review_notes,
    )

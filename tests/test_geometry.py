from __future__ import annotations

import json
from pathlib import Path

import pytest

from pdt_observer.geometry import (
    approved_records_for_child,
    geometry_item_from_payload,
    merge_geometry_items,
    parse_coordinate_text,
    save_geometry_review_item,
    spatially_validate_geocode_result,
    verified_csv,
)
from pdt_observer.models import (
    GeometryPoint,
    GeometryStatus,
    HarvestRunManifest,
    HarvestRunStatus,
)
from pdt_observer.workflow import write_model

LEAD = {
    "is_valid_occupancy_report": True,
    "source_url": "https://example.test/story",
    "source_title": "Workers evacuated",
    "source_type": "news",
    "evidence_quote": "Officials said 12 workers were evacuated.",
    "incident_date": "2026-01-02",
    "incident_time": "03:30 PM",
    "occupancy_data": [{"count": 12, "group_type": "workers"}],
    "location": {
        "facility_name": "Example Warehouse",
        "specific_address_or_landmark": "Industrial Drive",
        "city_or_region": "Tennessee",
        "country": "US",
    },
    "confidence": "high",
    "is_facility_level": True,
    "is_regional_aggregate": False,
    "review_flags": [],
    "review_notes": None,
}


def _write_child_run(root: Path, run_id: str) -> HarvestRunManifest:
    lead_path = root / "lead_runs" / f"{run_id}.json"
    lead_path.parent.mkdir()
    lead_path.write_text(json.dumps([LEAD]), encoding="utf-8")
    qaqc_path = root / "qaqc_runs" / f"{run_id}-qaqc.json"
    qaqc_path.parent.mkdir()
    qaqc_path.write_text(
        json.dumps(
            [
                {
                    "lead_index": 0,
                    "source_url": "https://example.test/story",
                    "verification_status": "verified",
                    "source_reachable": True,
                    "facility_match": True,
                    "location_match": True,
                    "count_checks": [],
                    "supporting_quote": "Officials said 12 workers were evacuated.",
                    "recommended_action": "keep",
                    "review_notes": "Verified.",
                }
            ]
        ),
        encoding="utf-8",
    )
    manifest = HarvestRunManifest(
        run_id=run_id,
        status=HarvestRunStatus.COMPLETED,
        country="US",
        locality="Tennessee",
        profile_set="manufacturing",
        target=1,
        prompt_path=f"work/{run_id}.md",
        lead_path=str(lead_path),
        started_at="2026-07-23T00:00:00Z",
        completed_at="2026-07-23T00:01:00Z",
        validation_valid=True,
    )
    write_model(root / "harvest_runs" / f"{run_id}.json", manifest)
    return manifest


def test_approved_records_filter_to_verified_keep(tmp_path: Path) -> None:
    manifest = _write_child_run(tmp_path, "us-tn-manufacturing")

    records = approved_records_for_child(tmp_path, manifest)

    assert len(records) == 1
    assert records[0]["item_id"] == "us-tn-manufacturing-0"
    assert records[0]["lead"]["location"]["facility_name"] == "Example Warehouse"


def test_parse_coordinate_text_supports_google_maps_copy_formats() -> None:
    assert parse_coordinate_text("33.7490, -84.3880") == (33.749, -84.388, False)
    assert parse_coordinate_text("33.7490° N, 84.3880° W") == (
        33.749,
        -84.388,
        False,
    )
    assert parse_coordinate_text(
        "https://www.google.com/maps/place/test/@33.7490,-84.3880,18z"
    ) == (33.749, -84.388, False)
    assert parse_coordinate_text("-118.2437, 34.0522") == (
        34.0522,
        -118.2437,
        True,
    )


def test_parse_coordinate_text_rejects_unrecognized_or_invalid_values() -> None:
    with pytest.raises(ValueError, match="not recognized"):
        parse_coordinate_text("Example Warehouse")
    with pytest.raises(ValueError, match="Latitude"):
        parse_coordinate_text("95, 150")


def test_polygon_area_and_geometry_review_item(tmp_path: Path) -> None:
    polygon = {
        "type": "Polygon",
        "coordinates": [
            [
                [-86.0, 36.0],
                [-86.0, 36.001],
                [-85.999, 36.001],
                [-85.999, 36.0],
                [-86.0, 36.0],
            ]
        ],
    }

    item = geometry_item_from_payload(
        item_id="us-tn-manufacturing-0",
        geocode_query="Example Warehouse, Tennessee, US",
        point=GeometryPoint(latitude=36.0, longitude=-86.0, source="user"),
        polygon_geojson=polygon,
        geometry_status=GeometryStatus.FOOTPRINT_DRAWN,
    )

    assert item.area_m2 is not None
    assert item.area_m2 > 0
    assert [geometry["type"] for geometry in item.geometries] == ["Point", "Polygon"]
    saved = save_geometry_review_item(tmp_path, item)
    assert saved.geometry_status == GeometryStatus.FOOTPRINT_DRAWN


def test_spatial_validation_accepts_specific_in_scope_candidate() -> None:
    candidate = {
        "display_name": "Example School, Atlanta, Georgia, United States",
        "latitude": 33.75,
        "longitude": -84.39,
        "provider": "nominatim",
        "query": "Example School, Atlanta, Georgia, US",
        "type": "school",
        "address": {
            "city": "Atlanta",
            "state": "Georgia",
            "country": "United States",
            "country_code": "us",
        },
    }

    accepted, validation = spatially_validate_geocode_result(
        {**candidate, "candidates": [candidate]},
        expected_country="US",
        expected_locality="Georgia",
    )

    assert accepted == candidate
    assert validation["status"] == "accepted"
    assert validation["requires_human_intervention"] is False


def test_spatial_validation_routes_out_of_scope_and_centroid_results_to_humans() -> None:
    mexico = {
        "display_name": "Example School, Monterrey, Nuevo León, México",
        "latitude": 25.67,
        "longitude": -100.31,
        "type": "school",
        "address": {
            "city": "Monterrey",
            "state": "Nuevo León",
            "country": "México",
            "country_code": "mx",
        },
    }
    florida = {
        "display_name": "Example School, Parkland, Florida, United States",
        "latitude": 26.31,
        "longitude": -80.24,
        "type": "school",
        "address": {"city": "Parkland", "state": "Florida", "country_code": "us"},
    }
    georgia_centroid = {
        "display_name": "Georgia, United States",
        "latitude": 32.68,
        "longitude": -83.22,
        "type": "state",
        "address": {
            "state": "Georgia",
            "country": "United States",
            "country_code": "us",
        },
    }

    accepted_outside, outside = spatially_validate_geocode_result(
        {**mexico, "candidates": [mexico]},
        expected_country="US",
        expected_locality="Georgia",
    )
    accepted_locality, locality = spatially_validate_geocode_result(
        {**florida, "candidates": [florida]},
        expected_country="US",
        expected_locality="Georgia",
    )
    accepted_centroid, centroid = spatially_validate_geocode_result(
        {**georgia_centroid, "candidates": [georgia_centroid]},
        expected_country="US",
        expected_locality="Georgia",
    )

    assert accepted_outside is None
    assert outside["status"] == "out_of_scope"
    assert accepted_locality is None
    assert locality["status"] == "requires_human"
    assert accepted_centroid is None
    assert centroid["status"] == "requires_human"


def test_spatial_validation_accepts_local_script_country_with_matching_code() -> None:
    candidate = {
        "display_name": "มหาวิทยาลัยสงขลานครินทร์, อำเภอหาดใหญ่, ประเทศไทย",
        "latitude": 7.0069589,
        "longitude": 100.5007596,
        "type": "university",
        "address": {
            "amenity": "มหาวิทยาลัยสงขลานครินทร์",
            "county": "อำเภอหาดใหญ่",
            "province": "จังหวัดสงขลา",
            "postcode": "90110",
            "country": "ประเทศไทย",
            "country_code": "th",
        },
    }

    accepted, validation = spatially_validate_geocode_result(
        {**candidate, "candidates": [candidate]},
        expected_country="Thailand",
        expected_locality="Hat Yai district, Songkhla",
        expected_postal_code="90110",
        expected_facility_name="Prince of Songkla University",
    )

    assert accepted == candidate
    assert validation["status"] == "accepted"
    assert "postal_code_match" in validation["assessments"][0]["support_signals"]


def test_spatial_validation_uses_global_country_name_map() -> None:
    candidate = {
        "display_name": "Tokyo International Forum, Tokyo, Japan",
        "latitude": 35.6769,
        "longitude": 139.7649,
        "type": "events_venue",
        "address": {
            "city": "Tokyo",
            "postcode": "100-0005",
            "country": "日本",
            "country_code": "jp",
        },
    }

    accepted, validation = spatially_validate_geocode_result(
        {**candidate, "candidates": [candidate]},
        expected_country="Japan",
        expected_locality="Tokyo",
        expected_postal_code="100-0005",
        expected_facility_name="Tokyo International Forum",
    )

    assert accepted == candidate
    assert validation["status"] == "accepted"


def test_verified_csv_includes_geometry_status(tmp_path: Path) -> None:
    manifest = _write_child_run(tmp_path, "us-tn-manufacturing")
    records = tuple(merge_geometry_items(tmp_path, approved_records_for_child(tmp_path, manifest)))

    payload = verified_csv(records)

    assert "Example Warehouse" in payload
    assert "needs_review" in payload

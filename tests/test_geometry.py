from __future__ import annotations

import json
from pathlib import Path

from pdt_observer.geometry import (
    approved_records_for_child,
    geometry_item_from_payload,
    merge_geometry_items,
    save_geometry_review_item,
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
    saved = save_geometry_review_item(tmp_path, item)
    assert saved.geometry_status == GeometryStatus.FOOTPRINT_DRAWN


def test_verified_csv_includes_geometry_status(tmp_path: Path) -> None:
    manifest = _write_child_run(tmp_path, "us-tn-manufacturing")
    records = tuple(merge_geometry_items(tmp_path, approved_records_for_child(tmp_path, manifest)))

    payload = verified_csv(records)

    assert "Example Warehouse" in payload
    assert "needs_review" in payload

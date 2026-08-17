from __future__ import annotations

import json
from pathlib import Path

from pdt_observer.addresses import load_address_results, upsert_address_result
from pdt_observer.artifact_migrations import migrate_workspace
from pdt_observer.dialogue import append_dialogue, load_dialogue
from pdt_observer.geometry import (
    geometry_item_from_payload,
    load_geometry_reviews,
    save_geometry_review_item,
)
from pdt_observer.models import (
    AddressEnrichmentResult,
    AddressEnrichmentStatus,
    GeometryPoint,
    GeometryStatus,
)
from pdt_observer.profiles import get_profile_set, write_profile_set


def _temp_files(directory: Path) -> list[Path]:
    return list(directory.glob("*.tmp"))


def test_address_results_are_written_atomically(tmp_path: Path) -> None:
    result = AddressEnrichmentResult(
        lead_index=0,
        item_id="run-0",
        facility_name="Example Warehouse",
        formatted_address="100 Industrial Drive, Nashville, TN",
        status=AddressEnrichmentStatus.FOUND,
        review_notes="Found a matching public address.",
    )

    upsert_address_result(tmp_path, "run", result)

    assert load_address_results(tmp_path / "address_runs" / "run-address.json") == (result,)
    assert _temp_files(tmp_path / "address_runs") == []


def test_geometry_reviews_are_written_atomically(tmp_path: Path) -> None:
    item = geometry_item_from_payload(
        item_id="run-0",
        geocode_query="Example Warehouse, Tennessee, US",
        point=GeometryPoint(latitude=36.1, longitude=-86.7, source="geocode"),
        polygon_geojson=None,
        geometry_status=GeometryStatus.POINT_CONFIRMED,
        geocode_result={"provider": "mock"},
        spatial_validation={"status": "accepted"},
    )

    save_geometry_review_item(tmp_path, item)

    assert load_geometry_reviews(tmp_path, "run") == (item,)
    assert _temp_files(tmp_path / "geometry_reviews") == []


def test_component_bundle_geometry_reviews_use_parent_run_id(tmp_path: Path) -> None:
    item = geometry_item_from_payload(
        item_id="china-guan-du-retail-service-2026-08-17t16-18-45z-component-bundle-7",
        geocode_query="昆明官渡大酒店, Kunming, China",
        point=GeometryPoint(latitude=25.0, longitude=102.7, source="geocode"),
        polygon_geojson=None,
        geometry_status=GeometryStatus.POINT_CONFIRMED,
        geocode_result={"provider": "mock"},
        spatial_validation={"status": "accepted"},
    )

    save_geometry_review_item(tmp_path, item)

    parent_run_id = "china-guan-du-retail-service-2026-08-17t16-18-45z"
    assert item.child_run_id == parent_run_id
    assert load_geometry_reviews(tmp_path, parent_run_id) == (item,)
    assert not (
        tmp_path
        / "geometry_reviews"
        / "china-guan-du-retail-service-2026-08-17t16-18-45z-component-bundle.json"
    ).exists()


def test_legacy_component_bundle_geometry_reviews_load_with_parent_run_id(
    tmp_path: Path,
) -> None:
    parent_run_id = "china-guan-du-retail-service-2026-08-17t16-18-45z"
    legacy_item = {
        "schema_version": 1,
        "item_id": f"{parent_run_id}-component-bundle-7",
        "child_run_id": f"{parent_run_id}-component-bundle",
        "lead_index": 7,
        "geocode_query": "昆明官渡大酒店, Kunming, China",
        "geocode_result": {"provider": "mock"},
        "point": {"latitude": 25.0, "longitude": 102.7, "source": "geocode"},
        "polygon_geojson": None,
        "geometries": [{"type": "Point", "coordinates": [102.7, 25.0], "source": "geocode"}],
        "area_m2": None,
        "spatial_validation": {"status": "accepted"},
        "geometry_status": "point_confirmed",
        "review_notes": "Accepted.",
    }
    legacy_path = (
        tmp_path
        / "geometry_reviews"
        / f"{parent_run_id}-component-bundle.json"
    )
    legacy_path.parent.mkdir()
    legacy_path.write_text(json.dumps([legacy_item]), encoding="utf-8")

    loaded = load_geometry_reviews(tmp_path, parent_run_id)

    assert len(loaded) == 1
    assert loaded[0].child_run_id == parent_run_id
    assert loaded[0].item_id == f"{parent_run_id}-component-bundle-7"


def test_dialogue_entries_are_written_atomically(tmp_path: Path) -> None:
    entry = append_dialogue(
        tmp_path,
        "conversation",
        speaker="Spatial Resolver",
        stage="automated_geocoding",
        message="I assigned an in-scope coordinate.",
    )

    assert load_dialogue(tmp_path, "conversation") == (entry,)
    assert _temp_files(tmp_path / "agent_dialogue") == []


def test_profile_sets_are_written_atomically(tmp_path: Path) -> None:
    profile_set = get_profile_set("schools")
    path = tmp_path / "profiles" / "schools.json"

    write_profile_set(profile_set, path)

    assert json.loads(path.read_text(encoding="utf-8"))["profile_set_id"] == "schools"
    assert _temp_files(path.parent) == []


def test_artifact_migrations_write_atomically(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "harvest_runs"
    artifact_dir.mkdir()
    artifact = artifact_dir / "example.json"
    artifact.write_text(json.dumps({"run_id": "example"}), encoding="utf-8")

    result = migrate_workspace(tmp_path, dry_run=False)

    assert result["changed_count"] == 1
    assert json.loads(artifact.read_text(encoding="utf-8"))["schema_version"] == 1
    assert artifact.with_suffix(".json.bak").is_file()
    assert _temp_files(artifact_dir) == []

from __future__ import annotations

import json
from pathlib import Path

from pdt_observer.models import BuildingProfileSet, CountMethod
from pdt_observer.profiles import (
    BUILTIN_PROFILE_SETS,
    get_profile_set,
    narrow_profile_set,
    resolve_profile_set,
)

CANONICAL_LAND_USES = (
    "residential",
    "institutions_public_service",
    "retail_service",
    "commercial",
    "transportation",
    "military_facility",
    "recreation_entertainment",
    "agriculture",
)


def _pdt_mapping() -> dict[str, object]:
    return json.loads(Path("profiles/pdt_count_mapping.json").read_text(encoding="utf-8"))


def _mapping_rows() -> dict[tuple[str, str], dict[str, object]]:
    return {
        (row["land_use_id"], row["facility_class_id"]): row
        for row in _pdt_mapping()["rows"]
    }


def test_builtin_profile_sets_are_canonical_land_uses() -> None:
    assert tuple(BUILTIN_PROFILE_SETS) == CANONICAL_LAND_USES
    assert [get_profile_set(profile_set_id).label for profile_set_id in CANONICAL_LAND_USES] == [
        "Residential",
        "Institutions/Public Service",
        "Retail and Service Outlets",
        "Commercial",
        "Transportation",
        "Military Facility",
        "Recreation/Entertainment",
        "Agriculture",
    ]


def test_repo_owned_mapping_fixture_reflects_updated_spreadsheet() -> None:
    mapping = _pdt_mapping()

    assert mapping["schema_version"] == 2
    assert mapping["taxonomy"]["land_use_count"] == 8
    assert mapping["taxonomy"]["facility_class_count"] == 65
    assert mapping["taxonomy"]["land_uses"] == [
        "Residential",
        "Institutions/Public Service",
        "Retail and Service Outlets",
        "Commercial",
        "Transportation",
        "Military Facility",
        "Recreation/Entertainment",
        "Agriculture",
    ]


def test_builtin_land_use_profiles_match_json() -> None:
    for profile_set_id in CANONICAL_LAND_USES:
        builtin = get_profile_set(profile_set_id)
        from_json = BuildingProfileSet.model_validate_json(
            Path(f"profiles/{profile_set_id}.json").read_text(encoding="utf-8")
        )

        assert builtin == from_json


def test_custom_profile_set_can_load_from_path(tmp_path: Path) -> None:
    path = tmp_path / "custom_facilities.json"
    path.write_text(
        """
        {
          "profile_set_id": "custom_facilities",
          "label": "Custom colleague-defined facilities",
          "profiles": [
            {
              "profile_id": "cold_storage",
              "label": "Cold storage warehouses",
              "source_search_prompt": "Find incident-tied headcounts in cold storage facilities.",
              "positive_evidence_patterns": ["workers were evacuated"],
              "negative_evidence_patterns": ["storage capacity"],
              "venue_aliases": ["cold storage", "refrigerated warehouse"]
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    profile_set = get_profile_set(str(path))

    assert profile_set.profile_set_id == "custom_facilities"
    assert profile_set.profiles[0].profile_id == "cold_storage"
    assert profile_set.profiles[0].land_use is None
    assert profile_set.profiles[0].facility_class is None


def test_profile_set_can_be_narrowed_with_legacy_alias() -> None:
    profile_set = get_profile_set("commercial")

    narrowed = narrow_profile_set(profile_set, "factories_warehouses")

    assert narrowed.profile_set_id == "commercial"
    assert [profile.profile_id for profile in narrowed.profiles] == ["light_manufacturing"]


def test_legacy_profile_sets_remain_loadable_but_not_canonical() -> None:
    assert get_profile_set("schools").profile_set_id == "schools"
    assert get_profile_set("manufacturing").profile_set_id == "manufacturing"
    assert get_profile_set("commercial_business").profile_set_id == "commercial_business"
    assert get_profile_set("philippines_commercial_business") == get_profile_set(
        "commercial_business"
    )
    assert "schools" not in BUILTIN_PROFILE_SETS
    assert "commercial_business" not in BUILTIN_PROFILE_SETS


def test_spreadsheet_mapping_drives_canonical_profiles() -> None:
    rows = _mapping_rows()
    for profile_set_id in CANONICAL_LAND_USES:
        profile_set = get_profile_set(profile_set_id)
        for profile in profile_set.profiles:
            key = (profile_set_id, profile.profile_id)
            row = rows[key]
            assert profile.land_use == row["land_use"]
            assert profile.facility_class == row["facility_class"]
            assert profile.pdt_subtype == row["facility_class"]
            assert profile.count_method.value == row["count_method"]
            if profile.count_method == CountMethod.POPULATION_SUBCOMPONENT:
                assert profile.component_count_fields == tuple(row["component_count_fields"])
            else:
                assert profile.component_count_fields == ()
                assert profile.contextual_count_fields == tuple(
                    row["direct_contextual_count_fields"]
                )
            assert profile.regional_stat_fields == tuple(row["regional_stat_fields"])


def test_hotel_motel_uses_updated_subcomponent_arguments() -> None:
    hotel = resolve_profile_set(
        "retail_service",
        profile_id="hotel_motel",
    ).profiles[0]

    assert hotel.count_method == CountMethod.POPULATION_SUBCOMPONENT
    assert hotel.component_count_fields == (
        "Number of rooms",
        "number of suites",
        "hotel stars",
        "average room size",
        "average suite size",
    )
    assert hotel.regional_stat_fields == ("Hotel Occupancy Rate",)
    assert "hotel occupancy rate" not in tuple(
        field.casefold() for field in hotel.component_count_fields
    )


def test_residential_arguments_are_only_household_and_dwelling_size() -> None:
    residential = get_profile_set("residential")
    single_family = next(
        profile for profile in residential.profiles if profile.profile_id == "single_family"
    )

    assert single_family.count_method == CountMethod.POPULATION_SUBCOMPONENT
    assert single_family.component_count_fields == ("Household size", "dwelling size")
    assert single_family.regional_stat_fields == (
        "School attendance rate",
        "unemployment rate",
        "age/sex breakdown",
    )


def test_school_hospital_and_commercial_fields_match_spreadsheet_arguments() -> None:
    school = resolve_profile_set(
        "institutions_public_service",
        profile_id="school_d_12",
    ).profiles[0]
    hospital = resolve_profile_set(
        "institutions_public_service",
        profile_id="hospital_clinic_with_beds",
    ).profiles[0]
    office = resolve_profile_set("commercial", profile_id="office_building").profiles[0]

    assert school.component_count_fields == (
        "Students",
        "staff",
        "faculty",
        "shift schooling",
    )
    assert school.regional_stat_fields == ("School attendance Rate",)
    assert hospital.component_count_fields == (
        "Inpatients",
        "outpatients",
        "beds",
        "nurses",
        "doctors",
        "staff",
    )
    assert hospital.regional_stat_fields == ("Bed Occupancy Rate",)
    assert office.component_count_fields == ("Employees", "shifts")


def test_direct_count_profiles_keep_arguments_as_context_not_components() -> None:
    religious = resolve_profile_set(
        "institutions_public_service",
        profile_id="religious",
    ).profiles[0]
    indoor_agriculture = resolve_profile_set(
        "agriculture",
        profile_id="indoor_agriculture",
    ).profiles[0]

    assert religious.count_method == CountMethod.DIRECT_COUNT
    assert religious.component_count_fields == ()
    assert religious.contextual_count_fields == ("Capacity", "staff")
    assert indoor_agriculture.component_count_fields == ()
    assert indoor_agriculture.contextual_count_fields == ("Employees",)


def test_count_method_override_reclassifies_context_fields_without_mutating_defaults() -> None:
    default_set = get_profile_set("agriculture")
    component_agriculture = resolve_profile_set(
        "agriculture",
        profile_id="outdoor_agriculture",
        count_method_override=CountMethod.POPULATION_SUBCOMPONENT,
    ).profiles[0]

    default_agriculture = next(
        profile
        for profile in default_set.profiles
        if profile.profile_id == "outdoor_agriculture"
    )
    assert default_agriculture.count_method == CountMethod.DIRECT_COUNT
    assert default_agriculture.component_count_fields == ()
    assert component_agriculture.count_method == CountMethod.POPULATION_SUBCOMPONENT
    assert component_agriculture.component_count_fields == ("Employees", "acres")

from __future__ import annotations

from pathlib import Path

from pdt_observer.models import BuildingProfileSet, CountMethod
from pdt_observer.profiles import get_profile_set, narrow_profile_set, resolve_profile_set


def test_public_venue_profile_set_loads_from_builtin() -> None:
    profile_set = get_profile_set("public_venues")

    assert profile_set.profile_set_id == "public_venues"
    assert [profile.profile_id for profile in profile_set.profiles] == [
        "restaurants_bars",
        "schools_childcare",
        "hospitals_care",
        "hotels_lodging",
        "retail_events",
    ]


def test_public_venue_profile_json_matches_model() -> None:
    profile_set = BuildingProfileSet.model_validate_json(
        Path("profiles/public_venues.json").read_text(encoding="utf-8")
    )

    assert profile_set.profile_set_id == "public_venues"
    assert all(profile.enabled for profile in profile_set.profiles)


def test_builtin_public_venue_profiles_match_json() -> None:
    builtin = get_profile_set("public_venues")
    from_json = BuildingProfileSet.model_validate_json(
        Path("profiles/public_venues.json").read_text(encoding="utf-8")
    )

    assert builtin == from_json


def test_commercial_business_profile_set_loads_from_builtin() -> None:
    profile_set = get_profile_set("commercial_business")

    assert profile_set.profile_set_id == "commercial_business"
    assert [profile.profile_id for profile in profile_set.profiles] == [
        "malls_retail_markets",
        "offices_bpo_call_centers",
        "factories_warehouses",
        "hotels_restaurants",
    ]


def test_residential_profile_set_loads_from_builtin() -> None:
    profile_set = get_profile_set("residential")

    assert profile_set.profile_set_id == "residential"
    assert [profile.profile_id for profile in profile_set.profiles] == [
        "apartments_condominiums",
        "houses_informal_settlements",
    ]


def test_pdt_facility_type_profile_sets_load_from_builtin() -> None:
    schools = get_profile_set("schools")
    manufacturing = get_profile_set("manufacturing")
    restaurants = get_profile_set("restaurants")

    assert [profile.profile_id for profile in schools.profiles] == [
        "primary_secondary_education",
        "university_college",
        "university_library",
    ]
    assert [profile.profile_id for profile in manufacturing.profiles] == [
        "light_manufacturing",
        "heavy_manufacturing",
        "chemical_refining_cement",
        "heat_processing",
        "power_plants",
        "warehouses",
    ]
    assert [profile.profile_id for profile in restaurants.profiles] == [
        "full_service_restaurants",
        "quick_service_restaurants",
        "bars_nightlife",
    ]


def test_builtin_commercial_business_profiles_match_json() -> None:
    builtin = get_profile_set("commercial_business")
    from_json = BuildingProfileSet.model_validate_json(
        Path("profiles/commercial_business.json").read_text(encoding="utf-8")
    )

    assert builtin == from_json


def test_builtin_residential_profiles_match_json() -> None:
    builtin = get_profile_set("residential")
    from_json = BuildingProfileSet.model_validate_json(
        Path("profiles/residential.json").read_text(encoding="utf-8")
    )

    assert builtin == from_json


def test_builtin_pdt_facility_type_profiles_match_json() -> None:
    for profile_set_id in (
        "schools",
        "manufacturing",
        "restaurants",
        "retail_service",
        "public_institutional",
        "transportation",
        "recreation_entertainment",
        "agriculture",
        "pdt_residential",
    ):
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


def test_profile_set_can_be_narrowed_to_one_profile() -> None:
    profile_set = get_profile_set("commercial_business")

    narrowed = narrow_profile_set(profile_set, "factories_warehouses")

    assert narrowed.profile_set_id == "commercial_business"
    assert [profile.profile_id for profile in narrowed.profiles] == ["factories_warehouses"]


def test_legacy_philippines_commercial_business_name_aliases_generic_profile_set() -> None:
    assert get_profile_set("philippines_commercial_business") == get_profile_set(
        "commercial_business"
    )


def test_commercial_business_profiles_include_facility_specific_proxy_phrases() -> None:
    profile_set = get_profile_set("commercial_business")
    offices = next(
        profile
        for profile in profile_set.profiles
        if profile.profile_id == "offices_bpo_call_centers"
    )
    factories = next(
        profile for profile in profile_set.profiles if profile.profile_id == "factories_warehouses"
    )

    assert "call center agents were evacuated" in offices.positive_evidence_patterns
    assert "BPO" in offices.venue_aliases
    assert "workers were trapped" in factories.positive_evidence_patterns
    assert "workforce size" in factories.negative_evidence_patterns


def test_residential_profiles_include_residential_proxy_phrases() -> None:
    profile_set = get_profile_set("residential")
    apartments = next(
        profile
        for profile in profile_set.profiles
        if profile.profile_id == "apartments_condominiums"
    )
    houses = next(
        profile
        for profile in profile_set.profiles
        if profile.profile_id == "houses_informal_settlements"
    )

    assert "residents were evacuated" in apartments.positive_evidence_patterns
    assert "condo" in apartments.venue_aliases
    assert "families displaced" in houses.positive_evidence_patterns
    assert "population" in houses.negative_evidence_patterns


def test_pdt_facility_type_profiles_include_subtype_guidance() -> None:
    schools = get_profile_set("schools")
    manufacturing = get_profile_set("manufacturing")
    restaurants = get_profile_set("restaurants")
    university = next(
        profile for profile in schools.profiles if profile.profile_id == "university_college"
    )
    heavy = next(
        profile for profile in manufacturing.profiles if profile.profile_id == "heavy_manufacturing"
    )
    quick_service = next(
        profile
        for profile in restaurants.profiles
        if profile.profile_id == "quick_service_restaurants"
    )

    assert "students were evacuated" in university.positive_evidence_patterns
    assert "campus population" in university.negative_evidence_patterns
    assert "workers were trapped" in heavy.positive_evidence_patterns
    assert "workforce size" in heavy.negative_evidence_patterns
    assert "employees were evacuated" in quick_service.positive_evidence_patterns
    assert "fast-food restaurant" in quick_service.venue_aliases


def test_pdt_facility_type_profiles_include_occurrence_guidance() -> None:
    schools = get_profile_set("schools")
    manufacturing = get_profile_set("manufacturing")
    retail = get_profile_set("retail_service")
    institutional = get_profile_set("public_institutional")
    transportation = get_profile_set("transportation")
    recreation = get_profile_set("recreation_entertainment")
    agriculture = get_profile_set("agriculture")
    residential = get_profile_set("pdt_residential")

    assert schools.profiles[0].pdt_subtype == "School (D-12)"
    assert "students" in schools.profiles[0].occupancy_groups
    assert "workforce size" in manufacturing.profiles[0].contextual_count_fields
    assert "shoppers" in retail.profiles[0].occupancy_groups
    assert "patients" in next(
        profile
        for profile in institutional.profiles
        if profile.profile_id == "hospitals_with_beds"
    ).occupancy_groups
    assert "passengers" in transportation.profiles[0].occupancy_groups
    assert "official_event_attendance" in [
        strategy.value for strategy in recreation.profiles[1].preferred_strategy_ids
    ]
    assert "workers" in agriculture.profiles[0].occupancy_groups
    assert "average household size" in residential.profiles[0].contextual_count_fields


def test_csv_mapped_profiles_expose_count_methods_and_component_fields() -> None:
    schools = get_profile_set("schools")
    manufacturing = get_profile_set("manufacturing")
    institutional = get_profile_set("public_institutional")
    recreation = get_profile_set("recreation_entertainment")
    residential = get_profile_set("pdt_residential")

    school = schools.profiles[0]
    factory = manufacturing.profiles[0]
    hospital = next(
        profile
        for profile in institutional.profiles
        if profile.profile_id == "hospitals_with_beds"
    )
    theater = next(profile for profile in recreation.profiles if profile.profile_id == "theaters")

    assert school.count_method == CountMethod.POPULATION_SUBCOMPONENT
    assert "Students" in school.component_count_fields
    assert "school attendace rate" in school.component_count_fields
    assert "Students" in school.regional_stat_fields
    assert factory.count_method == CountMethod.POPULATION_SUBCOMPONENT
    assert "Employees" in factory.component_count_fields
    assert hospital.count_method == CountMethod.POPULATION_SUBCOMPONENT
    assert "bed occupancy rate" in hospital.component_count_fields
    assert theater.count_method == CountMethod.DIRECT_COUNT
    assert theater.component_count_fields == ()
    assert residential.profiles[0].regional_stat_fields


def test_count_method_override_reclassifies_component_fields_without_mutating_defaults() -> None:
    default_set = get_profile_set("public_institutional")
    direct_hospital = resolve_profile_set(
        "public_institutional",
        profile_id="hospitals_with_beds",
        count_method_override=CountMethod.DIRECT_COUNT,
    ).profiles[0]
    component_theater = resolve_profile_set(
        "recreation_entertainment",
        profile_id="theaters",
        count_method_override=CountMethod.POPULATION_SUBCOMPONENT,
    ).profiles[0]

    default_hospital = next(
        profile
        for profile in default_set.profiles
        if profile.profile_id == "hospitals_with_beds"
    )
    assert default_hospital.count_method == CountMethod.POPULATION_SUBCOMPONENT
    assert direct_hospital.count_method == CountMethod.DIRECT_COUNT
    assert direct_hospital.component_count_fields == ()
    assert "bed occupancy rate" in direct_hospital.contextual_count_fields
    assert component_theater.count_method == CountMethod.POPULATION_SUBCOMPONENT
    assert "seating capacity" in component_theater.component_count_fields


def test_public_venue_profiles_include_evidence_first_phrases() -> None:
    profile_set = get_profile_set("public_venues")
    restaurants = next(
        profile for profile in profile_set.profiles if profile.profile_id == "restaurants_bars"
    )

    assert "people were inside" in restaurants.positive_evidence_patterns
    assert "customers were inside" in restaurants.source_search_prompt
    assert "inside the restaurant when" in restaurants.positive_evidence_patterns


def test_public_venue_profiles_include_source_type_guidance() -> None:
    profile_set = get_profile_set("public_venues")

    for profile in profile_set.profiles:
        assert "local or national news article" in profile.preferred_source_types
        assert "official venue, organizer, or event attendance announcement" in (
            profile.preferred_source_types
        )
        assert "Wikipedia or encyclopedia page" in profile.context_only_source_types
        assert "context only" in profile.source_search_prompt

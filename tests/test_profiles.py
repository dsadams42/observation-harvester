from __future__ import annotations

from pathlib import Path

from pdt_observer.models import BuildingProfileSet
from pdt_observer.profiles import get_profile_set, narrow_profile_set


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
    ]
    assert [profile.profile_id for profile in manufacturing.profiles] == [
        "light_manufacturing",
        "heavy_manufacturing",
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
    for profile_set_id in ("schools", "manufacturing", "restaurants"):
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

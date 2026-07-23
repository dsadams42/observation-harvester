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

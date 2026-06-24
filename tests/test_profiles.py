from __future__ import annotations

from pathlib import Path

from pdt_observer.models import BuildingProfileSet
from pdt_observer.profiles import get_profile_set


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

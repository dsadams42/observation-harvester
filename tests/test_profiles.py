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

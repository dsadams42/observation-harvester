from __future__ import annotations

import json
from pathlib import Path

from pdt_observer.models import BuildingProfileSet, BuildingTypeProfile

PUBLIC_VENUE_PROFILES = BuildingProfileSet(
    profile_set_id="public_venues",
    label="Public venues",
    profiles=(
        BuildingTypeProfile(
            profile_id="restaurants_bars",
            label="Restaurants and bars",
            source_search_prompt=(
                "Find incident reports mentioning an explicit count of people physically "
                "inside a restaurant, bar, cafe, diner, brewery, or nightclub."
            ),
            positive_evidence_patterns=("people inside", "patrons inside", "customers inside"),
            negative_evidence_patterns=("capacity", "seats", "address", "injured"),
            venue_aliases=("restaurant", "bar", "cafe", "diner", "brewery", "nightclub"),
            priority=10,
        ),
        BuildingTypeProfile(
            profile_id="schools_childcare",
            label="Schools and childcare",
            source_search_prompt=(
                "Find incident reports mentioning an explicit count of people physically "
                "inside a school, daycare, childcare center, preschool, or campus building."
            ),
            positive_evidence_patterns=("students inside", "people inside", "children inside"),
            negative_evidence_patterns=("enrollment", "graduation year", "address"),
            venue_aliases=("school", "daycare", "childcare", "preschool", "campus"),
            priority=20,
        ),
        BuildingTypeProfile(
            profile_id="hospitals_care",
            label="Hospitals and care facilities",
            source_search_prompt=(
                "Find incident reports mentioning an explicit count of people physically "
                "inside a hospital, clinic, nursing home, assisted living facility, or care home."
            ),
            positive_evidence_patterns=("patients inside", "residents inside", "people inside"),
            negative_evidence_patterns=("beds", "staffed beds", "cost", "capacity"),
            venue_aliases=("hospital", "clinic", "nursing home", "assisted living", "care home"),
            priority=30,
        ),
        BuildingTypeProfile(
            profile_id="hotels_lodging",
            label="Hotels and lodging",
            source_search_prompt=(
                "Find incident reports mentioning an explicit count of people physically "
                "inside a hotel, motel, inn, shelter, or lodging property."
            ),
            positive_evidence_patterns=("guests inside", "people inside", "occupants inside"),
            negative_evidence_patterns=("rooms", "room rate", "address", "built in"),
            venue_aliases=("hotel", "motel", "inn", "shelter", "lodge"),
            priority=40,
        ),
        BuildingTypeProfile(
            profile_id="retail_events",
            label="Retail and event venues",
            source_search_prompt=(
                "Find incident reports mentioning an explicit count of people physically "
                "inside a store, mall, market, theater, hall, arena, or event venue."
            ),
            positive_evidence_patterns=("people inside", "shoppers inside", "attendees inside"),
            negative_evidence_patterns=("capacity", "tickets sold", "construction cost"),
            venue_aliases=("store", "mall", "market", "theater", "hall", "arena", "venue"),
            priority=50,
        ),
    ),
)


def get_profile_set(name: str) -> BuildingProfileSet:
    if name == PUBLIC_VENUE_PROFILES.profile_set_id:
        return PUBLIC_VENUE_PROFILES
    path = Path(name)
    if path.is_file():
        return BuildingProfileSet.model_validate_json(path.read_text(encoding="utf-8"))
    raise ValueError(f"unknown profile set: {name}")


def write_profile_set(profile_set: BuildingProfileSet, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(profile_set.model_dump_json(indent=2), encoding="utf-8")


def profile_set_to_json(profile_set: BuildingProfileSet) -> str:
    return json.dumps(profile_set.model_dump(mode="json"), indent=2)

from __future__ import annotations

import json
from pathlib import Path

from pdt_observer.models import BuildingProfileSet, BuildingTypeProfile

_PREFERRED_SOURCE_TYPES = (
    "local or national news article",
    "wire-service article",
    "official emergency or public-safety incident report",
    "official government or regulator enforcement report",
    "official venue, organizer, or event attendance announcement",
    "official press release with a count-bearing event or incident detail",
)

_CONTEXT_ONLY_SOURCE_TYPES = (
    "Wikipedia or encyclopedia page",
    "generic directory, travel guide, listicle, or map listing",
    "venue marketing or about page without a count-bearing event or incident",
    "capacity, seating-chart, annual-report, or statistics page",
    "social media repost without an original authoritative source",
)

PUBLIC_VENUE_PROFILES = BuildingProfileSet(
    profile_set_id="public_venues",
    label="Public venues",
    profiles=(
        BuildingTypeProfile(
            profile_id="restaurants_bars",
            label="Restaurants and bars",
            source_search_prompt=(
                "Find incident reports with quoted count-bearing phrases such as "
                '"people were inside", "customers were inside", "patrons were inside", '
                '"people were evacuated", or "inside the restaurant when" for a restaurant, '
                "bar, cafe, diner, brewery, or nightclub. Prioritize news articles, official "
                "reports, and official attendance announcements; treat encyclopedia, directory, "
                "map, and generic venue pages as context only."
            ),
            preferred_source_types=_PREFERRED_SOURCE_TYPES,
            context_only_source_types=_CONTEXT_ONLY_SOURCE_TYPES,
            positive_evidence_patterns=(
                "people were inside",
                "people were present",
                "customers were inside",
                "patrons were inside",
                "guests were inside",
                "people were evacuated",
                "customers were evacuated",
                "people were rescued",
                "inside the restaurant when",
                "inside the bar when",
                "people inside",
                "patrons inside",
                "customers inside",
            ),
            negative_evidence_patterns=("capacity", "seats", "address", "injured"),
            venue_aliases=("restaurant", "bar", "cafe", "diner", "brewery", "nightclub"),
            priority=10,
        ),
        BuildingTypeProfile(
            profile_id="schools_childcare",
            label="Schools and childcare",
            source_search_prompt=(
                "Find incident reports with quoted count-bearing phrases such as "
                '"students were inside", "children were inside", "people were evacuated", '
                '"students were rescued", or "inside the school when" for a school, daycare, '
                "childcare center, preschool, or campus building. Prioritize news articles, "
                "official reports, and official attendance announcements; treat encyclopedia, "
                "directory, map, and generic venue pages as context only."
            ),
            preferred_source_types=_PREFERRED_SOURCE_TYPES,
            context_only_source_types=_CONTEXT_ONLY_SOURCE_TYPES,
            positive_evidence_patterns=(
                "students were inside",
                "children were inside",
                "people were evacuated",
                "students were evacuated",
                "students were rescued",
                "inside the school when",
                "students inside",
                "people inside",
                "children inside",
            ),
            negative_evidence_patterns=("enrollment", "graduation year", "address"),
            venue_aliases=("school", "daycare", "childcare", "preschool", "campus"),
            priority=20,
        ),
        BuildingTypeProfile(
            profile_id="hospitals_care",
            label="Hospitals and care facilities",
            source_search_prompt=(
                "Find incident reports with quoted count-bearing phrases such as "
                '"patients were inside", "residents were inside", "people were evacuated", '
                '"patients were rescued", or "inside the hospital when" for a hospital, '
                "clinic, nursing home, assisted living facility, or care home. Prioritize news "
                "articles, official reports, and official attendance announcements; treat "
                "encyclopedia, directory, map, and generic venue pages as context only."
            ),
            preferred_source_types=_PREFERRED_SOURCE_TYPES,
            context_only_source_types=_CONTEXT_ONLY_SOURCE_TYPES,
            positive_evidence_patterns=(
                "patients were inside",
                "residents were inside",
                "people were evacuated",
                "patients were evacuated",
                "patients were rescued",
                "inside the hospital when",
                "patients inside",
                "residents inside",
                "people inside",
            ),
            negative_evidence_patterns=("beds", "staffed beds", "cost", "capacity"),
            venue_aliases=("hospital", "clinic", "nursing home", "assisted living", "care home"),
            priority=30,
        ),
        BuildingTypeProfile(
            profile_id="hotels_lodging",
            label="Hotels and lodging",
            source_search_prompt=(
                "Find incident reports with quoted count-bearing phrases such as "
                '"guests were inside", "occupants were inside", "people were evacuated", '
                '"guests were rescued", or "inside the hotel when" for a hotel, motel, inn, '
                "shelter, or lodging property. Prioritize news articles, official reports, and "
                "official attendance announcements; treat encyclopedia, directory, map, and "
                "generic venue pages as context only."
            ),
            preferred_source_types=_PREFERRED_SOURCE_TYPES,
            context_only_source_types=_CONTEXT_ONLY_SOURCE_TYPES,
            positive_evidence_patterns=(
                "guests were inside",
                "occupants were inside",
                "people were evacuated",
                "guests were evacuated",
                "guests were rescued",
                "inside the hotel when",
                "guests inside",
                "people inside",
                "occupants inside",
            ),
            negative_evidence_patterns=("rooms", "room rate", "address", "built in"),
            venue_aliases=("hotel", "motel", "inn", "shelter", "lodge"),
            priority=40,
        ),
        BuildingTypeProfile(
            profile_id="retail_events",
            label="Retail and event venues",
            source_search_prompt=(
                "Find incident reports with quoted count-bearing phrases such as "
                '"people were inside", "shoppers were inside", "attendees were inside", '
                '"people were evacuated", or "inside the mall when" for a store, mall, '
                "market, theater, hall, arena, or event venue. Prioritize news articles, official "
                "reports, and official attendance announcements; treat encyclopedia, directory, "
                "map, and generic venue pages as context only."
            ),
            preferred_source_types=_PREFERRED_SOURCE_TYPES,
            context_only_source_types=_CONTEXT_ONLY_SOURCE_TYPES,
            positive_evidence_patterns=(
                "people were inside",
                "shoppers were inside",
                "attendees were inside",
                "people were evacuated",
                "shoppers were evacuated",
                "people were rescued",
                "inside the mall when",
                "people inside",
                "shoppers inside",
                "attendees inside",
            ),
            negative_evidence_patterns=("capacity", "tickets sold", "construction cost"),
            venue_aliases=("store", "mall", "market", "theater", "hall", "arena", "venue"),
            priority=50,
        ),
    ),
)

COMMERCIAL_BUSINESS_PROFILES = BuildingProfileSet.model_validate_json(
    (Path(__file__).resolve().parents[2] / "profiles" / "commercial_business.json").read_text(
        encoding="utf-8"
    )
)

BUILTIN_PROFILE_SETS = {
    PUBLIC_VENUE_PROFILES.profile_set_id: PUBLIC_VENUE_PROFILES,
    COMMERCIAL_BUSINESS_PROFILES.profile_set_id: COMMERCIAL_BUSINESS_PROFILES,
    "philippines_commercial_business": COMMERCIAL_BUSINESS_PROFILES,
}


def get_profile_set(name: str) -> BuildingProfileSet:
    if name in BUILTIN_PROFILE_SETS:
        return BUILTIN_PROFILE_SETS[name]
    path = Path(name)
    if path.is_file():
        return BuildingProfileSet.model_validate_json(path.read_text(encoding="utf-8"))
    raise ValueError(f"unknown profile set: {name}")


def get_builtin_profile(profile_id: str) -> BuildingTypeProfile:
    for profile_set in BUILTIN_PROFILE_SETS.values():
        for profile in profile_set.profiles:
            if profile.profile_id == profile_id:
                return profile
    raise ValueError(f"unknown builtin profile: {profile_id}")


def write_profile_set(profile_set: BuildingProfileSet, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(profile_set.model_dump_json(indent=2), encoding="utf-8")


def profile_set_to_json(profile_set: BuildingProfileSet) -> str:
    return json.dumps(profile_set.model_dump(mode="json"), indent=2)

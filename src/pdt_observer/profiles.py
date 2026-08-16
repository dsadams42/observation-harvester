from __future__ import annotations

import json
from pathlib import Path

from pdt_observer.models import BuildingProfileSet, BuildingTypeProfile, CountMethod
from pdt_observer.storage import write_json_file

_REPO_ROOT = Path(__file__).resolve().parents[2]

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

def _load_profile_set(filename: str) -> BuildingProfileSet:
    return BuildingProfileSet.model_validate_json(
        (_REPO_ROOT / "profiles" / filename).read_text(encoding="utf-8")
    )


COMMERCIAL_BUSINESS_PROFILES = _load_profile_set("commercial_business.json")
RESIDENTIAL_PROFILES = _load_profile_set("residential.json")
SCHOOLS_PROFILES = _load_profile_set("schools.json")
MANUFACTURING_PROFILES = _load_profile_set("manufacturing.json")
RESTAURANTS_PROFILES = _load_profile_set("restaurants.json")
RETAIL_SERVICE_PROFILES = _load_profile_set("retail_service.json")
PUBLIC_INSTITUTIONAL_PROFILES = _load_profile_set("public_institutional.json")
TRANSPORTATION_PROFILES = _load_profile_set("transportation.json")
RECREATION_ENTERTAINMENT_PROFILES = _load_profile_set("recreation_entertainment.json")
AGRICULTURE_PROFILES = _load_profile_set("agriculture.json")
PDT_RESIDENTIAL_PROFILES = _load_profile_set("pdt_residential.json")

BUILTIN_PROFILE_SETS = {
    SCHOOLS_PROFILES.profile_set_id: SCHOOLS_PROFILES,
    MANUFACTURING_PROFILES.profile_set_id: MANUFACTURING_PROFILES,
    RESTAURANTS_PROFILES.profile_set_id: RESTAURANTS_PROFILES,
    RETAIL_SERVICE_PROFILES.profile_set_id: RETAIL_SERVICE_PROFILES,
    PUBLIC_INSTITUTIONAL_PROFILES.profile_set_id: PUBLIC_INSTITUTIONAL_PROFILES,
    TRANSPORTATION_PROFILES.profile_set_id: TRANSPORTATION_PROFILES,
    RECREATION_ENTERTAINMENT_PROFILES.profile_set_id: RECREATION_ENTERTAINMENT_PROFILES,
    AGRICULTURE_PROFILES.profile_set_id: AGRICULTURE_PROFILES,
    PDT_RESIDENTIAL_PROFILES.profile_set_id: PDT_RESIDENTIAL_PROFILES,
    PUBLIC_VENUE_PROFILES.profile_set_id: PUBLIC_VENUE_PROFILES,
    COMMERCIAL_BUSINESS_PROFILES.profile_set_id: COMMERCIAL_BUSINESS_PROFILES,
    RESIDENTIAL_PROFILES.profile_set_id: RESIDENTIAL_PROFILES,
    "philippines_commercial_business": COMMERCIAL_BUSINESS_PROFILES,
}


def get_profile_set(name: str) -> BuildingProfileSet:
    if name in BUILTIN_PROFILE_SETS:
        return BUILTIN_PROFILE_SETS[name]
    path = Path(name)
    if path.is_file():
        return BuildingProfileSet.model_validate_json(path.read_text(encoding="utf-8"))
    raise ValueError(f"unknown profile set: {name}")


def narrow_profile_set(profile_set: BuildingProfileSet, profile_id: str) -> BuildingProfileSet:
    matching_profiles = tuple(
        profile for profile in profile_set.profiles if profile.profile_id == profile_id
    )
    if not matching_profiles:
        raise ValueError(
            f"profile {profile_id!r} not found in profile set {profile_set.profile_set_id!r}"
        )
    return profile_set.model_copy(update={"profiles": matching_profiles})


def _unique_values(*groups: tuple[str, ...]) -> tuple[str, ...]:
    values: list[str] = []
    for group in groups:
        for value in group:
            if value not in values:
                values.append(value)
    return tuple(values)


def _profile_with_count_method_override(
    profile: BuildingTypeProfile,
    count_method: CountMethod,
) -> BuildingTypeProfile:
    if count_method == profile.count_method:
        return profile

    if count_method == CountMethod.DIRECT_COUNT:
        return profile.model_copy(
            update={
                "count_method": count_method,
                "contextual_count_fields": _unique_values(
                    profile.contextual_count_fields,
                    profile.component_count_fields,
                    profile.regional_stat_fields,
                ),
                "component_count_fields": (),
                "regional_stat_fields": (),
                "component_source_guidance": None,
            }
        )

    component_fields = profile.component_count_fields
    if not component_fields:
        component_fields = profile.contextual_count_fields
    guidance = profile.component_source_guidance
    if guidance is None and profile.contextual_count_fields:
        guidance = (
            "Analyst override: treat configured context-only count fields as candidate "
            "population component inputs for this run; keep them role-labeled and do not "
            "derive final occupancy estimates."
        )
    return profile.model_copy(
        update={
            "count_method": count_method,
            "component_count_fields": component_fields,
            "component_source_guidance": guidance,
        }
    )


def apply_count_method_override(
    profile_set: BuildingProfileSet,
    count_method: CountMethod | None,
) -> BuildingProfileSet:
    if count_method is None:
        return profile_set
    return profile_set.model_copy(
        update={
            "profiles": tuple(
                _profile_with_count_method_override(profile, count_method)
                for profile in profile_set.profiles
            )
        }
    )


def resolve_profile_set(
    name: str,
    *,
    profile_id: str | None = None,
    count_method_override: CountMethod | None = None,
) -> BuildingProfileSet:
    profile_set = get_profile_set(name)
    if profile_id is not None:
        profile_set = narrow_profile_set(profile_set, profile_id)
    return apply_count_method_override(profile_set, count_method_override)


def get_builtin_profile(profile_id: str) -> BuildingTypeProfile:
    for profile_set in BUILTIN_PROFILE_SETS.values():
        for profile in profile_set.profiles:
            if profile.profile_id == profile_id:
                return profile
    raise ValueError(f"unknown builtin profile: {profile_id}")


def write_profile_set(profile_set: BuildingProfileSet, path: Path) -> None:
    write_json_file(path, profile_set.model_dump(mode="json"))


def profile_set_to_json(profile_set: BuildingProfileSet) -> str:
    return json.dumps(profile_set.model_dump(mode="json"), indent=2)

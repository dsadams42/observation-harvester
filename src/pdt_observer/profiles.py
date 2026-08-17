from __future__ import annotations

import json
from pathlib import Path

from pdt_observer.models import BuildingProfileSet, BuildingTypeProfile, CountMethod
from pdt_observer.storage import write_json_file

_REPO_ROOT = Path(__file__).resolve().parents[2]

def _load_profile_set(filename: str) -> BuildingProfileSet:
    return BuildingProfileSet.model_validate_json(
        (_REPO_ROOT / "profiles" / filename).read_text(encoding="utf-8")
    )


RESIDENTIAL_PROFILES = _load_profile_set("residential.json")
INSTITUTIONS_PUBLIC_SERVICE_PROFILES = _load_profile_set(
    "institutions_public_service.json"
)
RETAIL_SERVICE_PROFILES = _load_profile_set("retail_service.json")
COMMERCIAL_PROFILES = _load_profile_set("commercial.json")
TRANSPORTATION_PROFILES = _load_profile_set("transportation.json")
MILITARY_FACILITY_PROFILES = _load_profile_set("military_facility.json")
RECREATION_ENTERTAINMENT_PROFILES = _load_profile_set("recreation_entertainment.json")
AGRICULTURE_PROFILES = _load_profile_set("agriculture.json")

_PROFILE_ID_ALIASES: dict[tuple[str, str], str] = {
    ("commercial", "factories_warehouses"): "light_manufacturing",
    ("commercial", "manufacturing_facilities"): "light_manufacturing",
    ("commercial", "offices_bpo_call_centers"): "office_building",
    ("commercial", "power_plants"): "powerplants",
    ("commercial_business", "factories_warehouses"): "manufacturing_facilities",
    ("commercial_business", "hotels_restaurants"): "hotels_motels_hospitality",
    ("institutions_public_service", "primary_secondary_education"): "school_d_12",
    ("institutions_public_service", "schools_childcare"): "school_d_12",
    ("institutions_public_service", "university_college"): "university",
    ("institutions_public_service", "public_libraries"): "public_library",
    ("institutions_public_service", "hospitals_with_beds"): "hospital_clinic_with_beds",
    (
        "institutions_public_service",
        "clinics_without_beds",
    ): "hospital_clinic_without_beds",
    ("institutions_public_service", "hospitals_care"): "hospital_clinic_with_beds",
    ("institutions_public_service", "public_services"): "public_service",
    ("public_venues", "hospitals_care"): "hospitals_with_beds",
    ("public_venues", "retail_events"): "retail_markets",
    ("recreation_entertainment", "theaters_events"): "theater",
    ("recreation_entertainment", "theaters"): "theater",
    ("residential", "apartments_multi_family"): "multi_family",
    ("residential", "detached_houses"): "single_family",
    ("residential", "informal_settlements"): "slum",
    ("retail_service", "full_service_restaurants"): "restaurant",
    ("retail_service", "quick_service_restaurants"): "restaurant",
    ("retail_service", "restaurants_bars"): "restaurant",
    ("retail_service", "restaurants_hospitality"): "restaurant",
    ("retail_service", "hotels_lodging"): "hotel_motel",
    ("retail_service", "hotels_motels"): "hotel_motel",
    ("retail_service", "hotels_motels_hospitality"): "hotel_motel",
    ("retail_service", "malls_retail_markets"): "stores",
    ("retail_service", "retail_markets"): "stores",
}

_PROFILE_SET_ALIASES: dict[str, str] = {
}

BUILTIN_PROFILE_SETS = {
    RESIDENTIAL_PROFILES.profile_set_id: RESIDENTIAL_PROFILES,
    INSTITUTIONS_PUBLIC_SERVICE_PROFILES.profile_set_id: (
        INSTITUTIONS_PUBLIC_SERVICE_PROFILES
    ),
    RETAIL_SERVICE_PROFILES.profile_set_id: RETAIL_SERVICE_PROFILES,
    COMMERCIAL_PROFILES.profile_set_id: COMMERCIAL_PROFILES,
    TRANSPORTATION_PROFILES.profile_set_id: TRANSPORTATION_PROFILES,
    MILITARY_FACILITY_PROFILES.profile_set_id: MILITARY_FACILITY_PROFILES,
    RECREATION_ENTERTAINMENT_PROFILES.profile_set_id: RECREATION_ENTERTAINMENT_PROFILES,
    AGRICULTURE_PROFILES.profile_set_id: AGRICULTURE_PROFILES,
}


def _canonical_profile(profile_set_id: str, profile_id: str) -> BuildingTypeProfile:
    profile_set = BUILTIN_PROFILE_SETS[profile_set_id]
    return next(profile for profile in profile_set.profiles if profile.profile_id == profile_id)


def _unique_values(*groups: tuple[str, ...]) -> tuple[str, ...]:
    values: list[str] = []
    for group in groups:
        for value in group:
            if value not in values:
                values.append(value)
    return tuple(values)


def _compat_profile(
    profile_set_id: str,
    canonical_profile_id: str,
    legacy_profile_id: str,
    label: str,
    *,
    aliases: tuple[str, ...] = (),
    priority: int | None = None,
) -> BuildingTypeProfile:
    profile = _canonical_profile(profile_set_id, canonical_profile_id)
    update: dict[str, object] = {
        "profile_id": legacy_profile_id,
        "label": label,
        "venue_aliases": _unique_values((label,), aliases, profile.venue_aliases),
    }
    if priority is not None:
        update["priority"] = priority
    return profile.model_copy(update=update)


COMPATIBILITY_PROFILE_SETS = {
    "schools": BuildingProfileSet(
        profile_set_id="schools",
        label="Schools",
        profiles=(
            _compat_profile(
                "institutions_public_service",
                "school_d_12",
                "primary_secondary_education",
                "Primary and secondary education",
                aliases=("school", "K-12", "childcare"),
            ),
            _compat_profile(
                "institutions_public_service",
                "university",
                "university_college",
                "University and college",
                aliases=("college", "campus"),
            ),
            _compat_profile(
                "institutions_public_service",
                "university_library",
                "university_library",
                "University library",
            ),
        ),
    ),
    "manufacturing": BuildingProfileSet(
        profile_set_id="manufacturing",
        label="Manufacturing",
        profiles=(
            _compat_profile(
                "commercial",
                "light_manufacturing",
                "light_manufacturing",
                "Light manufacturing",
            ),
            _compat_profile(
                "commercial",
                "heavy_manufacturing",
                "heavy_manufacturing",
                "Heavy manufacturing",
            ),
            _compat_profile(
                "commercial",
                "chemical_refining_cement",
                "chemical_refining_cement",
                "Chemical, refining, and cement",
            ),
            _compat_profile(
                "commercial",
                "heat_processing",
                "heat_processing",
                "Heat processing",
            ),
            _compat_profile("commercial", "powerplants", "power_plants", "Power plants"),
            _compat_profile("transportation", "warehouse", "warehouses", "Warehouses"),
        ),
    ),
    "restaurants": BuildingProfileSet(
        profile_set_id="restaurants",
        label="Restaurants",
        profiles=(
            _compat_profile(
                "retail_service",
                "restaurant",
                "full_service_restaurants",
                "Full-service restaurants",
            ),
            _compat_profile(
                "retail_service",
                "restaurant",
                "quick_service_restaurants",
                "Quick-service restaurants",
                aliases=("fast-food restaurant",),
            ),
            _compat_profile(
                "recreation_entertainment",
                "night_club",
                "bars_nightlife",
                "Bars and nightlife",
            ),
        ),
    ),
    "retail_service": RETAIL_SERVICE_PROFILES,
    "public_institutional": BuildingProfileSet(
        profile_set_id="public_institutional",
        label="Public / institutional",
        profiles=(
            _compat_profile(
                "institutions_public_service",
                "religious",
                "religious",
                "Religious",
            ),
            _compat_profile(
                "institutions_public_service",
                "museum_urban",
                "museums",
                "Museums",
            ),
            _compat_profile(
                "institutions_public_service",
                "public_library",
                "public_libraries",
                "Public libraries",
            ),
            _compat_profile(
                "institutions_public_service",
                "hospital_clinic_with_beds",
                "hospitals_with_beds",
                "Hospitals/clinics with beds",
            ),
            _compat_profile(
                "institutions_public_service",
                "hospital_clinic_without_beds",
                "clinics_without_beds",
                "Hospitals/clinics without beds",
            ),
            _compat_profile(
                "institutions_public_service",
                "public_service",
                "public_services",
                "Public services",
            ),
            _compat_profile(
                "institutions_public_service",
                "police_stations",
                "police_fire_courts_prisons",
                "Police, fire, courts, and prisons",
            ),
            _compat_profile(
                "institutions_public_service",
                "civil_protection_shelters",
                "civil_protection_shelters",
                "Civil protection shelters",
            ),
        ),
    ),
    "pdt_residential": BuildingProfileSet(
        profile_set_id="pdt_residential",
        label="PDT Residential",
        profiles=(
            _compat_profile(
                "residential",
                "single_family_urban",
                "single_family_urban",
                "Single-family urban",
            ),
            _compat_profile(
                "residential",
                "multi_family_urban",
                "multi_family_urban",
                "Multi-family urban",
            ),
            _compat_profile("residential", "slum", "slum", "Slum"),
            _compat_profile(
                "residential",
                "refugee_settlement",
                "refugee_settlement",
                "Refugee settlement",
            ),
        ),
    ),
    "public_venues": BuildingProfileSet(
        profile_set_id="public_venues",
        label="Public venues",
        profiles=(
            _compat_profile(
                "retail_service",
                "restaurant",
                "restaurants_bars",
                "Restaurants and bars",
            ),
            _compat_profile(
                "institutions_public_service",
                "school_d_12",
                "schools_childcare",
                "Schools and childcare",
            ),
            _compat_profile(
                "institutions_public_service",
                "hospital_clinic_with_beds",
                "hospitals_with_beds",
                "Hospitals with beds",
            ),
            _compat_profile(
                "institutions_public_service",
                "hospital_clinic_without_beds",
                "clinics_without_beds",
                "Clinics without beds",
            ),
            _compat_profile(
                "retail_service",
                "hotel_motel",
                "hotels_lodging",
                "Hotels and lodging",
            ),
            _compat_profile("retail_service", "stores", "retail_markets", "Retail markets"),
            _compat_profile(
                "recreation_entertainment",
                "theater",
                "theaters_events",
                "Theaters and events",
            ),
        ),
    ),
    "commercial_business": BuildingProfileSet(
        profile_set_id="commercial_business",
        label="Commercial / business",
        profiles=(
            _compat_profile(
                "retail_service",
                "stores",
                "malls_retail_markets",
                "Malls, retail, and public markets",
                priority=0,
            ),
            _compat_profile(
                "commercial",
                "office_building",
                "offices_bpo_call_centers",
                "Offices, BPO, and call centers",
                aliases=("BPO", "call center", "office"),
                priority=10,
            ),
            _compat_profile(
                "commercial",
                "light_manufacturing",
                "manufacturing_facilities",
                "Manufacturing facilities",
                priority=20,
            ),
            _compat_profile(
                "transportation",
                "warehouse",
                "warehouses_storage",
                "Warehouses and storage",
                priority=30,
            ),
            _compat_profile(
                "retail_service",
                "hotel_motel",
                "hotels_motels_hospitality",
                "Hotels, motels, and hospitality",
                priority=40,
            ),
            _compat_profile(
                "retail_service",
                "restaurant",
                "restaurants_hospitality",
                "Restaurants and hospitality",
                priority=50,
            ),
        ),
    ),
}
COMPATIBILITY_PROFILE_SETS["philippines_commercial_business"] = COMPATIBILITY_PROFILE_SETS[
    "commercial_business"
]


def get_profile_set(name: str) -> BuildingProfileSet:
    if name in COMPATIBILITY_PROFILE_SETS:
        return COMPATIBILITY_PROFILE_SETS[name]
    resolved_name = _PROFILE_SET_ALIASES.get(name, name)
    if resolved_name in BUILTIN_PROFILE_SETS:
        return BUILTIN_PROFILE_SETS[resolved_name]
    path = Path(name)
    if path.is_file():
        return BuildingProfileSet.model_validate_json(path.read_text(encoding="utf-8"))
    raise ValueError(f"unknown profile set: {name}")


def resolve_profile_id_alias(profile_set: BuildingProfileSet, profile_id: str) -> str:
    return _PROFILE_ID_ALIASES.get(
        (profile_set.profile_set_id, profile_id),
        profile_id,
    )


def narrow_profile_set(profile_set: BuildingProfileSet, profile_id: str) -> BuildingProfileSet:
    profile_id = resolve_profile_id_alias(profile_set, profile_id)
    matching_profiles = tuple(
        profile for profile in profile_set.profiles if profile.profile_id == profile_id
    )
    if not matching_profiles:
        raise ValueError(
            f"profile {profile_id!r} not found in profile set {profile_set.profile_set_id!r}"
        )
    return profile_set.model_copy(update={"profiles": matching_profiles})


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
    profile_sets = tuple(BUILTIN_PROFILE_SETS.values()) + tuple(
        COMPATIBILITY_PROFILE_SETS.values()
    )
    for profile_set in profile_sets:
        for profile in profile_set.profiles:
            if profile.profile_id == profile_id:
                return profile
    raise ValueError(f"unknown builtin profile: {profile_id}")


def write_profile_set(profile_set: BuildingProfileSet, path: Path) -> None:
    write_json_file(path, profile_set.model_dump(mode="json"))


def profile_set_to_json(profile_set: BuildingProfileSet) -> str:
    return json.dumps(profile_set.model_dump(mode="json"), indent=2)

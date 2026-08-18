from __future__ import annotations

from pdt_observer.models import WorkItem, WorkStatus, WorkStatusReport
from pdt_observer.profiles import get_profile_set
from pdt_observer.prompting import render_work_prompt


def _status(item: WorkItem) -> WorkStatusReport:
    return WorkStatusReport(
        work_item_id=item.work_item_id,
        status=WorkStatus.OPEN,
        should_continue=True,
        quota=item.quota,
        progress=item.progress,
        remaining={
            "accepted_needed": 1,
            "sources_remaining": 10,
            "reviews_remaining": 10,
        },
    )


def _item(profile_id: str) -> WorkItem:
    return WorkItem(
        work_item_id=f"work-{profile_id}",
        batch_id="batch-1",
        locality="Amsterdam",
        country="NL",
        profile_id=profile_id,
        created_at="2026-08-17T00:00:00Z",
        updated_at="2026-08-17T00:00:00Z",
    )


def test_component_prompt_uses_arguments_and_does_not_invite_incident_harvest() -> None:
    profile = next(
        profile
        for profile in get_profile_set("retail_service").profiles
        if profile.profile_id == "hotel_motel"
    )
    item = _item(profile.profile_id)

    prompt = render_work_prompt(item=item, profile=profile, status=_status(item))

    assert "Count method: population_subcomponent" in prompt
    assert "Component input fields: Number of rooms" in prompt
    assert "Regional/country component fields: Hotel Occupancy Rate" in prompt
    assert "Search for the configured argument fields" in prompt
    assert "CSV/XLSX downloads, APIs, CKAN datastores, SDMX feeds" in prompt
    assert "row-level component data was not retrieved" in prompt
    assert "Incidents are one high-value direct-count evidence pathway" not in prompt
    assert "do not derive a final occupancy estimate" in prompt


def test_direct_prompt_keeps_component_like_values_context_only() -> None:
    profile = next(
        profile
        for profile in get_profile_set("retail_service").profiles
        if profile.profile_id == "restaurant"
    )
    item = _item(profile.profile_id)

    prompt = render_work_prompt(item=item, profile=profile, status=_status(item))

    assert "Count method: direct_count" in prompt
    assert "For this direct-count profile" in prompt
    assert "capacity, enrollment, bed counts, workforce size" in prompt
    assert "Component input fields:" not in prompt

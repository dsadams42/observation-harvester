from __future__ import annotations

from pathlib import Path

import pytest

from pdt_observer.curation import (
    approval_status,
    approve_curation,
    load_curation,
    rejected_examples,
    render_gap_fill_curation_guidance,
    restore_items,
    set_exclusions,
)
from pdt_observer.models import CurationReasonCode


def test_approval_without_exclusions_is_valid(tmp_path: Path) -> None:
    manifest = approve_curation(tmp_path, "sample-one", item_ids=("item-1", "item-2"))

    assert approval_status(manifest, ("item-1", "item-2")) == "approved"
    assert manifest.approval is not None
    assert manifest.approval.included_count == 2
    assert manifest.approval.excluded_count == 0


def test_exclusion_stales_approval_and_restore_is_non_destructive(tmp_path: Path) -> None:
    approve_curation(tmp_path, "sample-one", item_ids=("item-1", "item-2"))
    excluded = set_exclusions(
        tmp_path,
        "sample-one",
        item_ids=("item-2",),
        reason_code=CurationReasonCode.WRONG_FACILITY,
        reason_note="This is a warehouse, not a school.",
    )

    assert approval_status(excluded, ("item-1", "item-2")) == "stale"
    restored = restore_items(tmp_path, "sample-one", item_ids=("item-2",))
    assert restored.decisions == ()
    assert load_curation(tmp_path, "sample-one").decisions == ()


def test_other_exclusion_requires_note(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reason note"):
        set_exclusions(
            tmp_path,
            "sample-one",
            item_ids=("item-1",),
            reason_code=CurationReasonCode.OTHER,
        )


def test_rejected_examples_create_bounded_gap_fill_guidance(tmp_path: Path) -> None:
    set_exclusions(
        tmp_path,
        "sample-one",
        item_ids=("item-1",),
        reason_code=CurationReasonCode.OUTSIDE_GEOGRAPHIC_SCOPE,
        reason_note="The facility is in Alabama.",
    )
    manifest = load_curation(tmp_path, "sample-one")
    records = (
        {
            "item_id": "item-1",
            "facility_type": "schools",
            "lead": {
                "source_url": "https://example.test/school",
                "location": {"facility_name": "Example School", "city_or_region": "Alabama"},
            },
        },
    )

    examples = rejected_examples(manifest, records, facility_type="schools")
    guidance = render_gap_fill_curation_guidance(examples)

    assert "Outside geographic scope" in guidance
    assert "The facility is in Alabama" in guidance
    assert render_gap_fill_curation_guidance(()) == ""

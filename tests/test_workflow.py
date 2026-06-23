from __future__ import annotations

from pathlib import Path

from pdt_observer.models import ResultStatus, WorkStatus
from pdt_observer.workflow import (
    claim_work_item,
    create_batch,
    export_review_items,
    ingest_review,
    list_review_items,
    list_work_items,
)


def test_create_batch_writes_public_venue_work_items(tmp_path: Path) -> None:
    batch = create_batch(
        root=tmp_path,
        locality="Milltown",
        country="US",
        profile_set_name="public_venues",
        batch_id="batch-test",
    )

    items = list_work_items(tmp_path)

    assert batch.batch_id == "batch-test"
    assert len(items) == 5
    assert all(item.status == WorkStatus.OPEN for item in items)


def test_claim_work_item_marks_one_profile_item(tmp_path: Path) -> None:
    create_batch(
        root=tmp_path,
        locality="Milltown",
        country="US",
        profile_set_name="public_venues",
        batch_id="batch-test",
    )

    claimed = claim_work_item(
        tmp_path,
        profile_id="restaurants_bars",
        claimed_by="codex-restaurants",
    )

    assert claimed is not None
    assert claimed.status == WorkStatus.CLAIMED
    assert claimed.claimed_by == "codex-restaurants"
    assert claim_work_item(tmp_path, profile_id="restaurants_bars", claimed_by="again") is None


def test_ingest_review_and_export_jsonl(tmp_path: Path) -> None:
    item = ingest_review(Path("examples/milltown_codex_run.json"), root=tmp_path)

    assert item.status == ResultStatus.ACCEPTED
    assert item.validation_valid

    items = list_review_items(tmp_path, status=ResultStatus.ACCEPTED)
    exported = export_review_items(tmp_path, status=ResultStatus.ACCEPTED, output_format="jsonl")

    assert len(items) == 1
    assert "Blue Lantern" in exported

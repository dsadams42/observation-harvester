from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from pdt_observer.models import (
    HarvestBatch,
    InvestigationRun,
    ResultStatus,
    ReviewQueueItem,
    WorkItem,
    WorkStatus,
)
from pdt_observer.profiles import get_profile_set
from pdt_observer.validation import ValidationReport, validate_run

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def utc_now_text() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def slugify(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.casefold()).strip("-")
    return slug or "item"


def artifact_dir(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_model(path: Path, model: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = model.model_dump(mode="json") if isinstance(model, BaseModel) else model
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_work_item(path: Path) -> WorkItem:
    return WorkItem.model_validate_json(path.read_text(encoding="utf-8"))


def load_review_item(path: Path) -> ReviewQueueItem:
    return ReviewQueueItem.model_validate_json(path.read_text(encoding="utf-8"))


def create_batch(
    *,
    root: Path,
    locality: str,
    country: str,
    profile_set_name: str,
    batch_id: str | None = None,
    source_hints: tuple[str, ...] = (),
) -> HarvestBatch:
    profile_set = get_profile_set(profile_set_name)
    now = utc_now_text()
    resolved_batch_id = batch_id or f"{slugify(locality)}-{country.casefold()}-{slugify(now)}"
    enabled_profiles = tuple(profile for profile in profile_set.profiles if profile.enabled)
    work_items: list[WorkItem] = []

    for profile in sorted(enabled_profiles, key=lambda item: (item.priority, item.profile_id)):
        work_item_id = f"{resolved_batch_id}-{profile.profile_id}"
        work_items.append(
            WorkItem(
                work_item_id=work_item_id,
                batch_id=resolved_batch_id,
                locality=locality,
                country=country,
                profile_id=profile.profile_id,
                source_hints=source_hints,
                created_at=now,
                updated_at=now,
            )
        )

    batch = HarvestBatch(
        batch_id=resolved_batch_id,
        locality=locality,
        country=country,
        profile_set_id=profile_set.profile_set_id,
        profile_ids=tuple(item.profile_id for item in enabled_profiles),
        work_item_ids=tuple(item.work_item_id for item in work_items),
        created_at=now,
    )

    write_model(artifact_dir(root, "batches") / f"{batch.batch_id}.json", batch)
    for item in work_items:
        write_model(artifact_dir(root, "work_items") / f"{item.work_item_id}.json", item)
    return batch


def list_work_items(
    root: Path,
    *,
    status: WorkStatus | None = None,
    profile_id: str | None = None,
) -> list[WorkItem]:
    directory = root / "work_items"
    if not directory.exists():
        return []
    items = [load_work_item(path) for path in sorted(directory.glob("*.json"))]
    if status is not None:
        items = [item for item in items if item.status == status]
    if profile_id is not None:
        items = [item for item in items if item.profile_id == profile_id]
    return items


def claim_work_item(root: Path, *, profile_id: str, claimed_by: str) -> WorkItem | None:
    open_items = list_work_items(root, status=WorkStatus.OPEN, profile_id=profile_id)
    if not open_items:
        return None
    item = sorted(open_items, key=lambda candidate: candidate.created_at)[0]
    claimed = item.model_copy(
        update={
            "status": WorkStatus.CLAIMED,
            "claimed_by": claimed_by,
            "updated_at": utc_now_text(),
        }
    )
    write_model(root / "work_items" / f"{claimed.work_item_id}.json", claimed)
    return claimed


def review_item_from_run(run_file: Path, report: ValidationReport) -> ReviewQueueItem:
    run = InvestigationRun.model_validate_json(run_file.read_text(encoding="utf-8"))
    result = run.candidate.result
    evidence = result.evidence
    georeference_status = "present" if result.georeference is not None else "missing"
    status = result.status if report.valid else ResultStatus.REVIEW
    reason = result.reason if report.valid else f"Validation failed for proposed {result.status}."
    errors = tuple(f"{error.code}: {error.message}" for error in report.errors)
    stable_id = slugify(f"{run.task.task_id}-{run_file.stem}")
    return ReviewQueueItem(
        review_item_id=stable_id,
        run_file=str(run_file),
        status=status,
        validation_valid=report.valid,
        validation_errors=errors,
        reason=reason,
        source_url=None if evidence is None else evidence.source_url,
        supporting_quote=None if evidence is None else evidence.supporting_quote,
        count=result.count,
        place_name=result.place_name,
        georeference_status=georeference_status,
        ingested_at=utc_now_text(),
    )


def ingest_review(run_file: Path, *, root: Path) -> ReviewQueueItem:
    run = InvestigationRun.model_validate_json(run_file.read_text(encoding="utf-8"))
    report = validate_run(run)
    item = review_item_from_run(run_file, report)
    write_model(artifact_dir(root, "review") / f"{item.review_item_id}.json", item)
    return item


def list_review_items(root: Path, *, status: ResultStatus | None = None) -> list[ReviewQueueItem]:
    directory = root / "review"
    if not directory.exists():
        return []
    items = [load_review_item(path) for path in sorted(directory.glob("*.json"))]
    if status is not None:
        items = [item for item in items if item.status == status]
    return items


def export_review_items(
    root: Path,
    *,
    status: ResultStatus,
    output_format: str,
) -> str:
    if output_format != "jsonl":
        raise ValueError("only jsonl export is supported")
    lines = [
        json.dumps(item.model_dump(mode="json"), sort_keys=True)
        for item in list_review_items(root, status=status)
    ]
    return "\n".join(lines) + ("\n" if lines else "")

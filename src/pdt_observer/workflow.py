from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from pdt_observer.models import (
    HarvestBatch,
    InvestigationRun,
    ResultStatus,
    ReviewQueueItem,
    SourceOutcome,
    StopReason,
    WorkItem,
    WorkProgress,
    WorkQuota,
    WorkStatus,
    WorkStatusReport,
)
from pdt_observer.profiles import get_profile_set
from pdt_observer.validation import ValidationReport, validate_run

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def utc_now_text() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc_text(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


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


def work_item_path(root: Path, work_item_id: str) -> Path:
    return root / "work_items" / f"{work_item_id}.json"


def load_work_item_by_id(root: Path, work_item_id: str) -> WorkItem:
    path = work_item_path(root, work_item_id)
    if not path.exists():
        raise ValueError(f"work item not found: {work_item_id}")
    return load_work_item(path)


def save_work_item(root: Path, item: WorkItem) -> None:
    write_model(work_item_path(root, item.work_item_id), item)


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
    quota: WorkQuota | None = None,
) -> HarvestBatch:
    profile_set = get_profile_set(profile_set_name)
    now = utc_now_text()
    resolved_batch_id = batch_id or f"{slugify(locality)}-{country.casefold()}-{slugify(now)}"
    enabled_profiles = tuple(profile for profile in profile_set.profiles if profile.enabled)
    resolved_quota = quota or WorkQuota()
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
                quota=resolved_quota.model_copy(),
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
    now = utc_now_text()
    progress = item.progress.model_copy(
        update={
            "started_at": item.progress.started_at or now,
            "last_activity_at": now,
        }
    )
    claimed = item.model_copy(
        update={
            "status": WorkStatus.CLAIMED,
            "claimed_by": claimed_by,
            "progress": progress,
            "updated_at": now,
        }
    )
    claimed = _apply_stop_conditions(claimed)
    save_work_item(root, claimed)
    return claimed


def _remaining_minutes(item: WorkItem) -> int:
    started_at = _parse_utc_text(item.progress.started_at)
    if started_at is None:
        return item.quota.max_runtime_minutes
    elapsed = datetime.now(UTC) - started_at
    elapsed_minutes = elapsed.total_seconds() / 60
    return max(0, math.ceil(item.quota.max_runtime_minutes - elapsed_minutes))


def _runtime_limit_reached(item: WorkItem) -> bool:
    started_at = _parse_utc_text(item.progress.started_at)
    if started_at is None:
        return False
    elapsed = datetime.now(UTC) - started_at
    return elapsed.total_seconds() >= item.quota.max_runtime_minutes * 60


def _next_stop_reason(item: WorkItem) -> StopReason | None:
    progress = item.progress
    quota = item.quota
    if progress.accepted_count >= quota.target_accepted_count:
        return StopReason.TARGET_MET
    if progress.review_count >= quota.max_review_count:
        return StopReason.REVIEW_LIMIT_REACHED
    if progress.sources_examined >= quota.max_sources_examined:
        return StopReason.SOURCE_LIMIT_REACHED
    if progress.failed_sources >= quota.max_failed_sources:
        return StopReason.FAILED_SOURCE_LIMIT_REACHED
    if progress.empty_sources >= quota.max_empty_sources:
        return StopReason.EMPTY_SOURCE_LIMIT_REACHED
    if _runtime_limit_reached(item):
        return StopReason.RUNTIME_LIMIT_REACHED
    return None


def _apply_stop_conditions(item: WorkItem) -> WorkItem:
    if item.status == WorkStatus.FAILED:
        return item
    reason = item.progress.stop_reason or _next_stop_reason(item)
    if reason is None:
        return item
    progress = item.progress.model_copy(update={"stop_reason": reason})
    return item.model_copy(update={"status": WorkStatus.COMPLETED, "progress": progress})


def _active_item(item: WorkItem) -> None:
    if item.status == WorkStatus.COMPLETED:
        raise ValueError(f"work item {item.work_item_id} is already completed")
    if item.status == WorkStatus.FAILED:
        raise ValueError(f"work item {item.work_item_id} has failed")


def _touch_progress(progress: WorkProgress, now: str) -> WorkProgress:
    return progress.model_copy(update={"last_activity_at": now})


def status_report(item: WorkItem) -> WorkStatusReport:
    stop_reason = item.progress.stop_reason
    return WorkStatusReport(
        work_item_id=item.work_item_id,
        status=item.status,
        should_continue=item.status == WorkStatus.CLAIMED and stop_reason is None,
        quota=item.quota,
        progress=item.progress,
        stop_reason=stop_reason,
        remaining={
            "accepted_needed": max(
                item.quota.target_accepted_count - item.progress.accepted_count,
                0,
            ),
            "reviews_remaining": max(item.quota.max_review_count - item.progress.review_count, 0),
            "sources_remaining": max(
                item.quota.max_sources_examined - item.progress.sources_examined,
                0,
            ),
            "failed_sources_remaining": max(
                item.quota.max_failed_sources - item.progress.failed_sources,
                0,
            ),
            "empty_sources_remaining": max(
                item.quota.max_empty_sources - item.progress.empty_sources,
                0,
            ),
            "runtime_minutes_remaining": _remaining_minutes(item),
        },
    )


def get_work_status(root: Path, work_item_id: str) -> WorkStatusReport:
    item = _apply_stop_conditions(load_work_item_by_id(root, work_item_id))
    save_work_item(root, item)
    return status_report(item)


def record_source_outcome(
    root: Path,
    *,
    work_item_id: str,
    outcome: SourceOutcome,
) -> WorkStatusReport:
    item = load_work_item_by_id(root, work_item_id)
    _active_item(item)
    now = utc_now_text()
    updates = {
        "sources_examined": item.progress.sources_examined + 1,
        "last_activity_at": now,
    }
    if outcome == SourceOutcome.EMPTY:
        updates["empty_sources"] = item.progress.empty_sources + 1
    elif outcome == SourceOutcome.FAILED:
        updates["failed_sources"] = item.progress.failed_sources + 1
    progress = item.progress.model_copy(update=updates)
    updated = _apply_stop_conditions(
        item.model_copy(update={"progress": progress, "updated_at": now})
    )
    save_work_item(root, updated)
    return status_report(updated)


def record_run(root: Path, *, work_item_id: str, run_file: Path) -> WorkStatusReport:
    item = load_work_item_by_id(root, work_item_id)
    _active_item(item)
    review_item = ingest_review(run_file, root=root)
    now = utc_now_text()
    updates: dict[str, object] = {
        "sources_examined": item.progress.sources_examined + 1,
        "run_files": (*item.progress.run_files, str(run_file)),
        "last_activity_at": now,
    }
    if review_item.status == ResultStatus.ACCEPTED:
        updates["accepted_count"] = item.progress.accepted_count + 1
    elif review_item.status == ResultStatus.NOT_FOUND:
        updates["not_found_count"] = item.progress.not_found_count + 1
    else:
        updates["review_count"] = item.progress.review_count + 1
    progress = item.progress.model_copy(update=updates)
    updated = _apply_stop_conditions(
        item.model_copy(
            update={
                "progress": progress,
                "run_artifact_path": str(run_file),
                "updated_at": now,
            }
        )
    )
    save_work_item(root, updated)
    return status_report(updated)


def complete_work_item(root: Path, work_item_id: str) -> WorkStatusReport:
    item = load_work_item_by_id(root, work_item_id)
    now = utc_now_text()
    progress = _touch_progress(
        item.progress.model_copy(update={"stop_reason": StopReason.MANUAL_COMPLETE}),
        now,
    )
    completed = item.model_copy(
        update={
            "status": WorkStatus.COMPLETED,
            "progress": progress,
            "updated_at": now,
        }
    )
    save_work_item(root, completed)
    return status_report(completed)


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

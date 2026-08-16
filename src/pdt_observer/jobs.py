from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from pdt_observer.models import JobRecord, JobStatus, JobType
from pdt_observer.workflow import utc_now_text, write_model

TERMINAL_JOB_STATUSES = {
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
}


def job_dir(root: Path) -> Path:
    return root / "job_runs"


def job_path(root: Path, job_id: str) -> Path:
    return job_dir(root) / f"{job_id}.job.json"


def load_job(root: Path, job_id: str) -> JobRecord:
    path = job_path(root, job_id)
    if not path.is_file():
        raise ValueError(f"job record not found: {job_id}")
    return JobRecord.model_validate_json(path.read_text(encoding="utf-8"))


def list_jobs(root: Path) -> tuple[JobRecord, ...]:
    directory = job_dir(root)
    if not directory.is_dir():
        return ()
    jobs: list[JobRecord] = []
    for path in sorted(directory.glob("*.job.json")):
        try:
            jobs.append(JobRecord.model_validate_json(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ValidationError, ValueError):
            continue
    return tuple(jobs)


def write_job(root: Path, job: JobRecord) -> JobRecord:
    write_model(job_path(root, job.job_id), job)
    return job


def create_job(
    root: Path,
    *,
    job_id: str,
    job_type: JobType,
    parent_id: str | None = None,
    manifest_path: str | None = None,
    log_path: str | None = None,
    active_child_ids: tuple[str, ...] = (),
    summary: dict[str, object] | None = None,
) -> JobRecord:
    now = utc_now_text()
    return write_job(
        root,
        JobRecord(
            job_id=job_id,
            job_type=job_type,
            parent_id=parent_id,
            status=JobStatus.QUEUED,
            created_at=now,
            updated_at=now,
            manifest_path=manifest_path,
            log_path=log_path,
            active_child_ids=active_child_ids,
            summary=summary,
        ),
    )


def update_job(root: Path, job_id: str, **changes: object) -> JobRecord:
    job = load_job(root, job_id)
    changes["updated_at"] = utc_now_text()
    return write_job(root, job.model_copy(update=changes))


def mark_job_starting(root: Path, job_id: str) -> JobRecord:
    return update_job(root, job_id, status=JobStatus.STARTING)


def mark_job_running(
    root: Path,
    job_id: str,
    *,
    active_child_ids: tuple[str, ...] | None = None,
    manifest_path: str | None = None,
) -> JobRecord:
    changes: dict[str, object] = {
        "status": JobStatus.RUNNING,
        "started_at": utc_now_text(),
    }
    if active_child_ids is not None:
        changes["active_child_ids"] = active_child_ids
    if manifest_path is not None:
        changes["manifest_path"] = manifest_path
    return update_job(root, job_id, **changes)


def mark_job_completed(
    root: Path,
    job_id: str,
    *,
    manifest_path: str | None = None,
    summary: dict[str, object] | None = None,
) -> JobRecord:
    changes: dict[str, object] = {
        "status": JobStatus.COMPLETED,
        "completed_at": utc_now_text(),
        "active_child_ids": (),
        "error_message": None,
    }
    if manifest_path is not None:
        changes["manifest_path"] = manifest_path
    if summary is not None:
        changes["summary"] = summary
    return update_job(root, job_id, **changes)


def mark_job_failed(
    root: Path,
    job_id: str,
    error_message: str,
    *,
    manifest_path: str | None = None,
    summary: dict[str, object] | None = None,
) -> JobRecord:
    changes: dict[str, object] = {
        "status": JobStatus.FAILED,
        "completed_at": utc_now_text(),
        "active_child_ids": (),
        "error_message": error_message,
    }
    if manifest_path is not None:
        changes["manifest_path"] = manifest_path
    if summary is not None:
        changes["summary"] = summary
    return update_job(root, job_id, **changes)


def mark_job_cancelled(
    root: Path,
    job_id: str,
    *,
    error_message: str | None = None,
    manifest_path: str | None = None,
    summary: dict[str, object] | None = None,
) -> JobRecord:
    changes: dict[str, object] = {
        "status": JobStatus.CANCELLED,
        "completed_at": utc_now_text(),
        "active_child_ids": (),
        "error_message": error_message or "Cancelled by user.",
    }
    if manifest_path is not None:
        changes["manifest_path"] = manifest_path
    if summary is not None:
        changes["summary"] = summary
    return update_job(root, job_id, **changes)


def job_payload(job: JobRecord, *, active: bool = False) -> dict[str, Any]:
    payload = job.model_dump(mode="json")
    payload["active"] = active
    return payload


def _result_status(result: object) -> str | None:
    status = getattr(result, "status", None)
    if status is not None:
        return str(status)
    if isinstance(result, dict):
        result_status = result.get("status")
        if result_status is not None:
            return str(result_status)
        summary = result.get("summary")
        if isinstance(summary, dict):
            summary_status = summary.get("status")
            if summary_status is not None:
                return str(summary_status)
    return None


def run_managed_job[T](
    *,
    root: Path,
    job_id: str,
    log: Callable[[str], None],
    task: Callable[[], T],
    manifest_path: Callable[[T], str | None] | None = None,
    summary: Callable[[T], dict[str, object] | None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
) -> T | None:
    mark_job_running(root, job_id)
    try:
        result = task()
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        log(message)
        mark_job_failed(root, job_id, message)
        if on_error is not None:
            try:
                on_error(exc)
            except Exception as finalizer_exc:
                message = f"{message}; finalizer failed: {finalizer_exc}"
                log(f"Failure finalizer failed: {finalizer_exc}")
                mark_job_failed(root, job_id, message)
        return None
    result_status = _result_status(result)
    resolved_manifest_path = manifest_path(result) if manifest_path is not None else None
    resolved_summary = summary(result) if summary is not None else None
    if result_status == JobStatus.CANCELLED.value:
        mark_job_cancelled(
            root,
            job_id,
            error_message="Cancelled by user.",
            manifest_path=resolved_manifest_path,
            summary=resolved_summary,
        )
        return result
    if result_status == JobStatus.FAILED.value:
        error_message = "Job returned failed status."
        if isinstance(result, dict):
            raw_error = result.get("error_message")
            if raw_error:
                error_message = str(raw_error)
        mark_job_failed(
            root,
            job_id,
            error_message,
            manifest_path=resolved_manifest_path,
            summary=resolved_summary,
        )
        return result
    mark_job_completed(
        root,
        job_id,
        manifest_path=resolved_manifest_path,
        summary=resolved_summary,
    )
    return result


def read_job_payload(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"job artifact is not a JSON object: {path}")
    return payload

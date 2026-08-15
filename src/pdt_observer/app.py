from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import time
import webbrowser
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)
from starlette.routing import Route

from pdt_observer.addresses import (
    address_output_path,
    address_prompt_path,
    address_results_payload,
    approved_address_inputs,
    load_address_results,
    merge_address_results,
    render_address_correction_prompt,
    render_address_enrichment_prompt,
    upsert_address_result,
)
from pdt_observer.app_api import (
    GeographerPlanRequest,
    GeometryCoordinatePreviewRequest,
    GeometryGeocodeAllRequest,
    GeometryGeocodeRequest,
    GeometryResearchRequest,
    GeometrySaveRequest,
    HarvestBatchRunRequest,
    HarvestCampaignRunRequest,
    HarvestRunRequest,
    PromoteLeadRequest,
    SampleCurationExcludeRequest,
    SampleCurationRestoreRequest,
    SampleSetCreateRequest,
    SampleSetGapFillRequest,
)
from pdt_observer.app_runtime import ActiveCodexRegistry, delayed_hard_exit, run_background_job
from pdt_observer.app_ui import INDEX_HTML
from pdt_observer.curation import (
    approve_curation,
    curation_summary,
    ensure_current_approval,
    load_curation,
    restore_items,
    set_exclusions,
)
from pdt_observer.dialogue import (
    append_dialogue,
    combine_dialogue,
    load_dialogue,
    render_dialogue,
)
from pdt_observer.geographer import load_geographer_plan, run_geographer
from pdt_observer.geometry import (
    Geocoder,
    NominatimGeocoder,
    approved_records_for_child,
    footprints_geojson,
    geometry_item_from_payload,
    merge_geometry_items,
    parse_coordinate_text,
    save_geometry_review_item,
    spatially_validate_geocode_result,
    verified_csv,
    verified_json,
)
from pdt_observer.harvest import (
    CodexRunner,
    append_harvest_log,
    build_harvest_batch_id,
    build_harvest_campaign_id,
    build_harvest_run_id,
    log_path_for_run,
    run_harvest,
    run_harvest_batch,
    run_harvest_campaign,
)
from pdt_observer.jobs import create_job, job_payload, list_jobs, load_job
from pdt_observer.leads import (
    export_evidence_set,
    load_evidence_set,
    load_leads,
    load_qaqc_review_set,
    load_qaqc_reviews,
    promote_lead_to_run,
    render_lead_qaqc_prompt,
)
from pdt_observer.models import (
    AddressEnrichmentStatus,
    CountMethod,
    GeometryPoint,
    GeometryStatus,
    HarvestBatchRunManifest,
    HarvestCampaignRunManifest,
    HarvestRunManifest,
    HarvestRunStatus,
    JobStatus,
    JobType,
    LeadQaqcRecommendedAction,
    LeadQaqcVerificationStatus,
)
from pdt_observer.profiles import BUILTIN_PROFILE_SETS
from pdt_observer.samples import (
    compute_coverage_summary,
    coverage_output_path,
    create_sample_set_from_run,
    load_coverage_review,
    load_sample_set,
    refresh_sample_set,
    run_coverage_steering,
    run_gap_fill,
    sample_records,
)
from pdt_observer.strategies import STRATEGIES, build_strategy_plan
from pdt_observer.workflow import utc_now_text, write_model


def _json_error(message: str, *, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


async def _request_json(request: Request) -> dict[str, Any]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    return payload


def _manifest_dir(root: Path) -> Path:
    return root / "harvest_runs"


def _clear_runtime_history(root: Path) -> int:
    patterns = {
        "harvest_runs": ("*.json",),
        "harvest_logs": ("*.log",),
        "lead_runs": ("*.json",),
        "qaqc_runs": ("*.json",),
        "address_runs": ("*.json",),
        "agent_activity": ("*.json",),
        "coverage_runs": ("*.json",),
        "sample_sets": ("*.json",),
        "geographer_runs": ("*.json",),
        "strategy_runs": ("*.json",),
        "agent_dialogue": ("*.json",),
        "job_runs": ("*.job.json",),
        "work": ("*.md",),
    }
    deleted_count = 0
    for directory_name, globs in patterns.items():
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for pattern in globs:
            for path in directory.glob(pattern):
                if path.is_file():
                    path.unlink()
                    deleted_count += 1
    return deleted_count


def _run_manifest_path(root: Path, run_id: str) -> Path:
    return _manifest_dir(root) / f"{run_id}.json"


def _batch_manifest_path(root: Path, batch_id: str) -> Path:
    return _manifest_dir(root) / f"{batch_id}.batch.json"


def _campaign_manifest_path(root: Path, campaign_id: str) -> Path:
    return _manifest_dir(root) / f"{campaign_id}.campaign.json"


def _qaqc_id_for_run(run_id: str) -> str:
    return f"{run_id}-qaqc"


def _qaqc_prompt_path(root: Path, run_id: str) -> Path:
    return root / "work" / f"{_qaqc_id_for_run(run_id)}.md"


def _qaqc_output_path(root: Path, run_id: str) -> Path:
    return root / "qaqc_runs" / f"{_qaqc_id_for_run(run_id)}.json"


def _address_id_for_run(run_id: str) -> str:
    return f"{run_id}-address"


def _load_run_manifest(root: Path, run_id: str) -> HarvestRunManifest:
    path = _run_manifest_path(root, run_id)
    if not path.is_file():
        raise ValueError(f"run manifest not found: {run_id}")
    return HarvestRunManifest.model_validate_json(path.read_text(encoding="utf-8"))


def _load_batch_manifest(root: Path, batch_id: str) -> HarvestBatchRunManifest:
    path = _batch_manifest_path(root, batch_id)
    if not path.is_file():
        raise ValueError(f"batch manifest not found: {batch_id}")
    return HarvestBatchRunManifest.model_validate_json(path.read_text(encoding="utf-8"))


def _load_campaign_manifest(root: Path, campaign_id: str) -> HarvestCampaignRunManifest:
    path = _campaign_manifest_path(root, campaign_id)
    if not path.is_file():
        raise ValueError(f"campaign manifest not found: {campaign_id}")
    return HarvestCampaignRunManifest.model_validate_json(path.read_text(encoding="utf-8"))


def _list_manifests(root: Path) -> list[dict[str, Any]]:
    directory = _manifest_dir(root)
    if not directory.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(".campaign.json"):
            campaign_manifest = HarvestCampaignRunManifest.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            payload = campaign_manifest.model_dump(mode="json")
            payload["manifest_type"] = "campaign"
        elif path.name.endswith(".batch.json"):
            batch_manifest = HarvestBatchRunManifest.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            payload = batch_manifest.model_dump(mode="json")
            payload["manifest_type"] = "batch"
        else:
            run_manifest = HarvestRunManifest.model_validate_json(path.read_text(encoding="utf-8"))
            payload = run_manifest.model_dump(mode="json")
            payload["manifest_type"] = "run"
        items.append(payload)
    return items


def _manifest_type_for_job(job_type: JobType) -> str:
    if job_type == JobType.BATCH:
        return "batch"
    if job_type == JobType.CAMPAIGN:
        return "campaign"
    return "run"


def _manifest_path_for_job(root: Path, job_id: str, job_type: JobType) -> Path:
    if job_type == JobType.BATCH:
        return _batch_manifest_path(root, job_id)
    if job_type == JobType.CAMPAIGN:
        return _campaign_manifest_path(root, job_id)
    return _run_manifest_path(root, job_id)


def _pseudo_manifest_from_job(job: Any) -> dict[str, Any]:
    manifest_type = _manifest_type_for_job(job.job_type)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "status": job.status.value,
        "summary": job.summary or {},
        "error_message": job.error_message,
        "log_path": job.log_path,
        "manifest_type": manifest_type,
        "job": job_payload(job),
    }
    if manifest_type == "campaign":
        payload["campaign_id"] = job.job_id
        payload["country"] = ""
        payload["localities"] = ()
        payload["facility_types"] = ()
        payload["target"] = 1
        payload["child_run_ids"] = job.active_child_ids
        payload["child_manifest_paths"] = ()
        payload["started_at"] = job.started_at or job.created_at
    elif manifest_type == "batch":
        payload["batch_id"] = job.job_id
        payload["country"] = ""
        payload["locality"] = None
        payload["profile_set"] = ""
        payload["target"] = 1
        payload["child_run_ids"] = job.active_child_ids
        payload["child_manifest_paths"] = ()
        payload["started_at"] = job.started_at or job.created_at
    else:
        payload["run_id"] = job.job_id
        payload["country"] = ""
        payload["locality"] = None
        payload["profile_set"] = ""
        payload["target"] = 1
        payload["prompt_path"] = ""
        payload["lead_path"] = ""
        payload["started_at"] = job.started_at or job.created_at
    return payload


def _list_run_history(root: Path) -> list[dict[str, Any]]:
    manifests = _list_manifests(root)
    manifest_ids = {
        str(item.get("run_id") or item.get("batch_id") or item.get("campaign_id"))
        for item in manifests
    }
    jobs = [
        _pseudo_manifest_from_job(job)
        for job in list_jobs(root)
        if job.job_id not in manifest_ids
        and job.job_type in {JobType.HARVEST, JobType.BATCH, JobType.CAMPAIGN}
    ]
    return sorted(
        [*manifests, *jobs],
        key=lambda item: str(item.get("started_at") or item.get("created_at") or ""),
    )


def _latest_job_for_identity(root: Path, identity: str) -> Any | None:
    candidates = [
        job
        for job in list_jobs(root)
        if job.job_id == identity or job.parent_id == identity
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda job: job.updated_at)[-1]


def _manifest_payload_with_job_state(manifest: Any, job: Any | None) -> dict[str, Any]:
    payload = cast("dict[str, Any]", manifest.model_dump(mode="json"))
    if job is None:
        return payload
    if job.status in {JobStatus.CANCELLED, JobStatus.FAILED}:
        payload["status"] = job.status.value
        payload["error_message"] = job.error_message
    if job.log_path and not payload.get("log_path"):
        payload["log_path"] = job.log_path
    if job.summary and not payload.get("summary"):
        payload["summary"] = job.summary
    return payload


def _finalize_failed_run_manifest(
    *,
    root: Path,
    run_id: str,
    country: str,
    locality: str | None,
    profile_set: str,
    profile_id: str | None,
    count_method_override: CountMethod | None,
    target: int,
    error_message: str,
) -> None:
    try:
        manifest = _load_run_manifest(root, run_id)
    except ValueError:
        manifest = HarvestRunManifest(
            run_id=run_id,
            status=HarvestRunStatus.FAILED,
            country=country,
            locality=locality,
            profile_set=profile_set,
            profile_id=profile_id,
            count_method_override=count_method_override,
            target=target,
            prompt_path=str(root / "work" / f"{run_id}.md"),
            lead_path=str(root / "lead_runs" / f"{run_id}.json"),
            started_at=utc_now_text(),
            validation_valid=False,
            log_path=str(log_path_for_run(root, run_id)),
        )
    write_model(
        _run_manifest_path(root, run_id),
        manifest.model_copy(
            update={
                "status": HarvestRunStatus.FAILED,
                "completed_at": utc_now_text(),
                "validation_valid": False,
                "error_message": error_message,
            }
        ),
    )


def _finalize_failed_batch_manifest(
    *,
    root: Path,
    batch_id: str,
    country: str,
    locality: str | None,
    profile_set: str,
    count_method_override: CountMethod | None,
    target: int,
    error_message: str,
) -> None:
    try:
        manifest = _load_batch_manifest(root, batch_id)
    except ValueError:
        manifest = HarvestBatchRunManifest(
            batch_id=batch_id,
            status=HarvestRunStatus.FAILED,
            country=country,
            locality=locality,
            profile_set=profile_set,
            count_method_override=count_method_override,
            target=target,
            child_run_ids=(),
            child_manifest_paths=(),
            started_at=utc_now_text(),
            log_path=str(log_path_for_run(root, batch_id)),
        )
    write_model(
        _batch_manifest_path(root, batch_id),
        manifest.model_copy(
            update={
                "status": HarvestRunStatus.FAILED,
                "completed_at": utc_now_text(),
                "error_message": error_message,
            }
        ),
    )


def _finalize_failed_campaign_manifest(
    *,
    root: Path,
    campaign_id: str,
    country: str,
    localities: tuple[str, ...],
    facility_types: tuple[str, ...],
    count_method_override: CountMethod | None,
    target: int,
    error_message: str,
) -> None:
    try:
        manifest = _load_campaign_manifest(root, campaign_id)
    except ValueError:
        manifest = HarvestCampaignRunManifest(
            campaign_id=campaign_id,
            status=HarvestRunStatus.FAILED,
            country=country,
            localities=localities,
            facility_types=facility_types,
            count_method_override=count_method_override,
            target=target,
            child_run_ids=(),
            child_manifest_paths=(),
            started_at=utc_now_text(),
            log_path=str(log_path_for_run(root, campaign_id)),
        )
    write_model(
        _campaign_manifest_path(root, campaign_id),
        manifest.model_copy(
            update={
                "status": HarvestRunStatus.FAILED,
                "completed_at": utc_now_text(),
                "error_message": error_message,
            }
        ),
    )


def _profiles_payload() -> dict[str, Any]:
    preferred_order = {
        "schools": 0,
        "manufacturing": 1,
        "restaurants": 2,
        "retail_service": 3,
        "public_institutional": 4,
        "transportation": 5,
        "recreation_entertainment": 6,
        "agriculture": 7,
        "pdt_residential": 8,
    }
    profile_sets: list[dict[str, Any]] = []
    for profile_set_id, profile_set in BUILTIN_PROFILE_SETS.items():
        if profile_set_id.startswith("philippines_"):
            continue
        profile_sets.append(
            {
                "profile_set_id": profile_set.profile_set_id,
                "label": profile_set.label,
                "profiles": [
                    {
                        "profile_id": profile.profile_id,
                        "label": profile.label,
                        "enabled": profile.enabled,
                        "priority": profile.priority,
                        "pdt_subtype": profile.pdt_subtype,
                        "area_defined": profile.area_defined,
                        "day_occurrence": profile.day_occurrence,
                        "night_occurrence": profile.night_occurrence,
                        "episodic_occurrence": profile.episodic_occurrence,
                        "occupancy_groups": profile.occupancy_groups,
                        "contextual_count_fields": profile.contextual_count_fields,
                        "count_method": profile.count_method.value,
                        "component_count_fields": profile.component_count_fields,
                        "regional_stat_fields": profile.regional_stat_fields,
                        "component_source_guidance": profile.component_source_guidance,
                        "preferred_strategy_ids": tuple(
                            strategy_id.value for strategy_id in profile.preferred_strategy_ids
                        ),
                        "strategy_plan": build_strategy_plan(
                            profile_set,
                            profile_id=profile.profile_id,
                        ).model_dump(mode="json"),
                    }
                    for profile in profile_set.profiles
                ],
            }
        )
    return {
        "profile_sets": sorted(
            profile_sets,
            key=lambda item: (
                preferred_order.get(str(item["profile_set_id"]), 100),
                item["profile_set_id"],
            ),
        ),
        "strategies": [
            strategy.model_dump(mode="json")
            for strategy in STRATEGIES.values()
        ],
    }


def _leads_payload(path: str) -> list[dict[str, Any]]:
    leads = load_leads(Path(path))
    return [lead.model_dump(mode="json") for lead in leads]


def _manifest_identity(manifest: Any) -> str:
    return str(
        getattr(manifest, "run_id", None)
        or getattr(manifest, "batch_id", None)
        or getattr(manifest, "campaign_id", None)
    )


def _manifest_child_run_ids(manifest: Any) -> tuple[str, ...]:
    run_id = getattr(manifest, "run_id", None)
    if run_id is not None:
        return (str(run_id),)
    return tuple(str(run_id) for run_id in getattr(manifest, "child_run_ids", ()))


def _pipeline_transcript_text(
    root: Path,
    *,
    run_id: str | None = None,
    sample_set_id: str | None = None,
) -> str:
    conversation_ids: list[str] = []
    title_id = run_id
    if sample_set_id is not None:
        sample_set = load_sample_set(root, sample_set_id)
        title_id = sample_set.sample_set_id
        for sample_round in sample_set.rounds:
            for source_run_id in sample_round.source_run_ids:
                if source_run_id not in conversation_ids:
                    conversation_ids.append(source_run_id)
        conversation_ids.append(sample_set.sample_set_id)
    elif run_id is not None:
        conversation_ids.append(run_id)
    entries = combine_dialogue(
        *(load_dialogue(root, conversation_id) for conversation_id in conversation_ids)
    )
    heading = (
        "OASIS PIPELINE TRANSCRIPT\n"
        f"Pipeline: {title_id or 'unknown'}\n"
        f"Conversation sources: {', '.join(conversation_ids) or 'none'}\n"
    )
    rendered = render_dialogue(entries)
    return heading + ("\n" + rendered if rendered else "\n\nNo agent dialogue recorded.\n")


def _load_any_manifest(root: Path, run_id: str) -> Any:
    try:
        return _load_run_manifest(root, run_id)
    except ValueError:
        try:
            return _load_batch_manifest(root, run_id)
        except ValueError:
            return _load_campaign_manifest(root, run_id)


def _run_qaqc_for_child(
    *,
    root: Path,
    run_id: str,
    parent_id: str,
    codex_bin: str,
    runner: CodexRunner,
) -> dict[str, Any]:
    qaqc_id = _qaqc_id_for_run(run_id)
    prompt_path = _qaqc_prompt_path(root, run_id)
    output_path = _qaqc_output_path(root, run_id)
    manifest = _load_run_manifest(root, run_id)
    append_harvest_log(root, parent_id, f"Rendering QAQC prompt for {run_id}.")
    evidence_set = load_evidence_set(Path(manifest.lead_path))
    prompt = render_lead_qaqc_prompt(
        evidence_set,
        source_label=manifest.lead_path,
        expected_country=manifest.country,
        expected_locality=manifest.locality,
    )
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    append_harvest_log(root, parent_id, f"QAQC prompt written to {prompt_path}.")

    command = (
        codex_bin,
        "--search",
        "exec",
        "--sandbox",
        "workspace-write",
        "--cd",
        str(root),
        "-o",
        str(output_path),
        "-",
    )
    append_harvest_log(root, parent_id, f"Launching QAQC agent for {run_id}.")
    append_harvest_log(root, parent_id, f"QAQC command: {' '.join(command)}")
    result = runner(command, prompt, root)
    if result.stdout.strip():
        append_harvest_log(root, parent_id, f"QAQC stdout for {run_id}: {result.stdout.strip()}")
    if result.stderr.strip():
        append_harvest_log(root, parent_id, f"QAQC stderr for {run_id}: {result.stderr.strip()}")
    append_harvest_log(
        root,
        parent_id,
        f"QAQC agent for {run_id} exited with {result.returncode}.",
    )
    if result.returncode < 0:
        return {
            "run_id": run_id,
            "qaqc_id": qaqc_id,
            "status": "cancelled",
            "prompt_path": str(prompt_path),
            "qaqc_path": str(output_path),
            "review_count": 0,
            "error_message": "QAQC cancelled by user.",
        }
    if result.returncode != 0:
        return {
            "run_id": run_id,
            "qaqc_id": qaqc_id,
            "status": "failed",
            "prompt_path": str(prompt_path),
            "qaqc_path": str(output_path),
            "review_count": 0,
            "error_message": result.stderr.strip() or result.stdout.strip(),
        }
    try:
        review_set = load_qaqc_review_set(output_path)
        review_count = len(review_set.occupancy_reviews) + len(review_set.component_reviews)
    except Exception as exc:
        append_harvest_log(root, parent_id, f"QAQC validation failed for {run_id}: {exc}.")
        return {
            "run_id": run_id,
            "qaqc_id": qaqc_id,
            "status": "failed",
            "prompt_path": str(prompt_path),
            "qaqc_path": str(output_path),
            "review_count": 0,
            "error_message": str(exc),
        }
    append_harvest_log(
        root,
        parent_id,
        f"QAQC validation completed for {run_id}: {review_count} reviews.",
    )
    return {
        "run_id": run_id,
        "qaqc_id": qaqc_id,
        "status": "completed",
        "prompt_path": str(prompt_path),
        "qaqc_path": str(output_path),
        "review_count": review_count,
        "error_message": None,
    }


def _run_qaqc_for_manifest(
    *,
    root: Path,
    manifest: Any,
    codex_bin: str,
    runner: CodexRunner,
) -> dict[str, Any]:
    parent_id = _manifest_identity(manifest)
    child_run_ids = _manifest_child_run_ids(manifest)
    append_harvest_log(root, parent_id, f"Starting QAQC run for {len(child_run_ids)} child run(s).")
    child_results: list[dict[str, Any]] = []
    for child_run_id in child_run_ids:
        child_result = _run_qaqc_for_child(
            root=root,
            run_id=child_run_id,
            parent_id=parent_id,
            codex_bin=codex_bin,
            runner=runner,
        )
        child_results.append(child_result)
        append_harvest_log(
            root,
            parent_id,
            f"QAQC child {child_run_id} finished as {child_result['status']}.",
        )
        if child_result["status"] == "cancelled":
            break

    completed_count = sum(1 for result in child_results if result["status"] == "completed")
    failed_count = sum(1 for result in child_results if result["status"] == "failed")
    cancelled_count = sum(1 for result in child_results if result["status"] == "cancelled")
    review_count = sum(
        result["review_count"]
        for result in child_results
        if isinstance(result["review_count"], int)
    )
    status = "cancelled" if cancelled_count else ("failed" if failed_count else "completed")
    summary = {
        "status": status,
        "planned_count": len(child_run_ids),
        "completed_count": completed_count,
        "failed_count": failed_count,
        "cancelled_count": cancelled_count,
        "review_count": review_count,
    }
    append_harvest_log(root, parent_id, f"QAQC run finished: {summary}.")
    append_dialogue(
        root,
        parent_id,
        speaker="QAQC Agent",
        stage="qaqc",
        message=(
            f"I reviewed {review_count} lead record(s) across {completed_count} completed "
            f"child run(s)."
        ),
        rationale=(
            f"{failed_count} child review(s) failed and {cancelled_count} were cancelled. "
            "I checked source support, facility and location agreement, count evidence, and "
            "strategy semantics before recommending what to keep."
        ),
    )
    return {
        "parent_id": parent_id,
        "child_run_ids": child_run_ids,
        "child_results": child_results,
        "summary": summary,
    }


def _run_address_for_child(
    *,
    root: Path,
    run_id: str,
    parent_id: str,
    codex_bin: str,
    runner: CodexRunner,
) -> dict[str, Any]:
    address_id = _address_id_for_run(run_id)
    prompt_path = address_prompt_path(root, run_id)
    output_path = address_output_path(root, run_id)
    manifest = _load_run_manifest(root, run_id)
    append_harvest_log(root, parent_id, f"Rendering address enrichment prompt for {run_id}.")
    records = approved_address_inputs(root=root, manifest=manifest)
    if not records:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("[]", encoding="utf-8")
        append_harvest_log(
            root,
            parent_id,
            f"No QAQC-approved leads found for {run_id}; address enrichment skipped.",
        )
        return {
            "run_id": run_id,
            "address_id": address_id,
            "status": "completed",
            "prompt_path": str(prompt_path),
            "address_path": str(output_path),
            "result_count": 0,
            "error_message": None,
        }
    prompt = render_address_enrichment_prompt(records, source_label=f"{run_id} QAQC-approved leads")
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    append_harvest_log(root, parent_id, f"Address prompt written to {prompt_path}.")

    command = (
        codex_bin,
        "--search",
        "exec",
        "--sandbox",
        "workspace-write",
        "--cd",
        str(root),
        "-o",
        str(output_path),
        "-",
    )
    append_harvest_log(root, parent_id, f"Launching address enrichment agent for {run_id}.")
    append_harvest_log(root, parent_id, f"Address command: {' '.join(command)}")
    result = runner(command, prompt, root)
    if result.stdout.strip():
        append_harvest_log(
            root,
            parent_id,
            f"Address stdout for {run_id}: {result.stdout.strip()}",
        )
    if result.stderr.strip():
        append_harvest_log(
            root,
            parent_id,
            f"Address stderr for {run_id}: {result.stderr.strip()}",
        )
    append_harvest_log(
        root,
        parent_id,
        f"Address enrichment agent for {run_id} exited with {result.returncode}.",
    )
    if result.returncode < 0:
        return {
            "run_id": run_id,
            "address_id": address_id,
            "status": "cancelled",
            "prompt_path": str(prompt_path),
            "address_path": str(output_path),
            "result_count": 0,
            "error_message": "Address enrichment cancelled by user.",
        }
    if result.returncode != 0:
        return {
            "run_id": run_id,
            "address_id": address_id,
            "status": "failed",
            "prompt_path": str(prompt_path),
            "address_path": str(output_path),
            "result_count": 0,
            "error_message": result.stderr.strip() or result.stdout.strip(),
        }
    try:
        results = load_address_results(output_path)
    except Exception as exc:
        append_harvest_log(
            root,
            parent_id,
            f"Address validation failed for {run_id}: {exc}.",
        )
        return {
            "run_id": run_id,
            "address_id": address_id,
            "status": "failed",
            "prompt_path": str(prompt_path),
            "address_path": str(output_path),
            "result_count": 0,
            "error_message": str(exc),
        }
    append_harvest_log(
        root,
        parent_id,
        f"Address validation completed for {run_id}: {len(results)} result(s).",
    )
    return {
        "run_id": run_id,
        "address_id": address_id,
        "status": "completed",
        "prompt_path": str(prompt_path),
        "address_path": str(output_path),
        "result_count": len(results),
        "error_message": None,
    }


def _run_address_for_manifest(
    *,
    root: Path,
    manifest: Any,
    codex_bin: str,
    runner: CodexRunner,
) -> dict[str, Any]:
    parent_id = _manifest_identity(manifest)
    child_run_ids = _manifest_child_run_ids(manifest)
    append_harvest_log(
        root,
        parent_id,
        f"Starting address enrichment for {len(child_run_ids)} child run(s).",
    )
    child_results: list[dict[str, Any]] = []
    for child_run_id in child_run_ids:
        child_result = _run_address_for_child(
            root=root,
            run_id=child_run_id,
            parent_id=parent_id,
            codex_bin=codex_bin,
            runner=runner,
        )
        child_results.append(child_result)
        append_harvest_log(
            root,
            parent_id,
            f"Address child {child_run_id} finished as {child_result['status']}.",
        )
        if child_result["status"] == "cancelled":
            break

    completed_count = sum(1 for result in child_results if result["status"] == "completed")
    failed_count = sum(1 for result in child_results if result["status"] == "failed")
    cancelled_count = sum(1 for result in child_results if result["status"] == "cancelled")
    result_count = sum(
        result["result_count"]
        for result in child_results
        if isinstance(result["result_count"], int)
    )
    status = "cancelled" if cancelled_count else ("failed" if failed_count else "completed")
    summary = {
        "status": status,
        "planned_count": len(child_run_ids),
        "completed_count": completed_count,
        "failed_count": failed_count,
        "cancelled_count": cancelled_count,
        "result_count": result_count,
    }
    append_harvest_log(root, parent_id, f"Address enrichment finished: {summary}.")
    append_dialogue(
        root,
        parent_id,
        speaker="Address Agent",
        stage="address_enrichment",
        message=(
            f"I completed address research for {completed_count} child run(s) and returned "
            f"{result_count} address result(s)."
        ),
        rationale=(
            f"{failed_count} child enrichment run(s) failed and {cancelled_count} were cancelled. "
            "I limited the work to QAQC-approved leads and preserved ambiguous addresses for "
            "human review."
        ),
    )
    return {
        "parent_id": parent_id,
        "child_run_ids": child_run_ids,
        "child_results": child_results,
        "summary": summary,
    }


def _qaqc_reviews_payload(root: Path, child_run_ids: Sequence[str]) -> dict[str, Any]:
    child_reviews: list[dict[str, Any]] = []
    all_reviews: list[dict[str, Any]] = []
    all_component_reviews: list[dict[str, Any]] = []
    for child_run_id in child_run_ids:
        output_path = _qaqc_output_path(root, child_run_id)
        if not output_path.is_file():
            continue
        review_set = load_qaqc_review_set(output_path)
        reviews = review_set.occupancy_reviews
        component_reviews = review_set.component_reviews
        review_payload = [review.model_dump(mode="json") for review in reviews]
        component_review_payload = [
            review.model_dump(mode="json") for review in component_reviews
        ]
        child_reviews.append(
            {
                "run_id": child_run_id,
                "qaqc_path": str(output_path),
                "review_count": len(reviews) + len(component_reviews),
                "reviews": review_payload,
                "component_reviews": component_review_payload,
            }
        )
        all_reviews.extend(review_payload)
        all_component_reviews.extend(component_review_payload)
    return {
        "review_count": len(all_reviews) + len(all_component_reviews),
        "child_reviews": child_reviews,
        "reviews": all_reviews,
        "component_reviews": all_component_reviews,
    }


def _approved_records_for_manifest(root: Path, manifest: Any) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    for child_run_id in _manifest_child_run_ids(manifest):
        child_manifest = _load_run_manifest(root, child_run_id)
        records.extend(approved_records_for_child(root, child_manifest))
    return tuple(records)


def _approved_component_records_for_child(
    root: Path,
    manifest: HarvestRunManifest,
) -> tuple[dict[str, Any], ...]:
    qaqc_path = _qaqc_output_path(root, manifest.run_id)
    if not qaqc_path.is_file():
        raise FileNotFoundError(f"QAQC review not found for run: {manifest.run_id}")
    evidence_set = load_evidence_set(Path(manifest.lead_path))
    review_set = load_qaqc_review_set(qaqc_path)
    records: list[dict[str, Any]] = []
    for review in review_set.component_reviews:
        if review.lead_index >= len(evidence_set.component_leads):
            continue
        if review.verification_status != LeadQaqcVerificationStatus.VERIFIED:
            continue
        if review.recommended_action != LeadQaqcRecommendedAction.KEEP:
            continue
        lead = evidence_set.component_leads[review.lead_index]
        records.append(
            {
                "item_id": f"{manifest.run_id}-component-{review.lead_index}",
                "child_run_id": manifest.run_id,
                "lead_index": review.lead_index,
                "facility_type": manifest.profile_set,
                "component_lead": lead.model_dump(mode="json"),
                "component_qaqc_review": review.model_dump(mode="json"),
            }
        )
    return tuple(records)


def _approved_component_records_for_manifest(
    root: Path,
    manifest: Any,
) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    for child_run_id in _manifest_child_run_ids(manifest):
        child_manifest = _load_run_manifest(root, child_run_id)
        records.extend(_approved_component_records_for_child(root, child_manifest))
    return tuple(records)


def _component_records_json(records: Sequence[dict[str, Any]]) -> str:
    return json.dumps(list(records), indent=2)


def _component_records_csv(records: Sequence[dict[str, Any]]) -> str:
    output = io.StringIO()
    fieldnames = (
        "item_id",
        "child_run_id",
        "lead_index",
        "facility_type",
        "source_url",
        "source_title",
        "component_type",
        "value",
        "unit",
        "time_basis",
        "geography_level",
        "period_label",
        "facility_name",
        "geography_name",
        "country",
        "qaqc_status",
        "recommended_action",
        "review_notes",
    )
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for record in records:
        lead = record["component_lead"]
        review = record["component_qaqc_review"]
        location = lead.get("location") or {}
        for datum in lead["component_data"]:
            writer.writerow(
                {
                    "item_id": record["item_id"],
                    "child_run_id": record["child_run_id"],
                    "lead_index": record["lead_index"],
                    "facility_type": record.get("facility_type", ""),
                    "source_url": lead["source_url"],
                    "source_title": lead.get("source_title", ""),
                    "component_type": datum["component_type"],
                    "value": datum["value"],
                    "unit": datum["unit"],
                    "time_basis": datum["time_basis"],
                    "geography_level": datum["geography_level"],
                    "period_label": datum.get("period_label") or "",
                    "facility_name": location.get("facility_name", ""),
                    "geography_name": lead["geography_name"],
                    "country": lead["country"],
                    "qaqc_status": review["verification_status"],
                    "recommended_action": review["recommended_action"],
                    "review_notes": review.get("review_notes", ""),
                }
            )
    return output.getvalue()


def _geometry_items_payload(root: Path, manifest: Any) -> dict[str, Any]:
    records = merge_address_results(root, _approved_records_for_manifest(root, manifest))
    items = tuple(merge_geometry_items(root, records))
    return {"item_count": len(items), "items": items}


def _geometry_record_context(
    root: Path,
    item_id: str,
) -> tuple[HarvestRunManifest, dict[str, Any]]:
    child_run_id, _ = item_id.rsplit("-", 1)
    manifest = _load_run_manifest(root, child_run_id)
    records = _geometry_items_payload(root, manifest)["items"]
    record = next(
        (candidate for candidate in records if candidate.get("item_id") == item_id),
        None,
    )
    if not isinstance(record, dict):
        raise ValueError(f"geometry item not found: {item_id}")
    return manifest, record


def _geocode_context(
    root: Path,
    item_id: str,
    requested_query: str,
) -> tuple[HarvestRunManifest, tuple[str, ...]]:
    manifest, record = _geometry_record_context(root, item_id)
    queries: list[str] = []

    def add_query(value: object) -> None:
        text = str(value or "").strip()
        if text and text not in queries:
            queries.append(text)

    add_query(requested_query)
    address = record.get("address_enrichment")
    if isinstance(address, dict):
        add_query(
            ", ".join(
                str(address.get(key) or "").strip()
                for key in (
                    "address_line1",
                    "city_or_region",
                    "state_or_province",
                    "postal_code",
                    "country",
                )
                if str(address.get(key) or "").strip()
            )
        )
        add_query(address.get("formatted_address"))
    lead = record.get("lead")
    if isinstance(lead, dict):
        location = lead.get("location")
        if isinstance(location, dict):
            add_query(
                ", ".join(
                    str(location.get(key) or "").strip()
                    for key in (
                        "facility_name",
                        "specific_address_or_landmark",
                        "city_or_region",
                        "country",
                    )
                    if str(location.get(key) or "").strip()
                )
            )
            add_query(
                ", ".join(
                    str(location.get(key) or "").strip()
                    for key in ("facility_name", "city_or_region", "country")
                    if str(location.get(key) or "").strip()
                )
            )
    return manifest, tuple(queries)


def _run_address_spatial_retry(
    *,
    root: Path,
    item_id: str,
    spatial_feedback: dict[str, object],
    conversation_id: str,
    codex_bin: str,
    runner: CodexRunner,
) -> dict[str, Any]:
    manifest, geometry_record = _geometry_record_context(root, item_id)
    address_input = next(
        (
            record
            for record in approved_address_inputs(root=root, manifest=manifest)
            if record.get("item_id") == item_id
        ),
        None,
    )
    if address_input is None:
        return {"status": "skipped", "reason": "QAQC-approved address input was not found."}
    prompt = render_address_correction_prompt(
        address_input,
        current_address=(
            geometry_record.get("address_enrichment")
            if isinstance(geometry_record.get("address_enrichment"), dict)
            else None
        ),
        spatial_feedback=spatial_feedback,
    )
    retry_id = f"{item_id}-address-spatial-retry"
    prompt_path = root / "work" / f"{retry_id}.md"
    output_path = root / "address_runs" / f"{retry_id}.json"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    append_harvest_log(
        root,
        conversation_id,
        f"Address-spatial retry started for {item_id}.",
    )
    command = (
        codex_bin,
        "--search",
        "exec",
        "--sandbox",
        "workspace-write",
        "--cd",
        str(root),
        "-o",
        str(output_path),
        "-",
    )
    result = runner(command, prompt, root)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "Address retry failed."
        append_dialogue(
            root,
            conversation_id,
            speaker="Address-Spatial Review Agent",
            stage="address_spatial_retry",
            message=f"I could not complete corrected address research for {item_id}.",
            rationale=message,
        )
        return {"status": "failed", "reason": message}
    try:
        results = load_address_results(output_path)
        corrected = next((item for item in results if item.item_id == item_id), None)
        if corrected is None and len(results) == 1:
            candidate = results[0]
            corrected = candidate.model_copy(
                update={
                    "item_id": item_id,
                    "lead_index": int(address_input["lead_index"]),
                }
            )
        if corrected is None:
            raise ValueError("Address retry did not return the requested item.")
    except Exception as exc:
        return {"status": "failed", "reason": str(exc)}
    upsert_address_result(root, manifest.run_id, corrected)
    usable = (
        corrected.status == AddressEnrichmentStatus.FOUND
        and bool(corrected.formatted_address)
    )
    append_dialogue(
        root,
        conversation_id,
        speaker="Address-Spatial Review Agent",
        stage="address_spatial_retry",
        message=(
            f"I found a corrected address candidate for {corrected.facility_name}: "
            f"{corrected.formatted_address}."
            if usable
            else f"I could not establish a corrected address for {corrected.facility_name}."
        ),
        rationale=corrected.review_notes,
    )
    return {
        "status": "corrected" if usable else corrected.status.value,
        "address": corrected.model_dump(mode="json"),
        "prompt_path": str(prompt_path),
        "output_path": str(output_path),
    }


def _normalized_address_words(value: object) -> set[str]:
    return {
        word
        for word in re.findall(r"[^\W_]+", str(value or "").casefold(), flags=re.UNICODE)
        if len(word) >= 3 and not word.isdigit()
    }


def _address_candidate_mismatch(
    candidate: dict[str, Any],
    record: dict[str, Any],
) -> str | None:
    enriched = record.get("address_enrichment")
    if not isinstance(enriched, dict):
        return None
    candidate_address = candidate.get("address")
    if not isinstance(candidate_address, dict):
        return None
    expected_postal = re.sub(r"\W", "", str(enriched.get("postal_code") or "").casefold())
    candidate_postal = re.sub(
        r"\W",
        "",
        str(candidate_address.get("postcode") or "").casefold(),
    )
    if expected_postal and candidate_postal and expected_postal != candidate_postal:
        return (
            f"Geocoder postal code {candidate_postal} does not match researched "
            f"postal code {expected_postal}."
        )
    expected_line = str(enriched.get("address_line1") or "")
    expected_number_match = re.match(r"\s*(\d+[A-Za-z]?)\b", expected_line)
    expected_number = expected_number_match.group(1).casefold() if expected_number_match else ""
    candidate_number = str(candidate_address.get("house_number") or "").casefold()
    if expected_number and candidate_number and expected_number != candidate_number:
        return (
            f"Geocoder house number {candidate_number} does not match researched "
            f"house number {expected_number}."
        )
    expected_road_words = _normalized_address_words(
        re.sub(r"^\s*\d+[A-Za-z]?\s*", "", expected_line)
    )
    candidate_road_words = _normalized_address_words(
        candidate_address.get("road")
        or candidate_address.get("pedestrian")
        or candidate_address.get("industrial")
    )
    if (
        expected_road_words
        and candidate_road_words
        and not expected_road_words.intersection(candidate_road_words)
    ):
        return "Geocoder street name does not agree with the researched address."
    candidate_name_words = _normalized_address_words(candidate.get("name"))
    lead = record.get("lead")
    location = lead.get("location") if isinstance(lead, dict) else None
    facility_words = _normalized_address_words(
        location.get("facility_name") if isinstance(location, dict) else ""
    )
    if (
        candidate_name_words
        and facility_words
        and not candidate_name_words.intersection(facility_words)
    ):
        return "Named geocoder feature does not match the researched facility identity."
    return None


def _ranked_candidate_options(
    result: dict[str, Any] | None,
    *,
    record: dict[str, Any],
    expected_country: str,
    expected_locality: str | None,
    query: str,
) -> list[dict[str, Any]]:
    if result is None:
        return []
    raw_candidates = result.get("candidates")
    candidates = (
        [candidate for candidate in raw_candidates if isinstance(candidate, dict)]
        if isinstance(raw_candidates, list)
        else [result]
    )
    options: list[dict[str, Any]] = []
    lead = record.get("lead")
    location = lead.get("location") if isinstance(lead, dict) else None
    facility_words = _normalized_address_words(
        location.get("facility_name") if isinstance(location, dict) else ""
    )
    for candidate in candidates:
        latitude = candidate.get("latitude")
        longitude = candidate.get("longitude")
        if latitude is None or longitude is None:
            continue
        _, validation = spatially_validate_geocode_result(
            {"candidates": [candidate]},
            expected_country=expected_country,
            expected_locality=expected_locality,
        )
        raw_assessments = validation.get("assessments")
        assessment: dict[str, object] = (
            raw_assessments[0]
            if isinstance(raw_assessments, list)
            and raw_assessments
            and isinstance(raw_assessments[0], dict)
            else {}
        )
        scope_status = str(assessment.get("status") or validation["status"])
        scope_reason = str(assessment.get("reason") or validation["reason"])
        mismatch = _address_candidate_mismatch(candidate, record)
        candidate_words = _normalized_address_words(
            candidate.get("name") or candidate.get("display_name")
        )
        facility_match = bool(
            facility_words
            and candidate_words
            and facility_words.intersection(candidate_words)
        )
        score: float = {
            "accepted": 60,
            "requires_human": 35,
            "out_of_scope": -100,
        }.get(scope_status, 0)
        match_summary = [scope_reason]
        if mismatch is None:
            score += 25
            match_summary.append("No conflict with the researched address was detected.")
        else:
            score -= 30
            match_summary.append(mismatch)
        if facility_match:
            score += 15
            match_summary.append("The candidate name overlaps the facility name.")
        importance = candidate.get("importance")
        if isinstance(importance, int | float):
            score += min(max(float(importance), 0.0), 1.0) * 10
        confidence = (
            "likely"
            if score >= 75 and scope_status == "accepted" and mismatch is None
            else "possible"
            if score >= 25 and scope_status != "out_of_scope"
            else "conflicting"
        )
        geocode_result = {
            key: value
            for key, value in candidate.items()
            if key not in {"candidates", "cache_version"}
        }
        options.append(
            {
                "display_name": str(candidate.get("display_name") or ""),
                "latitude": float(latitude),
                "longitude": float(longitude),
                "provider": str(candidate.get("provider") or "geocoder"),
                "query": query,
                "category": candidate.get("category"),
                "type": candidate.get("type"),
                "name": candidate.get("name"),
                "address": candidate.get("address", {}),
                "scope_status": scope_status,
                "scope_reason": scope_reason,
                "address_mismatch": mismatch,
                "facility_name_match": facility_match,
                "score": round(score, 1),
                "confidence": confidence,
                "match_summary": match_summary,
                "geocode_result": geocode_result,
            }
        )
    return options


def _merge_ranked_candidate_options(
    existing: list[dict[str, Any]],
    additions: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_location: dict[tuple[float, float], dict[str, Any]] = {
        (
            round(float(option["latitude"]), 6),
            round(float(option["longitude"]), 6),
        ): option
        for option in existing
    }
    for option in additions:
        key = (
            round(float(option["latitude"]), 6),
            round(float(option["longitude"]), 6),
        )
        current = by_location.get(key)
        if current is None or float(option["score"]) > float(current["score"]):
            by_location[key] = dict(option)
    return sorted(
        by_location.values(),
        key=lambda option: (-float(option["score"]), str(option["display_name"])),
    )[:5]


def _spatially_geocode_item(
    *,
    root: Path,
    geocoder: Geocoder,
    item_id: str,
    requested_query: str,
) -> tuple[dict[str, Any] | None, dict[str, object], str]:
    manifest, record = _geometry_record_context(root, item_id)
    _, queries = _geocode_context(root, item_id, requested_query)
    attempts: list[dict[str, object]] = []
    final_validation: dict[str, object] = {
        "status": "no_match",
        "requires_human_intervention": True,
        "reason": "No geocoding query produced a usable candidate.",
        "candidate_count": 0,
    }
    candidate_options: list[dict[str, Any]] = []
    for query in queries:
        result = geocoder(query)
        candidate_options = _merge_ranked_candidate_options(
            candidate_options,
            _ranked_candidate_options(
                result,
                record=record,
                expected_country=manifest.country,
                expected_locality=manifest.locality,
                query=query,
            ),
        )
        accepted, validation = spatially_validate_geocode_result(
            result,
            expected_country=manifest.country,
            expected_locality=manifest.locality,
        )
        mismatch = _address_candidate_mismatch(accepted, record) if accepted is not None else None
        if mismatch is not None:
            validation = {
                **validation,
                "status": "address_mismatch",
                "requires_human_intervention": True,
                "reason": mismatch,
            }
            accepted = None
        attempts.append(
            {
                "query": query,
                "status": validation["status"],
                "reason": validation["reason"],
            }
        )
        final_validation = validation
        if accepted is not None:
            return (
                accepted,
                {
                    **validation,
                    "matched_query": query,
                    "attempts": attempts,
                    "candidate_options": candidate_options,
                },
                query,
            )
    return (
        None,
        {
            **final_validation,
            "attempts": attempts,
            "candidate_options": candidate_options,
        },
        requested_query,
    )


def _sample_geometry_items_payload(root: Path, sample_set_id: str) -> dict[str, Any]:
    sample_set = refresh_sample_set(root, load_sample_set(root, sample_set_id))
    items = sample_records(root, sample_set)
    return {
        "sample_set": sample_set.model_dump(mode="json"),
        "item_count": len(items),
        "items": list(items),
    }


def _latest_coverage_path(root: Path, sample_set_id: str) -> Path:
    candidates = sorted(
        root.glob(f"coverage_runs/{sample_set_id}-coverage*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"coverage review not found for sample set: {sample_set_id}")
    return candidates[0]


def _workflow_stage(
    *,
    stage_id: str,
    label: str,
    status: str,
    current: int,
    total: int,
    detail: str,
    metrics: dict[str, object] | None = None,
    action_id: str | None = None,
    action_label: str | None = None,
    indeterminate: bool = False,
) -> dict[str, object]:
    return {
        "id": stage_id,
        "label": label,
        "status": status,
        "current": current,
        "total": total,
        "detail": detail,
        "metrics": metrics or {},
        "action_id": action_id,
        "action_label": action_label,
        "indeterminate": indeterminate,
    }


def _workflow_status_payload(
    root: Path,
    *,
    manifest: Any | None = None,
    sample_set_id: str | None = None,
    active: bool = False,
) -> dict[str, object]:
    sample_set = load_sample_set(root, sample_set_id) if sample_set_id is not None else None
    if sample_set is not None:
        initial_child_ids = (
            sample_set.rounds[0].child_run_ids if sample_set.rounds else ()
        )
        all_child_ids = sample_set.combined_child_run_ids
        identity = sample_set.sample_set_id
        country = sample_set.country
        localities = sample_set.requested_localities
        facility_types = sample_set.facility_types
        target_per_job = sample_set.target
    else:
        if manifest is None:
            raise ValueError("workflow status requires a run or sample set")
        initial_child_ids = _manifest_child_run_ids(manifest)
        all_child_ids = initial_child_ids
        identity = _manifest_identity(manifest)
        country = str(getattr(manifest, "country", ""))
        locality = getattr(manifest, "locality", None)
        localities = tuple(
            str(item) for item in getattr(manifest, "localities", ())
        ) or tuple([str(locality)] if locality else [])
        profile_set = getattr(manifest, "profile_set", None)
        facility_types = tuple(
            str(item) for item in getattr(manifest, "facility_types", ())
        ) or tuple([str(profile_set)] if profile_set else [])
        target_per_job = int(getattr(manifest, "target", 1))

    child_manifests: dict[str, HarvestRunManifest] = {}
    for child_run_id in all_child_ids:
        try:
            child_manifests[child_run_id] = _load_run_manifest(root, child_run_id)
        except ValueError:
            continue

    initial_manifests = tuple(
        child_manifests[child_run_id]
        for child_run_id in initial_child_ids
        if child_run_id in child_manifests
    )
    planned_jobs = len(initial_child_ids)
    if sample_set is None and manifest is not None and manifest.summary is not None:
        planned_value = manifest.summary.get("planned_run_count")
        if isinstance(planned_value, int):
            planned_jobs = planned_value
    planned_jobs = max(planned_jobs, 1)
    finished_jobs = sum(
        child.status in {
            HarvestRunStatus.COMPLETED,
            HarvestRunStatus.FAILED,
            HarvestRunStatus.CANCELLED,
        }
        for child in initial_manifests
    )
    successful_jobs = sum(
        child.status == HarvestRunStatus.COMPLETED for child in initial_manifests
    )
    failed_jobs = finished_jobs - successful_jobs
    observation_count = 0
    for child in initial_manifests:
        if child.summary is not None:
            budget_count = child.summary.get("budget_observation_count")
            if isinstance(budget_count, int):
                observation_count += budget_count
                continue
        if child.validation_valid and Path(child.lead_path).is_file():
            observation_count += len(load_leads(Path(child.lead_path)))
    lead_quota = planned_jobs * target_per_job
    harvest_running = active and finished_jobs < planned_jobs
    if harvest_running:
        harvest_status = "running"
    elif failed_jobs or (finished_jobs >= planned_jobs and observation_count < lead_quota):
        harvest_status = "attention"
    elif finished_jobs >= planned_jobs:
        harvest_status = "complete"
    else:
        harvest_status = "ready"

    all_lead_count = 0
    review_count = 0
    verified_count = 0
    rejected_count = 0
    address_count = 0
    address_found_count = 0
    for child_run_id in all_child_ids:
        child_manifest = child_manifests.get(child_run_id)
        if (
            child_manifest is not None
            and child_manifest.validation_valid
            and Path(child_manifest.lead_path).is_file()
        ):
            all_lead_count += len(load_leads(Path(child_manifest.lead_path)))
        qaqc_path = _qaqc_output_path(root, child_run_id)
        if qaqc_path.is_file():
            reviews = load_qaqc_reviews(qaqc_path)
            review_count += len(reviews)
            verified_count += sum(
                review.verification_status.value == "verified"
                and review.recommended_action.value == "keep"
                for review in reviews
            )
            rejected_count += sum(
                review.recommended_action.value in {"reject", "retry"}
                or review.verification_status.value != "verified"
                for review in reviews
            )
        child_address_path = address_output_path(root, child_run_id)
        if child_address_path.is_file():
            address_results = load_address_results(child_address_path)
            address_count += len(address_results)
            address_found_count += sum(
                result.status.value == "found" for result in address_results
            )

    if review_count >= all_lead_count and all_lead_count > 0:
        qaqc_status = "complete"
    elif review_count > 0:
        qaqc_status = "attention"
    elif finished_jobs >= planned_jobs and all_lead_count > 0:
        qaqc_status = "ready"
    else:
        qaqc_status = "blocked"

    if verified_count > 0 and address_count >= verified_count:
        address_status = "complete"
    elif address_count > 0:
        address_status = "attention"
    elif qaqc_status == "complete" and verified_count > 0:
        address_status = "ready"
    else:
        address_status = "blocked"

    geometry_items: tuple[dict[str, Any], ...] = ()
    if verified_count > 0:
        try:
            if sample_set is not None:
                geometry_items = sample_records(root, refresh_sample_set(root, sample_set))
            elif manifest is not None:
                geometry_items = tuple(_geometry_items_payload(root, manifest)["items"])
        except (FileNotFoundError, ValueError):
            geometry_items = ()
    approved_count = len(geometry_items)
    geocoded_count = 0
    footprint_count = 0
    skipped_count = 0
    for item in geometry_items:
        geometry = item.get("geometry")
        if isinstance(geometry, dict):
            if geometry.get("point") is not None:
                geocoded_count += 1
            if geometry.get("polygon_geojson") is not None:
                footprint_count += 1
            if geometry.get("geometry_status") == "skipped":
                skipped_count += 1
    if approved_count > 0 and geocoded_count + skipped_count >= approved_count:
        geometry_status = "complete"
    elif geocoded_count or skipped_count:
        geometry_status = "running"
    elif verified_count > 0:
        geometry_status = "ready"
    else:
        geometry_status = "blocked"

    if sample_set is not None:
        sample_status = "complete"
    elif verified_count > 0:
        sample_status = "ready"
    else:
        sample_status = "blocked"

    curation_state = "not_available"
    curation_detail = "Create a sample set before human curation."
    curation_included = 0
    curation_excluded = 0
    current_curation_approval = None
    if sample_set is not None:
        all_curation_items = sample_records(root, sample_set, include_excluded=True)
        curation_manifest = load_curation(root, sample_set.sample_set_id)
        curation_item_ids = tuple(str(item["item_id"]) for item in all_curation_items)
        curation_payload = curation_summary(curation_manifest, curation_item_ids)
        curation_state = str(curation_payload["approval_status"])
        curation_included = int(curation_payload["included_count"])
        curation_excluded = int(curation_payload["excluded_count"])
        current_curation_approval = curation_manifest.approval
        if curation_state == "approved":
            curation_detail = (
                f"Human-approved snapshot includes {curation_included} observation(s) and "
                f"excludes {curation_excluded}."
            )
        elif curation_state == "stale":
            curation_detail = (
                "The sample or exclusions changed after approval. Review and approve it again."
            )
        else:
            curation_detail = (
                f"Review {len(all_curation_items)} observation(s), optionally exclude poor-fit "
                "items, then approve. Individual review is not required."
            )

    coverage_review = None
    coverage_status = "blocked"
    coverage_detail = "Approve the curated sample before coverage analysis."
    if (
        sample_set is not None
        and curation_state == "approved"
        and current_curation_approval is not None
    ):
        try:
            candidate_review = load_coverage_review(
                _latest_coverage_path(root, sample_set.sample_set_id)
            )
            if candidate_review.curation_snapshot_id == current_curation_approval.snapshot_id:
                coverage_review = candidate_review
                coverage_status = "complete"
                coverage_detail = (
                    f"Latest assessment: {coverage_review.dispersion_status.value}; "
                    f"{len(coverage_review.recommended_child_jobs)} gap-fill job(s) "
                    "recommended."
                )
            else:
                coverage_status = "ready"
                coverage_detail = "Curation changed; rerun coverage for the approved snapshot."
        except FileNotFoundError:
            coverage_status = "ready"
            coverage_detail = f"{curation_included} included observation(s) are ready to assess."

    gap_rounds = (
        tuple(round_item for round_item in sample_set.rounds if round_item.role.value == "gap_fill")
        if sample_set is not None
        else ()
    )
    latest_gap_round = gap_rounds[-1] if gap_rounds else None
    recommended_jobs = (
        len(coverage_review.recommended_child_jobs) if coverage_review is not None else 0
    )
    gap_completed = (
        len(latest_gap_round.child_run_ids) if latest_gap_round is not None else 0
    )
    if (
        latest_gap_round is not None
        and coverage_review is not None
        and latest_gap_round.recommended_coverage_id == coverage_review.coverage_id
    ):
        gap_status = (
            "complete"
            if latest_gap_round.status == HarvestRunStatus.COMPLETED
            else "attention"
        )
    elif coverage_review is not None and recommended_jobs > 0:
        gap_status = "ready"
    elif coverage_review is not None:
        gap_status = "complete"
    else:
        gap_status = "blocked"

    stages = [
        _workflow_stage(
            stage_id="scope",
            label="Define Scope",
            status="complete",
            current=planned_jobs,
            total=planned_jobs,
            detail=(
                f"{country}; {len(localities) or 1} geographic scope(s); "
                f"{len(facility_types)} facility type(s); {planned_jobs} initial job(s)."
            ),
        ),
        _workflow_stage(
            stage_id="harvest",
            label="Harvest Observations",
            status=harvest_status,
            current=observation_count,
            total=lead_quota,
            detail=(
                f"{successful_jobs}/{planned_jobs} jobs completed successfully; "
                f"{failed_jobs} failed or cancelled; "
                f"{observation_count}/{lead_quota} target observations."
            ),
            metrics={
                "planned_jobs": planned_jobs,
                "finished_jobs": finished_jobs,
                "successful_jobs": successful_jobs,
                "failed_jobs": failed_jobs,
                "lead_count": observation_count,
                "lead_quota": lead_quota,
            },
            indeterminate=harvest_running,
        ),
        _workflow_stage(
            stage_id="qaqc",
            label="Verify Evidence",
            status=qaqc_status,
            current=review_count,
            total=all_lead_count,
            detail=(
                f"{review_count}/{all_lead_count} leads reviewed; "
                f"{verified_count} verified; {rejected_count} rejected or unresolved."
            ),
            metrics={"verified_count": verified_count, "rejected_count": rejected_count},
            action_id="run_qaqc" if qaqc_status in {"ready", "attention"} else None,
            action_label="Run QAQC" if qaqc_status in {"ready", "attention"} else None,
            indeterminate=qaqc_status == "running",
        ),
        _workflow_stage(
            stage_id="address",
            label="Enrich Addresses",
            status=address_status,
            current=address_count,
            total=verified_count,
            detail=(
                f"{address_count}/{verified_count} verified facilities processed; "
                f"{address_found_count} addresses found. Optional but recommended before mapping."
            ),
            metrics={"found_count": address_found_count},
            action_id="run_address" if address_status in {"ready", "attention"} else None,
            action_label=(
                "Run Address Enrichment"
                if address_status in {"ready", "attention"}
                else None
            ),
            indeterminate=address_status == "running",
        ),
        _workflow_stage(
            stage_id="geometry",
            label="Geocode and Review Geometry",
            status=geometry_status,
            current=geocoded_count + skipped_count,
            total=approved_count or verified_count,
            detail=(
                f"{geocoded_count}/{approved_count or verified_count} geocoded; "
                f"{footprint_count} footprints saved; {skipped_count} skipped."
            ),
            metrics={
                "approved_count": approved_count,
                "geocoded_count": geocoded_count,
                "footprint_count": footprint_count,
                "skipped_count": skipped_count,
            },
            action_id="load_geometry" if geometry_status in {"ready", "running"} else None,
            action_label=(
                "Load Geometry Review"
                if geometry_status in {"ready", "running"}
                else None
            ),
        ),
        _workflow_stage(
            stage_id="sample",
            label="Create Sample Set",
            status=sample_status,
            current=1 if sample_set is not None else 0,
            total=1,
            detail=(
                f"Sample set {sample_set.sample_set_id} contains "
                f"{len(sample_set.combined_child_run_ids)} child run(s)."
                if sample_set is not None
                else "Create a durable sample after QAQC to enable coverage steering."
            ),
            action_id="create_sample" if sample_status == "ready" else None,
            action_label="Create Sample Set" if sample_status == "ready" else None,
        ),
        _workflow_stage(
            stage_id="curation",
            label="Review and Approve Sample",
            status=(
                "complete"
                if curation_state == "approved"
                else ("attention" if curation_state == "stale" else (
                    "ready" if sample_set is not None else "blocked"
                ))
            ),
            current=curation_included,
            total=curation_included + curation_excluded,
            detail=curation_detail,
            metrics={
                "included_count": curation_included,
                "excluded_count": curation_excluded,
            },
            action_id=(
                "review_curation"
                if sample_set is not None and curation_state != "approved"
                else None
            ),
            action_label=(
                "Review & Approve Sample"
                if sample_set is not None and curation_state != "approved"
                else None
            ),
        ),
        _workflow_stage(
            stage_id="coverage",
            label="Analyze Coverage",
            status=coverage_status,
            current=1 if coverage_status == "complete" else 0,
            total=1,
            detail=coverage_detail,
            action_id="analyze_coverage" if coverage_status == "ready" else None,
            action_label="Analyze Coverage" if coverage_status == "ready" else None,
            indeterminate=coverage_status == "running",
        ),
        _workflow_stage(
            stage_id="gap_fill",
            label="Fill Coverage Gaps",
            status=gap_status,
            current=gap_completed,
            total=recommended_jobs,
            detail=(
                f"{gap_completed}/{recommended_jobs} recommended gap-fill job(s) completed."
                if recommended_jobs
                else "No outstanding gap-fill jobs are currently recommended."
            ),
            action_id="run_gap_fill" if gap_status in {"ready", "attention"} else None,
            action_label="Run Gap Fill" if gap_status in {"ready", "attention"} else None,
            indeterminate=gap_status == "running",
        ),
        _workflow_stage(
            stage_id="export",
            label="Export Dataset",
            status="ready" if (approved_count if sample_set else verified_count) > 0 else "blocked",
            current=approved_count if sample_set else verified_count,
            total=approved_count if sample_set else verified_count,
            detail=(
                f"{approved_count if sample_set else verified_count} included observation(s) "
                "are available for export."
            ),
            action_id=(
                "export_json" if (approved_count if sample_set else verified_count) > 0 else None
            ),
            action_label=(
                "Download Verified JSON"
                if (approved_count if sample_set else verified_count) > 0
                else None
            ),
        ),
    ]
    next_action = next(
        (
            {
                "stage_id": stage["id"],
                "id": stage["action_id"],
                "label": stage["action_label"],
            }
            for stage in stages
            if stage["status"] in {"ready", "attention"} and stage["action_id"] is not None
        ),
        None,
    )
    return {
        "workflow_id": identity,
        "sample_set_id": sample_set.sample_set_id if sample_set is not None else None,
        "active": active,
        "next_action": next_action,
        "stages": stages,
    }


def _sample_verified_export_response(
    root: Path,
    sample_set_id: str,
    *,
    output_format: str,
) -> Response:
    try:
        sample_set = refresh_sample_set(root, load_sample_set(root, sample_set_id))
        items = sample_records(root, sample_set)
    except ValueError as exc:
        return _json_error(str(exc), status_code=404)

    if output_format == "json":
        payload = verified_json(items)
        media_type = "application/json"
        filename = f"{sample_set_id}.verified.json"
    elif output_format == "csv":
        payload = verified_csv(items)
        media_type = "text/csv"
        filename = f"{sample_set_id}.verified.csv"
    elif output_format == "geojson":
        payload = footprints_geojson(items)
        media_type = "application/geo+json"
        filename = f"{sample_set_id}.footprints.geojson"
    else:
        return _json_error(f"unsupported sample export format: {output_format}")

    return PlainTextResponse(
        payload,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _sample_component_export_response(
    root: Path,
    sample_set_id: str,
    *,
    output_format: str,
) -> Response:
    try:
        sample_set = refresh_sample_set(root, load_sample_set(root, sample_set_id))
        records: list[dict[str, Any]] = []
        round_by_child: dict[str, int] = {}
        for sample_round in sample_set.rounds:
            for child_run_id in sample_round.child_run_ids:
                round_by_child[child_run_id] = sample_round.round_number
        for child_run_id in sample_set.combined_child_run_ids:
            manifest = _load_run_manifest(root, child_run_id)
            for record in _approved_component_records_for_child(root, manifest):
                payload = dict(record)
                payload["sample_set_id"] = sample_set.sample_set_id
                payload["sample_round"] = round_by_child.get(child_run_id, "")
                records.append(payload)
    except FileNotFoundError as exc:
        return _json_error(str(exc), status_code=409)
    except ValueError as exc:
        return _json_error(str(exc), status_code=404)

    if output_format == "json":
        response_payload = _component_records_json(records)
        media_type = "application/json"
        filename = f"{sample_set_id}.components.json"
    elif output_format == "csv":
        response_payload = _component_records_csv(records)
        media_type = "text/csv"
        filename = f"{sample_set_id}.components.csv"
    else:
        return _json_error(f"unsupported component export format: {output_format}")

    return PlainTextResponse(
        response_payload,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _run_qaqc_missing_for_sample(
    *,
    root: Path,
    sample_set_id: str,
    codex_bin: str,
    runner: CodexRunner,
) -> dict[str, Any]:
    sample_set = load_sample_set(root, sample_set_id)
    missing = tuple(
        child_run_id
        for child_run_id in sample_set.combined_child_run_ids
        if not _qaqc_output_path(root, child_run_id).is_file()
    )
    append_harvest_log(
        root,
        sample_set_id,
        f"Starting missing QAQC for {len(missing)} child run(s).",
    )
    child_results = [
        _run_qaqc_for_child(
            root=root,
            run_id=child_run_id,
            parent_id=sample_set_id,
            codex_bin=codex_bin,
            runner=runner,
        )
        for child_run_id in missing
    ]
    refreshed = refresh_sample_set(root, sample_set)
    return {
        "parent_id": sample_set_id,
        "child_run_ids": missing,
        "child_results": child_results,
        "sample_set": refreshed.model_dump(mode="json"),
    }


def _run_address_missing_for_sample(
    *,
    root: Path,
    sample_set_id: str,
    codex_bin: str,
    runner: CodexRunner,
) -> dict[str, Any]:
    sample_set = load_sample_set(root, sample_set_id)
    skipped_needs_qaqc = tuple(
        child_run_id
        for child_run_id in sample_set.combined_child_run_ids
        if not _qaqc_output_path(root, child_run_id).is_file()
    )
    missing = tuple(
        child_run_id
        for child_run_id in sample_set.combined_child_run_ids
        if _qaqc_output_path(root, child_run_id).is_file()
        and not address_output_path(root, child_run_id).is_file()
    )
    append_harvest_log(
        root,
        sample_set_id,
        f"Starting missing address enrichment for {len(missing)} child run(s).",
    )
    if skipped_needs_qaqc:
        append_harvest_log(
            root,
            sample_set_id,
            f"Skipped {len(skipped_needs_qaqc)} address child run(s) that need QAQC first.",
        )
    child_results = [
        _run_address_for_child(
            root=root,
            run_id=child_run_id,
            parent_id=sample_set_id,
            codex_bin=codex_bin,
            runner=runner,
        )
        for child_run_id in missing
    ]
    refreshed = refresh_sample_set(root, sample_set)
    return {
        "parent_id": sample_set_id,
        "child_run_ids": missing,
        "skipped_needs_qaqc": skipped_needs_qaqc,
        "child_results": child_results,
        "sample_set": refreshed.model_dump(mode="json"),
    }


def _verified_export_response(root: Path, run_id: str, *, output_format: str) -> Response:
    try:
        manifest = _load_any_manifest(root, run_id)
        records = merge_address_results(root, _approved_records_for_manifest(root, manifest))
        items = tuple(merge_geometry_items(root, records))
    except FileNotFoundError as exc:
        return _json_error(str(exc), status_code=409)
    except ValueError as exc:
        return _json_error(str(exc), status_code=404)

    if output_format == "json":
        payload = verified_json(items)
        media_type = "application/json"
        filename = f"{run_id}.verified.json"
    elif output_format == "csv":
        payload = verified_csv(items)
        media_type = "text/csv"
        filename = f"{run_id}.verified.csv"
    elif output_format == "geojson":
        payload = footprints_geojson(items)
        media_type = "application/geo+json"
        filename = f"{run_id}.footprints.geojson"
    else:
        return _json_error(f"unsupported verified export format: {output_format}")

    return PlainTextResponse(
        payload,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _component_export_response(root: Path, run_id: str, *, output_format: str) -> Response:
    try:
        manifest = _load_any_manifest(root, run_id)
        records = _approved_component_records_for_manifest(root, manifest)
    except FileNotFoundError as exc:
        return _json_error(str(exc), status_code=409)
    except ValueError as exc:
        return _json_error(str(exc), status_code=404)

    if output_format == "json":
        payload = _component_records_json(records)
        media_type = "application/json"
        filename = f"{run_id}.components.json"
    elif output_format == "csv":
        payload = _component_records_csv(records)
        media_type = "text/csv"
        filename = f"{run_id}.components.csv"
    else:
        return _json_error(f"unsupported component export format: {output_format}")

    return PlainTextResponse(
        payload,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _table_rows_from_records(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        lead = record["lead"]
        location = lead["location"]
        review = record.get("qaqc_review") or {}
        address = record.get("address_enrichment") or {}
        geometry = record.get("geometry") or {}
        for count_index, datum in enumerate(lead["occupancy_data"]):
            rows.append(
                {
                    "row_id": f"{record['item_id']}-{count_index}",
                    "item_id": record["item_id"],
                    "run_id": record["child_run_id"],
                    "sample_set_id": record.get("sample_set_id", ""),
                    "sample_round": record.get("sample_round", ""),
                    "facility_type": record.get("facility_type", ""),
                    "evidence_role": "direct_occupancy",
                    "lead_index": record["lead_index"],
                    "count_index": count_index,
                    "facility_name": location["facility_name"],
                    "count": datum["count"],
                    "group_type": datum["group_type"],
                    "component_type": "",
                    "value": "",
                    "unit": "",
                    "time_basis": "",
                    "geography_level": "",
                    "incident_date": lead["incident_date"],
                    "incident_time": lead["incident_time"],
                    "strategy_id": lead.get("strategy_id") or "",
                    "representativeness": lead.get("representativeness") or "",
                    "confidence": lead.get("confidence") or "",
                    "city_or_region": location["city_or_region"],
                    "country": location["country"],
                    "source_url": lead["source_url"],
                    "qaqc_status": review.get("verification_status", ""),
                    "recommended_action": review.get("recommended_action", ""),
                    "address_status": record.get("address_status", ""),
                    "enriched_address": address.get("formatted_address") or "",
                    "geometry_status": record.get("geometry_status", ""),
                    "area_m2": record.get("area_m2") or geometry.get("area_m2") or "",
                    "review_notes": (
                        review.get("review_notes")
                        or address.get("review_notes")
                        or lead.get("review_notes")
                        or ""
                    ),
                    "excluded_from_dataset": False,
                    "exclusion_reason_code": "",
                    "exclusion_reason_note": "",
                }
            )
    return rows


def _all_lead_table_rows(root: Path, manifest: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for child_run_id in _manifest_child_run_ids(manifest):
        child_manifest = _load_run_manifest(root, child_run_id)
        evidence_set = load_evidence_set(Path(child_manifest.lead_path))
        for lead_index, lead in enumerate(evidence_set.occupancy_leads):
            lead_payload = lead.model_dump(mode="json")
            location = lead_payload["location"]
            item_id = f"{child_run_id}-{lead_index}"
            for count_index, datum in enumerate(lead_payload["occupancy_data"]):
                rows.append(
                    {
                        "row_id": f"{item_id}-{count_index}",
                        "item_id": item_id,
                        "run_id": child_run_id,
                        "sample_set_id": "",
                        "sample_round": "",
                        "facility_type": child_manifest.profile_set,
                        "evidence_role": "direct_occupancy",
                        "lead_index": lead_index,
                        "count_index": count_index,
                        "facility_name": location["facility_name"],
                        "count": datum["count"],
                        "group_type": datum["group_type"],
                        "component_type": "",
                        "value": "",
                        "unit": "",
                        "time_basis": "",
                        "geography_level": "",
                        "incident_date": lead_payload["incident_date"],
                        "incident_time": lead_payload["incident_time"],
                        "strategy_id": lead_payload.get("strategy_id") or "",
                        "representativeness": lead_payload.get("representativeness") or "",
                        "confidence": lead_payload.get("confidence") or "",
                        "city_or_region": location["city_or_region"],
                        "country": location["country"],
                        "source_url": lead_payload["source_url"],
                        "qaqc_status": "",
                        "recommended_action": "",
                        "address_status": "",
                        "enriched_address": "",
                        "geometry_status": "",
                        "area_m2": "",
                        "review_notes": lead_payload.get("review_notes") or "",
                        "excluded_from_dataset": False,
                        "exclusion_reason_code": "",
                        "exclusion_reason_note": "",
                    }
                )
        for lead_index, component_lead in enumerate(evidence_set.component_leads):
            lead_payload = component_lead.model_dump(mode="json")
            location = lead_payload.get("location") or {}
            item_id = f"{child_run_id}-component-{lead_index}"
            for count_index, datum in enumerate(lead_payload["component_data"]):
                rows.append(
                    {
                        "row_id": f"{item_id}-{count_index}",
                        "item_id": item_id,
                        "run_id": child_run_id,
                        "sample_set_id": "",
                        "sample_round": "",
                        "facility_type": child_manifest.profile_set,
                        "evidence_role": "component_input",
                        "lead_index": lead_index,
                        "count_index": count_index,
                        "facility_name": location.get(
                            "facility_name", lead_payload["geography_name"]
                        ),
                        "count": "",
                        "group_type": "",
                        "component_type": datum["component_type"],
                        "value": datum["value"],
                        "unit": datum["unit"],
                        "time_basis": datum["time_basis"],
                        "geography_level": datum["geography_level"],
                        "incident_date": datum.get("period_label") or "",
                        "incident_time": "",
                        "strategy_id": lead_payload.get("strategy_id") or "",
                        "representativeness": lead_payload.get("representativeness") or "",
                        "confidence": lead_payload.get("confidence") or "",
                        "city_or_region": (
                            location.get("city_or_region")
                            or lead_payload.get("geography_name")
                            or ""
                        ),
                        "country": lead_payload["country"],
                        "source_url": lead_payload["source_url"],
                        "qaqc_status": "",
                        "recommended_action": "",
                        "address_status": "not_applicable",
                        "enriched_address": "",
                        "geometry_status": "not_applicable",
                        "area_m2": "",
                        "review_notes": lead_payload.get("review_notes") or "",
                        "excluded_from_dataset": False,
                        "exclusion_reason_code": "",
                        "exclusion_reason_note": "",
                    }
                )
    return rows


def _table_rows_from_component_records(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        lead = record["component_lead"]
        review = record.get("component_qaqc_review") or {}
        location = lead.get("location") or {}
        for count_index, datum in enumerate(lead["component_data"]):
            rows.append(
                {
                    "row_id": f"{record['item_id']}-{count_index}",
                    "item_id": record["item_id"],
                    "run_id": record["child_run_id"],
                    "sample_set_id": record.get("sample_set_id", ""),
                    "sample_round": record.get("sample_round", ""),
                    "facility_type": record.get("facility_type", ""),
                    "evidence_role": "component_input",
                    "lead_index": record["lead_index"],
                    "count_index": count_index,
                    "facility_name": location.get("facility_name", lead["geography_name"]),
                    "count": "",
                    "group_type": "",
                    "component_type": datum["component_type"],
                    "value": datum["value"],
                    "unit": datum["unit"],
                    "time_basis": datum["time_basis"],
                    "geography_level": datum["geography_level"],
                    "incident_date": datum.get("period_label") or "",
                    "incident_time": "",
                    "strategy_id": lead.get("strategy_id") or "",
                    "representativeness": lead.get("representativeness") or "",
                    "confidence": lead.get("confidence") or "",
                    "city_or_region": (
                        location.get("city_or_region") or lead.get("geography_name") or ""
                    ),
                    "country": lead["country"],
                    "source_url": lead["source_url"],
                    "qaqc_status": review.get("verification_status", ""),
                    "recommended_action": review.get("recommended_action", ""),
                    "address_status": "not_applicable",
                    "enriched_address": "",
                    "geometry_status": "not_applicable",
                    "area_m2": "",
                    "review_notes": review.get("review_notes") or lead.get("review_notes") or "",
                    "excluded_from_dataset": False,
                    "exclusion_reason_code": "",
                    "exclusion_reason_note": "",
                }
            )
    return rows


def _run_table_payload(root: Path, run_id: str, *, mode: str) -> dict[str, Any]:
    manifest = _load_any_manifest(root, run_id)
    if mode == "all":
        rows = _all_lead_table_rows(root, manifest)
    elif mode == "verified":
        records = merge_address_results(root, _approved_records_for_manifest(root, manifest))
        rows = _table_rows_from_records(tuple(merge_geometry_items(root, records)))
        component_records = _approved_component_records_for_manifest(root, manifest)
        rows.extend(_table_rows_from_component_records(component_records))
    else:
        raise ValueError(f"unsupported table mode: {mode}")
    return {
        "mode": mode,
        "context_type": "run",
        "context_id": run_id,
        "row_count": len(rows),
        "rows": rows,
    }


def _sample_table_payload(root: Path, sample_set_id: str, *, mode: str) -> dict[str, Any]:
    if mode != "verified":
        raise ValueError("sample table only supports verified mode")
    sample_set = refresh_sample_set(root, load_sample_set(root, sample_set_id))
    records = sample_records(root, sample_set, include_excluded=True)
    component_records: list[dict[str, Any]] = []
    round_by_child: dict[str, int] = {}
    for sample_round in sample_set.rounds:
        for child_run_id in sample_round.child_run_ids:
            round_by_child[child_run_id] = sample_round.round_number
    for child_run_id in sample_set.combined_child_run_ids:
        manifest = _load_run_manifest(root, child_run_id)
        for record in _approved_component_records_for_child(root, manifest):
            payload = dict(record)
            payload["sample_set_id"] = sample_set.sample_set_id
            payload["sample_round"] = round_by_child.get(child_run_id, "")
            component_records.append(payload)
    curation = load_curation(root, sample_set_id)
    decisions = {decision.item_id: decision for decision in curation.decisions}
    rows = _table_rows_from_records(records)
    rows.extend(_table_rows_from_component_records(component_records))
    for row in rows:
        decision = decisions.get(str(row["item_id"]))
        if decision is None:
            continue
        row["excluded_from_dataset"] = True
        row["exclusion_reason_code"] = decision.reason_code.value
        row["exclusion_reason_note"] = decision.reason_note or ""
    item_ids = tuple(str(record["item_id"]) for record in records)
    return {
        "mode": mode,
        "context_type": "sample",
        "context_id": sample_set_id,
        "row_count": len(rows),
        "rows": rows,
        "curation": curation_summary(curation, item_ids),
    }


def _sample_curation_context(
    root: Path,
    sample_set_id: str,
) -> tuple[Any, tuple[dict[str, Any], ...], Any, tuple[str, ...]]:
    sample_set = refresh_sample_set(root, load_sample_set(root, sample_set_id))
    records = sample_records(root, sample_set, include_excluded=True)
    curation = load_curation(root, sample_set_id)
    item_ids = tuple(str(record["item_id"]) for record in records)
    return sample_set, records, curation, item_ids


def _sample_curation_payload(root: Path, sample_set_id: str) -> dict[str, Any]:
    sample_set, _, curation, item_ids = _sample_curation_context(root, sample_set_id)
    return {
        "sample_set": sample_set.model_dump(mode="json"),
        "curation": curation_summary(curation, item_ids),
    }


def _read_log_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _combined_log_text(root: Path, manifest: Any) -> str:
    chunks: list[str] = []
    log_path = getattr(manifest, "log_path", None)
    if log_path is not None:
        text = _read_log_text(Path(log_path))
        if text:
            chunks.append(text.rstrip())
    for child_run_id in _manifest_child_run_ids(manifest):
        qaqc_log_path = log_path_for_run(root, _qaqc_id_for_run(child_run_id))
        text = _read_log_text(qaqc_log_path)
        if text:
            chunks.append(f"--- QAQC subprocess log: {child_run_id} ---\n{text.rstrip()}")
        address_log_path = log_path_for_run(root, _address_id_for_run(child_run_id))
        text = _read_log_text(address_log_path)
        if text:
            chunks.append(f"--- Address subprocess log: {child_run_id} ---\n{text.rstrip()}")
    return "\n".join(chunks) + ("\n" if chunks else "")


async def _wait_for_manifest(load_manifest: Callable[[], Any]) -> Any:
    last_error: Exception | None = None
    for _ in range(20):
        try:
            return load_manifest()
        except ValueError as exc:
            last_error = exc
            await asyncio.sleep(0.05)
    if last_error is not None:
        raise last_error
    return load_manifest()


def create_app(
    *,
    workspace: Path,
    codex_bin: str = "codex",
    runner: CodexRunner | None = None,
    geocoder: Geocoder | None = None,
    background: bool = True,
    shutdown_callback: Callable[[], None] | None = None,
) -> Starlette:
    root = workspace.resolve()
    root.mkdir(parents=True, exist_ok=True)
    registry = ActiveCodexRegistry(root)
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="harvest")
    app_runner = runner or registry.runner
    app_geocoder = geocoder or NominatimGeocoder(root)

    async def index(request: Request) -> HTMLResponse:
        return HTMLResponse(INDEX_HTML)

    async def oasis_logo(request: Request) -> FileResponse:
        return FileResponse(
            Path(__file__).with_name("static") / "oasis-logo.jpg",
            media_type="image/jpeg",
        )

    async def profiles(request: Request) -> JSONResponse:
        return JSONResponse(_profiles_payload())

    async def geographer_plan(request: Request) -> JSONResponse:
        try:
            data = GeographerPlanRequest.model_validate(await _request_json(request))
            plan_id = (
                build_harvest_campaign_id(
                    country=data.country,
                    localities=data.localities,
                    facility_types=data.facility_types,
                )
                if data.mode == "campaign"
                else (
                build_harvest_batch_id(
                    country=data.country,
                    locality=data.locality,
                    profile_set_name=data.profiles,
                )
                if data.mode == "batch"
                else build_harvest_run_id(
                    country=data.country,
                    locality=data.locality,
                    profile_set_name=data.profiles,
                    profile_id=data.profile,
                )
                )
            )
            if data.mode == "campaign" and not data.facility_types:
                raise ValueError("campaign geographer review requires facility types")
            task = partial(
                run_geographer,
                root=root,
                plan_id=plan_id,
                country=data.country,
                locality=data.locality,
                profile_set_name=("campaign" if data.mode == "campaign" else data.profiles),
                profile_id=(None if data.mode == "campaign" else data.profile),
                localities=data.localities,
                facility_types=data.facility_types,
                codex_bin=codex_bin,
                runner=app_runner,
            )
            plan = await run_in_threadpool(task)
            return JSONResponse(
                {
                    "plan": plan.model_dump(mode="json"),
                    "plan_path": plan.artifact_path,
                    "run_id": plan_id,
                    "dialogue": render_dialogue(load_dialogue(root, plan_id)),
                }
            )
        except (ValidationError, ValueError) as exc:
            return _json_error(str(exc))

    async def harvest_run(request: Request) -> JSONResponse:
        try:
            data = HarvestRunRequest.model_validate(await _request_json(request))
            run_id = data.run_id or build_harvest_run_id(
                country=data.country,
                locality=data.locality,
                profile_set_name=data.profiles,
                profile_id=data.profile,
            )
            geographer = (
                load_geographer_plan(root, data.geographer_plan_path)
                if data.geographer_plan_path
                else None
            )
            if geographer is not None and geographer.plan_id != run_id:
                raise ValueError("geographer plan does not belong to this harvest run")
            task = partial(
                run_harvest,
                root=root,
                country=data.country,
                locality=data.locality,
                profile_set_name=data.profiles,
                profile_id=data.profile,
                count_method_override=data.count_method_override,
                target=data.target,
                run_id=run_id,
                codex_bin=codex_bin,
                runner=app_runner,
                geographer_plan=geographer,
            )
            if background:
                job = create_job(
                    root,
                    job_id=run_id,
                    job_type=JobType.HARVEST,
                    manifest_path=str(_run_manifest_path(root, run_id)),
                    log_path=str(log_path_for_run(root, run_id)),
                )
                executor.submit(
                    run_background_job,
                    root=root,
                    registry=registry,
                    identity=run_id,
                    job_id=job.job_id,
                    log=lambda message: append_harvest_log(
                        root,
                        run_id,
                        f"Harvest failed: {message}.",
                    ),
                    task=task,
                    manifest_path=lambda result: str(_run_manifest_path(root, result.run_id)),
                    summary=lambda result: result.summary or {},
                    on_error=lambda exc: _finalize_failed_run_manifest(
                        root=root,
                        run_id=run_id,
                        country=data.country,
                        locality=data.locality,
                        profile_set=data.profiles,
                        profile_id=data.profile,
                        count_method_override=data.count_method_override,
                        target=data.target,
                        error_message=str(exc),
                    ),
                )
                return JSONResponse(
                    {
                        "started": True,
                        "job_id": job.job_id,
                        "job": job_payload(job, active=True),
                        "manifest": _pseudo_manifest_from_job(job),
                        "summary": job.summary,
                        "leads": [],
                    }
                )
            else:
                manifest = await run_in_threadpool(task)
            leads = _leads_payload(manifest.lead_path) if manifest.validation_valid else []
            return JSONResponse(
                {
                    "manifest": manifest.model_dump(mode="json"),
                    "summary": manifest.summary,
                    "leads": leads,
                }
            )
        except (ValidationError, ValueError) as exc:
            return _json_error(str(exc))

    async def harvest_batch_run(request: Request) -> JSONResponse:
        try:
            data = HarvestBatchRunRequest.model_validate(await _request_json(request))
            batch_id = data.batch_id or build_harvest_batch_id(
                country=data.country,
                locality=data.locality,
                profile_set_name=data.profiles,
            )
            geographer = (
                load_geographer_plan(root, data.geographer_plan_path)
                if data.geographer_plan_path
                else None
            )
            if geographer is not None and geographer.plan_id != batch_id:
                raise ValueError("geographer plan does not belong to this harvest batch")
            task = partial(
                run_harvest_batch,
                root=root,
                country=data.country,
                locality=data.locality,
                profile_set_name=data.profiles,
                target=data.target,
                count_method_override=data.count_method_override,
                batch_id=batch_id,
                codex_bin=codex_bin,
                runner=app_runner,
                geographer_plan=geographer,
            )
            if background:
                job = create_job(
                    root,
                    job_id=batch_id,
                    job_type=JobType.BATCH,
                    manifest_path=str(_batch_manifest_path(root, batch_id)),
                    log_path=str(log_path_for_run(root, batch_id)),
                )
                executor.submit(
                    run_background_job,
                    root=root,
                    registry=registry,
                    identity=batch_id,
                    job_id=job.job_id,
                    log=lambda message: append_harvest_log(
                        root,
                        batch_id,
                        f"Batch failed: {message}.",
                    ),
                    task=task,
                    manifest_path=lambda result: str(_batch_manifest_path(root, result.batch_id)),
                    summary=lambda result: result.summary or {},
                    on_error=lambda exc: _finalize_failed_batch_manifest(
                        root=root,
                        batch_id=batch_id,
                        country=data.country,
                        locality=data.locality,
                        profile_set=data.profiles,
                        count_method_override=data.count_method_override,
                        target=data.target,
                        error_message=str(exc),
                    ),
                )
                return JSONResponse(
                    {
                        "started": True,
                        "job_id": job.job_id,
                        "job": job_payload(job, active=True),
                        "manifest": _pseudo_manifest_from_job(job),
                    }
                )
            else:
                manifest = await run_in_threadpool(task)
            return JSONResponse({"manifest": manifest.model_dump(mode="json")})
        except (ValidationError, ValueError) as exc:
            return _json_error(str(exc))

    async def harvest_campaign_run(request: Request) -> JSONResponse:
        try:
            data = HarvestCampaignRunRequest.model_validate(await _request_json(request))
            campaign_id = data.campaign_id or build_harvest_campaign_id(
                country=data.country,
                localities=data.localities,
                facility_types=data.facility_types,
            )
            geographer = (
                load_geographer_plan(root, data.geographer_plan_path)
                if data.geographer_plan_path
                else None
            )
            if geographer is not None and geographer.plan_id != campaign_id:
                raise ValueError("geographer plan does not belong to this harvest campaign")
            task = partial(
                run_harvest_campaign,
                root=root,
                country=data.country,
                localities=data.localities,
                facility_types=data.facility_types,
                target=data.target,
                count_method_override=data.count_method_override,
                campaign_id=campaign_id,
                codex_bin=codex_bin,
                runner=app_runner,
                geographer_plan=geographer,
            )
            if background:
                job = create_job(
                    root,
                    job_id=campaign_id,
                    job_type=JobType.CAMPAIGN,
                    manifest_path=str(_campaign_manifest_path(root, campaign_id)),
                    log_path=str(log_path_for_run(root, campaign_id)),
                )
                executor.submit(
                    run_background_job,
                    root=root,
                    registry=registry,
                    identity=campaign_id,
                    job_id=job.job_id,
                    log=lambda message: append_harvest_log(
                        root,
                        campaign_id,
                        f"Campaign failed: {message}.",
                    ),
                    task=task,
                    manifest_path=lambda result: str(
                        _campaign_manifest_path(root, result.campaign_id)
                    ),
                    summary=lambda result: result.summary or {},
                    on_error=lambda exc: _finalize_failed_campaign_manifest(
                        root=root,
                        campaign_id=campaign_id,
                        country=data.country,
                        localities=data.localities,
                        facility_types=data.facility_types,
                        count_method_override=data.count_method_override,
                        target=data.target,
                        error_message=str(exc),
                    ),
                )
                return JSONResponse(
                    {
                        "started": True,
                        "job_id": job.job_id,
                        "job": job_payload(job, active=True),
                        "manifest": _pseudo_manifest_from_job(job),
                    }
                )
            else:
                manifest = await run_in_threadpool(task)
            return JSONResponse({"manifest": manifest.model_dump(mode="json")})
        except (ValidationError, ValueError) as exc:
            return _json_error(str(exc))

    async def runs(request: Request) -> JSONResponse:
        return JSONResponse({"runs": _list_run_history(root)})

    async def clear_runs(request: Request) -> JSONResponse:
        active_count = registry.active_count()
        if active_count:
            return _json_error(
                f"Cannot clear history while {active_count} harvest process(es) are active.",
                status_code=409,
            )
        deleted_count = _clear_runtime_history(root)
        return JSONResponse({"cleared": True, "deleted_files": deleted_count})

    async def run_detail(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        try:
            manifest = _load_any_manifest(root, run_id)
            return JSONResponse({"manifest": manifest.model_dump(mode="json")})
        except ValueError as exc:
            return _json_error(str(exc), status_code=404)

    async def run_status(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        try:
            manifest = _load_any_manifest(root, run_id)
            identity = _manifest_identity(manifest)
            try:
                job = load_job(root, identity)
            except ValueError:
                job = None
            active = registry.is_active(identity)
            return JSONResponse(
                {
                    "manifest": _manifest_payload_with_job_state(manifest, job),
                    "active": active,
                    "job": job_payload(job, active=active) if job else None,
                }
            )
        except ValueError:
            try:
                job = load_job(root, run_id)
                active = registry.is_active(run_id)
                return JSONResponse(
                    {
                        "manifest": _pseudo_manifest_from_job(job),
                        "active": active,
                        "job": job_payload(job, active=active),
                    }
                )
            except ValueError as exc:
                return _json_error(str(exc), status_code=404)

    async def run_workflow_status(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        try:
            manifest = _load_any_manifest(root, run_id)
            return JSONResponse(
                _workflow_status_payload(
                    root,
                    manifest=manifest,
                    active=registry.is_active(_manifest_identity(manifest)),
                )
            )
        except ValueError:
            try:
                job = load_job(root, run_id)
            except ValueError as exc:
                return _json_error(str(exc), status_code=404)
            active = registry.is_active(run_id)
            return JSONResponse(
                {
                    "stages": [],
                    "next_action": None,
                    "active": active,
                    "job": job_payload(job, active=active),
                }
            )

    async def run_log(request: Request) -> PlainTextResponse:
        run_id = request.path_params["run_id"]
        try:
            manifest = _load_any_manifest(root, run_id)
        except ValueError as exc:
            try:
                job = load_job(root, run_id)
            except ValueError:
                return PlainTextResponse(str(exc), status_code=404)
            if job.log_path:
                return PlainTextResponse(
                    _read_log_text(Path(job.log_path)),
                    media_type="text/plain",
                )
            return PlainTextResponse("", media_type="text/plain")
        return PlainTextResponse(_combined_log_text(root, manifest), media_type="text/plain")

    async def run_dialogue(request: Request) -> PlainTextResponse:
        run_id = request.path_params["run_id"]
        return PlainTextResponse(
            _pipeline_transcript_text(root, run_id=run_id),
            media_type="text/plain",
        )

    async def run_transcript_download(request: Request) -> PlainTextResponse:
        run_id = request.path_params["run_id"]
        return PlainTextResponse(
            _pipeline_transcript_text(root, run_id=run_id),
            media_type="text/plain",
            headers={
                "content-disposition": f'attachment; filename="{run_id}-pipeline-transcript.txt"'
            },
        )

    async def cancel_run(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        try:
            manifest = _load_any_manifest(root, run_id)
            identity = _manifest_identity(manifest)
        except ValueError as exc:
            try:
                load_job(root, run_id)
                identity = run_id
            except ValueError:
                return _json_error(str(exc), status_code=404)
        cancelled_count = registry.cancel(identity)
        if cancelled_count == 0:
            return JSONResponse(
                {
                    "cancelled": False,
                    "active": False,
                    "message": "Run is not active in this app session.",
                }
            )
        return JSONResponse({"cancelled": True, "active": True, "count": cancelled_count})

    async def exit_app(request: Request) -> JSONResponse:
        cancelled_count = registry.cancel_all()

        if shutdown_callback is not None:
            shutdown_callback()

        return JSONResponse(
            {
                "shutting_down": shutdown_callback is not None,
                "cancelled_processes": cancelled_count,
            }
        )

    async def run_leads(request: Request) -> JSONResponse:
        try:
            manifest = _load_run_manifest(root, request.path_params["run_id"])
            return JSONResponse({"leads": _leads_payload(manifest.lead_path)})
        except ValueError as exc:
            return _json_error(str(exc), status_code=404)

    async def run_qaqc_prompt(request: Request) -> PlainTextResponse:
        run_id = request.path_params["run_id"]
        try:
            manifest = _load_any_manifest(root, run_id)
            if not isinstance(manifest, HarvestRunManifest):
                return PlainTextResponse(
                    "QAQC prompts are generated for child harvest runs. Open a child run from "
                    "this batch/campaign, then try again.",
                    status_code=400,
                )
            evidence_set = load_evidence_set(Path(manifest.lead_path))
            prompt = render_lead_qaqc_prompt(
                evidence_set,
                source_label=manifest.lead_path,
                expected_country=manifest.country,
                expected_locality=manifest.locality,
            )
            return PlainTextResponse(prompt, media_type="text/plain")
        except ValueError as exc:
            return PlainTextResponse(str(exc), status_code=404)

    async def run_qaqc(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        try:
            manifest = _load_any_manifest(root, run_id)
        except ValueError as exc:
            return _json_error(str(exc), status_code=404)
        identity = _manifest_identity(manifest)
        if registry.is_active(identity):
            return _json_error(f"Run already has active work: {identity}", status_code=409)

        task = partial(
            _run_qaqc_for_manifest,
            root=root,
            manifest=manifest,
            codex_bin=codex_bin,
            runner=app_runner,
        )
        if background:
            job = create_job(
                root,
                job_id=_qaqc_id_for_run(identity),
                job_type=JobType.QAQC,
                parent_id=identity,
                log_path=str(log_path_for_run(root, _qaqc_id_for_run(identity))),
                active_child_ids=_manifest_child_run_ids(manifest),
            )
            executor.submit(
                run_background_job,
                root=root,
                registry=registry,
                identity=identity,
                job_id=job.job_id,
                log=lambda message: append_harvest_log(root, identity, f"QAQC failed: {message}."),
                task=task,
                summary=lambda result: (
                    result.get("summary", {}) if isinstance(result, dict) else {}
                ),
            )
            return JSONResponse(
                {
                    "started": True,
                    "job_id": job.job_id,
                    "job": job_payload(job, active=True),
                    "parent_id": identity,
                    "child_run_ids": _manifest_child_run_ids(manifest),
                    "manifest": manifest.model_dump(mode="json"),
                }
            )

        result = await run_in_threadpool(task)
        return JSONResponse(
            {
                "started": False,
                "parent_id": identity,
                "manifest": manifest.model_dump(mode="json"),
                "qaqc": result,
            }
        )

    async def run_qaqc_reviews(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        try:
            manifest = _load_any_manifest(root, run_id)
            return JSONResponse(_qaqc_reviews_payload(root, _manifest_child_run_ids(manifest)))
        except ValueError as exc:
            return _json_error(str(exc), status_code=404)

    async def run_address(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        try:
            manifest = _load_any_manifest(root, run_id)
        except ValueError as exc:
            return _json_error(str(exc), status_code=404)
        identity = _manifest_identity(manifest)
        if registry.is_active(identity):
            return _json_error(f"Run already has active work: {identity}", status_code=409)

        task = partial(
            _run_address_for_manifest,
            root=root,
            manifest=manifest,
            codex_bin=codex_bin,
            runner=app_runner,
        )
        if background:
            job = create_job(
                root,
                job_id=_address_id_for_run(identity),
                job_type=JobType.ADDRESS,
                parent_id=identity,
                log_path=str(log_path_for_run(root, _address_id_for_run(identity))),
                active_child_ids=_manifest_child_run_ids(manifest),
            )
            executor.submit(
                run_background_job,
                root=root,
                registry=registry,
                identity=identity,
                job_id=job.job_id,
                log=lambda message: append_harvest_log(
                    root,
                    identity,
                    f"Address enrichment failed: {message}.",
                ),
                task=task,
                summary=lambda result: (
                    result.get("summary", {}) if isinstance(result, dict) else {}
                ),
            )
            return JSONResponse(
                {
                    "started": True,
                    "job_id": job.job_id,
                    "job": job_payload(job, active=True),
                    "parent_id": identity,
                    "child_run_ids": _manifest_child_run_ids(manifest),
                    "manifest": manifest.model_dump(mode="json"),
                }
            )

        try:
            result = await run_in_threadpool(task)
        except FileNotFoundError as exc:
            return _json_error(str(exc), status_code=409)
        return JSONResponse(
            {
                "started": False,
                "parent_id": identity,
                "manifest": manifest.model_dump(mode="json"),
                "address": result,
            }
        )

    async def run_address_results(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        try:
            manifest = _load_any_manifest(root, run_id)
            return JSONResponse(address_results_payload(root, _manifest_child_run_ids(manifest)))
        except ValueError as exc:
            return _json_error(str(exc), status_code=404)

    async def samples(request: Request) -> JSONResponse:
        items = []
        for path in sorted((root / "sample_sets").glob("*.json")):
            sample_set = load_sample_set(root, path.stem)
            items.append(refresh_sample_set(root, sample_set).model_dump(mode="json"))
        return JSONResponse({"sample_sets": items})

    async def sample_create_from_run(request: Request) -> JSONResponse:
        try:
            data = SampleSetCreateRequest.model_validate(await _request_json(request))
            sample_set = create_sample_set_from_run(
                root=root,
                run_id=data.run_id,
                sample_set_id=data.sample_set_id,
            )
            return JSONResponse({"sample_set": sample_set.model_dump(mode="json")})
        except (ValidationError, ValueError) as exc:
            return _json_error(str(exc), status_code=404)

    async def sample_detail(request: Request) -> JSONResponse:
        sample_set_id = request.path_params["sample_set_id"]
        try:
            sample_set = refresh_sample_set(root, load_sample_set(root, sample_set_id))
            return JSONResponse({"sample_set": sample_set.model_dump(mode="json")})
        except ValueError as exc:
            return _json_error(str(exc), status_code=404)

    async def sample_curation_detail(request: Request) -> JSONResponse:
        try:
            return JSONResponse(
                _sample_curation_payload(root, request.path_params["sample_set_id"])
            )
        except ValueError as exc:
            return _json_error(str(exc), status_code=404)

    async def sample_curation_exclude(request: Request) -> JSONResponse:
        sample_set_id = request.path_params["sample_set_id"]
        try:
            data = SampleCurationExcludeRequest.model_validate(await _request_json(request))
            _, _, _, item_ids = _sample_curation_context(root, sample_set_id)
            unknown = sorted(set(data.item_ids) - set(item_ids))
            if unknown:
                raise ValueError(
                    "observation item(s) are not part of this sample: " + ", ".join(unknown)
                )
            set_exclusions(
                root,
                sample_set_id,
                item_ids=data.item_ids,
                reason_code=data.reason_code,
                reason_note=data.reason_note,
            )
            append_dialogue(
                root,
                sample_set_id,
                speaker="Human Reviewer",
                stage="sample_curation",
                message=f"I excluded {len(set(data.item_ids))} observation(s) from the dataset.",
                rationale=(
                    f"Reason: {data.reason_code.value}. "
                    f"{data.reason_note or ''}"
                ).strip(),
            )
            return JSONResponse(_sample_curation_payload(root, sample_set_id))
        except (ValidationError, ValueError) as exc:
            return _json_error(str(exc), status_code=400)

    async def sample_curation_restore(request: Request) -> JSONResponse:
        sample_set_id = request.path_params["sample_set_id"]
        try:
            data = SampleCurationRestoreRequest.model_validate(await _request_json(request))
            restore_items(root, sample_set_id, item_ids=data.item_ids)
            append_dialogue(
                root,
                sample_set_id,
                speaker="Human Reviewer",
                stage="sample_curation",
                message=f"I restored {len(set(data.item_ids))} observation(s) to the dataset.",
                rationale="The restored observations will be reconsidered at the next approval.",
            )
            return JSONResponse(_sample_curation_payload(root, sample_set_id))
        except (ValidationError, ValueError) as exc:
            return _json_error(str(exc), status_code=400)

    async def sample_curation_approve(request: Request) -> JSONResponse:
        sample_set_id = request.path_params["sample_set_id"]
        try:
            _, _, _, item_ids = _sample_curation_context(root, sample_set_id)
            manifest = approve_curation(root, sample_set_id, item_ids=item_ids)
            summary = curation_summary(manifest, item_ids)
            append_dialogue(
                root,
                sample_set_id,
                speaker="Human Reviewer",
                stage="sample_curation",
                message=(
                    f"I approved the curated sample with {summary['included_count']} included "
                    f"and {summary['excluded_count']} excluded observation(s)."
                ),
                rationale=(
                    "No item-by-item feedback was required; only explicit exclusions affect "
                    "coverage and gap-fill guidance."
                ),
            )
            return JSONResponse(_sample_curation_payload(root, sample_set_id))
        except ValueError as exc:
            return _json_error(str(exc), status_code=400)

    async def sample_log(request: Request) -> PlainTextResponse:
        sample_set_id = request.path_params["sample_set_id"]
        return PlainTextResponse(_read_log_text(log_path_for_run(root, sample_set_id)))

    async def sample_dialogue(request: Request) -> PlainTextResponse:
        sample_set_id = request.path_params["sample_set_id"]
        try:
            text = _pipeline_transcript_text(root, sample_set_id=sample_set_id)
        except ValueError as exc:
            return PlainTextResponse(str(exc), status_code=404)
        return PlainTextResponse(text, media_type="text/plain")

    async def sample_transcript_download(request: Request) -> PlainTextResponse:
        sample_set_id = request.path_params["sample_set_id"]
        try:
            text = _pipeline_transcript_text(root, sample_set_id=sample_set_id)
        except ValueError as exc:
            return PlainTextResponse(str(exc), status_code=404)
        return PlainTextResponse(
            text,
            media_type="text/plain",
            headers={
                "content-disposition": (
                    f'attachment; filename="{sample_set_id}-pipeline-transcript.txt"'
                )
            },
        )

    async def sample_status(request: Request) -> JSONResponse:
        sample_set_id = request.path_params["sample_set_id"]
        try:
            sample_set = refresh_sample_set(root, load_sample_set(root, sample_set_id))
            return JSONResponse(
                {
                    "sample_set": sample_set.model_dump(mode="json"),
                    "active": registry.is_active(sample_set_id),
                    "job": (
                        job_payload(
                            latest_job,
                            active=registry.is_active(sample_set_id),
                        )
                        if (latest_job := _latest_job_for_identity(root, sample_set_id))
                        else None
                    ),
                }
            )
        except ValueError as exc:
            return _json_error(str(exc), status_code=404)

    async def sample_workflow_status(request: Request) -> JSONResponse:
        sample_set_id = request.path_params["sample_set_id"]
        try:
            load_sample_set(root, sample_set_id)
            return JSONResponse(
                _workflow_status_payload(
                    root,
                    sample_set_id=sample_set_id,
                    active=registry.is_active(sample_set_id),
                )
            )
        except ValueError as exc:
            return _json_error(str(exc), status_code=404)

    async def sample_coverage_summary(request: Request) -> JSONResponse:
        sample_set_id = request.path_params["sample_set_id"]
        try:
            sample_set = refresh_sample_set(root, load_sample_set(root, sample_set_id))
            return JSONResponse(
                {
                    "sample_set": sample_set.model_dump(mode="json"),
                    "summary": compute_coverage_summary(root, sample_set),
                }
            )
        except ValueError as exc:
            return _json_error(str(exc), status_code=404)

    async def sample_coverage_run(request: Request) -> JSONResponse:
        sample_set_id = request.path_params["sample_set_id"]
        try:
            _, _, curation, item_ids = _sample_curation_context(root, sample_set_id)
            ensure_current_approval(curation, item_ids)
        except ValueError as exc:
            return _json_error(str(exc), status_code=409)
        if registry.is_active(sample_set_id):
            return _json_error(
                f"Sample set already has active work: {sample_set_id}",
                status_code=409,
            )
        task = partial(
            run_coverage_steering,
            root=root,
            sample_set_id=sample_set_id,
            codex_bin=codex_bin,
            runner=app_runner,
        )
        if background:
            job = create_job(
                root,
                job_id=f"{sample_set_id}-coverage",
                job_type=JobType.COVERAGE,
                parent_id=sample_set_id,
                log_path=str(log_path_for_run(root, sample_set_id)),
            )
            executor.submit(
                run_background_job,
                root=root,
                registry=registry,
                identity=sample_set_id,
                job_id=job.job_id,
                log=lambda message: append_harvest_log(
                    root,
                    sample_set_id,
                    f"Coverage analysis failed: {message}.",
                ),
                task=task,
                summary=lambda result: (
                    result.get("summary", {}) if isinstance(result, dict) else {}
                ),
            )
            return JSONResponse(
                {
                    "started": True,
                    "sample_set_id": sample_set_id,
                    "job_id": job.job_id,
                    "job": job_payload(job, active=True),
                }
            )
        result = await run_in_threadpool(task)
        return JSONResponse({"started": False, "coverage": result})

    async def sample_coverage_results(request: Request) -> JSONResponse:
        sample_set_id = request.path_params["sample_set_id"]
        try:
            path = _latest_coverage_path(root, sample_set_id)
            review = load_coverage_review(path)
            return JSONResponse(
                {"coverage_path": str(path), "review": review.model_dump(mode="json")}
            )
        except FileNotFoundError as exc:
            return _json_error(str(exc), status_code=409)

    async def sample_gap_fill_run(request: Request) -> JSONResponse:
        sample_set_id = request.path_params["sample_set_id"]
        if registry.is_active(sample_set_id):
            return _json_error(
                f"Sample set already has active work: {sample_set_id}",
                status_code=409,
            )
        try:
            data = SampleSetGapFillRequest.model_validate(await _request_json(request))
            coverage_path = (
                coverage_output_path(root, data.coverage_id)
                if data.coverage_id
                else _latest_coverage_path(root, sample_set_id)
            )
            review = load_coverage_review(coverage_path)
            _, _, curation, item_ids = _sample_curation_context(root, sample_set_id)
            approval = ensure_current_approval(curation, item_ids)
            if review.curation_snapshot_id != approval.snapshot_id:
                raise ValueError(
                    "coverage analysis is stale for the current human curation approval; "
                    "rerun coverage"
                )
        except (ValidationError, FileNotFoundError) as exc:
            return _json_error(str(exc), status_code=409)
        except ValueError as exc:
            return _json_error(str(exc), status_code=409)
        task = partial(
            run_gap_fill,
            root=root,
            sample_set_id=sample_set_id,
            coverage_path=coverage_path,
            codex_bin=codex_bin,
            runner=app_runner,
        )
        if background:
            job = create_job(
                root,
                job_id=f"{sample_set_id}-gap-fill",
                job_type=JobType.GAP_FILL,
                parent_id=sample_set_id,
                log_path=str(log_path_for_run(root, sample_set_id)),
            )
            executor.submit(
                run_background_job,
                root=root,
                registry=registry,
                identity=sample_set_id,
                job_id=job.job_id,
                log=lambda message: append_harvest_log(
                    root,
                    sample_set_id,
                    f"Gap-fill failed: {message}.",
                ),
                task=task,
                manifest_path=lambda result: str(
                    root / "sample_sets" / f"{result.sample_set_id}.json"
                ),
                summary=lambda result: result.stage_summary or {},
            )
            return JSONResponse(
                {
                    "started": True,
                    "sample_set_id": sample_set_id,
                    "job_id": job.job_id,
                    "job": job_payload(job, active=True),
                }
            )
        sample_set = await run_in_threadpool(task)
        return JSONResponse({"started": False, "sample_set": sample_set.model_dump(mode="json")})

    async def sample_qaqc_missing(request: Request) -> JSONResponse:
        sample_set_id = request.path_params["sample_set_id"]
        if registry.is_active(sample_set_id):
            return _json_error(
                f"Sample set already has active work: {sample_set_id}",
                status_code=409,
            )
        task = partial(
            _run_qaqc_missing_for_sample,
            root=root,
            sample_set_id=sample_set_id,
            codex_bin=codex_bin,
            runner=app_runner,
        )
        if background:
            job = create_job(
                root,
                job_id=f"{sample_set_id}-qaqc-missing",
                job_type=JobType.SAMPLE_QAQC_MISSING,
                parent_id=sample_set_id,
                log_path=str(log_path_for_run(root, sample_set_id)),
            )
            executor.submit(
                run_background_job,
                root=root,
                registry=registry,
                identity=sample_set_id,
                job_id=job.job_id,
                log=lambda message: append_harvest_log(
                    root,
                    sample_set_id,
                    f"Missing QAQC failed: {message}.",
                ),
                task=task,
                summary=lambda result: (
                    result.get("sample_set", {}).get("stage_summary", {})
                    if isinstance(result, dict)
                    else {}
                ),
            )
            return JSONResponse(
                {
                    "started": True,
                    "sample_set_id": sample_set_id,
                    "job_id": job.job_id,
                    "job": job_payload(job, active=True),
                }
            )
        try:
            result = await run_in_threadpool(task)
        except (FileNotFoundError, ValueError) as exc:
            return _json_error(str(exc), status_code=409)
        return JSONResponse({"started": False, "qaqc": result})

    async def sample_address_missing(request: Request) -> JSONResponse:
        sample_set_id = request.path_params["sample_set_id"]
        if registry.is_active(sample_set_id):
            return _json_error(
                f"Sample set already has active work: {sample_set_id}",
                status_code=409,
            )
        task = partial(
            _run_address_missing_for_sample,
            root=root,
            sample_set_id=sample_set_id,
            codex_bin=codex_bin,
            runner=app_runner,
        )
        if background:
            job = create_job(
                root,
                job_id=f"{sample_set_id}-address-missing",
                job_type=JobType.SAMPLE_ADDRESS_MISSING,
                parent_id=sample_set_id,
                log_path=str(log_path_for_run(root, sample_set_id)),
            )
            executor.submit(
                run_background_job,
                root=root,
                registry=registry,
                identity=sample_set_id,
                job_id=job.job_id,
                log=lambda message: append_harvest_log(
                    root,
                    sample_set_id,
                    f"Missing address failed: {message}.",
                ),
                task=task,
                summary=lambda result: (
                    result.get("sample_set", {}).get("stage_summary", {})
                    if isinstance(result, dict)
                    else {}
                ),
            )
            return JSONResponse(
                {
                    "started": True,
                    "sample_set_id": sample_set_id,
                    "job_id": job.job_id,
                    "job": job_payload(job, active=True),
                }
            )
        try:
            result = await run_in_threadpool(task)
        except (FileNotFoundError, ValueError) as exc:
            return _json_error(str(exc), status_code=409)
        return JSONResponse({"started": False, "address": result})

    async def sample_geometry_items(request: Request) -> JSONResponse:
        try:
            return JSONResponse(
                _sample_geometry_items_payload(root, request.path_params["sample_set_id"])
            )
        except ValueError as exc:
            return _json_error(str(exc), status_code=404)

    async def sample_table(request: Request) -> JSONResponse:
        sample_set_id = request.path_params["sample_set_id"]
        mode = request.query_params.get("mode", "verified")
        try:
            return JSONResponse(_sample_table_payload(root, sample_set_id, mode=mode))
        except FileNotFoundError as exc:
            return _json_error(str(exc), status_code=409)
        except ValueError as exc:
            return _json_error(str(exc), status_code=404)

    async def sample_export_verified_json(request: Request) -> Response:
        return _sample_verified_export_response(
            root,
            request.path_params["sample_set_id"],
            output_format="json",
        )

    async def sample_export_verified_csv(request: Request) -> Response:
        return _sample_verified_export_response(
            root,
            request.path_params["sample_set_id"],
            output_format="csv",
        )

    async def sample_export_footprints_geojson(request: Request) -> Response:
        return _sample_verified_export_response(
            root,
            request.path_params["sample_set_id"],
            output_format="geojson",
        )

    async def sample_export_components_json(request: Request) -> Response:
        return _sample_component_export_response(
            root,
            request.path_params["sample_set_id"],
            output_format="json",
        )

    async def sample_export_components_csv(request: Request) -> Response:
        return _sample_component_export_response(
            root,
            request.path_params["sample_set_id"],
            output_format="csv",
        )

    async def verified_leads(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        try:
            manifest = _load_any_manifest(root, run_id)
            records = merge_address_results(root, _approved_records_for_manifest(root, manifest))
            return JSONResponse({"item_count": len(records), "items": list(records)})
        except FileNotFoundError as exc:
            return _json_error(str(exc), status_code=409)
        except ValueError as exc:
            return _json_error(str(exc), status_code=404)

    async def geometry_items(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        try:
            manifest = _load_any_manifest(root, run_id)
            return JSONResponse(_geometry_items_payload(root, manifest))
        except FileNotFoundError as exc:
            return _json_error(str(exc), status_code=409)
        except ValueError as exc:
            return _json_error(str(exc), status_code=404)

    async def run_table(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        mode = request.query_params.get("mode", "verified")
        try:
            return JSONResponse(_run_table_payload(root, run_id, mode=mode))
        except FileNotFoundError as exc:
            return _json_error(str(exc), status_code=409)
        except ValueError as exc:
            return _json_error(str(exc), status_code=404)

    async def geometry_geocode(request: Request) -> JSONResponse:
        try:
            data = GeometryGeocodeRequest.model_validate(await _request_json(request))
            child_run_id, _ = data.item_id.rsplit("-", 1)
            conversation_id = data.conversation_id or child_run_id
            _, geometry_record = _geometry_record_context(root, data.item_id)
            existing_geometry = geometry_record.get("geometry")
            existing_validation = (
                existing_geometry.get("spatial_validation")
                if isinstance(existing_geometry, dict)
                else None
            )
            existing_address_retry = (
                existing_validation.get("address_retry")
                if isinstance(existing_validation, dict)
                else None
            )
            address_retry_already_attempted = existing_address_retry is not None
            result, spatial_validation, matched_query = _spatially_geocode_item(
                root=root,
                geocoder=app_geocoder,
                item_id=data.item_id,
                requested_query=data.query,
            )
            address_retry: dict[str, Any] | None = None
            if (
                result is None
                and data.allow_address_retry
                and not address_retry_already_attempted
            ):
                initial_validation = spatial_validation
                address_retry = await run_in_threadpool(
                    partial(
                        _run_address_spatial_retry,
                        root=root,
                        item_id=data.item_id,
                        spatial_feedback=initial_validation,
                        conversation_id=conversation_id,
                        codex_bin=codex_bin,
                        runner=app_runner,
                    )
                )
                if address_retry.get("status") == "corrected":
                    result, retry_validation, matched_query = _spatially_geocode_item(
                        root=root,
                        geocoder=app_geocoder,
                        item_id=data.item_id,
                        requested_query=str(
                            address_retry["address"].get("formatted_address") or data.query
                        ),
                    )
                    spatial_validation = {
                        **retry_validation,
                        "initial_validation": initial_validation,
                        "address_retry": address_retry,
                    }
                else:
                    spatial_validation = {
                        **initial_validation,
                        "address_retry": address_retry,
                    }
            elif result is None and address_retry_already_attempted:
                spatial_validation = {
                    **spatial_validation,
                    "address_retry": existing_address_retry,
                    "address_retry_limit_reached": True,
                }
            point = (
                GeometryPoint(
                    latitude=float(result["latitude"]),
                    longitude=float(result["longitude"]),
                    source="geocode",
                )
                if result is not None
                else None
            )
            item = geometry_item_from_payload(
                item_id=data.item_id,
                geocode_query=matched_query,
                point=point,
                polygon_geojson=None,
                geometry_status=(
                    GeometryStatus.POINT_CONFIRMED
                    if point is not None
                    else GeometryStatus.NEEDS_REVIEW
                ),
                geocode_result=result,
                spatial_validation=spatial_validation,
                review_notes=str(spatial_validation["reason"]),
            )
            save_geometry_review_item(root, item)
            facility_name = str(
                geometry_record.get("lead", {}).get("location", {}).get(
                    "facility_name",
                    data.item_id,
                )
            )
            append_dialogue(
                root,
                conversation_id,
                speaker="Spatial Resolver",
                stage="automated_geocoding",
                message=(
                    f"I assigned an in-scope coordinate to {facility_name}."
                    if point is not None
                    else f"I routed {facility_name} to human coordinate assignment."
                ),
                rationale=(
                    f"{spatial_validation['reason']} "
                    f"Address correction retry: {'used' if address_retry else 'not needed'}."
                ),
            )
            return JSONResponse(
                {
                    "geocode_result": result,
                    "spatial_validation": spatial_validation,
                    "address_retry": address_retry,
                    "geometry": item.model_dump(mode="json"),
                }
            )
        except (ValidationError, ValueError) as exc:
            return _json_error(str(exc))

    async def geometry_research(request: Request) -> JSONResponse:
        try:
            data = GeometryResearchRequest.model_validate(await _request_json(request))
            child_run_id, _ = data.item_id.rsplit("-", 1)
            conversation_id = data.conversation_id or child_run_id
            _, geometry_record = _geometry_record_context(root, data.item_id)
            existing_geometry = geometry_record.get("geometry")
            existing_spatial_feedback = (
                existing_geometry.get("spatial_validation")
                if isinstance(existing_geometry, dict)
                else None
            )
            spatial_feedback: dict[str, object] = (
                dict(existing_spatial_feedback)
                if isinstance(existing_spatial_feedback, dict)
                else {
                    "status": "manual_research_requested",
                    "reason": "The human reviewer requested focused facility research.",
                }
            )
            address_retry = await run_in_threadpool(
                partial(
                    _run_address_spatial_retry,
                    root=root,
                    item_id=data.item_id,
                    spatial_feedback=spatial_feedback,
                    conversation_id=conversation_id,
                    codex_bin=codex_bin,
                    runner=app_runner,
                )
            )
            requested_query = str(geometry_record.get("geocode_query") or data.item_id)
            result: dict[str, Any] | None = None
            matched_query = requested_query
            if address_retry.get("status") == "corrected":
                requested_query = str(
                    address_retry["address"].get("formatted_address") or requested_query
                )
                result, spatial_validation, matched_query = _spatially_geocode_item(
                    root=root,
                    geocoder=app_geocoder,
                    item_id=data.item_id,
                    requested_query=requested_query,
                )
                spatial_validation = {
                    **spatial_validation,
                    "address_retry": address_retry,
                    "manual_research": True,
                }
            else:
                spatial_validation = {
                    **spatial_feedback,
                    "address_retry": address_retry,
                    "manual_research": True,
                    "requires_human_intervention": True,
                }
            researched_point = (
                GeometryPoint(
                    latitude=float(result["latitude"]),
                    longitude=float(result["longitude"]),
                    source="geocode",
                )
                if result is not None
                else None
            )
            existing_point_payload = (
                existing_geometry.get("point")
                if isinstance(existing_geometry, dict)
                and isinstance(existing_geometry.get("point"), dict)
                else None
            )
            existing_point = (
                GeometryPoint.model_validate(existing_point_payload)
                if existing_point_payload is not None
                else None
            )
            point = researched_point or existing_point
            existing_polygon = (
                existing_geometry.get("polygon_geojson")
                if isinstance(existing_geometry, dict)
                and isinstance(existing_geometry.get("polygon_geojson"), dict)
                else None
            )
            geometry_status = (
                GeometryStatus.FOOTPRINT_DRAWN
                if existing_polygon is not None
                else GeometryStatus.POINT_CONFIRMED
                if point is not None
                else GeometryStatus.NEEDS_REVIEW
            )
            item = geometry_item_from_payload(
                item_id=data.item_id,
                geocode_query=matched_query,
                point=point,
                polygon_geojson=existing_polygon,
                geometry_status=geometry_status,
                geocode_result=(
                    result
                    if result is not None
                    else existing_geometry.get("geocode_result")
                    if isinstance(existing_geometry, dict)
                    else None
                ),
                spatial_validation=spatial_validation,
                review_notes=str(
                    spatial_validation.get(
                        "reason",
                        "Focused facility research completed.",
                    )
                ),
            )
            save_geometry_review_item(root, item)
            facility_name = str(
                geometry_record.get("lead", {}).get("location", {}).get(
                    "facility_name",
                    data.item_id,
                )
            )
            append_dialogue(
                root,
                conversation_id,
                speaker="Spatial Resolver",
                stage="automated_geocoding",
                message=(
                    f"I assigned a coordinate to {facility_name} after focused research."
                    if researched_point is not None
                    else (
                        f"I prepared ranked location candidates for {facility_name} "
                        "after focused research."
                    )
                ),
                rationale=str(spatial_validation.get("reason") or ""),
            )
            return JSONResponse(
                {
                    "geocode_result": result,
                    "spatial_validation": spatial_validation,
                    "address_retry": address_retry,
                    "research_resolved": researched_point is not None,
                    "geometry": item.model_dump(mode="json"),
                }
            )
        except (ValidationError, ValueError) as exc:
            return _json_error(str(exc))

    async def geometry_coordinate_preview(request: Request) -> JSONResponse:
        try:
            data = GeometryCoordinatePreviewRequest.model_validate(
                await _request_json(request)
            )
            manifest, geometry_record = _geometry_record_context(root, data.item_id)
            latitude, longitude, reversed_order = parse_coordinate_text(
                data.coordinate_text
            )
            reverse_result: dict[str, Any] | None = None
            reverse_error: str | None = None
            reverse = getattr(app_geocoder, "reverse", None)
            if callable(reverse):
                try:
                    reverse_result = await run_in_threadpool(
                        partial(reverse, latitude, longitude)
                    )
                except Exception as exc:
                    reverse_error = str(exc)
            if reverse_result is None:
                validation: dict[str, object] = {
                    "status": "scope_unverified",
                    "requires_human_intervention": True,
                    "warning": True,
                    "reason": (
                        "The coordinate parsed successfully, but its country and locality "
                        "could not be verified by reverse geocoding."
                    ),
                }
                if reverse_error:
                    validation["reverse_geocode_error"] = reverse_error
            else:
                accepted, reverse_validation = spatially_validate_geocode_result(
                    reverse_result,
                    expected_country=manifest.country,
                    expected_locality=manifest.locality,
                )
                mismatch = (
                    _address_candidate_mismatch(accepted, geometry_record)
                    if accepted is not None
                    else None
                )
                if accepted is not None and mismatch is None:
                    validation = {
                        **reverse_validation,
                        "status": "in_scope",
                        "requires_human_intervention": False,
                        "warning": False,
                        "reason": (
                            "Reverse geocoding places this coordinate inside the requested "
                            "country/locality without an address conflict."
                        ),
                    }
                elif mismatch is not None:
                    validation = {
                        **reverse_validation,
                        "status": "address_mismatch",
                        "requires_human_intervention": True,
                        "warning": True,
                        "reason": mismatch,
                    }
                else:
                    validation = {
                        **reverse_validation,
                        "warning": True,
                    }
            return JSONResponse(
                {
                    "point": {
                        "latitude": latitude,
                        "longitude": longitude,
                        "source": "google-maps-human",
                    },
                    "normalized": f"{latitude:.7f}, {longitude:.7f}",
                    "reversed_order": reversed_order,
                    "reverse_geocode_result": reverse_result,
                    "spatial_validation": validation,
                }
            )
        except (ValidationError, ValueError) as exc:
            return _json_error(str(exc))

    async def geometry_geocode_all(request: Request) -> JSONResponse:
        try:
            data = GeometryGeocodeAllRequest.model_validate(await _request_json(request))
            items: list[dict[str, object]] = []
            geocoded_count = 0
            not_found_count = 0
            human_review_count = 0
            error_count = 0
            for requested in data.items:
                try:
                    result, spatial_validation, matched_query = _spatially_geocode_item(
                        root=root,
                        geocoder=app_geocoder,
                        item_id=requested.item_id,
                        requested_query=requested.query,
                    )
                    point = (
                        GeometryPoint(
                            latitude=float(result["latitude"]),
                            longitude=float(result["longitude"]),
                            source="geocode",
                        )
                        if result is not None
                        else None
                    )
                    item = geometry_item_from_payload(
                        item_id=requested.item_id,
                        geocode_query=matched_query,
                        point=point,
                        polygon_geojson=None,
                        geometry_status=(
                            GeometryStatus.POINT_CONFIRMED
                            if point is not None
                            else GeometryStatus.NEEDS_REVIEW
                        ),
                        geocode_result=result,
                        spatial_validation=spatial_validation,
                        review_notes=str(spatial_validation["reason"]),
                    )
                    save_geometry_review_item(root, item)
                    if point is not None:
                        geocoded_count += 1
                    else:
                        not_found_count += 1
                        if spatial_validation["requires_human_intervention"]:
                            human_review_count += 1
                    items.append(
                        {
                            "item_id": requested.item_id,
                            "geocode_result": result,
                            "spatial_validation": spatial_validation,
                            "geometry": item.model_dump(mode="json"),
                            "error": None,
                        }
                    )
                except Exception as exc:
                    error_count += 1
                    items.append(
                        {
                            "item_id": requested.item_id,
                            "geocode_result": None,
                            "geometry": None,
                            "error": str(exc),
                        }
                    )
            return JSONResponse(
                {
                    "requested_count": len(data.items),
                    "geocoded_count": geocoded_count,
                    "not_found_count": not_found_count,
                    "human_review_count": human_review_count,
                    "error_count": error_count,
                    "items": items,
                }
            )
        except (ValidationError, ValueError) as exc:
            return _json_error(str(exc))

    async def geometry_save(request: Request) -> JSONResponse:
        item_id = request.path_params["item_id"]
        try:
            data = GeometrySaveRequest.model_validate(await _request_json(request))
            if data.item_id != item_id:
                return _json_error("item_id in path and body must match")
            if data.geometry_status == GeometryStatus.POINT_CONFIRMED and data.point is None:
                return _json_error("point_confirmed geometry requires a point")
            if (
                data.geometry_status == GeometryStatus.FOOTPRINT_DRAWN
                and data.polygon_geojson is None
            ):
                return _json_error("footprint_drawn geometry requires a polygon")
            item = geometry_item_from_payload(
                item_id=item_id,
                geocode_query=data.geocode_query,
                point=data.point,
                polygon_geojson=data.polygon_geojson,
                geometry_status=data.geometry_status,
                geocode_result=data.geocode_result,
                spatial_validation=(
                    {
                        "status": "accepted_manual",
                        "requires_human_intervention": False,
                        "reason": "A user manually confirmed or assigned this coordinate.",
                        "candidate_count": 0,
                    }
                    if data.point is not None and data.point.source == "user"
                    else data.spatial_validation
                ),
                review_notes=data.review_notes,
            )
            save_geometry_review_item(root, item)
            if data.point is not None and data.conversation_id:
                _, geometry_record = _geometry_record_context(root, item_id)
                facility_name = str(
                    geometry_record.get("lead", {}).get("location", {}).get(
                        "facility_name",
                        item_id,
                    )
                )
                append_dialogue(
                    root,
                    data.conversation_id,
                    speaker="Human Reviewer",
                    stage="coordinate_assignment",
                    message=f"I manually confirmed a coordinate for {facility_name}.",
                    rationale=data.review_notes or "Coordinate assigned in Geometry Studio.",
                )
            return JSONResponse({"geometry": item.model_dump(mode="json")})
        except (ValidationError, ValueError) as exc:
            return _json_error(str(exc))

    async def export_csv(request: Request) -> Response:
        return _export_response(root, request.path_params["run_id"], output_format="csv")

    async def export_jsonl(request: Request) -> Response:
        return _export_response(root, request.path_params["run_id"], output_format="jsonl")

    async def export_verified_json(request: Request) -> Response:
        return _verified_export_response(
            root,
            request.path_params["run_id"],
            output_format="json",
        )

    async def export_verified_csv(request: Request) -> Response:
        return _verified_export_response(
            root,
            request.path_params["run_id"],
            output_format="csv",
        )

    async def export_footprints_geojson(request: Request) -> Response:
        return _verified_export_response(
            root,
            request.path_params["run_id"],
            output_format="geojson",
        )

    async def export_components_json(request: Request) -> Response:
        return _component_export_response(
            root,
            request.path_params["run_id"],
            output_format="json",
        )

    async def export_components_csv(request: Request) -> Response:
        return _component_export_response(
            root,
            request.path_params["run_id"],
            output_format="csv",
        )

    async def promote(request: Request) -> JSONResponse:
        try:
            run_id = request.path_params["run_id"]
            data = PromoteLeadRequest.model_validate(await _request_json(request))
            manifest = _load_run_manifest(root, run_id)
            leads = load_leads(Path(manifest.lead_path))
            if data.index >= len(leads):
                return _json_error(f"lead index out of range: {data.index}")
            run = promote_lead_to_run(leads[data.index], task_id=data.task_id)
            output = root / "runs" / f"{run_id}-{data.index + 1:03}.json"
            write_model(output, run)
            return JSONResponse({"run_file": str(output), "run": run.model_dump(mode="json")})
        except (ValidationError, ValueError) as exc:
            return _json_error(str(exc), status_code=404)

    routes = [
        Route("/", index),
        Route("/assets/oasis-logo.jpg", oasis_logo),
        Route("/api/profiles", profiles),
        Route("/api/geographer/plan", geographer_plan, methods=["POST"]),
        Route("/api/harvest/run", harvest_run, methods=["POST"]),
        Route("/api/harvest/batch-run", harvest_batch_run, methods=["POST"]),
        Route("/api/harvest/campaign-run", harvest_campaign_run, methods=["POST"]),
        Route("/api/runs", runs),
        Route("/api/runs/clear", clear_runs, methods=["POST"]),
        Route("/api/runs/{run_id}", run_detail),
        Route("/api/runs/{run_id}/status", run_status),
        Route("/api/runs/{run_id}/workflow-status", run_workflow_status),
        Route("/api/runs/{run_id}/log", run_log),
        Route("/api/runs/{run_id}/dialogue", run_dialogue),
        Route("/api/runs/{run_id}/transcript.txt", run_transcript_download),
        Route("/api/runs/{run_id}/leads", run_leads),
        Route("/api/runs/{run_id}/qaqc-prompt", run_qaqc_prompt),
        Route("/api/runs/{run_id}/qaqc-run", run_qaqc, methods=["POST"]),
        Route("/api/runs/{run_id}/qaqc-reviews", run_qaqc_reviews),
        Route("/api/runs/{run_id}/address-run", run_address, methods=["POST"]),
        Route("/api/runs/{run_id}/address-results", run_address_results),
        Route("/api/runs/{run_id}/verified-leads", verified_leads),
        Route("/api/runs/{run_id}/geometry-items", geometry_items),
        Route("/api/runs/{run_id}/table", run_table),
        Route("/api/geometry/geocode", geometry_geocode, methods=["POST"]),
        Route("/api/geometry/research", geometry_research, methods=["POST"]),
        Route(
            "/api/geometry/coordinate-preview",
            geometry_coordinate_preview,
            methods=["POST"],
        ),
        Route("/api/geometry/geocode-all", geometry_geocode_all, methods=["POST"]),
        Route("/api/geometry/items/{item_id}", geometry_save, methods=["POST"]),
        Route("/api/runs/{run_id}/export.csv", export_csv),
        Route("/api/runs/{run_id}/export.jsonl", export_jsonl),
        Route("/api/runs/{run_id}/export.verified.json", export_verified_json),
        Route("/api/runs/{run_id}/export.verified.csv", export_verified_csv),
        Route("/api/runs/{run_id}/export.footprints.geojson", export_footprints_geojson),
        Route("/api/runs/{run_id}/export.components.json", export_components_json),
        Route("/api/runs/{run_id}/export.components.csv", export_components_csv),
        Route("/api/runs/{run_id}/cancel", cancel_run, methods=["POST"]),
        Route("/api/runs/{run_id}/promote", promote, methods=["POST"]),
        Route("/api/samples", samples),
        Route("/api/samples/from-run", sample_create_from_run, methods=["POST"]),
        Route("/api/samples/{sample_set_id}", sample_detail),
        Route("/api/samples/{sample_set_id}/curation", sample_curation_detail),
        Route(
            "/api/samples/{sample_set_id}/curation/exclude",
            sample_curation_exclude,
            methods=["POST"],
        ),
        Route(
            "/api/samples/{sample_set_id}/curation/restore",
            sample_curation_restore,
            methods=["POST"],
        ),
        Route(
            "/api/samples/{sample_set_id}/curation/approve",
            sample_curation_approve,
            methods=["POST"],
        ),
        Route("/api/samples/{sample_set_id}/status", sample_status),
        Route("/api/samples/{sample_set_id}/workflow-status", sample_workflow_status),
        Route("/api/samples/{sample_set_id}/log", sample_log),
        Route("/api/samples/{sample_set_id}/dialogue", sample_dialogue),
        Route(
            "/api/samples/{sample_set_id}/transcript.txt",
            sample_transcript_download,
        ),
        Route("/api/samples/{sample_set_id}/coverage-summary", sample_coverage_summary),
        Route("/api/samples/{sample_set_id}/coverage-run", sample_coverage_run, methods=["POST"]),
        Route("/api/samples/{sample_set_id}/coverage-results", sample_coverage_results),
        Route("/api/samples/{sample_set_id}/gap-fill-run", sample_gap_fill_run, methods=["POST"]),
        Route("/api/samples/{sample_set_id}/qaqc-missing", sample_qaqc_missing, methods=["POST"]),
        Route(
            "/api/samples/{sample_set_id}/address-missing",
            sample_address_missing,
            methods=["POST"],
        ),
        Route("/api/samples/{sample_set_id}/geometry-items", sample_geometry_items),
        Route("/api/samples/{sample_set_id}/table", sample_table),
        Route("/api/samples/{sample_set_id}/export.verified.json", sample_export_verified_json),
        Route("/api/samples/{sample_set_id}/export.verified.csv", sample_export_verified_csv),
        Route("/api/samples/{sample_set_id}/export.components.json", sample_export_components_json),
        Route("/api/samples/{sample_set_id}/export.components.csv", sample_export_components_csv),
        Route(
            "/api/samples/{sample_set_id}/export.footprints.geojson",
            sample_export_footprints_geojson,
        ),
        Route("/api/app/exit", exit_app, methods=["POST"]),
    ]
    return Starlette(routes=routes)


def _export_response(root: Path, run_id: str, *, output_format: str) -> Response:
    try:
        manifest = _load_run_manifest(root, run_id)
        evidence_set = load_evidence_set(Path(manifest.lead_path))
        payload = export_evidence_set(evidence_set, output_format=output_format)
    except ValueError as exc:
        return _json_error(str(exc), status_code=404)
    media_type = "text/csv" if output_format == "csv" else "application/x-ndjson"
    filename = f"{run_id}.{output_format}"
    return PlainTextResponse(
        payload,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def serve_app(
    *,
    workspace: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    codex_bin: str = "codex",
    open_browser: bool = True,
) -> None:
    import uvicorn

    app = create_app(
        workspace=workspace,
        codex_bin=codex_bin,
        shutdown_callback=delayed_hard_exit(),
    )
    url = f"http://{host}:{port}/?v={int(time.time())}"
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(app, host=host, port=port)

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import time
import webbrowser
from collections import Counter
from collections.abc import AsyncIterator, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
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

from pdt_observer.activity import load_harvester_activity_report
from pdt_observer.addresses import (
    address_output_path,
    address_prompt_path,
    address_results_payload,
    approved_address_inputs,
    bundle_is_addressable_candidate,
    bundle_is_model_ready,
    bundle_readiness,
    load_address_results,
    merge_address_results,
    reconcile_address_results,
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
from pdt_observer.app_geometry import (
    _address_candidate_mismatch,
    _address_candidate_warnings,
    _merge_ranked_candidate_options,
    _ranked_candidate_options,
    _record_expected_postal_code,
    _record_facility_name,
    _should_retry_address_after_geocode,
)
from pdt_observer.app_runtime import ActiveCodexRegistry, delayed_hard_exit, run_background_job
from pdt_observer.app_ui import INDEX_HTML
from pdt_observer.countries import country_alias_map
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
from pdt_observer.geocoding import NominatimGeocoder
from pdt_observer.geographer import load_geographer_plan, run_geographer
from pdt_observer.geometry import (
    admin_scoped_csv,
    admin_scoped_json,
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
    bundle_is_allocated_shadow,
    export_evidence_set,
    load_evidence_set,
    load_leads,
    load_qaqc_review_set,
    promote_lead_to_run,
    render_lead_qaqc_prompt,
    summarize_evidence_set,
)
from pdt_observer.models import (
    AddressEnrichmentStatus,
    CountMethod,
    EvidenceStrategyType,
    GeometryPoint,
    GeometryStatus,
    HarvestBatchRunManifest,
    HarvestCampaignRunManifest,
    HarvestRunManifest,
    HarvestRunStatus,
    JobRecord,
    JobStatus,
    JobType,
    LeadQaqcRecommendedAction,
    LeadQaqcVerificationStatus,
)
from pdt_observer.ports import SpatialGeocoder
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
from pdt_observer.storage import write_json_file
from pdt_observer.strategies import STRATEGIES, build_strategy_plan
from pdt_observer.workflow import utc_now_text, write_model


def _json_error(message: str, *, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def _count_phrase(count: int, singular: str, plural: str | None = None) -> str:
    noun = singular if count == 1 else (plural or f"{singular}s")
    verb = "is" if count == 1 else "are"
    return f"{count} {noun} {verb}"


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
        "residential": 0,
        "institutions_public_service": 1,
        "retail_service": 2,
        "commercial": 3,
        "transportation": 4,
        "military_facility": 5,
        "recreation_entertainment": 6,
        "agriculture": 7,
    }
    profile_sets: list[dict[str, Any]] = []
    for profile_set in BUILTIN_PROFILE_SETS.values():
        profile_sets.append(
            {
                "profile_set_id": profile_set.profile_set_id,
                "label": profile_set.label,
                "land_use": profile_set.label,
                "profiles": [
                    {
                        "profile_id": profile.profile_id,
                        "label": profile.label,
                        "enabled": profile.enabled,
                        "priority": profile.priority,
                        "land_use": profile.land_use,
                        "facility_class": profile.facility_class,
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
        review_count = (
            len(review_set.occupancy_reviews)
            + len(review_set.component_reviews)
            + len(review_set.component_bundle_reviews)
        )
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
    audit_counters = _qaqc_audit_counters(root, child_run_ids)
    append_harvest_log(root, parent_id, f"QAQC run finished: {summary}.")
    append_dialogue(
        root,
        parent_id,
        speaker="QAQC Agent",
        stage="qaqc",
        message=(
            f"I completed an evidence QAQC audit for {completed_count} child run(s): "
            f"{audit_counters['direct_total']} direct review(s), "
            f"{audit_counters['component_total']} component review(s), and "
            f"{audit_counters['bundle_total']} bundle review(s). "
            f"{_count_phrase(audit_counters['approved_bundle_count'], 'bundle')} "
            "model-ready, "
            f"{_count_phrase(audit_counters['partial_bundle_count'], 'partial candidate')} "
            "addressable, and "
            f"{_count_phrase(audit_counters['held_bundle_count'], 'bundle')} held."
        ),
        rationale=(
            f"{failed_count} child review(s) failed and {cancelled_count} were cancelled. "
            f"Direct actions: {audit_counters['direct_actions']}. Component actions: "
            f"{audit_counters['component_actions']}. Bundle actions: "
            f"{audit_counters['bundle_actions']}. Common missing bundle fields: "
            f"{audit_counters['common_missing_bundle_fields']}. I checked source support, "
            "facility identity, role semantics, bundle completeness, and whether a bundle "
            "is model-ready, partial-but-addressable, or held for supervisor review."
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
        write_json_file(output_path, [])
        append_harvest_log(
            root,
            parent_id,
            f"No model-ready or partial addressable targets found for {run_id}; "
            "address enrichment skipped.",
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
    prompt = render_address_enrichment_prompt(
        records,
        source_label=f"{run_id} model-ready and partial candidate targets",
    )
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
        raw_results = load_address_results(output_path)
        results, reconciliation = reconcile_address_results(
            root=root,
            child_run_id=run_id,
            expected_records=records,
            results=raw_results,
        )
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
        f"Address validation completed for {run_id}: {len(results)} result(s); "
        f"reconciliation={reconciliation}.",
    )
    return {
        "run_id": run_id,
        "address_id": address_id,
        "status": "completed",
        "prompt_path": str(prompt_path),
        "address_path": str(output_path),
        "result_count": len(results),
        "expected_count": reconciliation["expected_count"],
        "returned_count": reconciliation["returned_count"],
        "missing_count": reconciliation["missing_count"],
        "missing_item_ids": reconciliation["missing_item_ids"],
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
    expected_count = sum(
        int(result.get("expected_count") or result.get("result_count") or 0)
        for result in child_results
    )
    missing_count = sum(int(result.get("missing_count") or 0) for result in child_results)
    missing_item_ids = [
        item_id
        for result in child_results
        for item_id in result.get("missing_item_ids", ())
    ]
    status = "cancelled" if cancelled_count else ("failed" if failed_count else "completed")
    summary = {
        "status": status,
        "planned_count": len(child_run_ids),
        "completed_count": completed_count,
        "failed_count": failed_count,
        "cancelled_count": cancelled_count,
        "result_count": result_count,
        "expected_count": expected_count,
        "missing_count": missing_count,
        "missing_item_ids": missing_item_ids,
    }
    bundle_counters = _component_bundle_review_counters(root, child_run_ids)
    append_harvest_log(root, parent_id, f"Address enrichment finished: {summary}.")
    append_dialogue(
        root,
        parent_id,
        speaker="Address Agent",
        stage="address_enrichment",
        message=(
            f"I completed an address-target audit for {completed_count} child run(s): "
            f"{expected_count} addressable target(s), {result_count} returned result(s), "
            f"and {missing_count} reconciled missing result(s)."
        ),
        rationale=(
            f"{failed_count} child enrichment run(s) failed and {cancelled_count} were cancelled. "
            f"Bundle QAQC status before addressing: {bundle_counters['approved']} approved "
            f"model-ready, {bundle_counters['partial']} partial candidate, and "
            f"{bundle_counters['held']} held. "
            + (
                "No address research was run because QAQC produced no model-ready or "
                "partial addressable targets; held component bundles need supervisor review "
                "or targeted follow-up first."
                if expected_count == 0 and bundle_counters["held"] > 0
                else "I included strict model-ready observations and partial bundle "
                "candidates, then preserved ambiguous or missing addresses for human review."
            )
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
    all_component_bundle_reviews: list[dict[str, Any]] = []
    for child_run_id in child_run_ids:
        output_path = _qaqc_output_path(root, child_run_id)
        if not output_path.is_file():
            continue
        review_set = load_qaqc_review_set(output_path)
        reviews = review_set.occupancy_reviews
        component_reviews = review_set.component_reviews
        component_bundle_reviews = review_set.component_bundle_reviews
        review_payload = [review.model_dump(mode="json") for review in reviews]
        component_review_payload = [
            review.model_dump(mode="json") for review in component_reviews
        ]
        component_bundle_review_payload = [
            review.model_dump(mode="json") for review in component_bundle_reviews
        ]
        child_reviews.append(
            {
                "run_id": child_run_id,
                "qaqc_path": str(output_path),
                "review_count": (
                    len(reviews) + len(component_reviews) + len(component_bundle_reviews)
                ),
                "reviews": review_payload,
                "component_reviews": component_review_payload,
                "component_bundle_reviews": component_bundle_review_payload,
            }
        )
        all_reviews.extend(review_payload)
        all_component_reviews.extend(component_review_payload)
        all_component_bundle_reviews.extend(component_bundle_review_payload)
    return {
        "review_count": (
            len(all_reviews) + len(all_component_reviews) + len(all_component_bundle_reviews)
        ),
        "child_reviews": child_reviews,
        "reviews": all_reviews,
        "component_reviews": all_component_reviews,
        "component_bundle_reviews": all_component_bundle_reviews,
    }


def _address_reconciliation_payload(root: Path, child_run_ids: Sequence[str]) -> dict[str, Any]:
    child_summaries: list[dict[str, Any]] = []
    all_missing: list[str] = []
    expected_count = 0
    returned_count = 0
    for child_run_id in child_run_ids:
        manifest = _load_run_manifest(root, child_run_id)
        expected = approved_address_inputs(root=root, manifest=manifest)
        expected_item_ids = [str(record["item_id"]) for record in expected]
        path = address_output_path(root, child_run_id)
        results = load_address_results(path) if path.is_file() else ()
        result_item_ids = [result.item_id for result in results]
        missing = [item_id for item_id in expected_item_ids if item_id not in result_item_ids]
        expected_count += len(expected_item_ids)
        returned_count += len(result_item_ids)
        all_missing.extend(missing)
        child_summaries.append(
            {
                "run_id": child_run_id,
                "expected_count": len(expected_item_ids),
                "returned_count": len(result_item_ids),
                "missing_count": len(missing),
                "missing_item_ids": missing,
            }
        )
    return {
        "expected_count": expected_count,
        "returned_count": returned_count,
        "missing_count": len(all_missing),
        "missing_item_ids": all_missing,
        "child_reconciliation": child_summaries,
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


def _bundle_review_allows_target(bundle: Any, review: Any | None) -> bool:
    if review is not None:
        return bundle_is_model_ready(review)
    return bool(bundle.counts_toward_target)


def _component_bundle_source_leads(
    bundle: Any,
    component_leads: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        component_leads[index]
        for index in bundle.source_lead_indexes
        if 0 <= index < len(component_leads)
    ]


def _approved_component_bundle_records_for_child(
    root: Path,
    manifest: HarvestRunManifest,
) -> tuple[dict[str, Any], ...]:
    qaqc_path = _qaqc_output_path(root, manifest.run_id)
    if not qaqc_path.is_file():
        raise FileNotFoundError(f"QAQC review not found for run: {manifest.run_id}")
    evidence_set = load_evidence_set(Path(manifest.lead_path))
    review_set = load_qaqc_review_set(qaqc_path)
    reviews_by_index = {
        review.bundle_index: review for review in review_set.component_bundle_reviews
    }
    records: list[dict[str, Any]] = []
    component_leads = [lead.model_dump(mode="json") for lead in evidence_set.component_leads]
    for bundle_index, bundle in enumerate(evidence_set.component_bundles):
        review = reviews_by_index.get(bundle_index)
        if not _bundle_review_allows_target(bundle, review):
            continue
        bundle_payload = bundle.model_dump(mode="json")
        source_leads = _component_bundle_source_leads(bundle, component_leads)
        readiness = bundle_readiness(
            bundle=bundle,
            review=review,
            source_leads=[
                evidence_set.component_leads[index]
                for index in bundle.source_lead_indexes
                if 0 <= index < len(evidence_set.component_leads)
            ],
        )
        records.append(
            {
                "item_id": f"{manifest.run_id}-component-bundle-{bundle_index}",
                "child_run_id": manifest.run_id,
                "lead_index": bundle_index,
                "bundle_index": bundle_index,
                "facility_type": manifest.profile_set,
                "component_bundle": bundle_payload,
                "component_bundle_qaqc_review": (
                    review.model_dump(mode="json") if review is not None else None
                ),
                "component_leads": source_leads,
                "bundle_readiness": readiness,
                "model_ready": readiness == "model_ready_bundle",
                "bundle_review_required": readiness != "model_ready_bundle",
            }
        )
    return tuple(records)


def _approved_allocated_component_records_for_child(
    root: Path,
    manifest: HarvestRunManifest,
) -> tuple[dict[str, Any], ...]:
    qaqc_path = _qaqc_output_path(root, manifest.run_id)
    if not qaqc_path.is_file():
        raise FileNotFoundError(f"QAQC review not found for run: {manifest.run_id}")
    evidence_set = load_evidence_set(Path(manifest.lead_path))
    review_set = load_qaqc_review_set(qaqc_path)
    records: list[dict[str, Any]] = []
    for review in review_set.allocated_component_reviews:
        if review.lead_index >= len(evidence_set.allocated_component_leads):
            continue
        if review.verification_status != LeadQaqcVerificationStatus.VERIFIED:
            continue
        if review.recommended_action != LeadQaqcRecommendedAction.KEEP:
            continue
        if not review.counts_toward_target_approved:
            continue
        lead = evidence_set.allocated_component_leads[review.lead_index]
        location = lead.facility_location
        geocode_query = ", ".join(
            value
            for value in (
                location.facility_name,
                location.specific_address_or_landmark,
                location.city_or_region,
                location.country,
            )
            if value and value.casefold() != "unknown"
        )
        records.append(
            {
                "item_id": f"{manifest.run_id}-allocated-component-{review.lead_index}",
                "child_run_id": manifest.run_id,
                "lead_index": review.lead_index,
                "facility_type": manifest.profile_set,
                "geocode_query": geocode_query,
                "allocated_component_lead": lead.model_dump(mode="json"),
                "allocated_component_qaqc_review": review.model_dump(mode="json"),
                "model_ready": True,
            }
        )
    return tuple(records)


def _addressable_component_bundle_records_for_child(
    root: Path,
    manifest: HarvestRunManifest,
) -> tuple[dict[str, Any], ...]:
    qaqc_path = _qaqc_output_path(root, manifest.run_id)
    if not qaqc_path.is_file():
        raise FileNotFoundError(f"QAQC review not found for run: {manifest.run_id}")
    evidence_set = load_evidence_set(Path(manifest.lead_path))
    review_set = load_qaqc_review_set(qaqc_path)
    reviews_by_index = {
        review.bundle_index: review for review in review_set.component_bundle_reviews
    }
    component_leads = [lead.model_dump(mode="json") for lead in evidence_set.component_leads]
    records: list[dict[str, Any]] = []
    for bundle_index, bundle in enumerate(evidence_set.component_bundles):
        review = reviews_by_index.get(bundle_index)
        source_lead_models = [
            evidence_set.component_leads[index]
            for index in bundle.source_lead_indexes
            if 0 <= index < len(evidence_set.component_leads)
        ]
        if review_set.component_bundle_reviews:
            if not bundle_is_addressable_candidate(
                bundle=bundle,
                review=review,
                source_leads=source_lead_models,
            ):
                continue
        elif not bundle.counts_toward_target:
            continue
        readiness = bundle_readiness(
            bundle=bundle,
            review=review,
            source_leads=source_lead_models,
        )
        bundle_payload = bundle.model_dump(mode="json")
        location = bundle_payload.get("location") or {}
        geocode_query = ", ".join(
            str(value or "").strip()
            for value in (
                location.get("facility_name") or bundle_payload.get("geography_name"),
                location.get("specific_address_or_landmark"),
                location.get("city_or_region"),
                location.get("country") or bundle_payload.get("country"),
            )
            if str(value or "").strip()
        )
        records.append(
            {
                "item_id": f"{manifest.run_id}-component-bundle-{bundle_index}",
                "child_run_id": manifest.run_id,
                "lead_index": bundle_index,
                "bundle_index": bundle_index,
                "facility_type": manifest.profile_set,
                "geocode_query": geocode_query,
                "component_bundle": bundle_payload,
                "component_bundle_qaqc_review": (
                    review.model_dump(mode="json") if review is not None else None
                ),
                "component_leads": _component_bundle_source_leads(bundle, component_leads),
                "bundle_readiness": readiness,
                "model_ready": readiness == "model_ready_bundle",
                "bundle_review_required": readiness != "model_ready_bundle",
            }
        )
    return tuple(records)


def _addressable_component_bundle_records_for_manifest(
    root: Path,
    manifest: Any,
) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    for child_run_id in _manifest_child_run_ids(manifest):
        child_manifest = _load_run_manifest(root, child_run_id)
        records.extend(_addressable_component_bundle_records_for_child(root, child_manifest))
    return tuple(records)


def _approved_component_bundle_records_for_manifest(
    root: Path,
    manifest: Any,
) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    for child_run_id in _manifest_child_run_ids(manifest):
        child_manifest = _load_run_manifest(root, child_run_id)
        records.extend(_approved_component_bundle_records_for_child(root, child_manifest))
    return tuple(records)


def _approved_allocated_component_records_for_manifest(
    root: Path,
    manifest: Any,
) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    for child_run_id in _manifest_child_run_ids(manifest):
        child_manifest = _load_run_manifest(root, child_run_id)
        records.extend(_approved_allocated_component_records_for_child(root, child_manifest))
    return tuple(records)


def _manifest_has_component_bundles(root: Path, manifest: Any) -> bool:
    for child_run_id in _manifest_child_run_ids(manifest):
        child_manifest = _load_run_manifest(root, child_run_id)
        if not Path(child_manifest.lead_path).is_file():
            continue
        evidence_set = load_evidence_set(Path(child_manifest.lead_path))
        if evidence_set.component_bundles:
            return True
    return False


def _component_bundle_review_counters(root: Path, child_run_ids: Sequence[str]) -> dict[str, int]:
    counters = {
        "total": 0,
        "approved": 0,
        "partial": 0,
        "held": 0,
        "review": 0,
        "retry": 0,
        "reject": 0,
    }
    for child_run_id in child_run_ids:
        qaqc_path = _qaqc_output_path(root, child_run_id)
        if not qaqc_path.is_file():
            continue
        review_set = load_qaqc_review_set(qaqc_path)
        manifest = _load_run_manifest(root, child_run_id)
        evidence_set = load_evidence_set(Path(manifest.lead_path))
        for review in review_set.component_bundle_reviews:
            counters["total"] += 1
            action = review.recommended_action.value
            if action in counters:
                counters[action] += 1
            bundle = (
                evidence_set.component_bundles[review.bundle_index]
                if review.bundle_index < len(evidence_set.component_bundles)
                else None
            )
            source_leads = (
                [
                    evidence_set.component_leads[index]
                    for index in bundle.source_lead_indexes
                    if 0 <= index < len(evidence_set.component_leads)
                ]
                if bundle is not None
                else []
            )
            readiness = (
                bundle_readiness(bundle=bundle, review=review, source_leads=source_leads)
                if bundle is not None
                else "held_component_bundle"
            )
            if readiness == "model_ready_bundle":
                counters["approved"] += 1
            elif readiness == "partial_component_bundle":
                counters["partial"] += 1
            else:
                counters["held"] += 1
    return counters


def _qaqc_audit_counters(root: Path, child_run_ids: Sequence[str]) -> dict[str, Any]:
    direct_actions: Counter[str] = Counter()
    component_actions: Counter[str] = Counter()
    bundle_actions: Counter[str] = Counter()
    held_bundle_missing_fields: Counter[str] = Counter()
    direct_total = 0
    component_total = 0
    bundle_total = 0
    approved_bundle_count = 0
    partial_bundle_count = 0
    for child_run_id in child_run_ids:
        qaqc_path = _qaqc_output_path(root, child_run_id)
        if not qaqc_path.is_file():
            continue
        review_set = load_qaqc_review_set(qaqc_path)
        manifest = _load_run_manifest(root, child_run_id)
        evidence_set = load_evidence_set(Path(manifest.lead_path))
        direct_total += len(review_set.occupancy_reviews)
        component_total += len(review_set.component_reviews)
        bundle_total += len(review_set.component_bundle_reviews)
        direct_actions.update(
            review.recommended_action.value for review in review_set.occupancy_reviews
        )
        component_actions.update(
            review.recommended_action.value for review in review_set.component_reviews
        )
        for review in review_set.component_bundle_reviews:
            bundle_actions.update((review.recommended_action.value,))
            bundle = (
                evidence_set.component_bundles[review.bundle_index]
                if review.bundle_index < len(evidence_set.component_bundles)
                else None
            )
            source_leads = (
                [
                    evidence_set.component_leads[index]
                    for index in bundle.source_lead_indexes
                    if 0 <= index < len(evidence_set.component_leads)
                ]
                if bundle is not None
                else []
            )
            readiness = (
                bundle_readiness(bundle=bundle, review=review, source_leads=source_leads)
                if bundle is not None
                else "held_component_bundle"
            )
            if readiness == "model_ready_bundle":
                approved_bundle_count += 1
            elif readiness == "partial_component_bundle":
                partial_bundle_count += 1
            else:
                held_bundle_missing_fields.update(review.missing_component_types)
    return {
        "direct_total": direct_total,
        "component_total": component_total,
        "bundle_total": bundle_total,
        "direct_actions": dict(direct_actions),
        "component_actions": dict(component_actions),
        "bundle_actions": dict(bundle_actions),
        "approved_bundle_count": approved_bundle_count,
        "partial_bundle_count": partial_bundle_count,
        "held_bundle_count": bundle_total - approved_bundle_count - partial_bundle_count,
        "common_missing_bundle_fields": dict(held_bundle_missing_fields.most_common(5)),
    }


def _approved_component_records_for_manifest(
    root: Path,
    manifest: Any,
) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    for child_run_id in _manifest_child_run_ids(manifest):
        child_manifest = _load_run_manifest(root, child_run_id)
        records.extend(_approved_component_records_for_child(root, child_manifest))
    return tuple(records)


def _approved_component_export_records_for_manifest(
    root: Path,
    manifest: Any,
) -> tuple[dict[str, Any], ...]:
    return (
        _approved_component_records_for_manifest(root, manifest)
        + _approved_allocated_component_records_for_manifest(root, manifest)
    )


def _component_records_json(records: Sequence[dict[str, Any]]) -> str:
    return json.dumps(list(records), indent=2)


def _component_records_csv(records: Sequence[dict[str, Any]]) -> str:
    output = io.StringIO()
    fieldnames = (
        "item_id",
        "child_run_id",
        "lead_index",
        "facility_type",
        "evidence_role",
        "source_url",
        "source_title",
        "component_type",
        "value",
        "allocated_value",
        "regional_source_value",
        "unit",
        "time_basis",
        "geography_level",
        "period_label",
        "facility_name",
        "geography_name",
        "country",
        "allocation_method",
        "denominator_scope",
        "facility_universe_count",
        "allocation_confidence",
        "allocation_notes",
        "qaqc_status",
        "recommended_action",
        "review_notes",
    )
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for record in records:
        if "allocated_component_lead" in record:
            lead = record["allocated_component_lead"]
            review = record["allocated_component_qaqc_review"]
            location = lead["facility_location"]
            writer.writerow(
                {
                    "item_id": record["item_id"],
                    "child_run_id": record["child_run_id"],
                    "lead_index": record["lead_index"],
                    "facility_type": record.get("facility_type", ""),
                    "evidence_role": "allocated_component_input",
                    "source_url": lead["regional_source_url"],
                    "source_title": lead.get("regional_source_title", ""),
                    "component_type": lead["component_type"],
                    "value": lead["allocated_value"],
                    "allocated_value": lead["allocated_value"],
                    "regional_source_value": lead["regional_value"],
                    "unit": lead["unit"],
                    "time_basis": lead["time_basis"],
                    "geography_level": "facility",
                    "period_label": lead.get("period_label") or "",
                    "facility_name": location["facility_name"],
                    "geography_name": lead["regional_geography_name"],
                    "country": lead["country"],
                    "allocation_method": lead["allocation_method"],
                    "denominator_scope": lead["denominator_scope"],
                    "facility_universe_count": lead["facility_universe_count"],
                    "allocation_confidence": lead["confidence"],
                    "allocation_notes": lead["allocation_notes"],
                    "qaqc_status": review["verification_status"],
                    "recommended_action": review["recommended_action"],
                    "review_notes": review.get("review_notes", ""),
                }
            )
            continue
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
                    "evidence_role": "component_input",
                    "source_url": lead["source_url"],
                    "source_title": lead.get("source_title", ""),
                    "component_type": datum["component_type"],
                    "value": datum["value"],
                    "allocated_value": "",
                    "regional_source_value": "",
                    "unit": datum["unit"],
                    "time_basis": datum["time_basis"],
                    "geography_level": datum["geography_level"],
                    "period_label": datum.get("period_label") or "",
                    "facility_name": location.get("facility_name", ""),
                    "geography_name": lead["geography_name"],
                    "country": lead["country"],
                    "allocation_method": "",
                    "denominator_scope": "",
                    "facility_universe_count": "",
                    "allocation_confidence": "",
                    "allocation_notes": "",
                    "qaqc_status": review["verification_status"],
                    "recommended_action": review["recommended_action"],
                    "review_notes": review.get("review_notes", ""),
                }
            )
    return output.getvalue()


def _format_component_table_value(datum: dict[str, Any]) -> str:
    value = datum.get("value", "")
    value_text = f"{value:g}" if isinstance(value, int | float) else str(value)
    unit = str(datum.get("unit") or "").strip()
    main = f"{value_text} {unit}".strip()
    qualifiers = [
        str(datum.get("time_basis") or "").strip(),
        str(datum.get("geography_level") or "").strip(),
        str(datum.get("period_label") or "").strip(),
    ]
    qualifier_text = ", ".join(qualifier for qualifier in qualifiers if qualifier)
    return f"{main} ({qualifier_text})" if qualifier_text else main


def _count_column_key(group_type: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", group_type.casefold()).strip("_")
    return f"count_{normalized or 'other'}"


def _count_values_from_occupancy_data(
    occupancy_data: Sequence[dict[str, Any]],
) -> dict[str, int | float | str]:
    values: dict[str, int | float | str] = {}
    for datum in occupancy_data:
        group_type = str(datum.get("group_type") or "other")
        key = _count_column_key(group_type)
        count = datum.get("count", "")
        existing = values.get(key)
        if isinstance(existing, int | float) and isinstance(count, int | float):
            values[key] = existing + count
        elif existing not in {None, ""}:
            values[key] = f"{existing}; {count}"
        else:
            values[key] = count
    return values


def _count_relationship(occupancy_data: Sequence[dict[str, Any]], notes: str) -> str:
    if len(occupancy_data) <= 1:
        return "single_count"
    notes_folded = notes.casefold()
    if any(term in notes_folded for term in ("do not add", "do not sum", "overlap")):
        return "overlapping_or_non_additive"
    if any(term in notes_folded for term in ("total", "subgroup", "avoid double counting")):
        return "mixed_total_and_subgroups"
    group_types = [
        str(datum.get("group_type") or "").strip().casefold() for datum in occupancy_data
    ]
    if all(group_types) and len(set(group_types)) == len(group_types):
        return "additive_subgroups"
    return "multiple_groups_unknown_additivity"


def _append_component_value(
    component_values: dict[str, str],
    datum: dict[str, Any],
) -> None:
    component_type = str(datum.get("component_type") or "").strip()
    if not component_type:
        return
    value = _format_component_table_value(datum)
    existing = component_values.get(component_type)
    if existing:
        existing_parts = {part.strip() for part in existing.split(";")}
        if value not in existing_parts:
            component_values[component_type] = f"{existing}; {value}"
    else:
        component_values[component_type] = value


def _component_location_values(
    lead: dict[str, Any],
) -> tuple[str, str, str, str]:
    location = lead.get("location") or {}
    facility_name = str(location.get("facility_name") or lead.get("geography_name") or "")
    city_or_region = str(location.get("city_or_region") or lead.get("geography_name") or "")
    country = str(location.get("country") or lead.get("country") or "")
    reported_address = str(location.get("specific_address_or_landmark") or "")
    return facility_name, city_or_region, country, reported_address


def _component_bundle_table_rows(
    *,
    root: Path,
    child_run_id: str,
    child_manifest: HarvestRunManifest,
    evidence_set: Any,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    component_leads = [
        lead.model_dump(mode="json") for lead in evidence_set.component_leads
    ]
    review_set = None
    qaqc_path = _qaqc_output_path(root, child_run_id)
    if qaqc_path.is_file():
        review_set = load_qaqc_review_set(qaqc_path)
    bundle_reviews = (
        {review.bundle_index: review for review in review_set.component_bundle_reviews}
        if review_set is not None
        else {}
    )
    address_by_item_id: dict[str, dict[str, Any]] = {}
    address_path = address_output_path(root, child_run_id)
    if address_path.is_file():
        address_by_item_id = {
            result.item_id: result.model_dump(mode="json")
            for result in load_address_results(address_path)
        }
    if evidence_set.component_bundles:
        for bundle_index, bundle in enumerate(evidence_set.component_bundles):
            bundle_payload = bundle.model_dump(mode="json")
            lead_payloads = [
                component_leads[index]
                for index in bundle_payload.get("source_lead_indexes", ())
                if 0 <= int(index) < len(component_leads)
            ]
            if not lead_payloads:
                continue
            component_values: dict[str, str] = {}
            source_urls: list[str] = []
            strategies: set[str] = set()
            confidence_values: set[str] = set()
            review_notes: list[str] = []
            for lead_payload in lead_payloads:
                source_url = str(lead_payload.get("source_url") or "")
                if source_url and source_url not in source_urls:
                    source_urls.append(source_url)
                if lead_payload.get("strategy_id"):
                    strategies.add(str(lead_payload["strategy_id"]))
                if lead_payload.get("confidence"):
                    confidence_values.add(str(lead_payload["confidence"]))
                if lead_payload.get("review_notes"):
                    review_notes.append(str(lead_payload["review_notes"]))
                for datum in lead_payload.get("component_data", ()):
                    _append_component_value(component_values, datum)
            location = bundle_payload.get("location") or {}
            fallback_lead = lead_payloads[0]
            fallback_facility, fallback_city, fallback_country, fallback_address = (
                _component_location_values(fallback_lead)
            )
            facility_name = str(
                location.get("facility_name")
                or bundle_payload.get("geography_name")
                or fallback_facility
            )
            city_or_region = str(location.get("city_or_region") or fallback_city)
            country = str(
                location.get("country") or bundle_payload.get("country") or fallback_country
            )
            reported_address = str(
                location.get("specific_address_or_landmark") or fallback_address
            )
            item_id = f"{child_run_id}-component-bundle-{bundle_index}"
            address = address_by_item_id.get(item_id)
            review = bundle_reviews.get(bundle_index)
            source_lead_models = [
                evidence_set.component_leads[index]
                for index in bundle.source_lead_indexes
                if 0 <= index < len(evidence_set.component_leads)
            ]
            readiness = bundle_readiness(
                bundle=bundle,
                review=review,
                source_leads=source_lead_models,
            )
            rows.append(
                {
                    "row_id": item_id,
                    "item_id": item_id,
                    "run_id": child_run_id,
                    "sample_set_id": "",
                    "sample_round": "",
                    "facility_type": child_manifest.profile_set,
                    "evidence_role": "component_input",
                    "lead_index": ",".join(
                        str(index) for index in bundle_payload.get("source_lead_indexes", ())
                    ),
                    "count_index": "",
                    "facility_name": facility_name,
                    "count": "",
                    "group_type": "",
                    "component_type": ", ".join(component_values),
                    "component_values": component_values,
                    "value": "; ".join(
                        f"{key}: {value}" for key, value in component_values.items()
                    ),
                    "unit": "",
                    "time_basis": "",
                    "geography_level": "",
                    "incident_date": "",
                    "incident_time": "",
                    "strategy_id": ", ".join(sorted(strategies)),
                    "representativeness": "component_input",
                    "confidence": bundle_payload.get("confidence")
                    or ", ".join(sorted(confidence_values)),
                    "city_or_region": city_or_region,
                    "country": country,
                    "source_url": source_urls[0] if source_urls else "",
                    "source_urls": "; ".join(source_urls),
                    "source_count": len(source_urls),
                    "qaqc_status": (
                        review.verification_status.value if review is not None else ""
                    ),
                    "recommended_action": (
                        review.recommended_action.value if review is not None else ""
                    ),
                    "bundle_qaqc_status": (
                        review.verification_status.value if review is not None else ""
                    ),
                    "address_status": (
                        str(address.get("status")) if address is not None else "not_run"
                    ),
                    "enriched_address": (
                        address.get("formatted_address")
                        if address is not None
                        else reported_address
                    )
                    or reported_address,
                    "geometry_status": "not_applicable",
                    "area_m2": "",
                    "review_notes": (
                        review.review_notes
                        if review is not None
                        else bundle_payload.get("completion_notes") or "; ".join(review_notes)
                    ),
                    "component_bundle_status": bundle_payload.get("completion_status", ""),
                    "counts_toward_target": bundle_payload.get("counts_toward_target", False),
                    "bundle_readiness": readiness,
                    "model_ready": readiness == "model_ready_bundle",
                    "bundle_review_required": readiness != "model_ready_bundle",
                    "missing_component_types": ", ".join(
                        bundle_payload.get("missing_component_types", ())
                    ),
                    "excluded_from_dataset": False,
                    "exclusion_reason_code": "",
                    "exclusion_reason_note": "",
                }
            )
        return rows

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for lead_payload in component_leads:
        facility_name, city_or_region, country, _ = _component_location_values(lead_payload)
        grouped.setdefault((facility_name, city_or_region, country), []).append(lead_payload)
    for group_index, ((facility_name, city_or_region, country), lead_payloads) in enumerate(
        grouped.items()
    ):
        fallback_component_values: dict[str, str] = {}
        fallback_source_urls: list[str] = []
        lead_indexes: list[str] = []
        for lead_payload in lead_payloads:
            lead_index = component_leads.index(lead_payload)
            lead_indexes.append(str(lead_index))
            source_url = str(lead_payload.get("source_url") or "")
            if source_url and source_url not in fallback_source_urls:
                fallback_source_urls.append(source_url)
            for datum in lead_payload.get("component_data", ()):
                _append_component_value(fallback_component_values, datum)
        _, _, _, reported_address = _component_location_values(lead_payloads[0])
        item_id = f"{child_run_id}-component-group-{group_index}"
        rows.append(
            {
                "row_id": item_id,
                "item_id": item_id,
                "run_id": child_run_id,
                "sample_set_id": "",
                "sample_round": "",
                "facility_type": child_manifest.profile_set,
                "evidence_role": "component_input",
                "lead_index": ",".join(lead_indexes),
                "count_index": "",
                "facility_name": facility_name,
                "count": "",
                "group_type": "",
                "component_type": ", ".join(fallback_component_values),
                "component_values": fallback_component_values,
                "value": "; ".join(
                    f"{key}: {value}" for key, value in fallback_component_values.items()
                ),
                "unit": "",
                "time_basis": "",
                "geography_level": "",
                "incident_date": "",
                "incident_time": "",
                "strategy_id": "",
                "representativeness": "component_input",
                "confidence": "",
                "city_or_region": city_or_region,
                "country": country,
                "source_url": fallback_source_urls[0] if fallback_source_urls else "",
                "source_urls": "; ".join(fallback_source_urls),
                "source_count": len(fallback_source_urls),
                "qaqc_status": "",
                "recommended_action": "",
                "address_status": "not_run",
                "enriched_address": reported_address,
                "geometry_status": "not_applicable",
                "area_m2": "",
                "review_notes": "",
                "component_bundle_status": "",
                "counts_toward_target": False,
                "missing_component_types": "",
                "excluded_from_dataset": False,
                "exclusion_reason_code": "",
                "exclusion_reason_note": "",
            }
        )
    return rows


def _allocated_component_table_row(
    *,
    child_run_id: str,
    child_manifest: HarvestRunManifest,
    lead_index: int,
    lead_payload: dict[str, Any],
    review_payload: dict[str, Any] | None = None,
    address_payload: dict[str, Any] | None = None,
    sample_set_id: str = "",
    sample_round: str | int = "",
) -> dict[str, Any]:
    location = lead_payload["facility_location"]
    item_id = f"{child_run_id}-allocated-component-{lead_index}"
    component_value = _format_component_table_value(
        {
            "component_type": lead_payload["component_type"],
            "value": lead_payload["allocated_value"],
            "unit": lead_payload["unit"],
            "time_basis": lead_payload["time_basis"],
            "geography_level": "facility",
            "period_label": lead_payload.get("period_label"),
        }
    )
    component_values = {lead_payload["component_type"]: component_value}
    address = address_payload or {}
    return {
        "row_id": item_id,
        "item_id": item_id,
        "run_id": child_run_id,
        "sample_set_id": sample_set_id,
        "sample_round": sample_round,
        "facility_type": child_manifest.profile_set,
        "evidence_role": "allocated_component_input",
        "lead_index": lead_index,
        "count_index": "",
        "facility_name": location["facility_name"],
        "count": "",
        "group_type": "",
        "component_type": lead_payload["component_type"],
        "component_values": component_values,
        "value": f"{lead_payload['component_type']}: {component_value}",
        "allocated_value": lead_payload["allocated_value"],
        "regional_source_value": lead_payload["regional_value"],
        "allocation_method": lead_payload["allocation_method"],
        "denominator_scope": lead_payload["denominator_scope"],
        "facility_universe_count": lead_payload["facility_universe_count"],
        "allocation_confidence": lead_payload["confidence"],
        "allocation_notes": lead_payload["allocation_notes"],
        "unit": lead_payload["unit"],
        "time_basis": lead_payload["time_basis"],
        "geography_level": "facility",
        "incident_date": "",
        "incident_time": "",
        "strategy_id": lead_payload.get("strategy_id") or "",
        "representativeness": lead_payload.get("representativeness") or "",
        "confidence": lead_payload.get("confidence") or "",
        "city_or_region": location["city_or_region"],
        "country": location["country"],
        "source_url": lead_payload["regional_source_url"],
        "source_urls": "; ".join(
            (lead_payload["regional_source_url"], lead_payload["facility_source_url"])
        ),
        "source_count": 2,
        "qaqc_status": review_payload.get("verification_status", "") if review_payload else "",
        "recommended_action": (
            review_payload.get("recommended_action", "") if review_payload else ""
        ),
        "address_status": str(address.get("status") or "not_run"),
        "enriched_address": (
            address.get("formatted_address")
            or location.get("specific_address_or_landmark")
            or ""
        ),
        "geometry_status": "not_applicable",
        "area_m2": "",
        "review_notes": (
            review_payload.get("review_notes", "")
            if review_payload
            else lead_payload.get("review_notes") or lead_payload["allocation_notes"]
        ),
        "component_bundle_status": "",
        "counts_toward_target": lead_payload.get("counts_toward_target", False),
        "bundle_readiness": "",
        "model_ready": bool(
            review_payload
            and review_payload.get("verification_status") == "verified"
            and review_payload.get("recommended_action") == "keep"
            and review_payload.get("counts_toward_target_approved")
        ),
        "bundle_review_required": False,
        "missing_component_types": "",
        "excluded_from_dataset": False,
        "exclusion_reason_code": "",
        "exclusion_reason_note": "",
    }


def _geometry_items_payload(root: Path, manifest: Any) -> dict[str, Any]:
    records = merge_address_results(root, _approved_records_for_manifest(root, manifest))
    bundle_records = merge_address_results(
        root, _addressable_component_bundle_records_for_manifest(root, manifest)
    )
    allocated_records = merge_address_results(
        root, _approved_allocated_component_records_for_manifest(root, manifest)
    )
    items = tuple(
        merge_geometry_items(
            root,
            tuple(records) + tuple(bundle_records) + tuple(allocated_records),
        )
    )
    return {"item_count": len(items), "items": items}


def _geometry_record_context(
    root: Path,
    item_id: str,
) -> tuple[HarvestRunManifest, dict[str, Any]]:
    if "-allocated-component-" in item_id:
        child_run_id = item_id.split("-allocated-component-", 1)[0]
    elif "-component-bundle-" in item_id:
        child_run_id = item_id.split("-component-bundle-", 1)[0]
    else:
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
        if text and text.casefold() != "unknown" and text not in queries:
            queries.append(text)

    def join_parts(*values: object) -> str:
        return ", ".join(
            str(value or "").strip()
            for value in values
            if str(value or "").strip() and str(value or "").strip().casefold() != "unknown"
        )

    def without_house_number(value: object) -> str:
        return re.sub(r"^\s*\d+[A-Za-z]?(?:\s+|,\s*)", "", str(value or "")).strip()

    add_query(requested_query)
    address = record.get("address_enrichment")
    facility_name = _record_facility_name(record)
    if isinstance(address, dict):
        address_line1 = str(address.get("address_line1") or "").strip()
        city = address.get("city_or_region")
        state = address.get("state_or_province")
        postal_code = address.get("postal_code")
        country = address.get("country")
        add_query(
            join_parts(address_line1, city, state, postal_code, country)
        )
        add_query(address.get("formatted_address"))
        add_query(
            join_parts(
                without_house_number(address_line1),
                city,
                state,
                postal_code,
                country,
            )
        )
        add_query(join_parts(facility_name, city, state, country))
        add_query(join_parts(postal_code, state, city, facility_name, country))
        add_query(join_parts(facility_name, address.get("address_line2"), city, state, country))
        evidence_quote = str(address.get("address_evidence_quote") or "")
        if any(ord(character) > 127 for character in evidence_quote):
            add_query(address.get("address_evidence_quote"))
    lead = record.get("lead")
    if isinstance(lead, dict):
        location = lead.get("location")
        if isinstance(location, dict):
            add_query(
                join_parts(
                    location.get("facility_name"),
                    location.get("specific_address_or_landmark"),
                    location.get("city_or_region"),
                    location.get("country"),
                )
            )
            add_query(
                join_parts(
                    location.get("facility_name"),
                    location.get("city_or_region"),
                    location.get("country"),
                )
            )
    bundle = record.get("component_bundle")
    if isinstance(bundle, dict):
        location = bundle.get("location")
        if isinstance(location, dict):
            add_query(
                join_parts(
                    location.get("facility_name") or bundle.get("geography_name"),
                    location.get("specific_address_or_landmark"),
                    location.get("city_or_region"),
                    location.get("country") or bundle.get("country"),
                )
            )
            add_query(
                join_parts(
                    location.get("facility_name") or bundle.get("geography_name"),
                    location.get("city_or_region"),
                    location.get("country") or bundle.get("country"),
                )
            )
    allocated = record.get("allocated_component_lead")
    if isinstance(allocated, dict):
        location = allocated.get("facility_location")
        if isinstance(location, dict):
            add_query(
                join_parts(
                    location.get("facility_name"),
                    location.get("specific_address_or_landmark"),
                    location.get("city_or_region"),
                    location.get("country") or allocated.get("country"),
                )
            )
            add_query(
                join_parts(
                    location.get("facility_name"),
                    location.get("city_or_region"),
                    location.get("country") or allocated.get("country"),
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
        return {
            "status": "skipped",
            "reason": "Model-ready or partial addressable input was not found.",
        }
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


def _geographer_validation_context(
    root: Path,
    manifest: HarvestRunManifest,
) -> tuple[dict[str, str], tuple[str, ...]]:
    if manifest.geographer_plan_path is None:
        return {}, ()
    try:
        plan = load_geographer_plan(root, manifest.geographer_plan_path)
    except (OSError, ValueError):
        return {}, ()
    proposal = plan.proposal
    country_code = proposal.country_code or manifest.country
    country_aliases = country_alias_map(country_code, proposal.country_aliases)
    locality_aliases = tuple(
        dict.fromkeys(
            term
            for item in proposal.administrative_terms
            for term in (item.standard_term, item.local_term)
            if term.strip()
        )
    )
    return country_aliases, locality_aliases


def _spatially_geocode_item(
    *,
    root: Path,
    geocoder: SpatialGeocoder,
    item_id: str,
    requested_query: str,
) -> tuple[dict[str, Any] | None, dict[str, object], str]:
    manifest, record = _geometry_record_context(root, item_id)
    _, queries = _geocode_context(root, item_id, requested_query)
    expected_facility_name = _record_facility_name(record)
    expected_postal_code = _record_expected_postal_code(record)
    expected_country_aliases, expected_locality_aliases = _geographer_validation_context(
        root, manifest
    )
    attempts: list[dict[str, object]] = []
    final_validation: dict[str, object] = {
        "status": "no_match",
        "requires_human_intervention": True,
        "reason": "No geocoding query produced a usable candidate.",
        "candidate_count": 0,
    }
    candidate_options: list[dict[str, Any]] = []
    for query in queries:
        result = geocoder.geocode(query)
        candidate_options = _merge_ranked_candidate_options(
            candidate_options,
            _ranked_candidate_options(
                result,
                record=record,
                expected_country=manifest.country,
                expected_locality=manifest.locality,
                expected_country_aliases=expected_country_aliases,
                expected_locality_aliases=expected_locality_aliases,
                query=query,
            ),
        )
        accepted, validation = spatially_validate_geocode_result(
            result,
            expected_country=manifest.country,
            expected_locality=manifest.locality,
            expected_country_aliases=expected_country_aliases,
            expected_locality_aliases=expected_locality_aliases,
            expected_postal_code=expected_postal_code,
            expected_facility_name=expected_facility_name,
        )
        mismatch = _address_candidate_mismatch(accepted, record) if accepted is not None else None
        if mismatch is not None:
            validation = {
                **validation,
                "status": "address_mismatch",
                "requires_human_intervention": True,
                "reason": mismatch,
                "hard_conflict": True,
            }
            accepted = None
        elif accepted is not None:
            warnings = _address_candidate_warnings(accepted, record)
            if warnings:
                validation = {
                    **validation,
                    "address_warnings": list(warnings),
                    "reason": f"{validation['reason']} {' '.join(warnings)}",
                }
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
    display_mode: str = "progress",
    alert_message: str | None = None,
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
        "display_mode": display_mode,
        "alert_message": alert_message if alert_message is not None else (
            detail if status in {"attention", "failed"} else None
        ),
    }


def _summary_int(summary: dict[str, object], key: str, default: int = 0) -> int:
    value = summary.get(key, default)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float | str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _summary_dicts(summary: dict[str, object], key: str) -> list[dict[str, object]]:
    value = summary.get(key, [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _activity_report_text(manifest: HarvestRunManifest) -> str:
    if manifest.activity_path is None:
        return ""
    path = Path(manifest.activity_path)
    if not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""

    chunks: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, str):
            chunks.append(value)
        elif isinstance(value, dict):
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    collect(payload)
    return " ".join(chunks).casefold()


def _harvester_activity_alert_summary(manifest: HarvestRunManifest) -> str:
    if manifest.activity_path is None:
        return ""
    path = Path(manifest.activity_path)
    if not path.is_file():
        return ""
    try:
        report = load_harvester_activity_report(path)
    except (OSError, ValueError, ValidationError):
        return ""

    parts = [f"Agent report: {report.overall_summary}"]
    unproductive = [
        item
        for item in report.strategy_activity
        if item.accepted_lead_count == 0 or item.outcome.value != "productive"
    ]
    if unproductive:
        outcomes = "; ".join(
            f"{item.strategy_id.value}: {item.outcome.value} ({item.accepted_lead_count})"
            for item in unproductive[:3]
        )
        parts.append(f"Limited pathways: {outcomes}.")
    blocker_notes = [
        note
        for note in report.rejected_or_context_notes
        if any(
            marker in note.casefold()
            for marker in (
                "not retrieved",
                "not found",
                "missing",
                "context",
                "not visible",
                "did not expose",
            )
        )
    ] or list(report.rejected_or_context_notes[:2])
    if blocker_notes:
        parts.append("Blockers: " + " ".join(blocker_notes[:2]))
    if report.follow_up_suggestions:
        parts.append("Suggested next: " + " ".join(report.follow_up_suggestions[:2]))
    return " ".join(parts)


def _has_dataset_row_extraction_gap(manifest: HarvestRunManifest) -> bool:
    if manifest.status != HarvestRunStatus.COMPLETED or manifest.strategy_plan is None:
        return False
    if not any(
        item.strategy_id == EvidenceStrategyType.DATASET_ROW_EXTRACTION
        for item in manifest.strategy_plan.recommendations
    ):
        return False
    summary = manifest.summary or {}
    if _summary_int(summary, "budget_observation_count") > 0:
        return False
    if (
        _summary_int(summary, "lead_count")
        + _summary_int(summary, "component_lead_count")
        + _summary_int(summary, "component_bundle_count")
    ) > 0:
        return False

    activity_text = _activity_report_text(manifest)
    if not activity_text:
        return False
    row_gap_terms = (
        "row-level component data not retrieved",
        "row-level values",
        "row level values",
        "row-level data",
        "csv/api",
        "csv",
        "api",
        "ckan",
        "sdmx",
        "downloadable",
        "dataset",
        "table",
    )
    data_source_terms = ("dataset", "csv", "api", "ckan", "sdmx", "download", "table")
    retrieval_gap_terms = (
        "not retrieved",
        "could not retrieve",
        "unable to retrieve",
        "no row",
        "without row",
        "could not extract",
        "unable to extract",
    )
    return (
        any(term in activity_text for term in row_gap_terms)
        and any(term in activity_text for term in data_source_terms)
        and any(term in activity_text for term in retrieval_gap_terms)
    )


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
    source_backed_facility_row_count = 0
    allocated_facility_row_count = 0
    regional_support_row_count = 0
    for child in initial_manifests:
        if child.validation_valid and Path(child.lead_path).is_file():
            try:
                fresh_summary = summarize_evidence_set(
                    load_evidence_set(Path(child.lead_path))
                )
            except (OSError, ValueError, ValidationError):
                observation_count += len(load_leads(Path(child.lead_path)))
                continue
            source_backed_facility_row_count += _summary_int(
                fresh_summary, "source_backed_facility_rows"
            )
            allocated_facility_row_count += _summary_int(
                fresh_summary, "allocated_facility_rows"
            )
            regional_support_row_count += _summary_int(
                fresh_summary, "regional_support_rows"
            )
            observation_count += _summary_int(fresh_summary, "budget_observation_count")
            continue
        if child.summary is not None:
            source_backed_facility_row_count += _summary_int(
                child.summary, "source_backed_facility_rows"
            )
            allocated_facility_row_count += _summary_int(
                child.summary, "allocated_facility_rows"
            )
            regional_support_row_count += _summary_int(
                child.summary, "regional_support_rows"
            )
            budget_count = child.summary.get("budget_observation_count")
            if isinstance(budget_count, int):
                observation_count += budget_count
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
    dataset_row_gap_run_ids = tuple(
        child.run_id for child in initial_manifests if _has_dataset_row_extraction_gap(child)
    )
    dataset_row_gap_count = len(dataset_row_gap_run_ids)
    harvest_detail = (
        f"{successful_jobs}/{planned_jobs} jobs completed successfully; "
        f"{failed_jobs} failed or cancelled; "
        f"{observation_count}/{lead_quota} target observations."
    )
    if dataset_row_gap_count:
        harvest_detail += (
            f" No row-level component data retrieved from dataset sources for "
            f"{dataset_row_gap_count} run(s); inspect the Harvester activity report for the "
            "dataset/API/manual extraction path."
        )
    harvest_alert_message = None
    if harvest_status == "attention":
        alert_parts: list[str] = []
        if failed_jobs:
            alert_parts.append(
                f"{failed_jobs} of {planned_jobs} harvest job(s) failed or were cancelled."
            )
        if observation_count < lead_quota:
            if source_backed_facility_row_count or allocated_facility_row_count:
                alert_parts.append(
                    f"Harvest stopped under target: {observation_count}/{lead_quota} "
                    f"facility row(s). Assembled {source_backed_facility_row_count} "
                    f"source-backed and {allocated_facility_row_count} allocated row(s); "
                    f"{regional_support_row_count} regional support row(s) do not count "
                    "unless converted into facility observations."
                )
            else:
                alert_parts.append(
                    f"Harvest stopped under target: {observation_count}/{lead_quota} "
                    "countable observation(s)."
                )
        if dataset_row_gap_count:
            alert_parts.append(
                "No row-level component data was retrieved from dataset sources for "
                f"{dataset_row_gap_count} run(s)."
            )
        activity_summaries = [
            activity_summary
            for child in initial_manifests
            if (activity_summary := _harvester_activity_alert_summary(child))
        ]
        if activity_summaries:
            alert_parts.append(activity_summaries[0])
        harvest_alert_message = " ".join(alert_parts) or harvest_detail

    all_lead_count = 0
    review_count = 0
    target_observation_count = 0
    target_review_count = 0
    verified_count = 0
    direct_verified_count = 0
    allocated_component_evidence_count = 0
    allocated_component_review_count = 0
    allocated_component_verified_count = 0
    supporting_component_evidence_count = 0
    supporting_component_review_count = 0
    supporting_component_verified_count = 0
    address_target_count = 0
    held_component_bundle_count = 0
    approved_component_bundle_count = 0
    partial_component_bundle_count = 0
    rejected_count = 0
    target_rejected_count = 0
    address_count = 0
    address_found_count = 0
    for child_run_id in all_child_ids:
        child_manifest = child_manifests.get(child_run_id)
        if (
            child_manifest is not None
            and child_manifest.validation_valid
            and Path(child_manifest.lead_path).is_file()
        ):
            evidence_set = load_evidence_set(Path(child_manifest.lead_path))
            all_lead_count += (
                len(evidence_set.occupancy_leads)
                + len(evidence_set.component_leads)
                + len(evidence_set.component_bundles)
                + len(evidence_set.allocated_component_leads)
            )
            allocated_component_evidence_count += len(
                evidence_set.allocated_component_leads
            )
            countable_allocated_indexes = {
                index
                for index, lead in enumerate(evidence_set.allocated_component_leads)
                if lead.counts_toward_target and lead.is_valid_allocated_component_report
            }
            countable_bundle_indexes = {
                index
                for index, bundle in enumerate(evidence_set.component_bundles)
                if bundle.counts_toward_target
                and not bundle_is_allocated_shadow(bundle)
            }
            target_component_source_indexes = {
                source_index
                for index, bundle in enumerate(evidence_set.component_bundles)
                if index in countable_bundle_indexes
                for source_index in bundle.source_lead_indexes
                if 0 <= source_index < len(evidence_set.component_leads)
            }
            if evidence_set.component_bundles:
                target_observation_count += (
                    len(evidence_set.occupancy_leads)
                    + len(countable_bundle_indexes)
                    + len(countable_allocated_indexes)
                )
                supporting_component_evidence_count += len(target_component_source_indexes)
            else:
                target_observation_count += (
                    len(evidence_set.occupancy_leads)
                    + len(evidence_set.component_leads)
                    + len(countable_allocated_indexes)
                )
        else:
            evidence_set = None
            countable_bundle_indexes = set()
            countable_allocated_indexes = set()
            target_component_source_indexes = set()
        qaqc_path = _qaqc_output_path(root, child_run_id)
        if qaqc_path.is_file():
            review_set = load_qaqc_review_set(qaqc_path)
            reviews = review_set.occupancy_reviews
            component_reviews = review_set.component_reviews
            component_bundle_reviews = review_set.component_bundle_reviews
            allocated_component_reviews = review_set.allocated_component_reviews
            review_count += len(reviews) + len(component_reviews) + len(
                component_bundle_reviews
            ) + len(allocated_component_reviews)
            allocated_component_review_count += len(allocated_component_reviews)
            has_bundle_reviews = bool(component_bundle_reviews)
            target_allocated_reviews = tuple(
                review
                for review in allocated_component_reviews
                if review.lead_index in countable_allocated_indexes
            )
            if has_bundle_reviews:
                target_bundle_reviews = tuple(
                    review
                    for review in component_bundle_reviews
                    if review.bundle_index in countable_bundle_indexes
                )
                target_review_count += (
                    len(reviews)
                    + len(target_bundle_reviews)
                    + len(target_allocated_reviews)
                )
                supporting_component_review_count += sum(
                    review.lead_index in target_component_source_indexes
                    for review in component_reviews
                )
            else:
                target_bundle_reviews = ()
                target_review_count += (
                    len(reviews)
                    + len(component_reviews)
                    + len(target_allocated_reviews)
                )
            direct_keep_count = sum(
                review.verification_status.value == "verified"
                and review.recommended_action.value == "keep"
                for review in reviews
            )
            component_keep_count = sum(
                review.verification_status.value == "verified"
                and review.recommended_action.value == "keep"
                for review in component_reviews
            )
            supporting_component_verified_count += sum(
                review.verification_status == LeadQaqcVerificationStatus.VERIFIED
                and (
                    not has_bundle_reviews
                    or review.lead_index in target_component_source_indexes
                )
                for review in component_reviews
            )
            bundle_keep_count = sum(
                review.verification_status == LeadQaqcVerificationStatus.VERIFIED
                and review.recommended_action == LeadQaqcRecommendedAction.KEEP
                and review.counts_toward_target_approved
                for review in component_bundle_reviews
            )
            allocated_keep_count = sum(
                review.verification_status == LeadQaqcVerificationStatus.VERIFIED
                and review.recommended_action == LeadQaqcRecommendedAction.KEEP
                and review.counts_toward_target_approved
                for review in allocated_component_reviews
            )
            partial_bundle_count = 0
            held_bundle_count = 0
            if child_manifest is not None and Path(child_manifest.lead_path).is_file():
                evidence_set_for_bundles = load_evidence_set(Path(child_manifest.lead_path))
                reviews_by_bundle = {
                    review.bundle_index: review for review in component_bundle_reviews
                }
                for bundle_index, bundle in enumerate(
                    evidence_set_for_bundles.component_bundles
                ):
                    source_leads = [
                        evidence_set_for_bundles.component_leads[index]
                        for index in bundle.source_lead_indexes
                        if 0 <= index < len(evidence_set_for_bundles.component_leads)
                    ]
                    readiness = bundle_readiness(
                        bundle=bundle,
                        review=reviews_by_bundle.get(bundle_index),
                        source_leads=source_leads,
                    )
                    if readiness == "partial_component_bundle":
                        partial_bundle_count += 1
                    elif readiness == "held_component_bundle":
                        held_bundle_count += 1
            else:
                held_bundle_count = len(component_bundle_reviews) - bundle_keep_count
            approved_component_bundle_count += bundle_keep_count
            allocated_component_verified_count += allocated_keep_count
            partial_component_bundle_count += partial_bundle_count
            held_component_bundle_count += held_bundle_count
            direct_verified_count += direct_keep_count
            verified_count += (
                direct_keep_count
                + (bundle_keep_count if has_bundle_reviews else component_keep_count)
                + allocated_keep_count
            )
            target_rejected_count += sum(
                review.recommended_action.value in {"reject", "retry"}
                or review.verification_status.value != "verified"
                for review in reviews
            ) + (
                sum(
                    review.recommended_action.value in {"reject", "retry"}
                    or review.verification_status.value != "verified"
                    or not review.counts_toward_target_approved
                    for review in target_bundle_reviews
                )
                if has_bundle_reviews
                else sum(
                    review.recommended_action.value in {"reject", "retry"}
                    or review.verification_status.value != "verified"
                    for review in component_reviews
                )
            ) + sum(
                review.recommended_action.value in {"reject", "retry"}
                or review.verification_status.value != "verified"
                or not review.counts_toward_target_approved
                for review in target_allocated_reviews
            )
            rejected_count += sum(
                review.recommended_action.value in {"reject", "retry"}
                or review.verification_status.value != "verified"
                for review in reviews
            ) + sum(
                review.recommended_action.value in {"reject", "retry"}
                or review.verification_status.value != "verified"
                for review in component_reviews
            ) + sum(
                review.recommended_action.value in {"reject", "retry"}
                or review.verification_status.value != "verified"
                or not review.counts_toward_target_approved
                for review in component_bundle_reviews
            ) + sum(
                review.recommended_action.value in {"reject", "retry"}
                or review.verification_status.value != "verified"
                or not review.counts_toward_target_approved
                for review in allocated_component_reviews
            )
            if child_manifest is not None:
                address_target_count += len(
                    approved_address_inputs(root=root, manifest=child_manifest)
                )
        child_address_path = address_output_path(root, child_run_id)
        if child_address_path.is_file():
            address_results = load_address_results(child_address_path)
            address_count += len(address_results)
            address_found_count += sum(
                result.status.value == "found" for result in address_results
            )

    if target_observation_count > 0 and target_review_count >= target_observation_count:
        qaqc_status = "complete"
    elif target_review_count > 0 or review_count > 0:
        qaqc_status = "attention"
    elif finished_jobs >= planned_jobs and target_observation_count > 0:
        qaqc_status = "ready"
    else:
        qaqc_status = "blocked"
    if target_observation_count == 0:
        target_observation_count = all_lead_count
    if target_review_count == 0 and review_count > 0 and not supporting_component_review_count:
        target_review_count = review_count
    target_needs_review_count = max(
        target_review_count - verified_count - target_rejected_count,
        0,
    )
    supporting_component_unreviewed_count = max(
        supporting_component_evidence_count - supporting_component_review_count,
        0,
    )
    facility_row_summary = (
        f"{observation_count}/{lead_quota} facility row(s) assembled"
        if lead_quota
        else f"{observation_count} facility row(s) assembled"
    )
    if source_backed_facility_row_count or allocated_facility_row_count:
        facility_row_summary += (
            f": {source_backed_facility_row_count} source-backed, "
            f"{allocated_facility_row_count} allocated"
        )
        if regional_support_row_count:
            facility_row_summary += f"; {regional_support_row_count} regional support row(s)"
        facility_row_summary += "."
        harvest_detail = (
            f"{successful_jobs}/{planned_jobs} jobs completed successfully; "
            f"{failed_jobs} failed or cancelled; {facility_row_summary}"
        )

    if address_target_count > 0 and address_count >= address_target_count:
        address_status = "complete"
    elif address_count > 0:
        address_status = "attention"
    elif qaqc_status == "complete" and address_target_count > 0:
        address_status = "ready"
    else:
        address_status = "blocked"

    geometry_items: tuple[dict[str, Any], ...] = ()
    if direct_verified_count > 0 or address_target_count > 0:
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
    exportable_count = (
        sum(1 for item in geometry_items if item.get("model_ready", True))
        if sample_set is not None
        else verified_count
    )
    if approved_count > 0 and geocoded_count + skipped_count >= approved_count:
        geometry_status = "complete"
    elif geocoded_count or skipped_count:
        geometry_status = "running"
    elif approved_count > 0 or address_target_count > 0:
        geometry_status = "ready"
    else:
        geometry_status = "blocked"

    reviewable_count = verified_count + partial_component_bundle_count
    if sample_set is not None:
        sample_status = "complete"
    elif reviewable_count > 0:
        sample_status = "ready"
    else:
        sample_status = "blocked"

    curation_state = "not_available"
    curation_detail = "Assemble a review dataset before approval."
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
    coverage_detail = "Approve the review dataset before checking coverage."
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
                recommended_count = len(coverage_review.recommended_child_jobs)
                if recommended_count:
                    coverage_detail = (
                        f"Coverage gaps found: {recommended_count} targeted follow-up "
                        "search(es) recommended."
                    )
                else:
                    coverage_detail = (
                        "Coverage sufficient. No targeted follow-ups recommended."
                    )
            else:
                coverage_status = "ready"
                coverage_detail = (
                    "Coverage check is stale; approve or recheck the current dataset."
                )
        except FileNotFoundError:
            coverage_status = "ready"
            coverage_detail = (
                f"{curation_included} included observation(s) are ready for coverage check."
            )

    gap_rounds = (
        tuple(round_item for round_item in sample_set.rounds if round_item.role.value == "gap_fill")
        if sample_set is not None
        else ()
    )
    latest_gap_round = gap_rounds[-1] if gap_rounds else None
    recommended_jobs = (
        len(coverage_review.recommended_child_jobs) if coverage_review is not None else 0
    )
    gap_summary: dict[str, object] = (
        latest_gap_round.summary
        if latest_gap_round is not None and latest_gap_round.summary is not None
        else {}
    )
    gap_failed = _summary_int(gap_summary, "failed_count")
    gap_planned = _summary_int(gap_summary, "planned_run_count", recommended_jobs)
    gap_completed = (
        _summary_int(gap_summary, "completed_count")
        if "completed_count" in gap_summary
        else len(latest_gap_round.child_run_ids)
        if latest_gap_round is not None
        else 0
    )
    gap_total = recommended_jobs or gap_planned
    failed_gap_children = (
        [
            {
                "run_id": str(child.get("run_id") or ""),
                "locality": child.get("locality"),
                "facility_type": child.get("facility_type"),
                "error_message": child.get("error_message"),
            }
            for child in _summary_dicts(gap_summary, "child_summaries")
            if str(child.get("status")) != HarvestRunStatus.COMPLETED
        ]
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
    if recommended_jobs == 0:
        gap_detail = "Not needed. No targeted follow-ups are currently recommended."
    elif gap_status == "attention":
        failed_labels = [
            str(child.get("locality") or child.get("run_id") or "unnamed follow-up")
            for child in failed_gap_children[:3]
        ]
        failed_text = f" Failed: {', '.join(failed_labels)}." if failed_labels else ""
        gap_detail = (
            f"{gap_completed}/{gap_total} targeted follow-up job(s) succeeded; "
            f"{gap_failed} need repair or retry.{failed_text}"
        )
    else:
        gap_detail = f"{gap_completed}/{gap_total} targeted follow-up job(s) complete."

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
            display_mode="progress",
        ),
        _workflow_stage(
            stage_id="harvest",
            label="Harvest Observations",
            status=harvest_status,
            current=observation_count,
            total=lead_quota,
            detail=harvest_detail,
            metrics={
                "planned_jobs": planned_jobs,
                "finished_jobs": finished_jobs,
                "successful_jobs": successful_jobs,
                "failed_jobs": failed_jobs,
                "lead_count": observation_count,
                "lead_quota": lead_quota,
                "source_backed_facility_row_count": source_backed_facility_row_count,
                "allocated_facility_row_count": allocated_facility_row_count,
                "regional_support_row_count": regional_support_row_count,
                "dataset_row_gap_count": dataset_row_gap_count,
                "dataset_row_gap_run_ids": dataset_row_gap_run_ids,
            },
            alert_message=harvest_alert_message,
            indeterminate=harvest_running,
            display_mode="progress",
        ),
        _workflow_stage(
            stage_id="qaqc",
            label="Verify Evidence",
            status=qaqc_status,
            current=target_review_count,
            total=target_observation_count,
            detail=(
                f"{target_review_count}/{target_observation_count} target observation(s) "
                f"reviewed; {verified_count} approved; "
                f"{target_needs_review_count} need human review; "
                f"{target_rejected_count} not approved."
                + (
                    f" {allocated_component_review_count} allocated row(s) checked; "
                    f"{allocated_component_verified_count} approved."
                    if allocated_component_review_count
                    else ""
                )
                + (
                    f" {supporting_component_review_count} supporting component evidence "
                    f"record(s) checked; {supporting_component_verified_count} verified."
                    + (
                        f" {supporting_component_unreviewed_count} "
                        "supporting record(s) were not individually reviewed."
                        if supporting_component_unreviewed_count
                        else ""
                    )
                    if supporting_component_review_count
                    else ""
                )
            ),
            metrics={
                "target_observation_count": target_observation_count,
                "target_review_count": target_review_count,
                "target_approved_count": verified_count,
                "target_needs_review_count": target_needs_review_count,
                "target_not_approved_count": target_rejected_count,
                "raw_evidence_record_count": all_lead_count,
                "raw_review_count": review_count,
                "supporting_component_evidence_count": supporting_component_evidence_count,
                "supporting_component_review_count": supporting_component_review_count,
                "supporting_component_verified_count": supporting_component_verified_count,
                "supporting_component_unreviewed_count": supporting_component_unreviewed_count,
                "allocated_component_evidence_count": allocated_component_evidence_count,
                "allocated_component_review_count": allocated_component_review_count,
                "allocated_component_verified_count": allocated_component_verified_count,
                "verified_count": verified_count,
                "direct_verified_count": direct_verified_count,
                "rejected_count": rejected_count,
            },
            action_id="run_qaqc" if qaqc_status in {"ready", "attention"} else None,
            action_label="Run QAQC" if qaqc_status in {"ready", "attention"} else None,
            indeterminate=qaqc_status == "running",
            display_mode="progress",
        ),
        _workflow_stage(
            stage_id="address",
            label="Enrich Addresses",
            status=address_status,
            current=address_count,
            total=address_target_count,
            detail=(
                (
                    f"{address_count}/{address_target_count} addressable target(s) "
                    f"processed; {address_found_count} addresses found. "
                    f"{approved_component_bundle_count} model-ready bundle(s), "
                    f"{allocated_component_verified_count} allocated component row(s), "
                    f"{partial_component_bundle_count} partial candidate bundle(s), and "
                    f"{held_component_bundle_count} held bundle(s)."
                )
                if address_target_count
                else (
                    (
                        "No addressable targets are approved yet. "
                        f"{allocated_component_review_count} allocated component row(s) "
                        "were reviewed, but QAQC did not approve them for target counting; "
                        "check allocation denominator and math notes."
                    )
                    if allocated_component_review_count
                    and not allocated_component_verified_count
                    else (
                        "No addressable targets exist. "
                        f"{held_component_bundle_count} component bundle(s) are held because "
                        "they lack a specific facility identity or useful source-backed "
                        "population component."
                    )
                )
            ),
            metrics={
                "target_count": address_target_count,
                "found_count": address_found_count,
                "approved_component_bundle_count": approved_component_bundle_count,
                "allocated_component_verified_count": allocated_component_verified_count,
                "partial_component_bundle_count": partial_component_bundle_count,
                "held_component_bundle_count": held_component_bundle_count,
            },
            action_id="run_address" if address_status in {"ready", "attention"} else None,
            action_label=(
                "Run Address Enrichment"
                if address_status in {"ready", "attention"}
                else None
            ),
            indeterminate=address_status == "running",
            display_mode="progress",
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
            display_mode="progress",
        ),
        _workflow_stage(
            stage_id="sample",
            label="Assemble Review Dataset",
            status=sample_status,
            current=1 if sample_set is not None else 0,
            total=1,
            detail=(
                f"Review dataset {sample_set.sample_set_id} contains "
                f"{len(sample_set.combined_child_run_ids)} child run(s)."
                if sample_set is not None
                else (
                    f"Assemble {reviewable_count} reviewable observation candidate(s), "
                    f"including {partial_component_bundle_count} partial bundle candidate(s)."
                    if reviewable_count
                    else "Assemble verified observations into a reviewable dataset."
                )
            ),
            action_id="create_sample" if sample_status == "ready" else None,
            action_label="Assemble Review Dataset" if sample_status == "ready" else None,
            display_mode="gate",
        ),
        _workflow_stage(
            stage_id="curation",
            label="Approve / Exclude Observations",
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
                "Review & Approve Dataset"
                if sample_set is not None and curation_state != "approved"
                else None
            ),
            display_mode="gate",
        ),
        _workflow_stage(
            stage_id="coverage",
            label="Check Coverage",
            status=coverage_status,
            current=1 if coverage_status == "complete" else 0,
            total=1,
            detail=coverage_detail,
            action_id="analyze_coverage" if coverage_status == "ready" else None,
            action_label="Check Coverage" if coverage_status == "ready" else None,
            indeterminate=coverage_status == "running",
            display_mode="progress" if coverage_status == "running" else "gate",
        ),
        _workflow_stage(
            stage_id="gap_fill",
            label="Run Targeted Follow-ups",
            status=gap_status,
            current=gap_completed,
            total=gap_total,
            detail=gap_detail,
            metrics={
                "planned_count": gap_total,
                "completed_count": gap_completed,
                "failed_count": gap_failed,
                "failed_child_runs": failed_gap_children,
            },
            action_id="run_gap_fill" if gap_status in {"ready", "attention"} else None,
            action_label=(
                (
                    "Retry Targeted Follow-ups"
                    if gap_status == "attention"
                    else "Run Targeted Follow-ups"
                )
                if gap_status in {"ready", "attention"}
                else None
            ),
            indeterminate=gap_status == "running",
            display_mode="job_progress" if recommended_jobs or gap_status == "running" else "gate",
        ),
        _workflow_stage(
            stage_id="export",
            label="Export Dataset",
            status="ready" if exportable_count > 0 else "blocked",
            current=exportable_count,
            total=exportable_count,
            detail=(
                f"{exportable_count} strict model-ready observation(s) are available for export."
            ),
            action_id=(
                "export_json" if exportable_count > 0 else None
            ),
            action_label=(
                "Download Verified JSON"
                if (approved_count if sample_set else verified_count) > 0
                else None
            ),
            display_mode="progress",
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
        items = tuple(
            item
            for item in sample_records(root, sample_set)
            if item.get("model_ready", True)
        )
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


def _admin_scoped_export_payload(
    records: Sequence[dict[str, Any]],
    *,
    identity: str,
    output_format: str,
) -> tuple[str, str, str]:
    if output_format == "json":
        return admin_scoped_json(records), "application/json", f"{identity}.admin_scoped.json"
    if output_format == "csv":
        return admin_scoped_csv(records), "text/csv", f"{identity}.admin_scoped.csv"
    raise ValueError(f"unsupported admin-scoped export format: {output_format}")


def _sample_admin_scoped_export_response(
    root: Path,
    sample_set_id: str,
    *,
    output_format: str,
) -> Response:
    try:
        sample_set = refresh_sample_set(root, load_sample_set(root, sample_set_id))
        records = tuple(sample_records(root, sample_set))
        payload, media_type, filename = _admin_scoped_export_payload(
            records,
            identity=sample_set_id,
            output_format=output_format,
        )
    except FileNotFoundError as exc:
        return _json_error(str(exc), status_code=409)
    except ValueError as exc:
        return _json_error(str(exc), status_code=404)

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
            child_records = (
                _approved_component_records_for_child(root, manifest)
                + _approved_allocated_component_records_for_child(root, manifest)
            )
            for record in child_records:
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


def _admin_scoped_export_response(root: Path, run_id: str, *, output_format: str) -> Response:
    try:
        manifest = _load_any_manifest(root, run_id)
        records = merge_address_results(root, _approved_records_for_manifest(root, manifest))
        items = tuple(merge_geometry_items(root, records))
        payload, media_type, filename = _admin_scoped_export_payload(
            items,
            identity=run_id,
            output_format=output_format,
        )
    except FileNotFoundError as exc:
        return _json_error(str(exc), status_code=409)
    except ValueError as exc:
        return _json_error(str(exc), status_code=404)

    return PlainTextResponse(
        payload,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _component_export_response(root: Path, run_id: str, *, output_format: str) -> Response:
    try:
        manifest = _load_any_manifest(root, run_id)
        records = _approved_component_export_records_for_manifest(root, manifest)
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
        occupancy_data = lead["occupancy_data"]
        count_values = _count_values_from_occupancy_data(occupancy_data)
        count_notes = (
            review.get("review_notes")
            or address.get("review_notes")
            or lead.get("review_notes")
            or ""
        )
        row = {
            "row_id": record["item_id"],
            "item_id": record["item_id"],
            "run_id": record["child_run_id"],
            "sample_set_id": record.get("sample_set_id", ""),
            "sample_round": record.get("sample_round", ""),
            "facility_type": record.get("facility_type", ""),
            "evidence_role": "direct_occupancy",
            "lead_index": record["lead_index"],
            "count_index": "",
            "facility_name": location["facility_name"],
            "count": sum(
                datum["count"]
                for datum in occupancy_data
                if isinstance(datum.get("count"), int | float)
            ),
            "group_type": ", ".join(str(datum["group_type"]) for datum in occupancy_data),
            "count_values": count_values,
            "count_relationship": _count_relationship(occupancy_data, count_notes),
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
            "review_notes": count_notes,
            "excluded_from_dataset": False,
            "exclusion_reason_code": "",
            "exclusion_reason_note": "",
        }
        row.update(count_values)
        rows.append(row)
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
            occupancy_data = lead_payload["occupancy_data"]
            count_values = _count_values_from_occupancy_data(occupancy_data)
            review_notes = lead_payload.get("review_notes") or ""
            row = {
                "row_id": item_id,
                "item_id": item_id,
                "run_id": child_run_id,
                "sample_set_id": "",
                "sample_round": "",
                "facility_type": child_manifest.profile_set,
                "evidence_role": "direct_occupancy",
                "lead_index": lead_index,
                "count_index": "",
                "facility_name": location["facility_name"],
                "count": sum(
                    datum["count"]
                    for datum in occupancy_data
                    if isinstance(datum.get("count"), int | float)
                ),
                "group_type": ", ".join(str(datum["group_type"]) for datum in occupancy_data),
                "count_values": count_values,
                "count_relationship": _count_relationship(occupancy_data, review_notes),
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
                "review_notes": review_notes,
                "excluded_from_dataset": False,
                "exclusion_reason_code": "",
                "exclusion_reason_note": "",
            }
            row.update(count_values)
            rows.append(row)
        rows.extend(
            _component_bundle_table_rows(
                root=root,
                child_run_id=child_run_id,
                child_manifest=child_manifest,
                evidence_set=evidence_set,
            )
        )
        allocated_reviews: dict[int, dict[str, Any]] = {}
        qaqc_path = _qaqc_output_path(root, child_run_id)
        if qaqc_path.is_file():
            review_set = load_qaqc_review_set(qaqc_path)
            allocated_reviews = {
                review.lead_index: review.model_dump(mode="json")
                for review in review_set.allocated_component_reviews
            }
        address_by_item_id: dict[str, dict[str, Any]] = {}
        address_path = address_output_path(root, child_run_id)
        if address_path.is_file():
            address_by_item_id = {
                result.item_id: result.model_dump(mode="json")
                for result in load_address_results(address_path)
            }
        for lead_index, allocated_lead in enumerate(evidence_set.allocated_component_leads):
            item_id = f"{child_run_id}-allocated-component-{lead_index}"
            rows.append(
                _allocated_component_table_row(
                    child_run_id=child_run_id,
                    child_manifest=child_manifest,
                    lead_index=lead_index,
                    lead_payload=allocated_lead.model_dump(mode="json"),
                    review_payload=allocated_reviews.get(lead_index),
                    address_payload=address_by_item_id.get(item_id),
                )
            )
    return rows


def _table_rows_from_component_records(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    for record in records:
        lead = record["component_lead"]
        facility_name, city_or_region, country, _ = _component_location_values(lead)
        key = (
            str(record["child_run_id"]),
            str(record.get("sample_set_id", "")),
            facility_name,
            city_or_region,
            country,
        )
        grouped.setdefault(key, []).append(record)

    for group_index, group_records in enumerate(grouped.values()):
        first = group_records[0]
        first_lead = first["component_lead"]
        facility_name, city_or_region, country, reported_address = _component_location_values(
            first_lead
        )
        component_values: dict[str, str] = {}
        source_urls: list[str] = []
        lead_indexes: list[str] = []
        review_statuses: set[str] = set()
        recommended_actions: set[str] = set()
        strategies: set[str] = set()
        confidences: set[str] = set()
        review_notes: list[str] = []
        selected_address: dict[str, Any] | None = None
        selected_address_status = "not_run"

        for record in group_records:
            lead = record["component_lead"]
            review = record.get("component_qaqc_review") or {}
            lead_indexes.append(str(record["lead_index"]))
            source_url = str(lead.get("source_url") or "")
            if source_url and source_url not in source_urls:
                source_urls.append(source_url)
            if review.get("verification_status"):
                review_statuses.add(str(review["verification_status"]))
            if review.get("recommended_action"):
                recommended_actions.add(str(review["recommended_action"]))
            if lead.get("strategy_id"):
                strategies.add(str(lead["strategy_id"]))
            if lead.get("confidence"):
                confidences.add(str(lead["confidence"]))
            note = review.get("review_notes") or lead.get("review_notes")
            if note:
                review_notes.append(str(note))
            for datum in lead["component_data"]:
                _append_component_value(component_values, datum)
            address = record.get("address_enrichment")
            address_status = str(record.get("address_status") or "not_run")
            if isinstance(address, dict) and address.get("formatted_address"):
                selected_address = address
                selected_address_status = address_status
            elif selected_address is None and address_status != "not_run":
                selected_address = address if isinstance(address, dict) else None
                selected_address_status = address_status

        item_id = str(first["item_id"])
        rows.append(
            {
                "row_id": f"{item_id}-components-{group_index}",
                "item_id": item_id,
                "run_id": first["child_run_id"],
                "sample_set_id": first.get("sample_set_id", ""),
                "sample_round": first.get("sample_round", ""),
                "facility_type": first.get("facility_type", ""),
                "evidence_role": "component_input",
                "lead_index": ",".join(lead_indexes),
                "count_index": "",
                "facility_name": facility_name,
                "count": "",
                "group_type": "",
                "component_type": ", ".join(component_values),
                "component_values": component_values,
                "value": "; ".join(f"{key}: {value}" for key, value in component_values.items()),
                "unit": "",
                "time_basis": "",
                "geography_level": "",
                "incident_date": "",
                "incident_time": "",
                "strategy_id": ", ".join(sorted(strategies)),
                "representativeness": "component_input",
                "confidence": ", ".join(sorted(confidences)),
                "city_or_region": city_or_region,
                "country": country,
                "source_url": source_urls[0] if source_urls else "",
                "source_urls": "; ".join(source_urls),
                "source_count": len(source_urls),
                "qaqc_status": ", ".join(sorted(review_statuses)),
                "recommended_action": ", ".join(sorted(recommended_actions)),
                "address_status": selected_address_status,
                "enriched_address": (
                    selected_address.get("formatted_address")
                    if isinstance(selected_address, dict)
                    else reported_address
                )
                or reported_address,
                "geometry_status": "not_applicable",
                "area_m2": "",
                "review_notes": "; ".join(review_notes),
                "component_bundle_status": "",
                "counts_toward_target": False,
                "missing_component_types": "",
                "excluded_from_dataset": False,
                "exclusion_reason_code": "",
                "exclusion_reason_note": "",
            }
        )
    return rows


def _table_rows_from_component_bundle_records(
    records: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        bundle = record["component_bundle"]
        review = record.get("component_bundle_qaqc_review") or {}
        component_values: dict[str, str] = {}
        source_urls: list[str] = []
        strategies: set[str] = set()
        confidences: set[str] = set()
        for lead in record.get("component_leads", ()):
            source_url = str(lead.get("source_url") or "")
            if source_url and source_url not in source_urls:
                source_urls.append(source_url)
            if lead.get("strategy_id"):
                strategies.add(str(lead["strategy_id"]))
            if lead.get("confidence"):
                confidences.add(str(lead["confidence"]))
            for datum in lead.get("component_data", ()):
                _append_component_value(component_values, datum)
        location = bundle.get("location") or {}
        fallback_lead = (record.get("component_leads") or [{}])[0]
        fallback_facility, fallback_city, fallback_country, fallback_address = (
            _component_location_values(fallback_lead)
        )
        facility_name = str(
            location.get("facility_name") or bundle.get("geography_name") or fallback_facility
        )
        city_or_region = str(location.get("city_or_region") or fallback_city)
        country = str(location.get("country") or bundle.get("country") or fallback_country)
        reported_address = str(location.get("specific_address_or_landmark") or fallback_address)
        address = record.get("address_enrichment")
        address_status = str(record.get("address_status") or "not_run")
        rows.append(
            {
                "row_id": record["item_id"],
                "item_id": record["item_id"],
                "run_id": record["child_run_id"],
                "sample_set_id": record.get("sample_set_id", ""),
                "sample_round": record.get("sample_round", ""),
                "facility_type": record.get("facility_type", ""),
                "evidence_role": "component_bundle",
                "lead_index": ",".join(
                    str(index) for index in bundle.get("source_lead_indexes", ())
                ),
                "count_index": "",
                "facility_name": facility_name,
                "count": "",
                "group_type": "",
                "component_type": ", ".join(component_values),
                "component_values": component_values,
                "value": "; ".join(f"{key}: {value}" for key, value in component_values.items()),
                "unit": "",
                "time_basis": "",
                "geography_level": "",
                "incident_date": "",
                "incident_time": "",
                "strategy_id": ", ".join(sorted(strategies)),
                "representativeness": "component_input",
                "confidence": bundle.get("confidence") or ", ".join(sorted(confidences)),
                "city_or_region": city_or_region,
                "country": country,
                "source_url": source_urls[0] if source_urls else "",
                "source_urls": "; ".join(source_urls),
                "source_count": len(source_urls),
                "qaqc_status": review.get("verification_status", ""),
                "recommended_action": review.get("recommended_action", ""),
                "bundle_qaqc_status": review.get("verification_status", ""),
                "address_status": address_status,
                "enriched_address": (
                    address.get("formatted_address")
                    if isinstance(address, dict)
                    else reported_address
                )
                or reported_address,
                "geometry_status": record.get("geometry_status", "not_applicable"),
                "area_m2": record.get("area_m2") or "",
                "review_notes": review.get("review_notes") or bundle.get("completion_notes") or "",
                "component_bundle_status": bundle.get("completion_status", ""),
                "bundle_readiness": record.get("bundle_readiness", ""),
                "model_ready": record.get("model_ready", False),
                "bundle_review_required": record.get("bundle_review_required", True),
                "counts_toward_target": bundle.get("counts_toward_target", False),
                "missing_component_types": ", ".join(
                    bundle.get("missing_component_types", ())
                ),
                "excluded_from_dataset": False,
                "exclusion_reason_code": "",
                "exclusion_reason_note": "",
            }
        )
    return rows


def _table_rows_from_allocated_component_records(
    root: Path,
    records: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        manifest = _load_run_manifest(root, str(record["child_run_id"]))
        address = record.get("address_enrichment")
        rows.append(
            _allocated_component_table_row(
                child_run_id=str(record["child_run_id"]),
                child_manifest=manifest,
                lead_index=int(record["lead_index"]),
                lead_payload=record["allocated_component_lead"],
                review_payload=record.get("allocated_component_qaqc_review"),
                address_payload=address if isinstance(address, dict) else None,
                sample_set_id=str(record.get("sample_set_id", "")),
                sample_round=record.get("sample_round", ""),
            )
        )
        rows[-1]["geometry_status"] = record.get("geometry_status", "not_applicable")
        rows[-1]["area_m2"] = record.get("area_m2") or ""
    return rows


def _run_table_payload(root: Path, run_id: str, *, mode: str) -> dict[str, Any]:
    manifest = _load_any_manifest(root, run_id)
    if mode == "all":
        rows = _all_lead_table_rows(root, manifest)
    elif mode == "verified":
        records = merge_address_results(root, _approved_records_for_manifest(root, manifest))
        rows = _table_rows_from_records(tuple(merge_geometry_items(root, records)))
        bundle_records = merge_address_results(
            root, _approved_component_bundle_records_for_manifest(root, manifest)
        )
        if bundle_records:
            rows.extend(_table_rows_from_component_bundle_records(bundle_records))
        elif not _manifest_has_component_bundles(root, manifest):
            component_records = merge_address_results(
                root, _approved_component_records_for_manifest(root, manifest)
            )
            rows.extend(_table_rows_from_component_records(component_records))
        allocated_records = merge_address_results(
            root, _approved_allocated_component_records_for_manifest(root, manifest)
        )
        rows.extend(_table_rows_from_allocated_component_records(root, allocated_records))
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
    curation = load_curation(root, sample_set_id)
    decisions = {decision.item_id: decision for decision in curation.decisions}
    direct_records = tuple(record for record in records if "lead" in record)
    component_bundle_records = tuple(
        record for record in records if "component_bundle" in record
    )
    allocated_component_records = tuple(
        record for record in records if "allocated_component_lead" in record
    )
    rows = _table_rows_from_records(direct_records)
    rows.extend(_table_rows_from_component_bundle_records(component_bundle_records))
    rows.extend(
        _table_rows_from_allocated_component_records(root, allocated_component_records)
    )
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
    geocoder: SpatialGeocoder | None = None,
    background: bool = True,
    shutdown_callback: Callable[[], None] | None = None,
) -> Starlette:
    root = workspace.resolve()
    root.mkdir(parents=True, exist_ok=True)
    registry = ActiveCodexRegistry(root)
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="harvest")
    app_runner = runner or registry.runner
    app_geocoder = geocoder or NominatimGeocoder(root)

    def _create_and_submit_background_job(
        *,
        identity: str,
        job_id: str,
        job_factory: Callable[[], JobRecord],
        log: Callable[[str], None],
        task: Callable[[], Any],
        manifest_path: Callable[[Any], str | None] | None = None,
        summary: Callable[[Any], dict[str, object] | None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> JobRecord | None:
        if not registry.try_mark_task_active(identity, job_id=job_id):
            return None
        try:
            job = job_factory()
            executor.submit(
                run_background_job,
                root=root,
                registry=registry,
                identity=identity,
                job_id=job.job_id,
                log=log,
                task=task,
                manifest_path=manifest_path,
                summary=summary,
                on_error=on_error,
            )
            return job
        except Exception:
            registry.mark_task_inactive(identity)
            raise

    def _active_work_error(label: str, identity: str) -> JSONResponse:
        return _json_error(f"{label} already has active work: {identity}", status_code=409)

    def _shutdown_background() -> None:
        registry.cancel_all()
        executor.shutdown(wait=False, cancel_futures=True)

    @asynccontextmanager
    async def _lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            _shutdown_background()

    async def index(request: Request) -> HTMLResponse:
        return HTMLResponse(INDEX_HTML)

    async def oasis_logo(request: Request) -> FileResponse:
        return FileResponse(
            Path(__file__).with_name("static") / "oasis-logo.jpg",
            media_type="image/jpeg",
        )

    async def app_css(request: Request) -> FileResponse:
        return FileResponse(
            Path(__file__).with_name("static") / "app.css",
            media_type="text/css",
        )

    async def app_js(request: Request) -> FileResponse:
        return FileResponse(
            Path(__file__).with_name("static") / "app.js",
            media_type="text/javascript",
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
                job = _create_and_submit_background_job(
                    identity=run_id,
                    job_id=run_id,
                    job_factory=lambda: create_job(
                        root,
                        job_id=run_id,
                        job_type=JobType.HARVEST,
                        manifest_path=str(_run_manifest_path(root, run_id)),
                        log_path=str(log_path_for_run(root, run_id)),
                    ),
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
                if job is None:
                    return _active_work_error("Run", run_id)
                append_harvest_log(root, run_id, "Manifest prepared as queued background job.")
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
                job = _create_and_submit_background_job(
                    identity=batch_id,
                    job_id=batch_id,
                    job_factory=lambda: create_job(
                        root,
                        job_id=batch_id,
                        job_type=JobType.BATCH,
                        manifest_path=str(_batch_manifest_path(root, batch_id)),
                        log_path=str(log_path_for_run(root, batch_id)),
                    ),
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
                if job is None:
                    return _active_work_error("Run", batch_id)
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
                job = _create_and_submit_background_job(
                    identity=campaign_id,
                    job_id=campaign_id,
                    job_factory=lambda: create_job(
                        root,
                        job_id=campaign_id,
                        job_type=JobType.CAMPAIGN,
                        manifest_path=str(_campaign_manifest_path(root, campaign_id)),
                        log_path=str(log_path_for_run(root, campaign_id)),
                    ),
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
                if job is None:
                    return _active_work_error("Run", campaign_id)
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
            job_id = _qaqc_id_for_run(identity)
            job = _create_and_submit_background_job(
                identity=identity,
                job_id=job_id,
                job_factory=lambda: create_job(
                    root,
                    job_id=job_id,
                    job_type=JobType.QAQC,
                    parent_id=identity,
                    log_path=str(log_path_for_run(root, job_id)),
                    active_child_ids=_manifest_child_run_ids(manifest),
                ),
                log=lambda message: append_harvest_log(root, identity, f"QAQC failed: {message}."),
                task=task,
                summary=lambda result: (
                    result.get("summary", {}) if isinstance(result, dict) else {}
                ),
            )
            if job is None:
                return _active_work_error("Run", identity)
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
            job_id = _address_id_for_run(identity)
            job = _create_and_submit_background_job(
                identity=identity,
                job_id=job_id,
                job_factory=lambda: create_job(
                    root,
                    job_id=job_id,
                    job_type=JobType.ADDRESS,
                    parent_id=identity,
                    log_path=str(log_path_for_run(root, job_id)),
                    active_child_ids=_manifest_child_run_ids(manifest),
                ),
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
            if job is None:
                return _active_work_error("Run", identity)
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
            child_run_ids = _manifest_child_run_ids(manifest)
            payload = address_results_payload(root, child_run_ids)
            payload["reconciliation"] = _address_reconciliation_payload(root, child_run_ids)
            return JSONResponse(payload)
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
                    "coverage and targeted follow-up guidance."
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
            job_id = f"{sample_set_id}-coverage"
            job = _create_and_submit_background_job(
                identity=sample_set_id,
                job_id=job_id,
                job_factory=lambda: create_job(
                    root,
                    job_id=job_id,
                    job_type=JobType.COVERAGE,
                    parent_id=sample_set_id,
                    log_path=str(log_path_for_run(root, sample_set_id)),
                ),
                log=lambda message: append_harvest_log(
                    root,
                    sample_set_id,
                    f"Coverage check failed: {message}.",
                ),
                task=task,
                summary=lambda result: (
                    result.get("summary", {}) if isinstance(result, dict) else {}
                ),
            )
            if job is None:
                return _active_work_error("Sample set", sample_set_id)
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
                    "coverage check is stale for the current human curation approval; "
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
            job_id = f"{sample_set_id}-gap-fill"
            job = _create_and_submit_background_job(
                identity=sample_set_id,
                job_id=job_id,
                job_factory=lambda: create_job(
                    root,
                    job_id=job_id,
                    job_type=JobType.GAP_FILL,
                    parent_id=sample_set_id,
                    log_path=str(log_path_for_run(root, sample_set_id)),
                ),
                log=lambda message: append_harvest_log(
                    root,
                    sample_set_id,
                    f"Targeted follow-ups failed: {message}.",
                ),
                task=task,
                manifest_path=lambda result: str(
                    root / "sample_sets" / f"{result.sample_set_id}.json"
                ),
                summary=lambda result: result.stage_summary or {},
            )
            if job is None:
                return _active_work_error("Sample set", sample_set_id)
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
            job_id = f"{sample_set_id}-qaqc-missing"
            job = _create_and_submit_background_job(
                identity=sample_set_id,
                job_id=job_id,
                job_factory=lambda: create_job(
                    root,
                    job_id=job_id,
                    job_type=JobType.SAMPLE_QAQC_MISSING,
                    parent_id=sample_set_id,
                    log_path=str(log_path_for_run(root, sample_set_id)),
                ),
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
            if job is None:
                return _active_work_error("Sample set", sample_set_id)
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
            job_id = f"{sample_set_id}-address-missing"
            job = _create_and_submit_background_job(
                identity=sample_set_id,
                job_id=job_id,
                job_factory=lambda: create_job(
                    root,
                    job_id=job_id,
                    job_type=JobType.SAMPLE_ADDRESS_MISSING,
                    parent_id=sample_set_id,
                    log_path=str(log_path_for_run(root, sample_set_id)),
                ),
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
            if job is None:
                return _active_work_error("Sample set", sample_set_id)
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

    async def sample_export_admin_scoped_json(request: Request) -> Response:
        return _sample_admin_scoped_export_response(
            root,
            request.path_params["sample_set_id"],
            output_format="json",
        )

    async def sample_export_admin_scoped_csv(request: Request) -> Response:
        return _sample_admin_scoped_export_response(
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
                and _should_retry_address_after_geocode(spatial_validation, geometry_record)
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
            child_run_id = data.item_id.split("-component-bundle-", 1)[0]
            if child_run_id == data.item_id:
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
            try:
                reverse_result = await run_in_threadpool(
                    partial(app_geocoder.reverse, latitude, longitude)
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

    async def export_admin_scoped_json(request: Request) -> Response:
        return _admin_scoped_export_response(
            root,
            request.path_params["run_id"],
            output_format="json",
        )

    async def export_admin_scoped_csv(request: Request) -> Response:
        return _admin_scoped_export_response(
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
        Route("/assets/app.css", app_css),
        Route("/assets/app.js", app_js),
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
        Route("/api/runs/{run_id}/export.admin_scoped.json", export_admin_scoped_json),
        Route("/api/runs/{run_id}/export.admin_scoped.csv", export_admin_scoped_csv),
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
        Route(
            "/api/samples/{sample_set_id}/export.admin_scoped.json",
            sample_export_admin_scoped_json,
        ),
        Route(
            "/api/samples/{sample_set_id}/export.admin_scoped.csv",
            sample_export_admin_scoped_csv,
        ),
        Route("/api/samples/{sample_set_id}/export.components.json", sample_export_components_json),
        Route("/api/samples/{sample_set_id}/export.components.csv", sample_export_components_csv),
        Route(
            "/api/samples/{sample_set_id}/export.footprints.geojson",
            sample_export_footprints_geojson,
        ),
        Route("/api/app/exit", exit_app, methods=["POST"]),
    ]
    return Starlette(routes=routes, lifespan=_lifespan)


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

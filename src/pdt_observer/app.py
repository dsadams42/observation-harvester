from __future__ import annotations

import asyncio
import os
import re
import subprocess
import threading
import time
import webbrowser
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError
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
from pdt_observer.leads import (
    export_leads,
    load_leads,
    load_qaqc_reviews,
    promote_lead_to_run,
    render_lead_qaqc_prompt,
)
from pdt_observer.models import (
    AddressEnrichmentStatus,
    GeometryPoint,
    GeometryStatus,
    HarvestBatchRunManifest,
    HarvestCampaignRunManifest,
    HarvestRunManifest,
    HarvestRunStatus,
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
from pdt_observer.workflow import write_model


class HarvestRunRequest(BaseModel):
    country: str = Field(min_length=2)
    locality: str | None = None
    profiles: str = "schools"
    profile: str | None = None
    target: int = Field(default=20, ge=1)
    run_id: str | None = None
    geographer_plan_path: str | None = None


class HarvestBatchRunRequest(BaseModel):
    country: str = Field(min_length=2)
    locality: str | None = None
    profiles: str = "schools"
    target: int = Field(default=20, ge=1)
    batch_id: str | None = None
    geographer_plan_path: str | None = None


class GeographerPlanRequest(BaseModel):
    country: str = Field(min_length=2)
    locality: str | None = None
    profiles: str = "schools"
    profile: str | None = None
    localities: tuple[str, ...] = ()
    facility_types: tuple[str, ...] = ()
    mode: str = Field(default="single", pattern=r"^(single|batch|campaign)$")


class HarvestCampaignRunRequest(BaseModel):
    country: str = Field(min_length=2)
    localities: tuple[str, ...] = ()
    facility_types: tuple[str, ...] = Field(min_length=1)
    target: int = Field(default=20, ge=1)
    campaign_id: str | None = None
    geographer_plan_path: str | None = None


class PromoteLeadRequest(BaseModel):
    index: int = Field(ge=0)
    task_id: str | None = None


class GeometryGeocodeRequest(BaseModel):
    item_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    allow_address_retry: bool = False
    conversation_id: str | None = None


class GeometryResearchRequest(BaseModel):
    item_id: str = Field(min_length=1)
    conversation_id: str | None = None


class GeometryCoordinatePreviewRequest(BaseModel):
    item_id: str = Field(min_length=1)
    coordinate_text: str = Field(min_length=1)


class GeometryGeocodeAllRequest(BaseModel):
    items: tuple[GeometryGeocodeRequest, ...] = Field(min_length=1)


class GeometrySaveRequest(BaseModel):
    item_id: str = Field(min_length=1)
    geocode_query: str = Field(min_length=1)
    point: GeometryPoint | None = None
    polygon_geojson: dict[str, Any] | None = None
    geometry_status: GeometryStatus = GeometryStatus.NEEDS_REVIEW
    geocode_result: dict[str, Any] | None = None
    spatial_validation: dict[str, Any] | None = None
    review_notes: str | None = None
    conversation_id: str | None = None


class SampleSetCreateRequest(BaseModel):
    run_id: str = Field(min_length=1)
    sample_set_id: str | None = None


class SampleSetGapFillRequest(BaseModel):
    coverage_id: str | None = None


class ActiveCodexRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._tasks: set[str] = set()

    def _run_id_from_command(self, command: Sequence[str]) -> str:
        try:
            output_path = Path(command[command.index("-o") + 1])
            return output_path.stem
        except (ValueError, IndexError):
            return "unknown"

    def runner(
        self,
        command: Sequence[str],
        prompt: str,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        run_id = self._run_id_from_command(command)
        append_harvest_log(self.root, run_id, "Codex subprocess starting.")
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
        )
        with self._lock:
            self._processes[run_id] = process
        try:
            stdout, stderr = process.communicate(input=prompt)
        finally:
            with self._lock:
                if self._processes.get(run_id) is process:
                    del self._processes[run_id]
        if process.returncode is not None and process.returncode < 0:
            stderr = f"{stderr.strip()}\nHarvest cancelled by user.".strip()
        append_harvest_log(self.root, run_id, "Codex subprocess finished.")
        return subprocess.CompletedProcess(command, process.returncode or 0, stdout, stderr)

    def is_active(self, run_id: str) -> bool:
        with self._lock:
            if any(
                active_id == run_id or active_id.startswith(f"{run_id}-")
                for active_id in self._tasks
            ):
                return True
            return any(
                active_id == run_id or active_id.startswith(f"{run_id}-")
                for active_id in self._processes
            )

    def active_count(self) -> int:
        with self._lock:
            return len(self._processes) + len(self._tasks)

    def mark_task_active(self, run_id: str) -> None:
        with self._lock:
            self._tasks.add(run_id)

    def mark_task_inactive(self, run_id: str) -> None:
        with self._lock:
            self._tasks.discard(run_id)

    def cancel(self, run_id: str) -> int:
        with self._lock:
            parent_task_active = run_id in self._tasks
            matches = [
                (active_id, process)
                for active_id, process in self._processes.items()
                if parent_task_active
                or active_id == run_id
                or active_id.startswith(f"{run_id}-")
            ]
        for active_id, process in matches:
            append_harvest_log(self.root, active_id, "Cancellation requested.")
            process.terminate()
        return len(matches)

    def cancel_all(self) -> int:
        with self._lock:
            matches = list(self._processes.items())
        for active_id, process in matches:
            append_harvest_log(self.root, active_id, "Application exit requested.")
            process.terminate()
        return len(matches)


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
    leads = load_leads(Path(manifest.lead_path))
    prompt = render_lead_qaqc_prompt(
        leads,
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
        reviews = load_qaqc_reviews(output_path)
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
        f"QAQC validation completed for {run_id}: {len(reviews)} reviews.",
    )
    return {
        "run_id": run_id,
        "qaqc_id": qaqc_id,
        "status": "completed",
        "prompt_path": str(prompt_path),
        "qaqc_path": str(output_path),
        "review_count": len(reviews),
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
    for child_run_id in child_run_ids:
        output_path = _qaqc_output_path(root, child_run_id)
        if not output_path.is_file():
            continue
        reviews = load_qaqc_reviews(output_path)
        review_payload = [review.model_dump(mode="json") for review in reviews]
        child_reviews.append(
            {
                "run_id": child_run_id,
                "qaqc_path": str(output_path),
                "review_count": len(reviews),
                "reviews": review_payload,
            }
        )
        all_reviews.extend(review_payload)
    return {
        "review_count": len(all_reviews),
        "child_reviews": child_reviews,
        "reviews": all_reviews,
    }


def _approved_records_for_manifest(root: Path, manifest: Any) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    for child_run_id in _manifest_child_run_ids(manifest):
        child_manifest = _load_run_manifest(root, child_run_id)
        records.extend(approved_records_for_child(root, child_manifest))
    return tuple(records)


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
    lead_count = 0
    for child in initial_manifests:
        if child.validation_valid and Path(child.lead_path).is_file():
            lead_count += len(load_leads(Path(child.lead_path)))
    lead_quota = planned_jobs * target_per_job
    harvest_running = active and finished_jobs < planned_jobs
    if harvest_running:
        harvest_status = "running"
    elif failed_jobs or (finished_jobs >= planned_jobs and lead_count < lead_quota):
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

    coverage_review = None
    coverage_status = "blocked"
    coverage_detail = "Create a sample set and geocode observations first."
    if sample_set is not None:
        try:
            coverage_review = load_coverage_review(
                _latest_coverage_path(root, sample_set.sample_set_id)
            )
            coverage_status = "complete"
            coverage_detail = (
                f"Latest assessment: {coverage_review.dispersion_status.value}; "
                f"{len(coverage_review.recommended_child_jobs)} gap-fill job(s) recommended."
            )
        except FileNotFoundError:
            if geocoded_count > 0:
                coverage_status = "ready"
                coverage_detail = f"{geocoded_count} geocoded observation(s) are ready to assess."
            else:
                coverage_detail = "Geocode at least one approved observation first."

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
            current=lead_count,
            total=lead_quota,
            detail=(
                f"{successful_jobs}/{planned_jobs} jobs completed successfully; "
                f"{failed_jobs} failed or cancelled; {lead_count}/{lead_quota} target leads."
            ),
            metrics={
                "planned_jobs": planned_jobs,
                "finished_jobs": finished_jobs,
                "successful_jobs": successful_jobs,
                "failed_jobs": failed_jobs,
                "lead_count": lead_count,
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
            status="ready" if verified_count > 0 else "blocked",
            current=verified_count,
            total=verified_count,
            detail=f"{verified_count} QAQC-approved observation(s) are available for export.",
            action_id="export_json" if verified_count > 0 else None,
            action_label="Download Verified JSON" if verified_count > 0 else None,
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
                target=data.target,
                run_id=run_id,
                codex_bin=codex_bin,
                runner=app_runner,
                geographer_plan=geographer,
            )
            if background:
                executor.submit(task)
                manifest = await _wait_for_manifest(lambda: _load_run_manifest(root, run_id))
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
                batch_id=batch_id,
                codex_bin=codex_bin,
                runner=app_runner,
                geographer_plan=geographer,
            )
            if background:
                executor.submit(task)
                manifest = await _wait_for_manifest(lambda: _load_batch_manifest(root, batch_id))
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
                campaign_id=campaign_id,
                codex_bin=codex_bin,
                runner=app_runner,
                geographer_plan=geographer,
            )
            if background:
                executor.submit(task)
                manifest = await _wait_for_manifest(
                    lambda: _load_campaign_manifest(root, campaign_id)
                )
            else:
                manifest = await run_in_threadpool(task)
            return JSONResponse({"manifest": manifest.model_dump(mode="json")})
        except (ValidationError, ValueError) as exc:
            return _json_error(str(exc))

    async def runs(request: Request) -> JSONResponse:
        return JSONResponse({"runs": _list_manifests(root)})

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
            return JSONResponse(
                {
                    "manifest": manifest.model_dump(mode="json"),
                    "active": registry.is_active(identity),
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
        except ValueError as exc:
            return _json_error(str(exc), status_code=404)

    async def run_log(request: Request) -> PlainTextResponse:
        run_id = request.path_params["run_id"]
        try:
            manifest = _load_any_manifest(root, run_id)
        except ValueError as exc:
            return PlainTextResponse(str(exc), status_code=404)
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
        except ValueError as exc:
            return _json_error(str(exc), status_code=404)
        identity = _manifest_identity(manifest)
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
            leads = load_leads(Path(manifest.lead_path))
            prompt = render_lead_qaqc_prompt(
                leads,
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
            registry.mark_task_active(identity)

            def background_task() -> None:
                try:
                    task()
                finally:
                    registry.mark_task_inactive(identity)

            executor.submit(background_task)
            return JSONResponse(
                {
                    "started": True,
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
            registry.mark_task_active(identity)

            def background_task() -> None:
                try:
                    task()
                except Exception as exc:
                    append_harvest_log(root, identity, f"Address enrichment failed: {exc}.")
                finally:
                    registry.mark_task_inactive(identity)

            executor.submit(background_task)
            return JSONResponse(
                {
                    "started": True,
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
            registry.mark_task_active(sample_set_id)

            def background_task() -> None:
                try:
                    task()
                except Exception as exc:
                    append_harvest_log(root, sample_set_id, f"Coverage analysis failed: {exc}.")
                finally:
                    registry.mark_task_inactive(sample_set_id)

            executor.submit(background_task)
            return JSONResponse({"started": True, "sample_set_id": sample_set_id})
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
        except (ValidationError, FileNotFoundError) as exc:
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
            registry.mark_task_active(sample_set_id)

            def background_task() -> None:
                try:
                    task()
                except Exception as exc:
                    append_harvest_log(root, sample_set_id, f"Gap-fill failed: {exc}.")
                finally:
                    registry.mark_task_inactive(sample_set_id)

            executor.submit(background_task)
            return JSONResponse({"started": True, "sample_set_id": sample_set_id})
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
            registry.mark_task_active(sample_set_id)

            def background_task() -> None:
                try:
                    task()
                except Exception as exc:
                    append_harvest_log(root, sample_set_id, f"Missing QAQC failed: {exc}.")
                finally:
                    registry.mark_task_inactive(sample_set_id)

            executor.submit(background_task)
            return JSONResponse({"started": True, "sample_set_id": sample_set_id})
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
            registry.mark_task_active(sample_set_id)

            def background_task() -> None:
                try:
                    task()
                except Exception as exc:
                    append_harvest_log(root, sample_set_id, f"Missing address failed: {exc}.")
                finally:
                    registry.mark_task_inactive(sample_set_id)

            executor.submit(background_task)
            return JSONResponse({"started": True, "sample_set_id": sample_set_id})
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
        Route("/api/runs/{run_id}/cancel", cancel_run, methods=["POST"]),
        Route("/api/runs/{run_id}/promote", promote, methods=["POST"]),
        Route("/api/samples", samples),
        Route("/api/samples/from-run", sample_create_from_run, methods=["POST"]),
        Route("/api/samples/{sample_set_id}", sample_detail),
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
        Route("/api/samples/{sample_set_id}/export.verified.json", sample_export_verified_json),
        Route("/api/samples/{sample_set_id}/export.verified.csv", sample_export_verified_csv),
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
        leads = load_leads(Path(manifest.lead_path))
        payload = export_leads(leads, output_format=output_format)
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

    def shutdown() -> None:
        def exit_later() -> None:
            time.sleep(0.25)
            os._exit(0)

        threading.Thread(target=exit_later, daemon=True).start()

    app = create_app(workspace=workspace, codex_bin=codex_bin, shutdown_callback=shutdown)
    url = f"http://{host}:{port}/?v={int(time.time())}"
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(app, host=host, port=port)


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OASIS</title>
  <link rel="icon" href="/assets/oasis-logo.jpg" type="image/jpeg">
  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
  >
  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.css"
  >
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script src="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.js"></script>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --panel-soft: #fbfcfd;
      --input-bg: #ffffff;
      --button-text: #ffffff;
      --selected: #edf8fb;
      --activity-bg: #111827;
      --activity-text: #e5e7eb;
      --line: #d9dee7;
      --text: #1f2937;
      --muted: #5f6b7a;
      --accent: #176b87;
      --accent-dark: #104d61;
      --danger: #a33a35;
      --ok: #216e4e;
    }
    html[data-theme="dark"] {
      color-scheme: dark;
      --bg: #111418;
      --panel: #191f26;
      --panel-soft: #151a20;
      --input-bg: #111820;
      --button-text: #ffffff;
      --selected: #12313b;
      --activity-bg: #070b10;
      --activity-text: #d7dee8;
      --line: #33404d;
      --text: #e5ebf2;
      --muted: #a6b1bf;
      --accent: #4ca4c3;
      --accent-dark: #6bb9d4;
      --danger: #ee827c;
      --ok: #65c18c;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
        "Segoe UI", sans-serif;
      font-size: 14px;
    }
    header {
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      padding: 14px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    h1 {
      margin: 0;
      font-size: 20px;
      line-height: 1.2;
      letter-spacing: 0;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 0;
    }
    .brand-mark {
      display: block;
      width: 58px;
      height: 58px;
      flex: 0 0 58px;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 50%;
      background: #0b3445;
      box-shadow: 0 2px 8px rgb(15 44 57 / 18%);
    }
    .brand-logo {
      display: block;
      width: 100%;
      height: 100%;
      object-fit: cover;
      transform: scale(1.82);
    }
    main {
      display: grid;
      grid-template-columns: minmax(280px, 380px) minmax(0, 1fr);
      gap: 18px;
      padding: 18px 24px 24px;
    }
    .workspace-tabs {
      display: flex;
      gap: 6px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      padding: 10px 24px 0;
    }
    .workspace-tabs button {
      border-color: transparent;
      border-bottom-left-radius: 0;
      border-bottom-right-radius: 0;
      background: transparent;
      color: var(--muted);
      padding: 10px 14px;
    }
    .workspace-tabs button.active {
      border-color: var(--line);
      border-bottom-color: var(--panel);
      background: var(--panel);
      color: var(--accent);
    }
    .tab-badge {
      display: inline-block;
      min-width: 20px;
      margin-left: 5px;
      border-radius: 999px;
      background: var(--panel-soft);
      color: var(--muted);
      font-size: 11px;
      padding: 2px 6px;
      text-align: center;
    }
    .workspace-tabs button.active .tab-badge {
      background: var(--selected);
      color: var(--accent);
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }
    section.wide {
      grid-column: 1 / -1;
    }
    h2 {
      margin: 0 0 14px;
      font-size: 15px;
      line-height: 1.2;
      letter-spacing: 0;
    }
    label {
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      margin: 12px 0 5px;
    }
    input, select, textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--input-bg);
      color: var(--text);
      font: inherit;
      padding: 9px 10px;
    }
    textarea {
      min-height: 460px;
      resize: vertical;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.45;
    }
    textarea.compact {
      min-height: 78px;
      font-family: inherit;
      font-size: 14px;
    }
    textarea.activity {
      min-height: 220px;
      background: var(--activity-bg);
      color: var(--activity-text);
    }
    textarea.dialogue {
      min-height: 250px;
      background: var(--panel-soft);
      font-family: inherit;
      font-size: 13px;
      line-height: 1.6;
    }
    select[multiple] {
      min-height: 116px;
    }
    .hidden {
      display: none;
    }
    .row {
      display: grid;
      grid-template-columns: 120px minmax(0, 1fr);
      gap: 10px;
      align-items: end;
    }
    .mode {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      border: 1px solid var(--line);
      border-radius: 6px;
      overflow: hidden;
    }
    .mode button {
      border: 0;
      border-radius: 0;
      background: var(--input-bg);
      color: var(--muted);
      padding: 9px;
    }
    .mode button.active {
      background: var(--accent);
      color: var(--button-text);
    }
    button {
      border: 1px solid var(--accent);
      border-radius: 6px;
      background: var(--accent);
      color: var(--button-text);
      cursor: pointer;
      font-weight: 650;
      padding: 9px 12px;
    }
    button.secondary {
      background: var(--input-bg);
      color: var(--accent);
    }
    button:disabled {
      cursor: wait;
      opacity: .6;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 12px 0;
    }
    details.action-group {
      border-top: 1px solid var(--line);
      margin-top: 12px;
      padding-top: 10px;
    }
    details.action-group summary {
      color: var(--muted);
      cursor: pointer;
      font-weight: 650;
    }
    .pipeline-callout {
      border: 1px solid var(--line);
      border-left: 4px solid var(--accent);
      border-radius: 6px;
      background: var(--panel-soft);
      margin: 12px 0;
      padding: 10px;
    }
    .pipeline-callout strong {
      display: block;
      margin-bottom: 3px;
    }
    .section-heading {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-top: 16px;
    }
    .section-heading h2 { margin: 0; }
    .summary {
      display: grid;
      grid-template-columns: repeat(4, minmax(90px, 1fr));
      gap: 8px;
      margin-bottom: 12px;
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      background: var(--panel-soft);
    }
    .metric span {
      display: block;
      color: var(--muted);
      font-size: 11px;
      font-weight: 650;
      margin-bottom: 4px;
    }
    .metric strong {
      font-size: 18px;
    }
    .status {
      min-height: 22px;
      margin-top: 10px;
      color: var(--muted);
    }
    .status.error { color: var(--danger); }
    .status.ok { color: var(--ok); }
    .workflow-panel {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-soft);
      margin-bottom: 14px;
      padding: 12px;
    }
    .workflow-header {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 10px;
    }
    .workflow-header h3 { margin: 0 0 4px; }
    .workflow-summary { color: var(--muted); font-size: 13px; }
    .workflow-list { display: grid; gap: 7px; }
    .workflow-step {
      display: grid;
      grid-template-columns: 24px minmax(0, 1fr) auto;
      gap: 9px;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--input-bg);
      padding: 8px;
    }
    .workflow-marker {
      display: grid;
      place-items: center;
      width: 22px;
      height: 22px;
      border-radius: 50%;
      background: var(--panel-soft);
      color: var(--muted);
      font-weight: 750;
    }
    .workflow-step.complete .workflow-marker { background: var(--ok); color: white; }
    .workflow-step.running .workflow-marker { background: var(--accent); color: white; }
    .workflow-step.attention .workflow-marker { background: var(--danger); color: white; }
    .workflow-title { font-weight: 700; }
    .workflow-detail { color: var(--muted); font-size: 12px; margin-top: 2px; }
    .workflow-progress {
      height: 5px;
      background: var(--panel-soft);
      border-radius: 5px;
      margin-top: 6px;
      overflow: hidden;
    }
    .workflow-progress-fill {
      height: 100%;
      background: var(--accent);
      transition: width 180ms ease;
    }
    .workflow-progress-fill.indeterminate {
      width: 35% !important;
      animation: workflow-pulse 1.2s ease-in-out infinite alternate;
    }
    .workflow-step.complete .workflow-progress-fill { background: var(--ok); }
    .workflow-step button { white-space: nowrap; padding: 7px 9px; }
    @keyframes workflow-pulse {
      from { transform: translateX(-70%); }
      to { transform: translateX(190%); }
    }
    .history {
      max-height: 220px;
      overflow: auto;
      border-top: 1px solid var(--line);
      margin-top: 16px;
      padding-top: 8px;
    }
    .history button {
      width: 100%;
      text-align: left;
      background: var(--input-bg);
      color: var(--text);
      border-color: var(--line);
      margin-top: 6px;
      font-weight: 500;
    }
    .geometry-layout {
      display: grid;
      grid-template-columns: minmax(260px, 360px) minmax(0, 1fr);
      gap: 14px;
    }
    .geometry-list {
      max-height: 440px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 6px;
    }
    .geometry-list button {
      width: 100%;
      text-align: left;
      background: var(--input-bg);
      color: var(--text);
      border-color: var(--line);
      margin-top: 6px;
      font-weight: 500;
    }
    .geometry-list button.active {
      border-color: var(--accent);
      background: var(--selected);
    }
    .geometry-queue-tabs {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      border: 1px solid var(--line);
      border-radius: 6px;
      overflow: hidden;
      margin-bottom: 8px;
    }
    .geometry-queue-tabs button {
      border: 0;
      border-radius: 0;
      background: var(--input-bg);
      color: var(--muted);
      padding: 8px;
      min-height: 42px;
    }
    .geometry-queue-tabs button.active {
      background: var(--accent);
      color: var(--button-text);
    }
    .extent-summary {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel-soft);
      color: var(--muted);
      font-size: 13px;
      line-height: 1.4;
      padding: 9px 10px;
      margin: 8px 0 12px;
    }
    .intervention-panel {
      border: 1px solid var(--line);
      border-left: 4px solid var(--danger);
      border-radius: 6px;
      background: var(--panel-soft);
      padding: 10px;
      margin: 8px 0 12px;
    }
    .intervention-panel h3 { margin: 0 0 5px; }
    .intervention-list { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
    .intervention-list button { padding: 6px 8px; }
    .coordinate-resolver {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel-soft);
      margin-top: 12px;
      padding: 10px;
    }
    .coordinate-resolver h3 { margin: 0 0 6px; }
    .resolution-reason {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      white-space: pre-wrap;
    }
    .resolution-links {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 8px;
    }
    .resolution-links a { color: var(--accent); }
    .candidate-options {
      display: grid;
      gap: 8px;
      margin-top: 10px;
    }
    .candidate-card {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--input-bg);
      padding: 9px;
    }
    .candidate-card.conflicting { border-left: 4px solid var(--danger); }
    .candidate-card.possible { border-left: 4px solid #d97706; }
    .candidate-card.likely { border-left: 4px solid var(--ok); }
    .candidate-heading {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      align-items: flex-start;
      font-size: 12px;
      font-weight: 700;
    }
    .candidate-badge {
      border-radius: 999px;
      background: var(--panel-soft);
      color: var(--muted);
      font-size: 10px;
      padding: 3px 6px;
      text-transform: uppercase;
      white-space: nowrap;
    }
    .candidate-reason {
      color: var(--muted);
      font-size: 11px;
      line-height: 1.4;
      margin: 6px 0;
    }
    .candidate-card button { padding: 6px 8px; }
    .map.placement-active {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px var(--selected);
      cursor: crosshair;
    }
    .theme-control {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 190px;
    }
    .theme-control label {
      margin: 0;
    }
    .map {
      min-height: 520px;
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }
    @media (max-width: 860px) {
      main { grid-template-columns: 1fr; padding: 12px; }
      .workspace-tabs { padding-left: 12px; padding-right: 12px; }
      .summary { grid-template-columns: repeat(2, 1fr); }
      .geometry-layout { grid-template-columns: 1fr; }
      .workflow-header { flex-direction: column; }
      .workflow-step { grid-template-columns: 24px minmax(0, 1fr); }
      .workflow-step button { grid-column: 2; justify-self: start; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <span class="brand-mark">
        <img
          class="brand-logo"
          src="/assets/oasis-logo.jpg"
          alt=""
          aria-hidden="true"
        >
      </span>
      <div>
        <h1>OASIS</h1>
        <div class="workflow-summary">
          Observation Acquisition and Spatial Information Synthesis
        </div>
      </div>
    </div>
    <div class="theme-control">
      <label for="themeSelect">Theme</label>
      <select id="themeSelect">
        <option value="system">System</option>
        <option value="light">Light</option>
        <option value="dark">Dark</option>
      </select>
    </div>
  </header>
  <nav class="workspace-tabs" role="tablist" aria-label="Application workspaces">
    <button
      id="workbenchTab"
      class="active"
      type="button"
      role="tab"
      aria-selected="true"
      aria-controls="harvestSetup resultsPanel samplePanel"
    >
      Agentic Workbench
    </button>
    <button
      id="geometryTab"
      type="button"
      role="tab"
      aria-selected="false"
      aria-controls="geometryPanel"
    >
      Geometry Studio <span id="geometryTabBadge" class="tab-badge">0</span>
    </button>
  </nav>
  <main>
    <section id="harvestSetup" data-workspace="workbench">
      <h2>New Harvest</h2>
      <label for="country">Country</label>
      <input id="country" value="US" autocomplete="off">

      <div id="singleLocalityBlock">
        <label for="locality">Region or Locality</label>
        <input id="locality" placeholder="Optional, e.g. Tennessee">
      </div>

      <div id="campaignLocalitiesBlock" class="hidden">
        <label for="localities">Regions or Localities</label>
        <textarea
          id="localities"
          class="compact"
          spellcheck="false"
          placeholder="Optional, one per line"
        ></textarea>
      </div>

      <div id="singleFacilityBlock">
        <label for="profileSet">Facility Type</label>
        <select id="profileSet"></select>
      </div>

      <div id="campaignFacilityBlock" class="hidden">
        <label for="campaignFacilityTypes">Facility Types</label>
        <select id="campaignFacilityTypes" multiple></select>
      </div>

      <div id="subtypeBlock">
        <label for="profile">Subtype</label>
        <select id="profile"></select>
      </div>

      <div class="row">
        <div>
          <label for="target">Target</label>
          <input id="target" type="number" min="1" value="20">
        </div>
        <div>
          <label>Mode</label>
          <div class="mode">
            <button id="singleMode" class="active" type="button">Single</button>
            <button id="batchMode" type="button">Batch</button>
            <button id="campaignMode" type="button">Campaign</button>
          </div>
        </div>
      </div>

      <div class="actions">
        <button id="runFullPipelineButton" type="button">Run Full Pipeline</button>
        <button id="runButton" type="button">Run Harvest</button>
        <button id="refreshButton" class="secondary" type="button">Refresh Runs</button>
        <button id="clearRunsButton" class="secondary" type="button">Clear All</button>
      </div>
      <div class="pipeline-callout">
        <strong id="fullPipelineHeading">Guided end-to-end workflow</strong>
        <span id="fullPipelineStatus">
          Runs through coverage analysis, then pauses before gap fill for your review.
        </span>
      </div>
      <div id="status" class="status">Ready.</div>
      <div class="history" id="history"></div>
    </section>

    <section id="resultsPanel" data-workspace="workbench">
      <h2>Results</h2>
      <div class="workflow-panel">
        <div class="workflow-header">
          <div>
            <h3>Project Workflow</h3>
            <div id="workflowSummary" class="workflow-summary">
              Start or select a harvest to see the full workflow.
            </div>
          </div>
          <button id="workflowNextButton" class="secondary" type="button" disabled>
            Next action
          </button>
        </div>
        <div id="workflowSteps" class="workflow-list"></div>
      </div>
      <div class="summary">
        <div class="metric"><span>Status</span><strong id="metricStatus">-</strong></div>
        <div class="metric"><span>Leads</span><strong id="metricLeads">0</strong></div>
        <div class="metric">
          <span id="metricFacilityLabel">Facility</span><strong id="metricFacility">0</strong>
        </div>
        <div class="metric">
          <span id="metricAggregateLabel">Aggregate</span><strong id="metricAggregate">0</strong>
        </div>
      </div>
      <div class="actions">
        <button id="runQaqcButton" class="secondary" type="button">Run QAQC</button>
        <button id="runAddressButton" class="secondary" type="button">
          Run Address Enrichment
        </button>
        <button id="geocodeButton" class="secondary" type="button">
          Geocode All Accepted
        </button>
      </div>
      <details class="action-group">
        <summary>Prompts, JSON, and exports</summary>
        <div class="actions">
          <button id="copyButton" class="secondary" type="button">Copy JSON</button>
          <button id="copyQaqcButton" class="secondary" type="button">Copy QAQC Prompt</button>
          <button id="downloadJsonButton" class="secondary" type="button">
            Download Verified JSON
          </button>
          <button id="downloadCsvButton" class="secondary" type="button">
            Download Verified CSV
          </button>
        </div>
      </details>
      <textarea
        id="jsonOutput"
        spellcheck="false"
        placeholder="Harvest JSON will appear here."
      ></textarea>

      <div class="section-heading">
        <h2>Full Pipeline Transcript</h2>
        <button id="downloadTranscriptButton" class="secondary" type="button">
          Download Transcript (.txt)
        </button>
      </div>
      <textarea
        id="dialogueOutput"
        class="dialogue"
        spellcheck="false"
        readonly
        placeholder="The geographer, harvester, and review agents will report their findings here."
      ></textarea>

      <h2>Agent Activity</h2>
      <div class="actions">
        <button id="cancelButton" class="secondary" type="button" disabled>Cancel Run</button>
        <button id="exitButton" class="secondary" type="button">Exit Application</button>
      </div>
      <textarea
        id="activityOutput"
        class="activity"
        spellcheck="false"
        readonly
        placeholder="Agent activity will appear here while a harvest runs."
      ></textarea>
    </section>

    <section id="geometryPanel" class="wide hidden" data-workspace="geometry">
      <h2>Geometry Studio</h2>
      <div class="workflow-summary">
        Resolve coordinates, inspect spatial placement, digitize building footprints, and
        calculate planar area.
      </div>
      <div class="actions">
        <button id="loadApprovedButton" class="secondary" type="button">Load Approved</button>
        <button id="loadAugmentedSampleButton" class="secondary" type="button">
          Load Augmented Sample
        </button>
        <button id="saveFootprintButton" class="secondary" type="button">Save Footprint</button>
        <button id="skipGeometryButton" class="secondary" type="button">Skip</button>
      </div>
      <details class="action-group">
        <summary>Map view and geometry exports</summary>
        <div class="actions">
          <button id="showSampleExtentButton" class="secondary" type="button">
            Show Sample Extent
          </button>
          <button id="zoomSampleExtentButton" class="secondary" type="button">
            Zoom To Extent
          </button>
          <button id="clearSampleExtentButton" class="secondary" type="button">
            Clear Extent
          </button>
          <button id="downloadVerifiedJsonButton" class="secondary" type="button">
            Download Verified JSON
          </button>
          <button id="downloadVerifiedCsvButton" class="secondary" type="button">
            Download Verified CSV
          </button>
          <button id="downloadFootprintsButton" class="secondary" type="button">
            Download Footprints GeoJSON
          </button>
          <button id="downloadSampleFootprintsButton" class="secondary" type="button">
            Download Sample Footprints
          </button>
        </div>
      </details>
      <div class="extent-summary" id="geometryExtentSummary">
        Extent: load approved observations, then geocode or save points to map the sample.
      </div>
      <div class="status" id="geometryStatus">Load QAQC-approved observations to begin.</div>
      <div class="intervention-panel">
        <h3>Coordinate Assignment Required - <span id="interventionCount">0</span></h3>
        <div class="workflow-summary">
          These observations could not be assigned a trustworthy in-scope facility coordinate.
          Select one, search a better address, or place its point from the map center.
        </div>
        <div id="interventionList" class="intervention-list">
          <span class="workflow-summary">No observations currently require intervention.</span>
        </div>
      </div>
      <div class="geometry-layout">
        <div>
          <div class="geometry-queue-tabs" role="tablist" aria-label="Geometry observation queues">
            <button
              id="geocodedQueueTab"
              class="active"
              type="button"
              role="tab"
              aria-selected="true"
            >
              Geocoded <span id="geocodedQueueCount">0</span>
            </button>
            <button
              id="manualQueueTab"
              type="button"
              role="tab"
              aria-selected="false"
            >
              Needs Manual Geocoding <span id="manualQueueCount">0</span>
            </button>
          </div>
          <div class="geometry-list" id="geometryList"></div>
          <div class="coordinate-resolver">
            <h3>Resolve Selected Coordinate</h3>
            <div id="resolutionReason" class="resolution-reason">
              Select an observation to see why automatic coordinate assignment failed.
            </div>
            <div class="resolution-links">
              <a id="resolutionSourceLink" class="hidden" target="_blank" rel="noopener">
                Open occupancy source
              </a>
              <a id="resolutionAddressLink" class="hidden" target="_blank" rel="noopener">
                Open address evidence
              </a>
              <a id="googleSearchLink" class="hidden" target="_blank" rel="noopener">
                Search Google
              </a>
              <a id="googleMapsLink" class="hidden" target="_blank" rel="noopener">
                Search Google Maps
              </a>
            </div>
            <div class="actions">
              <button id="researchFacilityButton" class="secondary" type="button">
                Research This Facility
              </button>
            </div>
            <div id="candidateOptions" class="candidate-options">
              <span class="workflow-summary">
                Ranked geocoder candidates will appear here after an automatic search.
              </span>
            </div>
            <label for="pastedCoordinates">Paste Google Maps Coordinates</label>
            <input
              id="pastedCoordinates"
              placeholder="33.7490, -84.3880 or paste a Google Maps URL"
            >
            <div class="actions">
              <button id="previewCoordinatesButton" class="secondary" type="button">
                Preview Coordinate
              </button>
            </div>
            <div id="coordinatePasteStatus" class="status">
              Preview pasted coordinates before saving them.
            </div>
            <label for="manualAddress">Corrected Address or Place</label>
            <input id="manualAddress" placeholder="Enter a corrected facility address">
            <div class="actions">
              <button id="searchAddressButton" class="secondary" type="button">
                Search Corrected Address
              </button>
              <button id="placePointButton" class="secondary" type="button">
                Place Point on Map
              </button>
              <button id="useMapCenterButton" class="secondary" type="button">
                Place at Map Center
              </button>
              <button id="saveCoordinateButton" type="button">Save Coordinate</button>
            </div>
            <label for="coordinateReviewNotes">Coordinate Review Notes</label>
            <input
              id="coordinateReviewNotes"
              placeholder="Optional evidence or reasoning for the manual assignment"
            >
            <div id="coordinateDraftStatus" class="status">
              No coordinate change is waiting to be saved.
            </div>
          </div>
          <label for="geometryDetail">Selected Observation</label>
          <textarea
            id="geometryDetail"
            class="compact"
            spellcheck="false"
            readonly
          ></textarea>
        </div>
        <div id="map" class="map"></div>
      </div>
    </section>

    <section id="samplePanel" class="wide" data-workspace="workbench">
      <h2>Sample Set / Coverage</h2>
      <div class="actions">
        <button id="createSampleButton" class="secondary" type="button">
          Create Sample Set
        </button>
        <button id="analyzeCoverageButton" class="secondary" type="button">
          Analyze Coverage
        </button>
        <button id="runGapFillButton" class="secondary" type="button">Run Gap Fill</button>
      </div>
      <details class="action-group">
        <summary>Repair passes and sample exports</summary>
        <div class="actions">
          <button id="runSampleQaqcButton" class="secondary" type="button">
            Run QAQC Missing
          </button>
          <button id="runSampleAddressButton" class="secondary" type="button">
            Run Address Missing
          </button>
          <button id="downloadSampleJsonButton" class="secondary" type="button">
            Download Sample JSON
          </button>
          <button id="downloadSampleCsvButton" class="secondary" type="button">
            Download Sample CSV
          </button>
        </div>
      </details>
      <div class="status" id="sampleStatus">
        Create a sample set after geometry review; coverage works best once approved
        observations have geocoded points.
      </div>
      <textarea
        id="sampleOutput"
        class="compact"
        spellcheck="false"
        readonly
        placeholder="Sample set and coverage output will appear here."
      ></textarea>
    </section>
  </main>
  <script>
    const state = {
      profiles: [],
      mode: 'single',
      currentRunId: null,
      currentSampleSetId: null,
      currentLeads: [],
      pollTimer: null,
      pollPurpose: 'harvest',
      samplePollTimer: null,
      samplePollPurpose: 'coverage',
      geometryItems: [],
      selectedGeometryItemId: null,
      map: null,
      drawnItems: null,
      marker: null,
      overviewPointLayer: null,
      overviewFootprintLayer: null,
      overviewExtentLayer: null,
      overviewBounds: null,
      sampleExtentVisible: false,
      coordinatePlacementMode: false,
      selectedCandidateOptions: [],
      pendingCoordinatePreview: null,
      geometryListTab: 'geocoded',
      themePreference: 'system',
      workflow: null,
      activeWorkspace: 'workbench',
      fullPipelineActive: false
    };
    const $ = (id) => document.getElementById(id);
    const terminalStatuses = ['completed', 'failed', 'cancelled'];

    function effectiveTheme(preference) {
      if (preference === 'light' || preference === 'dark') return preference;
      return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }

    function applyTheme(preference) {
      state.themePreference = preference;
      document.documentElement.dataset.theme = effectiveTheme(preference);
      $('themeSelect').value = preference;
    }

    function initTheme() {
      const stored = localStorage.getItem('observationHarvesterTheme') || 'system';
      applyTheme(stored);
      window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
        if (state.themePreference === 'system') applyTheme('system');
      });
    }

    function setWorkspaceTab(workspace) {
      state.activeWorkspace = workspace;
      for (const panel of document.querySelectorAll('[data-workspace]')) {
        panel.classList.toggle('hidden', panel.dataset.workspace !== workspace);
      }
      const workbenchActive = workspace === 'workbench';
      $('workbenchTab').classList.toggle('active', workbenchActive);
      $('workbenchTab').setAttribute('aria-selected', String(workbenchActive));
      $('geometryTab').classList.toggle('active', !workbenchActive);
      $('geometryTab').setAttribute('aria-selected', String(!workbenchActive));
      if (!workbenchActive) {
        initMap();
        window.setTimeout(() => {
          if (state.map) state.map.invalidateSize();
          if (state.selectedGeometryItemId) selectGeometryItem(state.selectedGeometryItemId);
        }, 0);
      }
    }

    function setPipelineStatus(heading, message, kind = '') {
      $('fullPipelineHeading').textContent = heading;
      $('fullPipelineStatus').textContent = message;
      $('fullPipelineStatus').className = kind ? `status ${kind}` : '';
    }

    function setStatus(message, kind = '') {
      $('status').textContent = message;
      $('status').className = `status ${kind}`;
    }

    function selectedProfileSet() {
      return state.profiles.find((item) => item.profile_set_id === $('profileSet').value);
    }

    function renderProfileSets() {
      $('profileSet').innerHTML = state.profiles.map((profileSet) =>
        `<option value="${profileSet.profile_set_id}">${profileSet.label}</option>`
      ).join('');
      $('campaignFacilityTypes').innerHTML = state.profiles.map((profileSet) => {
        const selected = ['schools', 'manufacturing', 'restaurants'].includes(
          profileSet.profile_set_id
        )
          ? ' selected'
          : '';
        return `<option value="${profileSet.profile_set_id}"${selected}>` +
          `${profileSet.label}</option>`;
      }).join('');
      renderProfiles();
    }

    function renderProfiles() {
      const profileSet = selectedProfileSet();
      const options = ['<option value="">All subtypes</option>'];
      if (profileSet) {
        for (const profile of profileSet.profiles) {
          options.push(`<option value="${profile.profile_id}">${profile.label}</option>`);
        }
      }
      $('profile').innerHTML = options.join('');
    }

    function setMode(mode) {
      state.mode = mode;
      $('singleMode').classList.toggle('active', mode === 'single');
      $('batchMode').classList.toggle('active', mode === 'batch');
      $('campaignMode').classList.toggle('active', mode === 'campaign');
      $('singleLocalityBlock').classList.toggle('hidden', mode === 'campaign');
      $('campaignLocalitiesBlock').classList.toggle('hidden', mode !== 'campaign');
      $('singleFacilityBlock').classList.toggle('hidden', mode === 'campaign');
      $('campaignFacilityBlock').classList.toggle('hidden', mode !== 'campaign');
      $('subtypeBlock').classList.toggle('hidden', mode !== 'single');
      $('profile').disabled = mode !== 'single';
    }

    function splitLocalities() {
      return $('localities').value
        .split(/[\n,]/)
        .map((item) => item.trim())
        .filter(Boolean);
    }

    function selectedCampaignFacilityTypes() {
      return Array.from($('campaignFacilityTypes').selectedOptions)
        .map((option) => option.value)
        .filter(Boolean);
    }

    function isTerminal(status) {
      return terminalStatuses.includes(status);
    }

    async function api(path, options = {}) {
      const response = await fetch(path, options);
      const contentType = response.headers.get('content-type') || '';
      const payload = contentType.includes('application/json')
        ? await response.json()
        : await response.text();
      if (!response.ok) {
        throw new Error(
          typeof payload === 'string' ? payload : (payload.error || 'Request failed')
        );
      }
      return payload;
    }

    function escapeHtml(value) {
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
    }

    function workflowAction(actionId) {
      const targets = {
        run_qaqc: 'runQaqcButton',
        run_address: 'runAddressButton',
        load_geometry: 'loadApprovedButton',
        create_sample: 'createSampleButton',
        analyze_coverage: 'analyzeCoverageButton',
        run_gap_fill: 'runGapFillButton',
        export_json: state.currentSampleSetId
          ? 'downloadSampleJsonButton'
          : 'downloadVerifiedJsonButton'
      };
      const target = targets[actionId];
      if (target) {
        if (actionId === 'load_geometry') setWorkspaceTab('geometry');
        $(target).click();
      }
    }

    function renderWorkflow(payload) {
      state.workflow = payload;
      const stages = payload?.stages || [];
      const nextAction = payload?.next_action || null;
      $('workflowSummary').textContent = nextAction
        ? `Recommended next: ${nextAction.label}`
        : (stages.length ? 'All available workflow stages are up to date.' :
          'Start or select a harvest to see the full workflow.');
      $('workflowNextButton').disabled = !nextAction;
      $('workflowNextButton').textContent = nextAction ? nextAction.label : 'Next action';
      $('workflowNextButton').dataset.action = nextAction?.id || '';
      const symbols = {
        complete: '✓',
        running: '●',
        attention: '!',
        ready: '→',
        blocked: '○'
      };
      $('workflowSteps').innerHTML = stages.map((stage) => {
        const total = Number(stage.total || 0);
        const current = Number(stage.current || 0);
        const percent = stage.status === 'complete'
          ? 100
          : (total > 0 ? Math.min(100, Math.round((current / total) * 100)) : 0);
        const progress = total > 0 || stage.indeterminate
          ? `<div class="workflow-progress"><div class="workflow-progress-fill` +
            `${stage.indeterminate ? ' indeterminate' : ''}" style="width:${percent}%"></div></div>`
          : '';
        const action = stage.action_id && ['ready', 'attention'].includes(stage.status)
          ? `<button class="secondary" type="button" data-workflow-action="` +
            `${escapeHtml(stage.action_id)}">${escapeHtml(stage.action_label)}</button>`
          : '';
        return `<div class="workflow-step ${escapeHtml(stage.status)}">
          <div class="workflow-marker">${symbols[stage.status] || '○'}</div>
          <div>
            <div class="workflow-title">${escapeHtml(stage.label)}</div>
            <div class="workflow-detail">${escapeHtml(stage.detail)}</div>
            ${progress}
          </div>
          ${action}
        </div>`;
      }).join('');
    }

    function renderGeocodingProgress({
      attempted,
      total,
      geocoded,
      humanReview,
      errors,
      working = false
    }) {
      if (!state.workflow) return;
      const workflow = JSON.parse(JSON.stringify(state.workflow));
      const stage = (workflow.stages || []).find((item) => item.id === 'geometry');
      if (!stage) return;
      stage.status = 'running';
      stage.current = attempted;
      stage.total = total;
      stage.indeterminate = working;
      stage.detail =
        `${attempted}/${total} attempted; ${geocoded} positioned; ` +
        `${humanReview} need coordinate assignment; ${errors} errors.`;
      workflow.next_action = null;
      renderWorkflow(workflow);
      if (state.fullPipelineActive) {
        setPipelineStatus(
          'Step 4 of 6 - Automated geocoding',
          `${attempted}/${total} observations processed; ${geocoded} positioned; ` +
            `${humanReview} need human review.`
        );
      }
    }

    async function loadWorkflowStatus() {
      const path = state.currentSampleSetId
        ? `/api/samples/${state.currentSampleSetId}/workflow-status`
        : (state.currentRunId ? `/api/runs/${state.currentRunId}/workflow-status` : null);
      if (!path) return renderWorkflow(null);
      const payload = await api(path);
      if (payload.active) {
        const purpose = state.currentSampleSetId ? state.samplePollPurpose : state.pollPurpose;
        const activeStages = {
          harvest: 'harvest',
          qaqc: 'qaqc',
          address: 'address',
          coverage: 'coverage',
          'gap fill': 'gap_fill',
          'missing QAQC': 'qaqc',
          'missing address': 'address'
        };
        const activeStage = payload.stages.find((stage) => stage.id === activeStages[purpose]);
        if (activeStage && activeStage.status !== 'complete') {
          activeStage.status = 'running';
          activeStage.indeterminate = true;
          activeStage.detail = `${activeStage.label} agent work is currently running.`;
        }
      }
      renderWorkflow(payload);
    }

    function requestBody() {
      if (state.mode === 'campaign') {
        return {
          country: $('country').value.trim(),
          localities: splitLocalities(),
          facility_types: selectedCampaignFacilityTypes(),
          target: Number($('target').value || 20)
        };
      }
      const body = {
        country: $('country').value.trim(),
        locality: $('locality').value.trim() || null,
        profiles: $('profileSet').value,
        target: Number($('target').value || 20)
      };
      if (state.mode === 'single' && $('profile').value) body.profile = $('profile').value;
      return body;
    }

    function renderResult(manifest, leads) {
      state.currentRunId = manifest.run_id || manifest.batch_id || manifest.campaign_id || null;
      state.currentLeads = leads || [];
      const summary = manifest.summary || {};
      const grouped = Boolean(manifest.batch_id || manifest.campaign_id);
      $('metricStatus').textContent = manifest.status || '-';
      $('metricLeads').textContent = summary.lead_count || leads.length || 0;
      $('metricFacilityLabel').textContent = grouped ? 'Completed' : 'Facility';
      $('metricAggregateLabel').textContent = grouped ? 'Failed' : 'Aggregate';
      $('metricFacility').textContent = grouped
        ? (summary.completed_count || 0)
        : (summary.facility_level_count || 0);
      $('metricAggregate').textContent = grouped
        ? (summary.failed_count || 0)
        : (summary.regional_aggregate_count || 0);
      $('jsonOutput').value = JSON.stringify(leads.length ? leads : { manifest }, null, 2);
    }

    function resetResults() {
      stopPolling();
      stopSamplePolling();
      state.currentRunId = null;
      state.currentSampleSetId = null;
      state.currentLeads = [];
      state.geometryItems = [];
      state.selectedGeometryItemId = null;
      $('metricStatus').textContent = '-';
      $('metricLeads').textContent = '0';
      $('metricFacilityLabel').textContent = 'Facility';
      $('metricAggregateLabel').textContent = 'Aggregate';
      $('metricFacility').textContent = '0';
      $('metricAggregate').textContent = '0';
      $('jsonOutput').value = '';
      $('sampleOutput').value = '';
      $('activityOutput').value = '';
      $('dialogueOutput').value = '';
      $('geometryDetail').value = '';
      renderGeometryList();
      renderWorkflow(null);
    }

    async function loadLog(runId) {
      if (!runId) return;
      const response = await fetch(`/api/runs/${runId}/log`);
      $('activityOutput').value = response.ok ? await response.text() : await response.text();
      $('activityOutput').scrollTop = $('activityOutput').scrollHeight;
    }

    function transcriptPath(download = false) {
      if (state.currentSampleSetId) {
        return `/api/samples/${state.currentSampleSetId}/` +
          (download ? 'transcript.txt' : 'dialogue');
      }
      if (state.currentRunId) {
        return `/api/runs/${state.currentRunId}/` +
          (download ? 'transcript.txt' : 'dialogue');
      }
      return null;
    }

    async function loadDialogue() {
      const path = transcriptPath(false);
      if (!path) return;
      const response = await fetch(path);
      $('dialogueOutput').value = response.ok ? await response.text() : '';
      $('dialogueOutput').scrollTop = $('dialogueOutput').scrollHeight;
    }

    async function downloadTranscript() {
      const path = transcriptPath(true);
      if (!path) return setStatus('No pipeline selected.', 'error');
      const response = await fetch(path);
      if (!response.ok) return setStatus(await response.text(), 'error');
      const identity = state.currentSampleSetId || state.currentRunId;
      downloadText(
        `${identity}-pipeline-transcript.txt`,
        await response.text(),
        'text/plain'
      );
    }

    function stopPolling() {
      if (state.pollTimer) window.clearInterval(state.pollTimer);
      state.pollTimer = null;
      state.pollPurpose = 'harvest';
      $('cancelButton').disabled = true;
    }

    async function pollCurrentRun() {
      if (!state.currentRunId) return;
      const payload = await api(`/api/runs/${state.currentRunId}/status`);
      const manifest = payload.manifest;
      let leads = state.currentLeads;
      if (manifest.run_id && manifest.validation_valid) {
        try {
          leads = (await api(`/api/runs/${state.currentRunId}/leads`)).leads;
        } catch (_) {
          leads = [];
        }
      }
      renderResult(manifest, leads);
      $('cancelButton').disabled = !payload.active;
      await loadLog(state.currentRunId);
      await loadDialogue(state.currentRunId);
      await loadWorkflowStatus();
      if (state.pollPurpose === 'qaqc') {
        if (payload.active) {
          const stamp = new Date().toLocaleTimeString();
          const heartbeat = `\\n[local ${stamp}] QAQC still running...\\n`;
          if (!$('activityOutput').value.endsWith(heartbeat)) {
            $('activityOutput').value += heartbeat;
            $('activityOutput').scrollTop = $('activityOutput').scrollHeight;
          }
          setStatus('QAQC still running. Watching agent activity...', 'ok');
        }
        if (!payload.active) {
          stopPolling();
          const reviews = await api(`/api/runs/${state.currentRunId}/qaqc-reviews`);
          $('jsonOutput').value = JSON.stringify(reviews, null, 2);
          $('metricStatus').textContent = 'qaqc complete';
          $('metricLeads').textContent = reviews.review_count || 0;
          $('metricFacilityLabel').textContent = 'Children';
          $('metricAggregateLabel').textContent = 'Reviews';
          $('metricFacility').textContent = (reviews.child_reviews || []).length;
          $('metricAggregate').textContent = reviews.review_count || 0;
          setStatus('QAQC complete.', 'ok');
        }
        return payload;
      }
      if (state.pollPurpose === 'address') {
        if (payload.active) {
          const stamp = new Date().toLocaleTimeString();
          const heartbeat = `\\n[local ${stamp}] Address enrichment still running...\\n`;
          if (!$('activityOutput').value.endsWith(heartbeat)) {
            $('activityOutput').value += heartbeat;
            $('activityOutput').scrollTop = $('activityOutput').scrollHeight;
          }
          setStatus('Address enrichment still running. Watching agent activity...', 'ok');
        }
        if (!payload.active) {
          stopPolling();
          const results = await api(`/api/runs/${state.currentRunId}/address-results`);
          $('jsonOutput').value = JSON.stringify(results, null, 2);
          $('metricStatus').textContent = 'address complete';
          $('metricLeads').textContent = results.result_count || 0;
          $('metricFacilityLabel').textContent = 'Children';
          $('metricAggregateLabel').textContent = 'Addresses';
          $('metricFacility').textContent = (results.child_results || []).length;
          $('metricAggregate').textContent = results.result_count || 0;
          setStatus('Address enrichment complete.', 'ok');
        }
        return payload;
      }
      if (isTerminal(manifest.status)) {
        stopPolling();
        setStatus(
          manifest.status === 'completed' ? 'Harvest complete.' : `Harvest ${manifest.status}.`,
          manifest.status === 'completed' ? 'ok' : 'error'
        );
        await loadRuns();
      }
      return payload;
    }

    function startPolling(runId, purpose = 'harvest') {
      stopPolling();
      state.currentRunId = runId;
      state.pollPurpose = purpose;
      state.pollTimer = window.setInterval(() => {
        pollCurrentRun().catch((error) => setStatus(error.message, 'error'));
      }, 1500);
      pollCurrentRun().catch((error) => setStatus(error.message, 'error'));
    }

    async function runHarvest(options = {}) {
      const managed = Boolean(options.managed);
      const button = $('runButton');
      button.disabled = true;
      state.currentSampleSetId = null;
      state.geometryItems = [];
      state.selectedGeometryItemId = null;
      renderGeometryList();
      setStatus('Preparing geographic vernacular review...');
      try {
        const body = requestBody();
        const geographerRequest = state.mode === 'campaign'
          ? {
              country: body.country,
              localities: body.localities,
              facility_types: body.facility_types,
              mode: 'campaign'
            }
          : {
              country: body.country,
              locality: body.locality,
              profiles: body.profiles,
              profile: state.mode === 'single' ? (body.profile || null) : null,
              mode: state.mode
            };
        const geographerPayload = await api('/api/geographer/plan', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(geographerRequest)
        });
        body.geographer_plan_path = geographerPayload.plan_path;
        if (state.mode === 'single') {
          body.run_id = geographerPayload.run_id;
        } else if (state.mode === 'batch') {
          body.batch_id = geographerPayload.run_id;
        } else {
          body.campaign_id = geographerPayload.run_id;
        }
        $('dialogueOutput').value = geographerPayload.dialogue || '';
        $('dialogueOutput').scrollTop = $('dialogueOutput').scrollHeight;
        setStatus(
          geographerPayload.plan.status === 'fallback'
            ? 'Geographer used the safe fallback. Starting harvest...'
            : 'Geographer prepared local terminology. Starting harvest...',
          'ok'
        );
        const endpoint = state.mode === 'campaign'
          ? '/api/harvest/campaign-run'
          : (state.mode === 'batch' ? '/api/harvest/batch-run' : '/api/harvest/run');
        const payload = await api(endpoint, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(body)
        });
        renderResult(payload.manifest, payload.leads || []);
        await loadLog(state.currentRunId);
        await loadDialogue(state.currentRunId);
        const failed = payload.manifest.status === 'failed';
        setStatus(
          isTerminal(payload.manifest.status)
            ? (failed ? 'Harvest failed. See manifest output.' : 'Harvest complete.')
            : 'Harvest started. Watching agent activity...',
          failed ? 'error' : 'ok'
        );
        if (!isTerminal(payload.manifest.status) && !managed) {
          startPolling(state.currentRunId);
        }
        await loadRuns();
        return payload;
      } catch (error) {
        setStatus(error.message, 'error');
        if (managed) throw error;
        return null;
      } finally {
        button.disabled = state.fullPipelineActive;
      }
    }

    async function loadRuns() {
      const payload = await api('/api/runs');
      $('history').innerHTML = payload.runs.slice().reverse().map((run) => {
        const id = run.run_id || run.batch_id || run.campaign_id;
        const label = run.manifest_type === 'campaign'
          ? 'Campaign'
          : (run.manifest_type === 'batch' ? 'Batch' : 'Run');
        const scope = run.manifest_type === 'campaign'
          ? [run.country, (run.localities || []).join(', ') || 'countrywide'].join(' / ')
          : [run.country, run.locality].filter(Boolean).join(' / ');
        return `<button type="button" data-run="${id}">
          ${label}: ${id}<br>${scope} - ${run.status}
        </button>`;
      }).join('') || '<div class="status">No runs yet.</div>';
      for (const button of $('history').querySelectorAll('button[data-run]')) {
        button.addEventListener('click', () => loadRun(button.dataset.run));
      }
    }

    async function clearRuns() {
      if (!window.confirm('Clear recent harvest history and generated lead/log/prompt files?')) {
        return;
      }
      const payload = await api('/api/runs/clear', { method: 'POST' });
      resetResults();
      await loadRuns();
      setStatus(`Cleared ${payload.deleted_files} generated file(s).`, 'ok');
    }

    async function loadRun(runId) {
      const detail = await api(`/api/runs/${runId}`);
      state.currentSampleSetId = null;
      let leads = [];
      if (detail.manifest.run_id) {
        try {
          leads = (await api(`/api/runs/${runId}/leads`)).leads;
        } catch (_) {
          leads = [];
        }
      }
      renderResult(detail.manifest, leads);
      await loadLog(runId);
      await loadDialogue(runId);
      await loadWorkflowStatus();
      if (!isTerminal(detail.manifest.status)) {
        startPolling(runId);
      } else {
        stopPolling();
      }
      setStatus(`Loaded ${runId}.`);
    }

    async function cancelRun() {
      if (!state.currentRunId) return setStatus('No run selected.', 'error');
      const payload = await api(`/api/runs/${state.currentRunId}/cancel`, { method: 'POST' });
      setStatus(
        payload.cancelled ? 'Cancellation requested.' : payload.message,
        payload.cancelled ? 'ok' : 'error'
      );
      await pollCurrentRun();
    }

    async function copyQaqcPrompt() {
      if (!state.currentRunId) return setStatus('No run selected.', 'error');
      const response = await fetch(`/api/runs/${state.currentRunId}/qaqc-prompt`);
      const text = await response.text();
      if (!response.ok) return setStatus(text, 'error');
      await navigator.clipboard.writeText(text);
      setStatus('QAQC prompt copied.', 'ok');
    }

    async function runQaqc(options = {}) {
      const managed = Boolean(options.managed);
      if (!state.currentRunId) return setStatus('No run selected.', 'error');
      const button = $('runQaqcButton');
      button.disabled = true;
      setStatus('Starting QAQC agent run...');
      try {
        const payload = await api(`/api/runs/${state.currentRunId}/qaqc-run`, { method: 'POST' });
        $('activityOutput').value +=
          `\\nQAQC started for ${(payload.child_run_ids || []).length || 1} child run(s).\\n`;
        setStatus(
          payload.started ? 'QAQC started. Watching agent activity...' : 'QAQC complete.',
          'ok'
        );
        if (!managed && payload.started) startPolling(state.currentRunId, 'qaqc');
        return payload;
      } catch (error) {
        setStatus(error.message, 'error');
        if (managed) throw error;
        return null;
      } finally {
        button.disabled = state.fullPipelineActive;
      }
    }

    async function runAddressEnrichment(options = {}) {
      const managed = Boolean(options.managed);
      if (!state.currentRunId) return setStatus('No run selected.', 'error');
      const button = $('runAddressButton');
      button.disabled = true;
      setStatus('Starting address enrichment agent run...');
      try {
        const payload = await api(`/api/runs/${state.currentRunId}/address-run`, {
          method: 'POST'
        });
        $('activityOutput').value +=
          `\\nAddress enrichment started for ${(payload.child_run_ids || []).length || 1} ` +
          'child run(s).\\n';
        setStatus(
          payload.started
            ? 'Address enrichment started. Watching agent activity...'
            : 'Address enrichment complete.',
          'ok'
        );
        if (!managed && payload.started) startPolling(state.currentRunId, 'address');
        return payload;
      } catch (error) {
        setStatus(error.message, 'error');
        if (managed) throw error;
        return null;
      } finally {
        button.disabled = state.fullPipelineActive;
      }
    }

    function setSampleStatus(message, kind = '') {
      $('sampleStatus').textContent = message;
      $('sampleStatus').className = `status ${kind}`;
    }

    function stopSamplePolling() {
      if (state.samplePollTimer) window.clearInterval(state.samplePollTimer);
      state.samplePollTimer = null;
    }

    async function loadSampleLog(sampleSetId) {
      const response = await fetch(`/api/samples/${sampleSetId}/log`);
      if (response.ok) {
        $('activityOutput').value = await response.text();
        $('activityOutput').scrollTop = $('activityOutput').scrollHeight;
      }
      await loadDialogue(sampleSetId);
    }

    async function pollSampleSet() {
      if (!state.currentSampleSetId) return;
      const payload = await api(`/api/samples/${state.currentSampleSetId}/status`);
      $('sampleOutput').value = JSON.stringify(payload.sample_set, null, 2);
      await loadSampleLog(state.currentSampleSetId);
      await loadWorkflowStatus();
      if (payload.active) {
        setSampleStatus(`${state.samplePollPurpose} still running...`, 'ok');
        return payload;
      }
      stopSamplePolling();
      if (state.samplePollPurpose === 'coverage') {
        try {
          const coverage = await api(`/api/samples/${state.currentSampleSetId}/coverage-results`);
          $('sampleOutput').value = JSON.stringify(coverage, null, 2);
          setSampleStatus('Coverage analysis complete.', 'ok');
        } catch (error) {
          setSampleStatus(error.message, 'error');
        }
      } else {
        setSampleStatus(`${state.samplePollPurpose} complete.`, 'ok');
      }
      return payload;
    }

    function startSamplePolling(sampleSetId, purpose) {
      stopSamplePolling();
      state.currentSampleSetId = sampleSetId;
      state.samplePollPurpose = purpose;
      state.samplePollTimer = window.setInterval(() => {
        pollSampleSet().catch((error) => setSampleStatus(error.message, 'error'));
      }, 1500);
      pollSampleSet().catch((error) => setSampleStatus(error.message, 'error'));
    }

    async function createSampleSet() {
      if (!state.currentRunId) return setSampleStatus('Select a run first.', 'error');
      const payload = await api('/api/samples/from-run', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ run_id: state.currentRunId })
      });
      state.currentSampleSetId = payload.sample_set.sample_set_id;
      $('sampleOutput').value = JSON.stringify(payload, null, 2);
      setSampleStatus(`Sample set created: ${state.currentSampleSetId}.`, 'ok');
      await loadWorkflowStatus();
      await loadDialogue();
      return payload;
    }

    async function analyzeCoverage(options = {}) {
      const managed = Boolean(options.managed);
      if (!state.currentSampleSetId) return setSampleStatus('Create a sample set first.', 'error');
      let geometryNote = '';
      try {
        const summaryPayload = await api(
          `/api/samples/${state.currentSampleSetId}/coverage-summary`
        );
        const summary = summaryPayload.summary || {};
        if (summary.approved_count && !summary.geocoded_count) {
          geometryNote = ' No geocoded observations yet; geometry review can improve steering.';
        } else if (summary.geocoded_count < summary.approved_count) {
          geometryNote =
            ` ${summary.geocoded_count}/${summary.approved_count} approved observations ` +
            'have geocoded points.';
        }
      } catch (_) {
        geometryNote = '';
      }
      const payload = await api(`/api/samples/${state.currentSampleSetId}/coverage-run`, {
        method: 'POST'
      });
      $('sampleOutput').value = JSON.stringify(payload, null, 2);
      setSampleStatus(`Coverage analysis started.${geometryNote}`, 'ok');
      if (payload.started && !managed) {
        startSamplePolling(state.currentSampleSetId, 'coverage');
      }
      return payload;
    }

    function pipelineDelay(milliseconds) {
      return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
    }

    async function waitForRunStage(purpose) {
      stopPolling();
      state.pollPurpose = purpose;
      while (state.fullPipelineActive) {
        const payload = await pollCurrentRun();
        if (!payload.active) return payload;
        await pipelineDelay(1500);
      }
      throw new Error('Full pipeline stopped.');
    }

    async function waitForSampleStage(purpose) {
      stopSamplePolling();
      state.samplePollPurpose = purpose;
      while (state.fullPipelineActive) {
        const payload = await pollSampleSet();
        if (!payload.active) return payload;
        await pipelineDelay(1500);
      }
      throw new Error('Full pipeline stopped.');
    }

    function setPipelineControlsDisabled(disabled) {
      for (const id of [
        'runFullPipelineButton',
        'runButton',
        'runQaqcButton',
        'runAddressButton',
        'geocodeButton',
        'createSampleButton',
        'analyzeCoverageButton'
      ]) {
        $(id).disabled = disabled;
      }
    }

    async function runFullPipeline() {
      if (state.fullPipelineActive) return;
      state.fullPipelineActive = true;
      state.currentSampleSetId = null;
      setPipelineControlsDisabled(true);
      setWorkspaceTab('workbench');
      try {
        setPipelineStatus(
          'Step 1 of 6 - Geographic review and harvest',
          'The Geographer Agent will adapt terminology before the harvest jobs begin.'
        );
        const harvestStart = await runHarvest({ managed: true });
        if (!harvestStart) throw new Error('The harvest could not be started.');
        const harvestStatus = await waitForRunStage('harvest');
        if (harvestStatus.manifest.status !== 'completed') {
          throw new Error(`Harvest ended with status: ${harvestStatus.manifest.status}.`);
        }

        setPipelineStatus(
          'Step 2 of 6 - QAQC',
          'Reviewing every harvested observation for evidence quality and geographic scope.'
        );
        await runQaqc({ managed: true });
        await waitForRunStage('qaqc');

        setPipelineStatus(
          'Step 3 of 6 - Address enrichment',
          'Improving facility addresses before coordinate assignment.'
        );
        await runAddressEnrichment({ managed: true });
        await waitForRunStage('address');

        setPipelineStatus(
          'Step 4 of 6 - Automated geocoding',
          'Assigning spatially validated coordinates to accepted observations.'
        );
        const geocodeSummary = await geocodeAcceptedObservations();
        if (!geocodeSummary) {
          throw new Error('No accepted observations were available for geocoding.');
        }

        setPipelineStatus(
          'Step 5 of 6 - Sample creation',
          'Combining the reviewed observations into a sample set.'
        );
        await createSampleSet();

        setPipelineStatus(
          'Step 6 of 6 - Coverage analysis',
          'Assessing geographic and facility coverage before any gap-fill work.'
        );
        await analyzeCoverage({ managed: true });
        await waitForSampleStage('coverage');
        const coverage = await api(
          `/api/samples/${state.currentSampleSetId}/coverage-results`
        );
        $('sampleOutput').value = JSON.stringify(coverage, null, 2);
        const interventionCount = Number($('interventionCount').textContent || 0);
        const reviewNote = interventionCount
          ? ` ${interventionCount} coordinate assignment(s) also need review in Geometry Studio.`
          : '';
        setPipelineStatus(
          'Coverage ready - human review required',
          'The automated pipeline is paused before gap fill. Review the coverage findings, ' +
            `then choose Run Gap Fill if the proposed work is appropriate.${reviewNote}`,
          'ok'
        );
        setSampleStatus(
          'Coverage analysis complete. Review the findings before running gap fill.',
          'ok'
        );
        await loadWorkflowStatus();
      } catch (error) {
        setPipelineStatus('Full pipeline stopped', error.message, 'error');
        setStatus(error.message, 'error');
      } finally {
        stopPolling();
        stopSamplePolling();
        state.fullPipelineActive = false;
        setPipelineControlsDisabled(false);
      }
    }

    async function runGapFill() {
      if (!state.currentSampleSetId) return setSampleStatus('Create a sample set first.', 'error');
      const payload = await api(`/api/samples/${state.currentSampleSetId}/gap-fill-run`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({})
      });
      $('sampleOutput').value = JSON.stringify(payload, null, 2);
      setSampleStatus('Gap-fill started.', 'ok');
      if (payload.started) startSamplePolling(state.currentSampleSetId, 'gap fill');
    }

    async function runSampleQaqcMissing() {
      if (!state.currentSampleSetId) return setSampleStatus('Create a sample set first.', 'error');
      const payload = await api(`/api/samples/${state.currentSampleSetId}/qaqc-missing`, {
        method: 'POST'
      });
      $('sampleOutput').value = JSON.stringify(payload, null, 2);
      setSampleStatus(
        payload.started ? 'Missing QAQC started.' : 'Missing QAQC pass complete.',
        'ok'
      );
      if (payload.started) startSamplePolling(state.currentSampleSetId, 'missing QAQC');
    }

    async function runSampleAddressMissing() {
      if (!state.currentSampleSetId) return setSampleStatus('Create a sample set first.', 'error');
      const payload = await api(`/api/samples/${state.currentSampleSetId}/address-missing`, {
        method: 'POST'
      });
      $('sampleOutput').value = JSON.stringify(payload, null, 2);
      setSampleStatus(
        payload.started ? 'Missing address enrichment started.' : 'Missing address pass complete.',
        'ok'
      );
      if (payload.started) startSamplePolling(state.currentSampleSetId, 'missing address');
    }

    function setGeometryStatus(message, kind = '') {
      $('geometryStatus').textContent = message;
      $('geometryStatus').className = `status ${kind}`;
    }

    function setAutomatedGeocodeStatus(message, kind = '') {
      setGeometryStatus(message, kind);
      setStatus(message, kind);
    }

    function initMap() {
      if (state.map || typeof L === 'undefined') return;
      state.map = L.map('map').setView([20, 0], 2);
      const streets = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19,
        attribution: '&copy; OpenStreetMap contributors'
      });
      const imagery = L.tileLayer(
        'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        {
          maxZoom: 19,
          attribution: 'Tiles &copy; Esri'
        }
      );
      imagery.addTo(state.map);
      L.control.layers({ Imagery: imagery, Streets: streets }).addTo(state.map);
      state.overviewFootprintLayer = new L.FeatureGroup();
      state.overviewPointLayer = new L.FeatureGroup();
      state.overviewExtentLayer = new L.FeatureGroup();
      state.map.addLayer(state.overviewFootprintLayer);
      state.map.addLayer(state.overviewPointLayer);
      state.map.addLayer(state.overviewExtentLayer);
      state.drawnItems = new L.FeatureGroup();
      state.map.addLayer(state.drawnItems);
      const drawControl = new L.Control.Draw({
        draw: {
          polygon: true,
          rectangle: false,
          polyline: false,
          circle: false,
          circlemarker: false,
          marker: false
        },
        edit: { featureGroup: state.drawnItems }
      });
      state.map.addControl(drawControl);
      state.map.on(L.Draw.Event.CREATED, (event) => {
        state.drawnItems.clearLayers();
        state.drawnItems.addLayer(event.layer);
        setGeometryStatus(
          'Footprint drawn. Select Save Footprint to calculate area and store its geometry.',
          'ok'
        );
      });
      state.map.on('click', (event) => {
        if (!state.coordinatePlacementMode) return;
        setMarker({
          latitude: event.latlng.lat,
          longitude: event.latlng.lng,
          source: 'user'
        });
        state.coordinatePlacementMode = false;
        $('map').classList.remove('placement-active');
        $('coordinateDraftStatus').textContent =
          'Draft coordinate placed. Drag the marker if needed, then select Save Coordinate.';
        setGeometryStatus('Draft coordinate placed on the map. Save it to confirm.', 'ok');
      });
    }

    function selectedGeometryItem() {
      return state.geometryItems.find((item) => item.item_id === state.selectedGeometryItemId);
    }

    function geometryRound(item) {
      return item.sample_round ? Number(item.sample_round) : 0;
    }

    function geometryRoundLabel(item) {
      const round = geometryRound(item);
      if (!round) return 'current run';
      return round === 1 ? 'round 1' : `gap-fill round ${round}`;
    }

    function geometryColor(item) {
      const round = geometryRound(item);
      if (round > 1) return '#d97706';
      if (round === 1) return '#2563eb';
      return '#16a34a';
    }

    function geometrySummary() {
      const rounds = new Set();
      let geocoded = 0;
      let footprints = 0;
      let intervention = 0;
      for (const item of state.geometryItems) {
        if (pointFromGeometry(item)) geocoded += 1;
        if (polygonFromGeometry(item)) footprints += 1;
        if (
          item.geometry_status !== 'skipped' &&
          item.geometry?.spatial_validation?.requires_human_intervention
        ) intervention += 1;
        rounds.add(geometryRoundLabel(item));
      }
      return {
        approved: state.geometryItems.length,
        geocoded,
        footprints,
        intervention,
        missing: Math.max(state.geometryItems.length - geocoded, 0),
        rounds: Array.from(rounds).join(', ') || 'none'
      };
    }

    function updateGeometrySummary() {
      const summary = geometrySummary();
      $('geometryExtentSummary').textContent =
        `Extent: ${summary.approved} approved, ${summary.geocoded} geocoded, ` +
        `${summary.footprints} footprint(s), ${summary.missing} missing point(s). ` +
        `${summary.intervention} need coordinate assignment. Rounds: ${summary.rounds}.`;
    }

    function renderInterventionQueue() {
      const items = state.geometryItems.filter(
        (item) =>
          item.geometry_status !== 'skipped' &&
          item.geometry?.spatial_validation?.requires_human_intervention
      );
      $('interventionCount').textContent = items.length;
      $('geometryTabBadge').textContent = items.length;
      $('geometryTabBadge').title = `${items.length} coordinate assignment(s) need review`;
      $('interventionList').innerHTML = items.map((item) => {
        const facility = item.lead?.location?.facility_name || item.item_id;
        const reason = item.geometry.spatial_validation.reason || 'Coordinate needs review.';
        return `<button class="secondary" type="button" data-intervention="` +
          `${escapeHtml(item.item_id)}">${escapeHtml(facility)} - ${escapeHtml(reason)}</button>`;
      }).join('') ||
        '<span class="workflow-summary">No observations currently require intervention.</span>';
      for (const button of $('interventionList').querySelectorAll('[data-intervention]')) {
        button.addEventListener('click', () => selectGeometryItem(button.dataset.intervention));
      }
    }

    function needsManualGeocoding(item) {
      if (!item || item.geometry_status === 'skipped') return false;
      if (pointFromGeometry(item)) return false;
      return (
        item.geometry_status === 'needs_review' ||
        Boolean(item.geometry?.spatial_validation?.requires_human_intervention)
      );
    }

    function geocodedGeometryItems() {
      return state.geometryItems.filter((item) => Boolean(pointFromGeometry(item)));
    }

    function manualGeometryItems() {
      return state.geometryItems.filter((item) => needsManualGeocoding(item));
    }

    function geometryItemsForActiveTab() {
      return state.geometryListTab === 'manual'
        ? manualGeometryItems()
        : geocodedGeometryItems();
    }

    function setGeometryListTab(tab) {
      state.geometryListTab = tab === 'manual' ? 'manual' : 'geocoded';
      renderGeometryList();
    }

    function chooseGeometryListTabForLoadedItems() {
      state.geometryListTab = manualGeometryItems().length ? 'manual' : 'geocoded';
    }

    function renderGeometryQueueTabs() {
      const geocodedCount = geocodedGeometryItems().length;
      const manualCount = manualGeometryItems().length;
      $('geocodedQueueCount').textContent = geocodedCount;
      $('manualQueueCount').textContent = manualCount;
      $('geocodedQueueTab').classList.toggle('active', state.geometryListTab === 'geocoded');
      $('manualQueueTab').classList.toggle('active', state.geometryListTab === 'manual');
      $('geocodedQueueTab').setAttribute(
        'aria-selected',
        String(state.geometryListTab === 'geocoded')
      );
      $('manualQueueTab').setAttribute(
        'aria-selected',
        String(state.geometryListTab === 'manual')
      );
    }

    function renderGeometryList() {
      const listedItems = geometryItemsForActiveTab();
      const emptyMessage = state.geometryListTab === 'manual'
        ? 'No observations need manual geocoding.'
        : 'No geocoded observations yet.';
      $('geometryList').innerHTML = listedItems.map((item) => {
        const lead = item.lead;
        const addressStatus = item.address_status || 'not_run';
        const label = `${lead.location.facility_name} - ${addressStatus} - ` +
          `${item.geometry_status}`;
        const active = item.item_id === state.selectedGeometryItemId ? ' active' : '';
        return `<button type="button" class="${active}" data-geometry="${item.item_id}">
          ${label}<br>${lead.location.city_or_region}, ${lead.location.country} -
          ${geometryRoundLabel(item)}
        </button>`;
      }).join('') || `<div class="status">${emptyMessage}</div>`;
      for (const button of $('geometryList').querySelectorAll('button[data-geometry]')) {
        button.addEventListener('click', () => selectGeometryItem(button.dataset.geometry));
      }
      renderInterventionQueue();
      renderGeometryQueueTabs();
      updateGeometrySummary();
    }

    function pointFromGeometry(item) {
      if (item.geometry && item.geometry.point) return item.geometry.point;
      return null;
    }

    function polygonFromGeometry(item) {
      if (item.geometry && item.geometry.polygon_geojson) return item.geometry.polygon_geojson;
      return null;
    }

    function setMarker(point) {
      initMap();
      if (!state.map || !point) return;
      if (state.marker) state.map.removeLayer(state.marker);
      state.marker = L.marker(
        [point.latitude, point.longitude],
        { draggable: true }
      ).addTo(state.map);
      state.map.setView([point.latitude, point.longitude], 18);
    }

    function clearMarker() {
      if (state.marker && state.map) state.map.removeLayer(state.marker);
      state.marker = null;
    }

    function overviewPopup(item) {
      const lead = item.lead;
      const counts = (lead.occupancy_data || [])
        .map((count) => `${count.count} ${count.group_type}`)
        .join(', ');
      return `<strong>${lead.location.facility_name}</strong><br>` +
        `${lead.location.city_or_region}, ${lead.location.country}<br>` +
        `${geometryRoundLabel(item)}<br>${counts}`;
    }

    function clearSampleExtent() {
      initMap();
      if (state.overviewPointLayer) state.overviewPointLayer.clearLayers();
      if (state.overviewFootprintLayer) state.overviewFootprintLayer.clearLayers();
      if (state.overviewExtentLayer) state.overviewExtentLayer.clearLayers();
      state.overviewBounds = null;
      state.sampleExtentVisible = false;
      updateGeometrySummary();
      setGeometryStatus('Sample extent cleared.', 'ok');
    }

    function renderSampleExtent(fit = false) {
      initMap();
      if (!state.map) return;
      state.sampleExtentVisible = true;
      state.overviewPointLayer.clearLayers();
      state.overviewFootprintLayer.clearLayers();
      state.overviewExtentLayer.clearLayers();
      const bounds = L.latLngBounds([]);
      let mapped = 0;
      for (const item of state.geometryItems) {
        const color = geometryColor(item);
        const point = pointFromGeometry(item);
        if (point) {
          const marker = L.circleMarker([point.latitude, point.longitude], {
            radius: 7,
            color,
            fillColor: color,
            fillOpacity: 0.8,
            weight: 2
          });
          marker.bindPopup(overviewPopup(item));
          marker.on('click', () => selectGeometryItem(item.item_id));
          marker.addTo(state.overviewPointLayer);
          bounds.extend([point.latitude, point.longitude]);
          mapped += 1;
        }
        const polygon = polygonFromGeometry(item);
        if (polygon) {
          const footprint = L.geoJSON(polygon, {
            style: {
              color,
              fillColor: color,
              fillOpacity: 0.16,
              weight: 2
            }
          });
          footprint.bindPopup(overviewPopup(item));
          footprint.on('click', () => selectGeometryItem(item.item_id));
          footprint.addTo(state.overviewFootprintLayer);
          const footprintBounds = footprint.getBounds();
          if (footprintBounds.isValid()) bounds.extend(footprintBounds);
          mapped += 1;
        }
      }
      if (!bounds.isValid()) {
        state.overviewBounds = null;
        updateGeometrySummary();
        return setGeometryStatus('No geocoded observations are available for an extent.', 'error');
      }
      state.overviewBounds = bounds;
      L.rectangle(bounds, {
        color: '#dc2626',
        fillOpacity: 0,
        dashArray: '6 6',
        weight: 2
      }).addTo(state.overviewExtentLayer);
      if (fit) state.map.fitBounds(bounds.pad(0.15));
      updateGeometrySummary();
      setGeometryStatus(`Sample extent shows ${mapped} mapped geometry layer(s).`, 'ok');
    }

    function zoomSampleExtent() {
      if (!state.sampleExtentVisible || !state.overviewBounds) renderSampleExtent(false);
      if (!state.overviewBounds || !state.overviewBounds.isValid()) {
        return setGeometryStatus('No geocoded observations are available for an extent.', 'error');
      }
      state.map.fitBounds(state.overviewBounds.pad(0.15));
      setGeometryStatus('Zoomed to sample extent.', 'ok');
    }

    function selectGeometryItem(itemId) {
      initMap();
      state.selectedGeometryItemId = itemId;
      renderGeometryList();
      const item = selectedGeometryItem();
      if (!item) return;
      state.coordinatePlacementMode = false;
      state.pendingCoordinatePreview = null;
      $('map').classList.remove('placement-active');
      $('pastedCoordinates').value = '';
      $('coordinatePasteStatus').className = 'status';
      $('coordinatePasteStatus').textContent =
        'Preview pasted coordinates before saving them.';
      $('manualAddress').value = item.geocode_query || '';
      $('coordinateReviewNotes').value = '';
      $('coordinateDraftStatus').textContent = pointFromGeometry(item)
        ? 'This observation already has a saved coordinate.'
        : 'No coordinate change is waiting to be saved.';
      $('resolutionReason').textContent = resolutionExplanation(item);
      setResolutionLink('resolutionSourceLink', item.lead?.source_url);
      setResolutionLink(
        'resolutionAddressLink',
        item.address_enrichment?.address_source_url
      );
      updateExternalSearchLinks(item);
      renderCandidateOptions(item);
      $('geometryDetail').value = JSON.stringify({
        item_id: item.item_id,
        facility: item.lead.location.facility_name,
        query: item.geocode_query,
        enriched_address: item.address_enrichment,
        source_url: item.lead.source_url,
        counts: item.lead.occupancy_data,
        qaqc: item.qaqc_review.review_notes,
        address_status: item.address_status,
        geometry_status: item.geometry_status,
        spatial_validation: item.geometry?.spatial_validation || null,
        area_m2: item.area_m2
      }, null, 2);
      if (state.drawnItems) state.drawnItems.clearLayers();
      const point = pointFromGeometry(item);
      if (point) setMarker(point);
      else clearMarker();
      const polygon = polygonFromGeometry(item);
      if (polygon && state.drawnItems) {
        const layer = L.geoJSON(polygon).getLayers()[0];
        state.drawnItems.addLayer(layer);
        state.map.fitBounds(layer.getBounds());
      }
    }

    function setResolutionLink(elementId, url) {
      const link = $(elementId);
      const usable = typeof url === 'string' && new RegExp('^https?://', 'i').test(url);
      link.classList.toggle('hidden', !usable);
      if (usable) link.href = url;
      else link.removeAttribute('href');
    }

    function facilitySearchQuery(item) {
      const location = item?.lead?.location || {};
      return [
        location.facility_name,
        item?.address_enrichment?.formatted_address,
        location.city_or_region,
        location.country
      ].filter(Boolean).join(', ');
    }

    function updateExternalSearchLinks(item) {
      const query = facilitySearchQuery(item);
      setResolutionLink(
        'googleSearchLink',
        query ? `https://www.google.com/search?q=${encodeURIComponent(query)}` : null
      );
      setResolutionLink(
        'googleMapsLink',
        query
          ? `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(query)}`
          : null
      );
    }

    function candidateOptionsForItem(item) {
      const validation = item?.geometry?.spatial_validation;
      const current = Array.isArray(validation?.candidate_options)
        ? validation.candidate_options
        : [];
      const initial = Array.isArray(validation?.initial_validation?.candidate_options)
        ? validation.initial_validation.candidate_options
        : [];
      const seen = new Set();
      return [...current, ...initial].filter((candidate) => {
        const key = `${candidate.latitude},${candidate.longitude}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return candidate.latitude != null && candidate.longitude != null;
      }).slice(0, 5);
    }

    function renderCandidateOptions(item) {
      state.selectedCandidateOptions = candidateOptionsForItem(item);
      $('candidateOptions').innerHTML = state.selectedCandidateOptions.map(
        (candidate, index) => {
          const confidence = ['likely', 'possible', 'conflicting'].includes(
            candidate.confidence
          ) ? candidate.confidence : 'possible';
          const reason = Array.isArray(candidate.match_summary)
            ? candidate.match_summary.join(' ')
            : (candidate.scope_reason || 'Candidate requires human review.');
          const acceptDisabled = candidate.scope_status === 'out_of_scope'
            ? ' disabled title="Outside the requested geographic scope"'
            : '';
          return `<div class="candidate-card ${confidence}">
            <div class="candidate-heading">
              <span>${escapeHtml(candidate.display_name || 'Unnamed candidate')}</span>
              <span class="candidate-badge">${escapeHtml(confidence)}</span>
            </div>
            <div class="candidate-reason">${escapeHtml(reason)}</div>
            <div class="candidate-reason">
              ${escapeHtml(candidate.provider || 'geocoder')} · score
              ${escapeHtml(candidate.score ?? '—')}
            </div>
            <div class="actions">
              <button class="secondary" type="button" data-view-candidate="${index}">
                View on map
              </button>
              <button type="button" data-accept-candidate="${index}"${acceptDisabled}>
                Accept this location
              </button>
            </div>
          </div>`;
        }
      ).join('') || `<span class="workflow-summary">
        No ranked candidates are available yet. Search a corrected address or select
        Research This Facility.
      </span>`;
      for (const button of $('candidateOptions').querySelectorAll('[data-view-candidate]')) {
        button.addEventListener('click', () => viewCandidate(Number(button.dataset.viewCandidate)));
      }
      for (
        const button of $('candidateOptions').querySelectorAll('[data-accept-candidate]')
      ) {
        button.addEventListener('click', () => {
          acceptCandidate(Number(button.dataset.acceptCandidate)).catch(
            (error) => setGeometryStatus(error.message, 'error')
          );
        });
      }
    }

    function viewCandidate(index) {
      const candidate = state.selectedCandidateOptions[index];
      if (!candidate || !state.map) return;
      state.map.setView([candidate.latitude, candidate.longitude], 17);
      $('coordinateDraftStatus').textContent =
        'Candidate centered for inspection; no coordinate has been assigned.';
      setGeometryStatus('Candidate centered on the map for review.', 'ok');
    }

    function resolutionExplanation(item) {
      const validation = item.geometry?.spatial_validation;
      if (!validation) {
        return 'Automatic coordinate assignment has not reported a validation result.';
      }
      const lines = [
        `Status: ${validation.status || 'unknown'}`,
        `Reason: ${validation.reason || 'No reason was recorded.'}`
      ];
      const initial = validation.initial_validation;
      if (initial?.reason) lines.push(`Initial result: ${initial.reason}`);
      const retry = validation.address_retry;
      if (retry?.address?.formatted_address) {
        lines.push(`Address retry: ${retry.address.formatted_address}`);
      } else if (retry?.reason) {
        lines.push(`Address retry: ${retry.reason}`);
      }
      const attempts = [
        ...(Array.isArray(initial?.attempts) ? initial.attempts : []),
        ...(Array.isArray(validation.attempts) ? validation.attempts : [])
      ];
      if (attempts.length) {
        lines.push('Automatic attempts:');
        for (const attempt of attempts) {
          lines.push(`- ${attempt.query}: ${attempt.reason || attempt.status}`);
        }
      }
      return lines.join('\\n');
    }

    async function loadApprovedGeometry() {
      if (!state.currentRunId) return setGeometryStatus('No run selected.', 'error');
      initMap();
      const payload = await api(`/api/runs/${state.currentRunId}/geometry-items`);
      state.geometryItems = payload.items || [];
      $('jsonOutput').value = JSON.stringify(state.geometryItems, null, 2);
      chooseGeometryListTabForLoadedItems();
      state.selectedGeometryItemId =
        geometryItemsForActiveTab()[0]?.item_id || state.geometryItems[0]?.item_id || null;
      renderGeometryList();
      if (state.selectedGeometryItemId) selectGeometryItem(state.selectedGeometryItemId);
      else {
        clearMarker();
        if (state.drawnItems) state.drawnItems.clearLayers();
        $('geometryDetail').value = '';
        setResolutionLink('resolutionSourceLink', null);
        setResolutionLink('resolutionAddressLink', null);
        setResolutionLink('googleSearchLink', null);
        setResolutionLink('googleMapsLink', null);
        renderCandidateOptions(null);
      }
      setGeometryStatus(`Loaded ${state.geometryItems.length} QAQC-approved observation(s).`, 'ok');
      if (state.sampleExtentVisible) renderSampleExtent(false);
    }

    async function loadGeometryItemsForAutomatedGeocoding() {
      const usingSample = Boolean(state.currentSampleSetId);
      if (!usingSample && !state.currentRunId) {
        setAutomatedGeocodeStatus('Select a run before geocoding.', 'error');
        return false;
      }
      const path = usingSample
        ? `/api/samples/${state.currentSampleSetId}/geometry-items`
        : `/api/runs/${state.currentRunId}/geometry-items`;
      const payload = await api(path);
      state.geometryItems = payload.items || [];
      chooseGeometryListTabForLoadedItems();
      state.selectedGeometryItemId =
        geometryItemsForActiveTab()[0]?.item_id || state.geometryItems[0]?.item_id || null;
      renderGeometryList();
      $('jsonOutput').value = JSON.stringify(state.geometryItems, null, 2);
      setAutomatedGeocodeStatus(
        `Prepared ${state.geometryItems.length} accepted observation(s) for geocoding.`,
        'ok'
      );
      return true;
    }

    async function geocodeAcceptedObservations() {
      if (!(await loadGeometryItemsForAutomatedGeocoding())) return null;
      await loadWorkflowStatus();
      return geocodeAll();
    }

    async function loadAugmentedSampleGeometry() {
      if (!state.currentSampleSetId) return setGeometryStatus('No sample set selected.', 'error');
      initMap();
      const payload = await api(`/api/samples/${state.currentSampleSetId}/geometry-items`);
      state.geometryItems = payload.items || [];
      chooseGeometryListTabForLoadedItems();
      state.selectedGeometryItemId =
        geometryItemsForActiveTab()[0]?.item_id || state.geometryItems[0]?.item_id || null;
      renderGeometryList();
      if (state.selectedGeometryItemId) selectGeometryItem(state.selectedGeometryItemId);
      else {
        clearMarker();
        if (state.drawnItems) state.drawnItems.clearLayers();
        $('geometryDetail').value = '';
        setResolutionLink('resolutionSourceLink', null);
        setResolutionLink('resolutionAddressLink', null);
        setResolutionLink('googleSearchLink', null);
        setResolutionLink('googleMapsLink', null);
        renderCandidateOptions(null);
      }
      setGeometryStatus(
        `Loaded ${state.geometryItems.length} augmented sample observation(s).`,
        'ok'
      );
      if (state.sampleExtentVisible) renderSampleExtent(true);
    }

    function currentPointPayload(source = 'user') {
      if (!state.marker) return null;
      const latlng = state.marker.getLatLng();
      return { latitude: latlng.lat, longitude: latlng.lng, source };
    }

    function currentPolygonGeojson() {
      if (!state.drawnItems) return null;
      const layers = state.drawnItems.getLayers();
      if (!layers.length) return null;
      return layers[0].toGeoJSON().geometry;
    }

    async function geocodeSelected(queryOverride = null) {
      const item = selectedGeometryItem();
      if (!item) return setGeometryStatus('No approved observation selected.', 'error');
      const query = (queryOverride || item.geocode_query || '').trim();
      if (!query) return setGeometryStatus('No address query available.', 'error');
      const payload = await api('/api/geometry/geocode', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          item_id: item.item_id,
          query,
          allow_address_retry: false,
          conversation_id: state.currentRunId
        })
      });
      item.geometry = payload.geometry;
      item.geometry_status = payload.geometry.geometry_status;
      item.geometries = payload.geometry.geometries || [];
      item.area_m2 = payload.geometry.area_m2;
      item.geocode_query = query;
      if (payload.geometry.point) {
        setMarker(payload.geometry.point);
        state.geometryListTab = 'geocoded';
      } else {
        state.geometryListTab = 'manual';
        const assessment = (payload.spatial_validation.assessments || []).find(
          (candidate) => candidate.latitude != null && candidate.longitude != null
        );
        if (assessment && state.map) {
          state.map.setView([assessment.latitude, assessment.longitude], 14);
          $('coordinateDraftStatus').textContent =
            'A rejected candidate was used only to center the map; no coordinate was assigned.';
        }
      }
      renderGeometryList();
      $('resolutionReason').textContent = resolutionExplanation(item);
      renderCandidateOptions(item);
      if (state.sampleExtentVisible) renderSampleExtent(false);
      setGeometryStatus(
        payload.geocode_result
          ? 'Geocode placed an in-scope facility point.'
          : `Coordinate assignment requires human review: ${payload.spatial_validation.reason}`,
        payload.geocode_result ? 'ok' : ''
      );
    }

    async function researchSelectedFacility() {
      const item = selectedGeometryItem();
      if (!item) return setGeometryStatus('No approved observation selected.', 'error');
      state.pendingCoordinatePreview = null;
      const button = $('researchFacilityButton');
      button.disabled = true;
      setGeometryStatus(
        `Researching ${item.lead?.location?.facility_name || item.item_id}…`,
        'ok'
      );
      $('coordinateDraftStatus').textContent =
        'The address-spatial agent is researching official facility evidence.';
      try {
        const payload = await api('/api/geometry/research', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({
            item_id: item.item_id,
            conversation_id: state.currentRunId
          })
        });
        item.geometry = payload.geometry;
        item.geometry_status = payload.geometry.geometry_status;
        item.geometries = payload.geometry.geometries || [];
        item.area_m2 = payload.geometry.area_m2;
        item.geocode_query = payload.geometry.geocode_query;
        if (payload.address_retry?.address) {
          item.address_enrichment = payload.address_retry.address;
          item.address_status = payload.address_retry.address.status;
          $('manualAddress').value =
            payload.address_retry.address.formatted_address || item.geocode_query;
        }
        if (payload.research_resolved && payload.geometry.point) {
          state.geometryListTab = 'geocoded';
          setMarker(payload.geometry.point);
          $('coordinateDraftStatus').textContent =
            'Focused research produced and saved an in-scope coordinate.';
        } else {
          state.geometryListTab = 'manual';
          $('coordinateDraftStatus').textContent =
            'Focused research completed. Review the ranked candidates below.';
        }
        renderGeometryList();
        $('resolutionReason').textContent = resolutionExplanation(item);
        updateExternalSearchLinks(item);
        renderCandidateOptions(item);
        await loadDialogue();
        setGeometryStatus(
          payload.research_resolved
            ? 'Focused research resolved this coordinate.'
            : 'Focused research completed; candidate selection is ready.',
          'ok'
        );
      } finally {
        button.disabled = false;
      }
    }

    async function acceptCandidate(index) {
      const item = selectedGeometryItem();
      const candidate = state.selectedCandidateOptions[index];
      if (!item || !candidate) {
        return setGeometryStatus('The selected candidate is no longer available.', 'error');
      }
      if (candidate.scope_status === 'out_of_scope') {
        return setGeometryStatus(
          'This candidate is outside the requested geographic scope and cannot be accepted.',
          'error'
        );
      }
      state.pendingCoordinatePreview = null;
      const point = {
        latitude: candidate.latitude,
        longitude: candidate.longitude,
        source: `${candidate.provider || 'geocoder'}-human`
      };
      setMarker(point);
      const spatialValidation = {
        ...(item.geometry?.spatial_validation || {}),
        status: 'human_accepted_candidate',
        requires_human_intervention: false,
        reason: 'A human reviewer accepted a ranked geocoder candidate.',
        accepted_candidate: candidate
      };
      await saveGeometry('point_confirmed', {
        point,
        geocodeResult: candidate.geocode_result || candidate,
        spatialValidation,
        reviewNotes:
          `Human accepted ${candidate.display_name || 'a ranked geocoder candidate'}.`
      });
    }

    async function geocodeAll() {
      if (!state.geometryItems.length) {
        setAutomatedGeocodeStatus('No accepted observations are available to geocode.', 'error');
        return null;
      }
      const alreadyPositioned = state.geometryItems.filter(
        (item) => pointFromGeometry(item)
      ).length;
      const pending = state.geometryItems.filter(
        (item) => !pointFromGeometry(item) && (item.geocode_query || '').trim()
      );
      const missingQuery = state.geometryItems.filter(
        (item) => !pointFromGeometry(item) && !(item.geocode_query || '').trim()
      ).length;
      if (!pending.length) {
        setAutomatedGeocodeStatus(
          `No observations need geocoding. ${alreadyPositioned} already have points` +
          `${missingQuery ? `; ${missingQuery} have no address query` : ''}.`,
          'ok'
        );
        return {
          geocoded: 0,
          attempted: 0,
          already_positioned: alreadyPositioned,
          missing_query: missingQuery,
          needs_human_review: 0,
          errors: 0
        };
      }
      const button = $('geocodeButton');
      button.disabled = true;
      let geocodedCount = 0;
      let notFoundCount = 0;
      let humanReviewCount = 0;
      let errorCount = 0;
      try {
        renderGeocodingProgress({
          attempted: 0,
          total: pending.length,
          geocoded: 0,
          humanReview: 0,
          errors: 0,
          working: true
        });
        for (let index = 0; index < pending.length; index += 1) {
          const item = pending[index];
          renderGeocodingProgress({
            attempted: index,
            total: pending.length,
            geocoded: geocodedCount,
            humanReview: humanReviewCount,
            errors: errorCount,
            working: true
          });
          setAutomatedGeocodeStatus(
            `Geocoding ${index + 1}/${pending.length}: ${item.geocode_query.trim()}`
          );
          try {
            const payload = await api('/api/geometry/geocode', {
              method: 'POST',
              headers: { 'content-type': 'application/json' },
              body: JSON.stringify({
                item_id: item.item_id,
                query: item.geocode_query.trim(),
                allow_address_retry: true,
                conversation_id: state.currentRunId
              })
            });
            item.geometry = payload.geometry;
            item.geometry_status = payload.geometry.geometry_status;
            item.geometries = payload.geometry.geometries || [];
            item.area_m2 = payload.geometry.area_m2;
            item.geocode_query = payload.geometry.geocode_query;
            if (payload.address_retry?.address) {
              item.address_enrichment = payload.address_retry.address;
              item.address_status = payload.address_retry.address.status;
            }
            if (payload.geocode_result) geocodedCount += 1;
            else {
              notFoundCount += 1;
              if (payload.spatial_validation.requires_human_intervention) {
                humanReviewCount += 1;
              }
            }
          } catch (_) {
            errorCount += 1;
          }
          renderGeometryList();
          updateGeometrySummary();
          renderGeocodingProgress({
            attempted: index + 1,
            total: pending.length,
            geocoded: geocodedCount,
            humanReview: humanReviewCount,
            errors: errorCount,
            working: false
          });
        }
        const selected = selectedGeometryItem();
        if (
          state.activeWorkspace === 'geometry' &&
          selected &&
          pointFromGeometry(selected)
        ) {
          setMarker(pointFromGeometry(selected));
        }
        state.geometryListTab = manualGeometryItems().length ? 'manual' : 'geocoded';
        renderGeometryList();
        updateGeometrySummary();
        if (state.activeWorkspace === 'geometry' && state.sampleExtentVisible) {
          renderSampleExtent(false);
        }
        const suffix = [
          `${notFoundCount} not found`,
          `${humanReviewCount} need human coordinate assignment`,
          `${errorCount} error(s)`,
          `${alreadyPositioned} already positioned`,
          `${missingQuery} without an address query`
        ].join(', ');
        setAutomatedGeocodeStatus(
          `Geocoded ${geocodedCount} of ${pending.length} observation(s). ` +
          suffix + '.',
          errorCount ? 'error' : 'ok'
        );
        await loadWorkflowStatus();
        await loadDialogue();
        return {
          geocoded: geocodedCount,
          attempted: pending.length,
          already_positioned: alreadyPositioned,
          missing_query: missingQuery,
          needs_human_review: humanReviewCount,
          errors: errorCount
        };
      } finally {
        button.disabled = state.fullPipelineActive;
      }
    }

    async function searchManualAddress() {
      const query = $('manualAddress').value.trim();
      if (!query) return setGeometryStatus('Enter an address or place to search.', 'error');
      state.pendingCoordinatePreview = null;
      await geocodeSelected(query);
    }

    async function previewPastedCoordinates() {
      const item = selectedGeometryItem();
      if (!item) return setGeometryStatus('No approved observation selected.', 'error');
      const coordinateText = $('pastedCoordinates').value.trim();
      if (!coordinateText) {
        return setGeometryStatus('Paste coordinates or a Google Maps URL first.', 'error');
      }
      const button = $('previewCoordinatesButton');
      button.disabled = true;
      try {
        const payload = await api('/api/geometry/coordinate-preview', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({
            item_id: item.item_id,
            coordinate_text: coordinateText
          })
        });
        state.pendingCoordinatePreview = payload;
        setMarker(payload.point);
        const validation = payload.spatial_validation || {};
        const messages = [
          `Parsed coordinate: ${payload.normalized}.`,
          validation.reason || 'Geographic scope was not verified.'
        ];
        if (payload.reversed_order) {
          messages.push('Longitude and latitude appeared reversed and were corrected.');
        }
        $('coordinatePasteStatus').className =
          `status ${validation.warning ? 'error' : 'ok'}`;
        $('coordinatePasteStatus').textContent = messages.join(' ');
        $('coordinateDraftStatus').textContent = validation.warning
          ? 'Coordinate previewed with a warning. Verify the marker before saving.'
          : 'Coordinate previewed and passed the available geographic checks.';
        setGeometryStatus(
          validation.warning
            ? 'Coordinate previewed with a geographic warning.'
            : 'Coordinate previewed successfully. Select Save Coordinate to confirm.',
          validation.warning ? 'error' : 'ok'
        );
      } finally {
        button.disabled = false;
      }
    }

    function useMapCenter() {
      const item = selectedGeometryItem();
      if (!item || !state.map) {
        return setGeometryStatus('No approved observation selected.', 'error');
      }
      const center = state.map.getCenter();
      state.pendingCoordinatePreview = null;
      setMarker({ latitude: center.lat, longitude: center.lng, source: 'user' });
      state.coordinatePlacementMode = false;
      $('map').classList.remove('placement-active');
      $('coordinateDraftStatus').textContent =
        'Draft coordinate placed at the map center. Select Save Coordinate to confirm it.';
      setGeometryStatus('Point set from map center.', 'ok');
    }

    function startPointPlacement() {
      const item = selectedGeometryItem();
      if (!item || !state.map) {
        return setGeometryStatus('Select an observation before placing a point.', 'error');
      }
      state.pendingCoordinatePreview = null;
      state.coordinatePlacementMode = true;
      $('map').classList.add('placement-active');
      $('coordinateDraftStatus').textContent =
        'Placement mode active: click the facility location on the map.';
      setGeometryStatus('Click the facility location on the map.', 'ok');
    }

    async function saveCoordinate() {
      if (!currentPointPayload()) {
        return setGeometryStatus(
          'Place a point on the map or search a corrected address before saving.',
          'error'
        );
      }
      if (state.pendingCoordinatePreview) {
        const preview = state.pendingCoordinatePreview;
        const item = selectedGeometryItem();
        const previewValidation = preview.spatial_validation || {};
        const savedPoint = currentPointPayload(preview.point.source);
        const markerMoved = Math.abs(savedPoint.latitude - preview.point.latitude) > 1e-7 ||
          Math.abs(savedPoint.longitude - preview.point.longitude) > 1e-7;
        const savedNormalized =
          `${savedPoint.latitude.toFixed(7)}, ${savedPoint.longitude.toFixed(7)}`;
        await saveGeometry('point_confirmed', {
          point: savedPoint,
          geocodeResult:
            preview.reverse_geocode_result || item?.geometry?.geocode_result || null,
          spatialValidation: {
            ...(item?.geometry?.spatial_validation || {}),
            status: 'human_pasted_coordinate',
            requires_human_intervention: false,
            reason: previewValidation.warning
              ? (
                'A human reviewer saved a pasted Google Maps coordinate after reviewing ' +
                `this warning: ${previewValidation.reason}`
              )
              : (
                'A human reviewer saved a pasted Google Maps coordinate after preview.' +
                (markerMoved ? ' The reviewer adjusted the marker after preview.' : '')
              ),
            pasted_coordinate_validation: previewValidation,
            pasted_coordinate_text: $('pastedCoordinates').value.trim(),
            normalized_coordinate: savedNormalized,
            marker_adjusted_after_preview: markerMoved
          },
          reviewNotes: $('coordinateReviewNotes').value.trim() || (
            `Human-reviewed Google Maps coordinate: ${savedNormalized}.`
          )
        });
        state.pendingCoordinatePreview = null;
        $('coordinatePasteStatus').className = 'status ok';
        $('coordinatePasteStatus').textContent =
          `Saved Google Maps coordinate ${savedNormalized}.`;
        return;
      }
      await saveGeometry('point_confirmed');
    }

    async function saveFootprint() {
      if (!currentPolygonGeojson()) {
        return setGeometryStatus(
          'Draw a building polygon on the map before saving a footprint.',
          'error'
        );
      }
      await saveGeometry('footprint_drawn');
    }

    async function saveGeometry(status = null, overrides = {}) {
      const item = selectedGeometryItem();
      if (!item) return setGeometryStatus('No approved observation selected.', 'error');
      const polygon = currentPolygonGeojson();
      const point = overrides.point || currentPointPayload();
      const geometryStatus = status || (polygon ? 'footprint_drawn' : 'point_confirmed');
      const payload = await api(`/api/geometry/items/${item.item_id}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          item_id: item.item_id,
          geocode_query: item.geocode_query,
          point,
          polygon_geojson: polygon,
          geometry_status: geometryStatus,
          geocode_result: overrides.geocodeResult ?? item.geometry?.geocode_result ?? null,
          spatial_validation:
            overrides.spatialValidation ?? item.geometry?.spatial_validation ?? null,
          review_notes: overrides.reviewNotes || (
            geometryStatus === 'skipped'
              ? 'Skipped in geometry review.'
              : ($('coordinateReviewNotes').value.trim() || null)
          ),
          conversation_id: state.currentRunId
        })
      });
      item.geometry = payload.geometry;
      item.geometry_status = payload.geometry.geometry_status;
      item.geometries = payload.geometry.geometries || [];
      item.area_m2 = payload.geometry.area_m2;
      if (geometryStatus === 'point_confirmed' && pointFromGeometry(item)) {
        state.geometryListTab = 'geocoded';
      } else if (needsManualGeocoding(item)) {
        state.geometryListTab = 'manual';
      }
      renderGeometryList();
      $('resolutionReason').textContent = resolutionExplanation(item);
      renderCandidateOptions(item);
      $('coordinateDraftStatus').textContent =
        geometryStatus === 'point_confirmed'
          ? 'Coordinate saved and removed from the intervention queue.'
          : 'Geometry saved.';
      $('jsonOutput').value = JSON.stringify(state.geometryItems, null, 2);
      if (state.sampleExtentVisible) renderSampleExtent(false);
      selectGeometryItem(item.item_id);
      const areaMessage = item.area_m2 == null
        ? ''
        : ` Area: ${Math.round(item.area_m2).toLocaleString()} square meters.`;
      setGeometryStatus(`Geometry saved: ${item.geometry_status}.${areaMessage}`, 'ok');
      await loadWorkflowStatus();
      await loadDialogue();
    }

    async function exitApplication() {
      if (!window.confirm('Exit OASIS and cancel active harvests?')) return;
      try {
        await api('/api/app/exit', { method: 'POST' });
      } catch (_) {
        // The server may close before the browser receives the response.
      }
      stopPolling();
      setStatus('Server shutting down.', 'ok');
      $('activityOutput').value += '\\nServer shutting down. You may close this tab.\\n';
    }

    function downloadText(filename, text, type) {
      const blob = new Blob([text], { type });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      link.click();
      URL.revokeObjectURL(url);
    }

    async function downloadExport(format) {
      if (!state.currentRunId) return setStatus('No run selected.', 'error');
      const response = await fetch(`/api/runs/${state.currentRunId}/export.verified.${format}`);
      if (!response.ok) return setStatus(await response.text(), 'error');
      downloadText(
        `observation-harvest.verified.${format}`,
        await response.text(),
        format === 'csv' ? 'text/csv' : 'application/json'
      );
    }

    async function downloadFootprints() {
      if (!state.currentRunId) return setGeometryStatus('No run selected.', 'error');
      const response = await fetch(`/api/runs/${state.currentRunId}/export.footprints.geojson`);
      if (!response.ok) return setGeometryStatus(await response.text(), 'error');
      downloadText(
        'observation-footprints.geojson',
        await response.text(),
        'application/geo+json'
      );
    }

    async function downloadSampleExport(format) {
      if (!state.currentSampleSetId) return setSampleStatus('No sample set selected.', 'error');
      const response = await fetch(
        `/api/samples/${state.currentSampleSetId}/export.verified.${format}`
      );
      if (!response.ok) return setSampleStatus(await response.text(), 'error');
      downloadText(
        `observation-sample.verified.${format}`,
        await response.text(),
        format === 'csv' ? 'text/csv' : 'application/json'
      );
    }

    async function downloadSampleFootprints() {
      if (!state.currentSampleSetId) return setGeometryStatus('No sample set selected.', 'error');
      const response = await fetch(
        `/api/samples/${state.currentSampleSetId}/export.footprints.geojson`
      );
      if (!response.ok) return setGeometryStatus(await response.text(), 'error');
      downloadText(
        'observation-sample-footprints.geojson',
        await response.text(),
        'application/geo+json'
      );
    }

    async function boot() {
      initTheme();
      setWorkspaceTab('workbench');
      renderWorkflow(null);
      const payload = await api('/api/profiles');
      state.profiles = payload.profile_sets;
      renderProfileSets();
      await loadRuns();
      $('themeSelect').addEventListener('change', () => {
        localStorage.setItem('observationHarvesterTheme', $('themeSelect').value);
        applyTheme($('themeSelect').value);
      });
      $('workbenchTab').addEventListener('click', () => setWorkspaceTab('workbench'));
      $('geometryTab').addEventListener('click', () => setWorkspaceTab('geometry'));
      $('profileSet').addEventListener('change', renderProfiles);
      $('singleMode').addEventListener('click', () => setMode('single'));
      $('batchMode').addEventListener('click', () => setMode('batch'));
      $('campaignMode').addEventListener('click', () => setMode('campaign'));
      $('runFullPipelineButton').addEventListener('click', runFullPipeline);
      $('runButton').addEventListener('click', runHarvest);
      $('refreshButton').addEventListener('click', loadRuns);
      $('clearRunsButton').addEventListener('click', () => {
        clearRuns().catch((error) => setStatus(error.message, 'error'));
      });
      $('cancelButton').addEventListener('click', cancelRun);
      $('exitButton').addEventListener('click', exitApplication);
      $('copyButton').addEventListener('click', async () => {
        await navigator.clipboard.writeText($('jsonOutput').value);
        setStatus('JSON copied.', 'ok');
      });
      $('copyQaqcButton').addEventListener('click', copyQaqcPrompt);
      $('downloadTranscriptButton').addEventListener('click', () => {
        downloadTranscript().catch((error) => setStatus(error.message, 'error'));
      });
      $('runQaqcButton').addEventListener('click', runQaqc);
      $('runAddressButton').addEventListener('click', runAddressEnrichment);
      $('workflowNextButton').addEventListener('click', () => {
        workflowAction($('workflowNextButton').dataset.action);
      });
      $('workflowSteps').addEventListener('click', (event) => {
        const button = event.target.closest('[data-workflow-action]');
        if (button) workflowAction(button.dataset.workflowAction);
      });
      $('createSampleButton').addEventListener('click', () => {
        createSampleSet().catch((error) => setSampleStatus(error.message, 'error'));
      });
      $('analyzeCoverageButton').addEventListener('click', () => {
        analyzeCoverage().catch((error) => setSampleStatus(error.message, 'error'));
      });
      $('runGapFillButton').addEventListener('click', () => {
        runGapFill().catch((error) => setSampleStatus(error.message, 'error'));
      });
      $('runSampleQaqcButton').addEventListener('click', () => {
        runSampleQaqcMissing().catch((error) => setSampleStatus(error.message, 'error'));
      });
      $('runSampleAddressButton').addEventListener('click', () => {
        runSampleAddressMissing().catch((error) => setSampleStatus(error.message, 'error'));
      });
      $('downloadSampleJsonButton').addEventListener('click', () => downloadSampleExport('json'));
      $('downloadSampleCsvButton').addEventListener('click', () => downloadSampleExport('csv'));
      $('downloadJsonButton').addEventListener('click', () => downloadExport('json'));
      $('downloadCsvButton').addEventListener('click', () => downloadExport('csv'));
      $('loadApprovedButton').addEventListener('click', () => {
        loadApprovedGeometry().catch((error) => setGeometryStatus(error.message, 'error'));
      });
      $('loadAugmentedSampleButton').addEventListener('click', () => {
        loadAugmentedSampleGeometry().catch((error) => setGeometryStatus(error.message, 'error'));
      });
      $('geocodedQueueTab').addEventListener('click', () => setGeometryListTab('geocoded'));
      $('manualQueueTab').addEventListener('click', () => setGeometryListTab('manual'));
      $('geocodeButton').addEventListener('click', () => {
        geocodeAcceptedObservations().catch(
          (error) => setAutomatedGeocodeStatus(error.message, 'error')
        );
      });
      $('searchAddressButton').addEventListener('click', () => {
        searchManualAddress().catch((error) => setGeometryStatus(error.message, 'error'));
      });
      $('researchFacilityButton').addEventListener('click', () => {
        researchSelectedFacility().catch(
          (error) => setGeometryStatus(error.message, 'error')
        );
      });
      $('previewCoordinatesButton').addEventListener('click', () => {
        previewPastedCoordinates().catch(
          (error) => setGeometryStatus(error.message, 'error')
        );
      });
      $('placePointButton').addEventListener('click', startPointPlacement);
      $('useMapCenterButton').addEventListener('click', useMapCenter);
      $('saveCoordinateButton').addEventListener('click', () => {
        saveCoordinate().catch((error) => setGeometryStatus(error.message, 'error'));
      });
      $('saveFootprintButton').addEventListener('click', () => {
        saveFootprint().catch((error) => setGeometryStatus(error.message, 'error'));
      });
      $('skipGeometryButton').addEventListener('click', () => {
        saveGeometry('skipped').catch((error) => setGeometryStatus(error.message, 'error'));
      });
      $('showSampleExtentButton').addEventListener('click', () => renderSampleExtent(true));
      $('zoomSampleExtentButton').addEventListener('click', zoomSampleExtent);
      $('clearSampleExtentButton').addEventListener('click', clearSampleExtent);
      $('downloadVerifiedJsonButton').addEventListener('click', () => downloadExport('json'));
      $('downloadVerifiedCsvButton').addEventListener('click', () => downloadExport('csv'));
      $('downloadFootprintsButton').addEventListener('click', downloadFootprints);
      $('downloadSampleFootprintsButton').addEventListener('click', downloadSampleFootprints);
    }
    boot().catch((error) => setStatus(error.message, 'error'));
  </script>
</body>
</html>
"""

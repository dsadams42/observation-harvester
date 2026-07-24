from __future__ import annotations

import asyncio
import os
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
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from pdt_observer.geometry import (
    Geocoder,
    NominatimGeocoder,
    approved_records_for_child,
    footprints_geojson,
    geometry_item_from_payload,
    merge_geometry_items,
    save_geometry_review_item,
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
    GeometryPoint,
    GeometryStatus,
    HarvestBatchRunManifest,
    HarvestCampaignRunManifest,
    HarvestRunManifest,
)
from pdt_observer.profiles import BUILTIN_PROFILE_SETS
from pdt_observer.workflow import write_model


class HarvestRunRequest(BaseModel):
    country: str = Field(min_length=2)
    locality: str | None = None
    profiles: str = "schools"
    profile: str | None = None
    target: int = Field(default=20, ge=1)


class HarvestBatchRunRequest(BaseModel):
    country: str = Field(min_length=2)
    locality: str | None = None
    profiles: str = "schools"
    target: int = Field(default=20, ge=1)


class HarvestCampaignRunRequest(BaseModel):
    country: str = Field(min_length=2)
    localities: tuple[str, ...] = ()
    facility_types: tuple[str, ...] = Field(min_length=1)
    target: int = Field(default=20, ge=1)


class PromoteLeadRequest(BaseModel):
    index: int = Field(ge=0)
    task_id: str | None = None


class GeometryGeocodeRequest(BaseModel):
    item_id: str = Field(min_length=1)
    query: str = Field(min_length=1)


class GeometrySaveRequest(BaseModel):
    item_id: str = Field(min_length=1)
    geocode_query: str = Field(min_length=1)
    point: GeometryPoint | None = None
    polygon_geojson: dict[str, Any] | None = None
    geometry_status: GeometryStatus = GeometryStatus.NEEDS_REVIEW
    geocode_result: dict[str, Any] | None = None
    review_notes: str | None = None


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
            matches = [
                (active_id, process)
                for active_id, process in self._processes.items()
                if active_id == run_id or active_id.startswith(f"{run_id}-")
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
    preferred_order = {"schools": 0, "manufacturing": 1, "restaurants": 2}
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
        )
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
    prompt = render_lead_qaqc_prompt(leads, source_label=manifest.lead_path)
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
    records = _approved_records_for_manifest(root, manifest)
    items = tuple(merge_geometry_items(root, records))
    return {"item_count": len(items), "items": items}


def _verified_export_response(root: Path, run_id: str, *, output_format: str) -> Response:
    try:
        manifest = _load_any_manifest(root, run_id)
        items = tuple(merge_geometry_items(root, _approved_records_for_manifest(root, manifest)))
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

    async def profiles(request: Request) -> JSONResponse:
        return JSONResponse(_profiles_payload())

    async def harvest_run(request: Request) -> JSONResponse:
        try:
            data = HarvestRunRequest.model_validate(await _request_json(request))
            run_id = build_harvest_run_id(
                country=data.country,
                locality=data.locality,
                profile_set_name=data.profiles,
                profile_id=data.profile,
            )
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
            batch_id = build_harvest_batch_id(
                country=data.country,
                locality=data.locality,
                profile_set_name=data.profiles,
            )
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
            campaign_id = build_harvest_campaign_id(
                country=data.country,
                localities=data.localities,
                facility_types=data.facility_types,
            )
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

    async def run_log(request: Request) -> PlainTextResponse:
        run_id = request.path_params["run_id"]
        try:
            manifest = _load_any_manifest(root, run_id)
        except ValueError as exc:
            return PlainTextResponse(str(exc), status_code=404)
        return PlainTextResponse(_combined_log_text(root, manifest), media_type="text/plain")

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
            prompt = render_lead_qaqc_prompt(leads, source_label=manifest.lead_path)
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

    async def verified_leads(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        try:
            manifest = _load_any_manifest(root, run_id)
            records = _approved_records_for_manifest(root, manifest)
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
            result = app_geocoder(data.query)
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
                geocode_query=data.query,
                point=point,
                polygon_geojson=None,
                geometry_status=(
                    GeometryStatus.POINT_CONFIRMED
                    if point is not None
                    else GeometryStatus.NEEDS_REVIEW
                ),
                geocode_result=result,
            )
            save_geometry_review_item(root, item)
            return JSONResponse(
                {"geocode_result": result, "geometry": item.model_dump(mode="json")}
            )
        except (ValidationError, ValueError) as exc:
            return _json_error(str(exc))

    async def geometry_save(request: Request) -> JSONResponse:
        item_id = request.path_params["item_id"]
        try:
            data = GeometrySaveRequest.model_validate(await _request_json(request))
            if data.item_id != item_id:
                return _json_error("item_id in path and body must match")
            item = geometry_item_from_payload(
                item_id=item_id,
                geocode_query=data.geocode_query,
                point=data.point,
                polygon_geojson=data.polygon_geojson,
                geometry_status=data.geometry_status,
                geocode_result=data.geocode_result,
                review_notes=data.review_notes,
            )
            save_geometry_review_item(root, item)
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
        Route("/api/profiles", profiles),
        Route("/api/harvest/run", harvest_run, methods=["POST"]),
        Route("/api/harvest/batch-run", harvest_batch_run, methods=["POST"]),
        Route("/api/harvest/campaign-run", harvest_campaign_run, methods=["POST"]),
        Route("/api/runs", runs),
        Route("/api/runs/clear", clear_runs, methods=["POST"]),
        Route("/api/runs/{run_id}", run_detail),
        Route("/api/runs/{run_id}/status", run_status),
        Route("/api/runs/{run_id}/log", run_log),
        Route("/api/runs/{run_id}/leads", run_leads),
        Route("/api/runs/{run_id}/qaqc-prompt", run_qaqc_prompt),
        Route("/api/runs/{run_id}/qaqc-run", run_qaqc, methods=["POST"]),
        Route("/api/runs/{run_id}/qaqc-reviews", run_qaqc_reviews),
        Route("/api/runs/{run_id}/verified-leads", verified_leads),
        Route("/api/runs/{run_id}/geometry-items", geometry_items),
        Route("/api/geometry/geocode", geometry_geocode, methods=["POST"]),
        Route("/api/geometry/items/{item_id}", geometry_save, methods=["POST"]),
        Route("/api/runs/{run_id}/export.csv", export_csv),
        Route("/api/runs/{run_id}/export.jsonl", export_jsonl),
        Route("/api/runs/{run_id}/export.verified.json", export_verified_json),
        Route("/api/runs/{run_id}/export.verified.csv", export_verified_csv),
        Route("/api/runs/{run_id}/export.footprints.geojson", export_footprints_geojson),
        Route("/api/runs/{run_id}/cancel", cancel_run, methods=["POST"]),
        Route("/api/runs/{run_id}/promote", promote, methods=["POST"]),
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
  <title>Observation Harvester</title>
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
      --line: #d9dee7;
      --text: #1f2937;
      --muted: #5f6b7a;
      --accent: #176b87;
      --accent-dark: #104d61;
      --danger: #a33a35;
      --ok: #216e4e;
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
    }
    h1 {
      margin: 0;
      font-size: 20px;
      line-height: 1.2;
      letter-spacing: 0;
    }
    main {
      display: grid;
      grid-template-columns: minmax(280px, 380px) minmax(0, 1fr);
      gap: 18px;
      padding: 18px 24px 24px;
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
      background: #fff;
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
      background: #111827;
      color: #e5e7eb;
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
      background: #fff;
      color: var(--muted);
      padding: 9px;
    }
    .mode button.active {
      background: var(--accent);
      color: #fff;
    }
    button {
      border: 1px solid var(--accent);
      border-radius: 6px;
      background: var(--accent);
      color: white;
      cursor: pointer;
      font-weight: 650;
      padding: 9px 12px;
    }
    button.secondary {
      background: #fff;
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
      background: #fbfcfd;
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
      background: #fff;
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
      background: #fff;
      color: var(--text);
      border-color: var(--line);
      margin-top: 6px;
      font-weight: 500;
    }
    .geometry-list button.active {
      border-color: var(--accent);
      background: #edf8fb;
    }
    .map {
      min-height: 520px;
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }
    @media (max-width: 860px) {
      main { grid-template-columns: 1fr; padding: 12px; }
      .summary { grid-template-columns: repeat(2, 1fr); }
      .geometry-layout { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header><h1>Observation Harvester</h1></header>
  <main>
    <section>
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
        <button id="runButton" type="button">Run Harvest</button>
        <button id="refreshButton" class="secondary" type="button">Refresh Runs</button>
        <button id="clearRunsButton" class="secondary" type="button">Clear All</button>
      </div>
      <div id="status" class="status">Ready.</div>
      <div class="history" id="history"></div>
    </section>

    <section>
      <h2>Results</h2>
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
        <button id="copyButton" class="secondary" type="button">Copy JSON</button>
        <button id="copyQaqcButton" class="secondary" type="button">Copy QAQC Prompt</button>
        <button id="runQaqcButton" class="secondary" type="button">Run QAQC</button>
        <button id="downloadJsonButton" class="secondary" type="button">
          Download Verified JSON
        </button>
        <button id="downloadCsvButton" class="secondary" type="button">
          Download Verified CSV
        </button>
      </div>
      <textarea
        id="jsonOutput"
        spellcheck="false"
        placeholder="Harvest JSON will appear here."
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

    <section class="wide">
      <h2>Geometry Review</h2>
      <div class="actions">
        <button id="loadApprovedButton" class="secondary" type="button">Load Approved</button>
        <button id="geocodeButton" class="secondary" type="button">Geocode</button>
        <button id="useMapCenterButton" class="secondary" type="button">Use Map Center</button>
        <button id="saveFootprintButton" class="secondary" type="button">Save Footprint</button>
        <button id="skipGeometryButton" class="secondary" type="button">Skip</button>
        <button id="downloadVerifiedJsonButton" class="secondary" type="button">
          Download Verified JSON
        </button>
        <button id="downloadVerifiedCsvButton" class="secondary" type="button">
          Download Verified CSV
        </button>
        <button id="downloadFootprintsButton" class="secondary" type="button">
          Download Footprints GeoJSON
        </button>
      </div>
      <div class="status" id="geometryStatus">Load QAQC-approved observations to begin.</div>
      <div class="geometry-layout">
        <div>
          <div class="geometry-list" id="geometryList"></div>
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
  </main>
  <script>
    const state = {
      profiles: [],
      mode: 'single',
      currentRunId: null,
      currentLeads: [],
      pollTimer: null,
      pollPurpose: 'harvest',
      geometryItems: [],
      selectedGeometryItemId: null,
      map: null,
      drawnItems: null,
      marker: null
    };
    const $ = (id) => document.getElementById(id);
    const terminalStatuses = ['completed', 'failed', 'cancelled'];

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
      state.currentRunId = null;
      state.currentLeads = [];
      $('metricStatus').textContent = '-';
      $('metricLeads').textContent = '0';
      $('metricFacilityLabel').textContent = 'Facility';
      $('metricAggregateLabel').textContent = 'Aggregate';
      $('metricFacility').textContent = '0';
      $('metricAggregate').textContent = '0';
      $('jsonOutput').value = '';
      $('activityOutput').value = '';
    }

    async function loadLog(runId) {
      if (!runId) return;
      const response = await fetch(`/api/runs/${runId}/log`);
      $('activityOutput').value = response.ok ? await response.text() : await response.text();
      $('activityOutput').scrollTop = $('activityOutput').scrollHeight;
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
        return;
      }
      if (isTerminal(manifest.status)) {
        stopPolling();
        setStatus(
          manifest.status === 'completed' ? 'Harvest complete.' : `Harvest ${manifest.status}.`,
          manifest.status === 'completed' ? 'ok' : 'error'
        );
        await loadRuns();
      }
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

    async function runHarvest() {
      const button = $('runButton');
      button.disabled = true;
      setStatus('Running harvest. Codex may take a while...');
      try {
        const endpoint = state.mode === 'campaign'
          ? '/api/harvest/campaign-run'
          : (state.mode === 'batch' ? '/api/harvest/batch-run' : '/api/harvest/run');
        const payload = await api(endpoint, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(requestBody())
        });
        renderResult(payload.manifest, payload.leads || []);
        await loadLog(state.currentRunId);
        const failed = payload.manifest.status === 'failed';
        setStatus(
          isTerminal(payload.manifest.status)
            ? (failed ? 'Harvest failed. See manifest output.' : 'Harvest complete.')
            : 'Harvest started. Watching agent activity...',
          failed ? 'error' : 'ok'
        );
        if (!isTerminal(payload.manifest.status)) startPolling(state.currentRunId);
        await loadRuns();
      } catch (error) {
        setStatus(error.message, 'error');
      } finally {
        button.disabled = false;
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

    async function runQaqc() {
      if (!state.currentRunId) return setStatus('No run selected.', 'error');
      const button = $('runQaqcButton');
      button.disabled = true;
      setStatus('Starting QAQC agent run...');
      try {
        const payload = await api(`/api/runs/${state.currentRunId}/qaqc-run`, { method: 'POST' });
        $('activityOutput').value +=
          `\\nQAQC started for ${payload.child_run_ids.length} child run(s).\\n`;
        setStatus('QAQC started. Watching agent activity...', 'ok');
        startPolling(state.currentRunId, 'qaqc');
      } catch (error) {
        setStatus(error.message, 'error');
      } finally {
        button.disabled = false;
      }
    }

    function setGeometryStatus(message, kind = '') {
      $('geometryStatus').textContent = message;
      $('geometryStatus').className = `status ${kind}`;
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
      });
    }

    function selectedGeometryItem() {
      return state.geometryItems.find((item) => item.item_id === state.selectedGeometryItemId);
    }

    function renderGeometryList() {
      $('geometryList').innerHTML = state.geometryItems.map((item) => {
        const lead = item.lead;
        const label = `${lead.location.facility_name} - ${item.geometry_status}`;
        const active = item.item_id === state.selectedGeometryItemId ? ' active' : '';
        return `<button type="button" class="${active}" data-geometry="${item.item_id}">
          ${label}<br>${lead.location.city_or_region}, ${lead.location.country}
        </button>`;
      }).join('') || '<div class="status">No QAQC-approved observations loaded.</div>';
      for (const button of $('geometryList').querySelectorAll('button[data-geometry]')) {
        button.addEventListener('click', () => selectGeometryItem(button.dataset.geometry));
      }
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

    function selectGeometryItem(itemId) {
      initMap();
      state.selectedGeometryItemId = itemId;
      renderGeometryList();
      const item = selectedGeometryItem();
      if (!item) return;
      $('geometryDetail').value = JSON.stringify({
        item_id: item.item_id,
        facility: item.lead.location.facility_name,
        query: item.geocode_query,
        source_url: item.lead.source_url,
        counts: item.lead.occupancy_data,
        qaqc: item.qaqc_review.review_notes,
        geometry_status: item.geometry_status,
        area_m2: item.area_m2
      }, null, 2);
      if (state.drawnItems) state.drawnItems.clearLayers();
      const point = pointFromGeometry(item);
      if (point) setMarker(point);
      const polygon = polygonFromGeometry(item);
      if (polygon && state.drawnItems) {
        const layer = L.geoJSON(polygon).getLayers()[0];
        state.drawnItems.addLayer(layer);
        state.map.fitBounds(layer.getBounds());
      }
    }

    async function loadApprovedGeometry() {
      if (!state.currentRunId) return setGeometryStatus('No run selected.', 'error');
      initMap();
      const payload = await api(`/api/runs/${state.currentRunId}/geometry-items`);
      state.geometryItems = payload.items || [];
      state.selectedGeometryItemId = state.geometryItems[0]?.item_id || null;
      renderGeometryList();
      if (state.selectedGeometryItemId) selectGeometryItem(state.selectedGeometryItemId);
      setGeometryStatus(`Loaded ${state.geometryItems.length} QAQC-approved observation(s).`, 'ok');
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

    async function geocodeSelected() {
      const item = selectedGeometryItem();
      if (!item) return setGeometryStatus('No approved observation selected.', 'error');
      const payload = await api('/api/geometry/geocode', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ item_id: item.item_id, query: item.geocode_query })
      });
      item.geometry = payload.geometry;
      item.geometry_status = payload.geometry.geometry_status;
      item.area_m2 = payload.geometry.area_m2;
      if (payload.geometry.point) setMarker(payload.geometry.point);
      renderGeometryList();
      setGeometryStatus(
        payload.geocode_result ? 'Geocode placed a point.' : 'No geocode result.',
        'ok'
      );
    }

    function useMapCenter() {
      const item = selectedGeometryItem();
      if (!item || !state.map) {
        return setGeometryStatus('No approved observation selected.', 'error');
      }
      const center = state.map.getCenter();
      setMarker({ latitude: center.lat, longitude: center.lng, source: 'user' });
      setGeometryStatus('Point set from map center.', 'ok');
    }

    async function saveGeometry(status = null) {
      const item = selectedGeometryItem();
      if (!item) return setGeometryStatus('No approved observation selected.', 'error');
      const polygon = currentPolygonGeojson();
      const point = currentPointPayload();
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
          geocode_result: item.geometry?.geocode_result || null,
          review_notes: geometryStatus === 'skipped' ? 'Skipped in geometry review.' : null
        })
      });
      item.geometry = payload.geometry;
      item.geometry_status = payload.geometry.geometry_status;
      item.area_m2 = payload.geometry.area_m2;
      renderGeometryList();
      selectGeometryItem(item.item_id);
      setGeometryStatus(`Geometry saved: ${item.geometry_status}.`, 'ok');
    }

    async function exitApplication() {
      if (!window.confirm('Exit Observation Harvester and cancel active harvests?')) return;
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

    async function boot() {
      const payload = await api('/api/profiles');
      state.profiles = payload.profile_sets;
      renderProfileSets();
      await loadRuns();
      $('profileSet').addEventListener('change', renderProfiles);
      $('singleMode').addEventListener('click', () => setMode('single'));
      $('batchMode').addEventListener('click', () => setMode('batch'));
      $('campaignMode').addEventListener('click', () => setMode('campaign'));
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
      $('runQaqcButton').addEventListener('click', runQaqc);
      $('downloadJsonButton').addEventListener('click', () => {
        downloadText('observation-harvest.json', $('jsonOutput').value, 'application/json');
      });
      $('downloadCsvButton').addEventListener('click', () => downloadExport('csv'));
      $('loadApprovedButton').addEventListener('click', () => {
        loadApprovedGeometry().catch((error) => setGeometryStatus(error.message, 'error'));
      });
      $('geocodeButton').addEventListener('click', () => {
        geocodeSelected().catch((error) => setGeometryStatus(error.message, 'error'));
      });
      $('useMapCenterButton').addEventListener('click', useMapCenter);
      $('saveFootprintButton').addEventListener('click', () => {
        saveGeometry().catch((error) => setGeometryStatus(error.message, 'error'));
      });
      $('skipGeometryButton').addEventListener('click', () => {
        saveGeometry('skipped').catch((error) => setGeometryStatus(error.message, 'error'));
      });
      $('downloadVerifiedJsonButton').addEventListener('click', () => downloadExport('json'));
      $('downloadVerifiedCsvButton').addEventListener('click', () => downloadExport('csv'));
      $('downloadFootprintsButton').addEventListener('click', downloadFootprints);
    }
    boot().catch((error) => setStatus(error.message, 'error'));
  </script>
</body>
</html>
"""

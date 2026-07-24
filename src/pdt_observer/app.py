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

from pdt_observer.harvest import (
    CodexRunner,
    append_harvest_log,
    build_harvest_batch_id,
    build_harvest_campaign_id,
    build_harvest_run_id,
    run_harvest,
    run_harvest_batch,
    run_harvest_campaign,
)
from pdt_observer.leads import (
    export_leads,
    load_leads,
    promote_lead_to_run,
    render_lead_qaqc_prompt,
)
from pdt_observer.models import (
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


class ActiveCodexRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen[str]] = {}

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
            return any(
                active_id == run_id or active_id.startswith(f"{run_id}-")
                for active_id in self._processes
            )

    def active_count(self) -> int:
        with self._lock:
            return len(self._processes)

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


def _load_any_manifest(root: Path, run_id: str) -> Any:
    try:
        return _load_run_manifest(root, run_id)
    except ValueError:
        try:
            return _load_batch_manifest(root, run_id)
        except ValueError:
            return _load_campaign_manifest(root, run_id)


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
    background: bool = True,
    shutdown_callback: Callable[[], None] | None = None,
) -> Starlette:
    root = workspace.resolve()
    root.mkdir(parents=True, exist_ok=True)
    registry = ActiveCodexRegistry(root)
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="harvest")
    app_runner = runner or registry.runner

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
        log_path = getattr(manifest, "log_path", None)
        if log_path is None:
            return PlainTextResponse("")
        path = Path(log_path)
        if not path.is_file():
            return PlainTextResponse("")
        return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/plain")

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

    async def export_csv(request: Request) -> Response:
        return _export_response(root, request.path_params["run_id"], output_format="csv")

    async def export_jsonl(request: Request) -> Response:
        return _export_response(root, request.path_params["run_id"], output_format="jsonl")

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
        Route("/api/runs/{run_id}/export.csv", export_csv),
        Route("/api/runs/{run_id}/export.jsonl", export_jsonl),
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
    @media (max-width: 860px) {
      main { grid-template-columns: 1fr; padding: 12px; }
      .summary { grid-template-columns: repeat(2, 1fr); }
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
        <button id="downloadJsonButton" class="secondary" type="button">Download JSON</button>
        <button id="downloadCsvButton" class="secondary" type="button">Download CSV</button>
        <button id="downloadJsonlButton" class="secondary" type="button">Download JSONL</button>
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
  </main>
  <script>
    const state = {
      profiles: [],
      mode: 'single',
      currentRunId: null,
      currentLeads: [],
      pollTimer: null
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
      if (isTerminal(manifest.status)) {
        stopPolling();
        setStatus(
          manifest.status === 'completed' ? 'Harvest complete.' : `Harvest ${manifest.status}.`,
          manifest.status === 'completed' ? 'ok' : 'error'
        );
        await loadRuns();
      }
    }

    function startPolling(runId) {
      stopPolling();
      state.currentRunId = runId;
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
      const response = await fetch(`/api/runs/${state.currentRunId}/export.${format}`);
      if (!response.ok) return setStatus(await response.text(), 'error');
      downloadText(
        `observation-harvest.${format}`,
        await response.text(),
        format === 'csv' ? 'text/csv' : 'application/x-ndjson'
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
      $('downloadJsonButton').addEventListener('click', () => {
        downloadText('observation-harvest.json', $('jsonOutput').value, 'application/json');
      });
      $('downloadCsvButton').addEventListener('click', () => downloadExport('csv'));
      $('downloadJsonlButton').addEventListener('click', () => downloadExport('jsonl'));
    }
    boot().catch((error) => setStatus(error.message, 'error'));
  </script>
</body>
</html>
"""

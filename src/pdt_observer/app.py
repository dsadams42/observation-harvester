from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from pdt_observer.harvest import CodexRunner, run_harvest, run_harvest_batch
from pdt_observer.leads import export_leads, load_leads, promote_lead_to_run
from pdt_observer.models import HarvestBatchRunManifest, HarvestRunManifest
from pdt_observer.profiles import BUILTIN_PROFILE_SETS
from pdt_observer.workflow import write_model


class HarvestRunRequest(BaseModel):
    country: str = Field(min_length=2)
    locality: str | None = None
    profiles: str = "commercial_business"
    profile: str | None = None
    target: int = Field(default=20, ge=1)


class HarvestBatchRunRequest(BaseModel):
    country: str = Field(min_length=2)
    locality: str | None = None
    profiles: str = "commercial_business"
    target: int = Field(default=20, ge=1)


class PromoteLeadRequest(BaseModel):
    index: int = Field(ge=0)
    task_id: str | None = None


def _json_error(message: str, *, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


async def _request_json(request: Request) -> dict[str, Any]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    return payload


def _manifest_dir(root: Path) -> Path:
    return root / "harvest_runs"


def _run_manifest_path(root: Path, run_id: str) -> Path:
    return _manifest_dir(root) / f"{run_id}.json"


def _batch_manifest_path(root: Path, batch_id: str) -> Path:
    return _manifest_dir(root) / f"{batch_id}.batch.json"


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


def _list_manifests(root: Path) -> list[dict[str, Any]]:
    directory = _manifest_dir(root)
    if not directory.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(".batch.json"):
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
    return {"profile_sets": sorted(profile_sets, key=lambda item: item["profile_set_id"])}


def _leads_payload(path: str) -> list[dict[str, Any]]:
    leads = load_leads(Path(path))
    return [lead.model_dump(mode="json") for lead in leads]


def create_app(
    *,
    workspace: Path,
    codex_bin: str = "codex",
    runner: CodexRunner | None = None,
) -> Starlette:
    root = workspace.resolve()

    async def index(request: Request) -> HTMLResponse:
        return HTMLResponse(INDEX_HTML)

    async def profiles(request: Request) -> JSONResponse:
        return JSONResponse(_profiles_payload())

    async def harvest_run(request: Request) -> JSONResponse:
        try:
            data = HarvestRunRequest.model_validate(await _request_json(request))
            manifest = await run_in_threadpool(
                run_harvest,
                root=root,
                country=data.country,
                locality=data.locality,
                profile_set_name=data.profiles,
                profile_id=data.profile,
                target=data.target,
                codex_bin=codex_bin,
                runner=runner,
            )
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
            manifest = await run_in_threadpool(
                run_harvest_batch,
                root=root,
                country=data.country,
                locality=data.locality,
                profile_set_name=data.profiles,
                target=data.target,
                codex_bin=codex_bin,
                runner=runner,
            )
            return JSONResponse({"manifest": manifest.model_dump(mode="json")})
        except (ValidationError, ValueError) as exc:
            return _json_error(str(exc))

    async def runs(request: Request) -> JSONResponse:
        return JSONResponse({"runs": _list_manifests(root)})

    async def run_detail(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        try:
            run_manifest = _load_run_manifest(root, run_id)
            return JSONResponse({"manifest": run_manifest.model_dump(mode="json")})
        except ValueError:
            try:
                batch_manifest = _load_batch_manifest(root, run_id)
                return JSONResponse({"manifest": batch_manifest.model_dump(mode="json")})
            except ValueError as exc:
                return _json_error(str(exc), status_code=404)

    async def run_leads(request: Request) -> JSONResponse:
        try:
            manifest = _load_run_manifest(root, request.path_params["run_id"])
            return JSONResponse({"leads": _leads_payload(manifest.lead_path)})
        except ValueError as exc:
            return _json_error(str(exc), status_code=404)

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
        Route("/api/runs", runs),
        Route("/api/runs/{run_id}", run_detail),
        Route("/api/runs/{run_id}/leads", run_leads),
        Route("/api/runs/{run_id}/export.csv", export_csv),
        Route("/api/runs/{run_id}/export.jsonl", export_jsonl),
        Route("/api/runs/{run_id}/promote", promote, methods=["POST"]),
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

    app = create_app(workspace=workspace, codex_bin=codex_bin)
    url = f"http://{host}:{port}"
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
    .row {
      display: grid;
      grid-template-columns: 1fr 110px;
      gap: 10px;
    }
    .mode {
      display: grid;
      grid-template-columns: 1fr 1fr;
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

      <label for="locality">Region or Locality</label>
      <input id="locality" placeholder="Optional, e.g. Tennessee">

      <label for="profileSet">Profile Set</label>
      <select id="profileSet"></select>

      <label for="profile">Facility Type</label>
      <select id="profile"></select>

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
          </div>
        </div>
      </div>

      <div class="actions">
        <button id="runButton" type="button">Run Harvest</button>
        <button id="refreshButton" class="secondary" type="button">Refresh Runs</button>
      </div>
      <div id="status" class="status">Ready.</div>
      <div class="history" id="history"></div>
    </section>

    <section>
      <h2>Results</h2>
      <div class="summary">
        <div class="metric"><span>Status</span><strong id="metricStatus">-</strong></div>
        <div class="metric"><span>Leads</span><strong id="metricLeads">0</strong></div>
        <div class="metric"><span>Facility</span><strong id="metricFacility">0</strong></div>
        <div class="metric"><span>Aggregate</span><strong id="metricAggregate">0</strong></div>
      </div>
      <div class="actions">
        <button id="copyButton" class="secondary" type="button">Copy JSON</button>
        <button id="downloadJsonButton" class="secondary" type="button">Download JSON</button>
        <button id="downloadCsvButton" class="secondary" type="button">Download CSV</button>
        <button id="downloadJsonlButton" class="secondary" type="button">Download JSONL</button>
      </div>
      <textarea
        id="jsonOutput"
        spellcheck="false"
        placeholder="Harvest JSON will appear here."
      ></textarea>
    </section>
  </main>
  <script>
    const state = { profiles: [], mode: 'single', currentRunId: null, currentLeads: [] };
    const $ = (id) => document.getElementById(id);

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
      renderProfiles();
    }

    function renderProfiles() {
      const profileSet = selectedProfileSet();
      const options = ['<option value="">All / profile set default</option>'];
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
      $('profile').disabled = mode === 'batch';
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
      state.currentRunId = manifest.run_id || manifest.batch_id || null;
      state.currentLeads = leads || [];
      const summary = manifest.summary || {};
      $('metricStatus').textContent = manifest.status || '-';
      $('metricLeads').textContent = summary.lead_count || leads.length || 0;
      $('metricFacility').textContent = summary.facility_level_count || 0;
      $('metricAggregate').textContent = summary.regional_aggregate_count || 0;
      $('jsonOutput').value = JSON.stringify(leads.length ? leads : { manifest }, null, 2);
    }

    async function runHarvest() {
      const button = $('runButton');
      button.disabled = true;
      setStatus('Running harvest. Codex may take a while...');
      try {
        const endpoint = state.mode === 'batch' ? '/api/harvest/batch-run' : '/api/harvest/run';
        const payload = await api(endpoint, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(requestBody())
        });
        renderResult(payload.manifest, payload.leads || []);
        const failed = payload.manifest.status === 'failed';
        setStatus(
          failed ? 'Harvest failed. See manifest output.' : 'Harvest complete.',
          failed ? 'error' : 'ok'
        );
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
        const id = run.run_id || run.batch_id;
        const label = run.manifest_type === 'batch' ? 'Batch' : 'Run';
        const scope = [run.country, run.locality].filter(Boolean).join(' / ');
        return `<button type="button" data-run="${id}">
          ${label}: ${id}<br>${scope} - ${run.status}
        </button>`;
      }).join('') || '<div class="status">No runs yet.</div>';
      for (const button of $('history').querySelectorAll('button[data-run]')) {
        button.addEventListener('click', () => loadRun(button.dataset.run));
      }
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
      setStatus(`Loaded ${runId}.`);
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
      $('runButton').addEventListener('click', runHarvest);
      $('refreshButton').addEventListener('click', loadRuns);
      $('copyButton').addEventListener('click', async () => {
        await navigator.clipboard.writeText($('jsonOutput').value);
        setStatus('JSON copied.', 'ok');
      });
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

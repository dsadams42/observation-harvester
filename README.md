# PDT Observation Harvester

This repository is a small proof of concept for harvesting Population Density Table
observations. The default workflow is designed for Codex / ChatGPT subscription usage:
Codex performs a prompted or scheduled investigation, writes a local JSON candidate, and this
Python package validates the candidate deterministically.

The app can still run an optional OpenAI Agents SDK path, but that is no longer the default.

## What Decides What

- Codex decides how to investigate when you run a prompted chat or scheduled Codex automation.
- The JSON run artifact records the task, source bundle, geocoder bundle, and proposed result.
- Python application code validates exact quotes, counts, document IDs, source URLs, place IDs,
  coordinates, locality, and country.
- The model output is only a proposal; accepted observations must pass deterministic validation.

During one run, state exists as a task, source documents, place records, a candidate result, and a
validation report. Offline tests do not call an LLM because they prove the local harness without
network access, cost, or non-determinism.

## Install

Use Python 3.12 or newer.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
```

For the optional API-backed agent command, install:

```powershell
.\.venv\Scripts\python -m pip install -e ".[api-agent,dev]"
```

Copy `.env.example` only if you want to run the optional API mode. Do not store a real key in
source control.

## Codex Subscription Workflow

Use one of the prompts in `prompts/` from a Codex chat or automation. The prompt should produce an
`InvestigationRun` JSON file shaped like `examples/milltown_codex_run.json`.

Validate that run locally:

```powershell
python -m pdt_observer validate examples/milltown_codex_run.json
```

Print a compact human summary:

```powershell
python -m pdt_observer summarize examples/milltown_codex_run.json
```

Ad-hoc run artifacts should go under `runs/`, which is ignored by git.

## Offline Demo

The deterministic mock demo still exercises search, fetch, extraction, geocoding, and validation
without an API key:

```powershell
python -m pdt_observer demo
```

## Optional API Mode

The OpenAI Agents SDK path remains available for future deployment or comparison. It uses the same
models, mock services, and validator:

```powershell
$env:OPENAI_API_KEY = "sk-..."
python -m pdt_observer investigate-api examples/milltown_task.json
```

The model defaults to `gpt-5.4-mini`. Override it with `PDT_OBSERVER_MODEL`.

## Verify

```powershell
pytest
ruff check .
mypy
```

The ordinary test suite is offline and deterministic. Any live SDK test should remain behind an
explicit marker and environment-variable gate.

## Replacing Mocks Later

Real search and geocoding providers should implement the protocols in `ports.py`. For a
Codex-operated workflow, Codex can also gather source and place records directly into an
`InvestigationRun` artifact, then hand validation back to this package.

## Outside This Proof Of Concept

This project does not include continuous scraping, social-media integrations, databases, building
footprints, floor counting, occupancy estimation, Docker, orchestration systems, multiple
cooperating agents, or a graphical UI.

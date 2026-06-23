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

Use one of the prompts in `prompts/` from a Codex chat or automation. For multi-agent harvesting,
start with `prompts/building_type_agent.md` and the profile definitions in
`profiles/public_venues.json`.

Create a local batch of profile-specific work items:

```powershell
python -m pdt_observer batch create --locality Milltown --country US --profiles public_venues
```

Each Codex agent claims work for one profile:

```powershell
python -m pdt_observer work claim --profile restaurants_bars --claimed-by codex-restaurants
```

The agent performs web discovery using Codex web capabilities, writes an `InvestigationRun` JSON
file shaped like `examples/milltown_codex_run.json`, then validates it.

Validate that run locally:

```powershell
python -m pdt_observer validate runs/<file>.json
python -m pdt_observer review ingest runs/<file>.json
```

List and export review queue entries:

```powershell
python -m pdt_observer review list --status review
python -m pdt_observer export --status accepted --format jsonl
```

Ad-hoc batch, work, run, review, and export artifacts are ignored by git.

## Direct URL Fetching

Python is not a general search engine here. It can fetch direct public URLs supplied by Codex or a
user:

```powershell
python -m pdt_observer source fetch https://example.com/story --output runs/source.json
```

The fetcher uses GET-only requests, robots.txt checks, a custom user agent, content-type and size
limits, timeouts, URL canonicalization, basic HTML text extraction, and RSS/sitemap URL extraction.
It does not bypass logins, paywalls, CAPTCHAs, or site blocks.

## Offline Demo

The deterministic mock demo still exercises search, fetch, extraction, geocoding, and validation
without an API key:

```powershell
python -m pdt_observer demo
```

## Optional API Mode

The OpenAI Agents SDK path remains available only for future comparison. It is not part of the
recommended no-key workflow:

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

Codex is expected to gather source and place records directly into an `InvestigationRun` artifact,
then hand validation back to this package. If future deployments use real APIs, keep them behind
typed ports and optional extras so the no-key Codex workflow remains intact.

## Outside This Proof Of Concept

This project does not include continuous scraping, social-media integrations, databases, building
footprints, floor counting, occupancy estimation, Docker, orchestration systems, multiple
cooperating agents, or a graphical UI.

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
  coordinates, locality, country, and time-context consistency when time context is present.
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

For the local browser app, install:

```powershell
.\.venv\Scripts\python -m pip install -e ".[app]"
```

Copy `.env.example` only if you want to run the optional API mode. Do not store a real key in
source control.

## Local Browser App

The local app gives a browser-based workflow without a hosted backend or app-owned API key. It
runs on your machine and uses your locally authenticated Codex CLI session.

One-time requirements:

- Python 3.12 or newer
- Codex CLI installed and authenticated
- This repository cloned locally

On macOS, double-click:

```text
Observation Harvester.command
```

The launcher creates `.venv` if needed, installs `.[app]`, checks that `codex` is on `PATH`,
starts the local server, and opens:

```text
http://127.0.0.1:8765
```

Manual fallback:

```powershell
python -m pip install -e ".[app]"
python -m pdt_observer app
```

The first screen is the tool itself: enter a country, optional region/locality, facility type,
optional subtype, and target count. The app writes the same runtime artifacts as the CLI:
`work/`, `lead_runs/`, `harvest_runs/`, `harvest_logs/`, `exports/`, and `runs/`. Results are
displayed as copyable JSON, with CSV and JSONL export buttons.

The app includes an Agent Activity panel while a harvest runs. It polls the run manifest and log
file so the user can see prompt rendering, Codex launch, validation, completion, failure, or
cancellation. Cancel Run stops active Codex harvest subprocesses launched by the current app
session. Exit Application cancels active harvest children and shuts down this local app server; it
does not kill unrelated Python or Codex processes elsewhere on the machine.

## Codex Subscription Workflow

Use one of the prompts in `prompts/` from a Codex chat or automation. For multi-agent harvesting,
start with `prompts/building_type_agent.md` and a facility-type definition such as
`profiles/schools.json`, `profiles/manufacturing.json`, or `profiles/restaurants.json`.

Create a local batch of subtype-specific work items:

```powershell
python -m pdt_observer batch create --locality Tennessee --country US --facility-type schools
```

Each Codex agent claims work for one subtype:

```powershell
python -m pdt_observer work claim --profile primary_secondary_education --claimed-by codex-schools
```

Render a subtype-specific working prompt for the claimed item:

```powershell
python -m pdt_observer work prompt --work-item-id <work_item_id>
```

For country pilots, claims can be narrowed to a locality or exact work item:

```powershell
python -m pdt_observer work claim --profile light_manufacturing --locality Manila --country PH --claimed-by codex-manufacturing
python -m pdt_observer work claim --work-item-id ph-manila-pilot-001-light_manufacturing --claimed-by codex-manufacturing
```

Claiming is protected by a local file-backed lock so two agents do not intentionally receive the
same open work item during normal file-backed operation.

Each work item has quotas and progress counters. Check whether to continue:

```powershell
python -m pdt_observer work status --work-item-id <work_item_id>
```

The agent performs web discovery using Codex web capabilities, inspects one source at a time, and
records progress:

```powershell
python -m pdt_observer work record-source --work-item-id <work_item_id> --outcome empty
python -m pdt_observer work record-source --work-item-id <work_item_id> --outcome failed
python -m pdt_observer work record-source --work-item-id <work_item_id> --outcome examined
```

The building-type agent prompt uses evidence-first quoted searches such as
`"<locality>" "people were inside" <venue>`, `"<locality>" "customers were evacuated" <venue>`,
and `"<locality>" "inside the <venue> when"` before broad venue discovery.

Facility types are the top-level PDT observation families. Subtypes tune search and extraction
inside a family. The built-in PDT-oriented facility types are currently `schools`, `manufacturing`,
and `restaurants`, with subtypes such as `primary_secondary_education`, `university_college`,
`light_manufacturing`, `heavy_manufacturing`, `full_service_restaurants`,
`quick_service_restaurants`, and `bars_nightlife`. Legacy profile sets such as
`commercial_business`, `public_venues`, and `residential` remain available for compatibility.

The CLI still accepts `--profiles` and `--profile`, but new usage can read more naturally as
`--facility-type` and `--subtype`. For example, `--facility-type manufacturing --country PH`
searches manufacturing facilities in the Philippines, while `--subtype light_manufacturing`
narrows that run to light industrial facilities. Source-tied phrases such as evacuated employees,
trapped workers, rescued students, evacuated diners, and patrons inside a venue are acceptable
real-time occupancy proxies, while accepted observations remain gated by exact URL, exact quote,
count, facility identity, locality/country, and georeference validation.

Qualifying evidence should come from source types that can plausibly document a count-bearing
event or incident: news articles, wire-service stories, official public-safety or government
reports, official venue/event attendance announcements, and official press releases. Wikipedia,
encyclopedias, directories, travel guides, map listings, venue profile pages, capacity pages, and
unsourced social reposts are context only; they may provide leads, but not accepted observations.

## Broad Lead Harvest

For country-wide discovery, start with a permissive lead harvest. This mirrors a broad
ChatGPT-style extraction prompt: collect many facility-specific occupancy leads, allow missing
metadata as `Unknown` or `Not provided`, and keep subgroup counts when a source breaks them out.
Lead harvests are not final accepted observations; they are reviewable candidates that can later
be promoted into strict `InvestigationRun` artifacts.

Prepare a broad lead prompt:

```powershell
python -m pdt_observer harvest prepare --country PH --facility-type manufacturing --target 20 --output work/ph-manufacturing-leads.md
```

Or run the harvest end-to-end through Codex CLI without manual shell piping:

```powershell
python -m pdt_observer harvest run --country PH --facility-type manufacturing --target 20
```

For region-specific or subtype-specific pilots, add `--locality` and `--subtype`:

```powershell
python -m pdt_observer harvest run --country US --locality Tennessee --facility-type schools --subtype university_college --target 5
```

`harvest run` writes the rendered prompt under `work/`, the JSON lead output under `lead_runs/`,
activity logs under `harvest_logs/`, and a run manifest under `harvest_runs/`. The manifest records
the run ID, scope, facility type, optional subtype, Codex command, output paths, log path, exit
code, validation result, summary, and failure or cancellation message when applicable.

If you prefer to run Codex manually, pass the prepared prompt through Codex CLI with web search:

```powershell
codex --search exec --sandbox workspace-write --cd . -o lead_runs/ph-manufacturing-001.json - < work/ph-manufacturing-leads.md
```

Codex should return only the JSON array, and `-o` writes the final response to
`lead_runs/ph-manufacturing-001.json`. Validate and summarize it:

```powershell
python -m pdt_observer leads validate lead_runs/ph-manufacturing-001.json
python -m pdt_observer leads summarize lead_runs/ph-manufacturing-001.json
```

Run one harvest per enabled subtype in a facility type:

```powershell
python -m pdt_observer harvest batch-run --country US --locality Tennessee --facility-type restaurants --target 10
```

Run a country-anchored campaign across multiple localities and facility types:

```powershell
python -m pdt_observer harvest campaign-run \
  --country PH \
  --locality Manila \
  --locality Makati \
  --locality "Cebu City" \
  --facility-type schools \
  --facility-type manufacturing \
  --facility-type restaurants \
  --target 10
```

`campaign-run` runs one child harvest per locality/facility-type pair. If no `--locality` is
provided, it runs countrywide once per selected facility type. Campaign manifests are written to
`harvest_runs/<campaign_id>.campaign.json` and summarize planned, completed, failed, and total
lead counts across all child runs.

Export lead outputs for review:

```powershell
python -m pdt_observer leads export lead_runs/us-tennessee-factories.json --format csv --output exports/us-tennessee-factories.csv
python -m pdt_observer leads export lead_runs/us-tennessee-factories.json --format jsonl --output exports/us-tennessee-factories.jsonl
```

Promote a promising lead into a draft strict run:

```powershell
python -m pdt_observer leads promote lead_runs/us-tennessee-factories.json --index 0 --output runs/us-tennessee-factories-001.json
```

Promoted runs are intentionally marked `review` until exact source text, exact supporting quote,
and georeference details are completed well enough for strict validation.

## Harvest Flow

```text
User chooses harvest level
  |
  +--> run
  |      country + optional locality + one facility type + optional subtype
  |
  +--> batch-run
  |      country + optional locality + one facility type expanded across subtypes
  |
  +--> campaign-run
         one country + N localities + N facility types
              |
              |  creates one child run per locality/facility-type pair
              v
         harvest_runs/<campaign-id>.campaign.json
              |
              |  lists child run IDs and aggregate summary
              v

Child harvest run
  |
  |  country + optional locality + facility type + target count
  v
python -m pdt_observer harvest prepare
  |
  |  writes/reprints a reusable Codex prompt
  v
work/<country>-<facility-type>-leads.md
  |
  |  Codex CLI/Desktop runs prompt with web search
  v
lead_runs/<country>-<facility-type>-001.json
  |
  |  run metadata and validation summary
  v
harvest_runs/<country>-<facility-type>-001.json
  |
  |  permissive lead schema:
  |  - partial metadata allowed
  |  - Unknown / Not provided allowed
  |  - subgroup counts preserved
  |  - source/quote/quality flags captured when available
  v
python -m pdt_observer leads validate
python -m pdt_observer leads summarize
  |
  |  human/Codex selects strong leads for audit-grade promotion
  v
runs/<specific-observation>.json
  |
  |  strict InvestigationRun schema:
  |  - exact source text
  |  - exact supporting quote
  |  - count appears in quote
  |  - source URL and place record included
  v
python -m pdt_observer validate
python -m pdt_observer work record-run
  |
  |  deterministic validation and review queue ingestion
  v
review/<review-item>.json
exports/*.jsonl
```

When a source supports a candidate, write an `InvestigationRun` JSON file shaped like
`examples/milltown_codex_run.json`, then validate, ingest, and count it. Keep
`observed_time_text` as the exact source phrase and use `time_context` for normalized values such
as `observed_time_local`, `time_precision`, `day_part`, and `daylight_state`:

```powershell
python -m pdt_observer work record-run --work-item-id <work_item_id> --run-file runs/<file>.json
```

`record-run` counts as one examined source and increments accepted/review/not_found counters.
Python marks the work item completed when it reaches its accepted target or an early-stop limit.

List and export review queue entries:

```powershell
python -m pdt_observer review list --status review
python -m pdt_observer export --status accepted --format jsonl
```

Quota defaults per work item are:

```json
{
  "target_accepted_count": 5,
  "max_review_count": 10,
  "max_sources_examined": 40,
  "max_failed_sources": 20,
  "max_empty_sources": 15,
  "max_runtime_minutes": 60
}
```

Override them during batch creation with flags such as `--target-accepted`,
`--max-sources`, `--max-failed-sources`, `--max-empty-sources`, `--max-review`, and
`--max-runtime-minutes`.

Ad-hoc batch, work, run, review, and export artifacts are ignored by git.

## Time Context

The first-class observation remains `people_present`; time is supporting context. If a source says
when the count was observed, store the exact phrase in `observed_time_text` and optionally add:

```json
{
  "observed_time_local": "21:10",
  "time_precision": "approximate",
  "day_part": "night",
  "daylight_state": "unknown",
  "timezone": null
}
```

Clock times are bucketed as `early_morning`, `morning`, `afternoon`, `evening`, or `night`.
Broad phrases such as "Friday night" may be stored as `day_part_only`. Solar daylight is left
`unknown` unless a future source or local place record gives enough deterministic evidence.

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
footprints, floor counting, occupancy estimation, Docker, a hosted orchestration system, or a
graphical UI.

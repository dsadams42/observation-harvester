# OASIS Development Plan

## Current Status

OASIS is an active local proof of concept. The browser workflow is operational from
agentic harvest through QAQC, address enrichment, geocoding, coverage review, manual
coordinate resolution, footprint digitization, and verified export. It remains a
local-first, file-backed tool rather than a hosted multi-user service or unattended
nationwide runner.

The current implementation emphasizes three constraints:

- keep external services behind typed interfaces;
- preserve exact source quotations and deterministic validation before export;
- avoid required API keys for default installation and ordinary tests.

## Current Architecture

Core package responsibilities now break down as follows:

1. `models.py` defines Pydantic v2 domain, evidence, geometry, and tool-result models.
2. `ports.py` defines typed protocols for search, document fetching, and spatial
   geocoding.
3. `geocoding.py` implements Nominatim geocoding and local cache behavior behind the
   `SpatialGeocoder` protocol.
4. `geometry.py` provides reusable spatial parsing, bounds checks, and area calculations.
5. `app_geometry.py` contains browser-app coordinate candidate ranking and retry helpers.
6. `storage.py` centralizes atomic JSON writes for durable local runtime artifacts.
7. `validation.py` checks proposed observations against source text, place, and
   time-context rules.
8. `agent.py` contains agent instructions, tool wrappers, scripted offline demo logic,
   and the optional OpenAI Agents SDK runner.
9. `cli.py` provides local commands for demo, validation, harvest, review, artifact,
   and app workflows.
10. `workflow.py` and `profiles.py` provide file-backed orchestration for batches,
    campaigns, coverage, and facility profiles.
11. `app.py` owns Starlette routes, browser workflow actions, background job records,
    and static asset endpoints.
12. `app_ui.py` renders the browser HTML shell; `static/app.css` and `static/app.js`
    hold presentation and browser interaction logic.
13. `artifact_migrations.py` inspects and upgrades legacy runtime JSON with backups.
14. `web.py` provides no-key direct URL fetching and fixture-testable parsing.
15. `time_context.py` normalizes source time phrases into local clock time and day-part
    buckets.
16. `prompts/` contains durable prompts for prompted investigation, strategy, QAQC,
    address, coverage, gap-fill, and coordinate review work.

## Implemented Capabilities

- Single facility-type harvests, subtype batches, and multi-locality campaigns.
- Localized Geographer guidance and strategy-guided Harvester prompts.
- Bounded parallel campaign and coverage-gap jobs with deterministic consolidation.
- Source-by-source QAQC of quotations, counts, facility identity, dates, and geography.
- Address enrichment, Nominatim geocoding, cached geocoder responses, and spatial
  extent validation.
- Human coordinate resolution through candidate review, Google Search/Maps helper
  links, pasted coordinates or Google Maps URLs, and map-click placement.
- Optional non-destructive sample curation feeding coverage and gap-fill prompts.
- Durable geometry review records with footprint polygons and planar `area_m2`.
- Tabular verified/raw review workspace and QAQC-gated JSON, CSV, JSONL, and GeoJSON
  exports.
- Pipeline transcripts written as visible colleague-style agent reports.
- Artifact inspection and migration commands for schema-versioned local JSON.

## Recent Hardening

The latest update reduced brittleness in the browser app and spatial stack:

- Introduced a `SpatialGeocoder` protocol so app logic depends on a typed interface
  rather than a concrete Nominatim implementation.
- Moved Nominatim HTTP/cache behavior out of `geometry.py` and into `geocoding.py`.
- Kept reusable geometry math in `geometry.py` while moving app-specific coordinate
  ranking and retry behavior into `app_geometry.py`.
- Added `storage.py` for shared atomic JSON writes used by runtime artifact updates.
- Split inline browser styling and scripting into packaged `static/app.css` and
  `static/app.js` assets served by explicit app routes.
- Added focused tests for geocoding cache behavior, static asset routes, and atomic
  state writes.
- Added `httpx2` to the development dependencies so Starlette route tests can run
  cleanly with current client behavior.

## Verification Expectations

After material changes, run:

```powershell
pytest
ruff check .
mypy
```

The ordinary test suite must stay deterministic and offline. Tests must not require
`OPENAI_API_KEY` or make unapproved network calls. Direct OpenAI API or Agents SDK
execution remains behind explicit optional commands and extras.

## Current Boundaries

Not implemented yet:

- Automatic nationwide locality planning.
- A hosted or shared multi-user observation pool.
- Background scheduling or unattended large-scale crawling.
- Automatic floor-count or total-floor-area estimation.
- Production authentication, permissions, database storage, or deployment.

Likely next development areas are to harden long-running pilot recovery and
observability, expand evaluation datasets, improve packaged browser-app ergonomics,
and then design floor-count/total-floor-area enrichment plus shared-data architecture
on top of the validated observation pipeline.

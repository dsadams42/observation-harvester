# OASIS

<img
  src="src/pdt_observer/static/oasis-logo.jpg"
  alt="OASIS logo"
  width="180"
>

**Observation Acquisition and Spatial Information Synthesis**

OASIS is a local human-AI workbench for finding, reviewing, geocoding, and spatially
enriching public-web observations of people at facilities. It was created to support
Population Density Table (PDT) research, where a defensible observation needs more than
a facility name or stated capacity: it needs source-backed evidence that a particular
number of people were present at a real place.

The application combines several bounded AI research stages with deterministic Python
validation and explicit human review. It is not a general autonomous web crawler, and
model output is never treated as accepted data merely because a model produced it.

> Project status: active proof of concept. The complete local workflow is operational
> from harvest through QAQC, address enrichment, geocoding, coverage review, manual
> coordinate resolution, footprint digitization, and verified export. The system is
> ready for structured pilot work, but not yet a hosted multi-user data service or an
> unattended nationwide runner.

## What OASIS Does

OASIS currently supports:

- Single facility-type harvests, subtype batches, and multi-locality campaigns.
- A Geographer Agent that adapts search terminology to local language, administrative
  structure, agency names, and facility vernacular.
- Facility-aware evidence strategies rather than one incident-only search pattern.
- Parallel campaign and gap-fill jobs with one Harvester Agent per job and bounded
  concurrency.
- Source-by-source QAQC of quotations, counts, facility identity, dates, and geographic
  scope.
- Address enrichment followed by Nominatim geocoding and spatial extent validation.
- Human coordinate resolution using focused research, ranked candidates, Google Search
  and Google Maps links, map placement, or pasted coordinates/Google Maps URLs.
- Optional human sample curation, followed by coverage analysis and reviewer-approved
  gap-fill campaigns.
- Footprint digitization, geometry storage, and planar building-area calculation.
- A tabular review workspace for QAQC-approved records or raw lead debugging.
- QAQC-gated JSON, CSV, and footprint GeoJSON exports.
- A persistent, downloadable pipeline transcript written as concise colleague-style
  agent reports rather than hidden chain-of-thought.

The built-in PDT facility families include `schools`, `manufacturing`, `restaurants`,
`retail_service`, `public_institutional`, `transportation`,
`recreation_entertainment`, `agriculture`, and residential facilities. Each family includes
more specific PDT subtypes with aliases, evidence phrases, negative traps, expected
occupancy groups, occurrence hints, and preferred evidence strategies. Older profile
families remain available for compatibility.

## Human-AI Workflow

```text
Country, locality, facility scope, and target
                       |
                       v
               Geographer review
                       |
                       v
          Strategy-guided lead harvest
                       |
                       v
          Evidence and location QAQC
                       |
                       v
              Address enrichment
                       |
                       v
     Geocoding + geographic-scope validation
                       |
                       v
       Sample creation + coverage analysis
                       |
                 human decision
                  /           \
          accept coverage    run gap fill
                                  |
                                  v
                         QAQC/enrich new leads
                       |
                       v
     Human coordinate and footprint review
                       |
                       v
              Verified data exports
```

The **Run Full Pipeline** button performs the first harvest, QAQC, address enrichment,
automated geocoding, sample creation, and coverage analysis. It deliberately pauses
before gap fill so a person can inspect the coverage recommendations before creating
more research jobs.

## How Decisions Are Divided

- The **Geographer Agent** proposes search-language and vernacular adjustments for the
  requested geography and facility scope.
- The deterministic **strategy planner** assigns an ordered set of evidence strategies
  to each job.
- **Harvester Agents** use web research to propose source-backed observations.
- **QAQC Agents** independently revisit the evidence and recommend keep, review, reject,
  or retry.
- **Address Agents** research precise facility addresses for approved observations.
- Python validates schemas, exact quotations, counts, identifiers, URLs, locality,
  country, coordinates, and spatial scope.
- Human reviewers resolve ambiguous coordinates, approve gap-fill work, and digitize
  footprints.

This is an agentic workflow, but a bounded one. Agents research and propose; deterministic
checks and human decisions control what becomes usable output.

## Evidence Strategies

Facility type and evidence strategy are separate concepts. A job may receive several
ordered strategies and the Harvester Agent can use more than one when useful:

- `incident_evacuation`
- `enforcement_inspection`
- `official_event_attendance`
- `routine_dated_attendance`
- `shift_operational_presence`
- `legal_investigative_records`
- `temporary_use_occupancy`
- `research_measured_occupancy`

For example, manufacturing prioritizes incident, shift-presence, and investigative
records; restaurants prioritize inspections and incident evidence; schools add routine
attendance and official events. Temporary-use evidence is reserved for intermittently
occupied places such as arenas, halls, theaters, event venues, and shelters.

Capacity pages, directories, map listings, encyclopedias, travel guides, and unsourced
social reposts can suggest leads but do not qualify as accepted occupancy evidence.
Ticket sales, scheduled staffing, enrollment, and maximum capacity must not silently
become claims about physical presence.

Profile subtypes can now provide PDT occurrence hints such as day/open use, night/closed
use, episodic use, expected occupancy groups, and contextual count fields. These hints
guide search and interpretation without weakening the evidence contract. For example,
school enrollment, hotel room count, hospital licensed beds, airport annual passengers,
or factory workforce size may help understand a facility, but they are not direct
observed occupancy unless the source ties the count to a bounded date, time, event,
shift, inspection, incident, or measured period.

## Install and Launch

### Requirements

- Python 3.12 or newer
- Codex CLI installed and authenticated
- A local clone of this repository

OASIS uses the locally authenticated Codex CLI for its default agent workflow. It does
not require an application-owned OpenAI API key.

### Windows

Double-click:

```text
OASIS.bat
```

### macOS

Double-click:

```text
OASIS.command
```

The launchers:

1. create a local `.venv` when needed;
2. bootstrap `pip` inside that environment if it is missing;
3. install the browser-app dependencies;
4. locate the authenticated Codex CLI;
5. start OASIS and open `http://127.0.0.1:8771`.

The previous `Observation Harvester` launcher filenames remain as compatibility
wrappers, so existing shortcuts continue to work.

### Manual installation

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[app,dev]"
.\.venv\Scripts\python -m pdt_observer app
```

On macOS or Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[app,dev]"
.venv/bin/python -m pdt_observer app
```

`pdt_observer`, `pdt-observer`, and `PDT_OBSERVER_MODEL` are retained as technical
compatibility identifiers in this release. The launchers accept `OASIS_PORT` and, on
Windows, `OASIS_CODEX_BIN`; the previous `OBSERVATION_HARVESTER_*` names remain valid
fallbacks.

## Using the Browser Application

### Agentic Workbench

1. Enter a country and, optionally, a region or locality.
2. Choose a facility type, mode, and target.
3. Select **Run Full Pipeline** for the guided workflow, or run individual stages.
4. Follow progress in the project workflow and agent-activity panels.
5. Read or download the full pipeline transcript.
6. Review coverage before deciding whether to run gap fill.
7. Download only records that passed QAQC.

The modes are:

- **Single:** one facility type and optional subtype.
- **Batch:** one child job for each enabled subtype in a facility family.
- **Campaign:** one child job for every selected locality/facility-type pair. If no
  locality is provided, each selected facility type receives one countrywide job; OASIS
  does not automatically invent a nationwide locality plan.

Campaign and gap-fill children run concurrently, up to three jobs at once by default.
Their artifacts remain isolated and are consolidated in deterministic job order.

### Geometry Studio

Geometry Studio loads QAQC-approved observations and supports:

- Geocoding all accepted observations with visible progress and a result summary.
- Rejecting coordinates outside the campaign country/region/locality extent.
- Retrying address research when geocoding exposes an address problem.
- Reviewing ranked coordinate candidates.
- Opening pre-populated Google Search or Google Maps queries in a new browser tab.
- Pasting `latitude, longitude` or a Google Maps URL, previewing the point, and saving
  it only after human confirmation.
- Clicking the map to assign or adjust a coordinate.
- Drawing and editing a building-footprint polygon.
- Saving multiple geometry records on an observation and computing `area_m2` for the
  active footprint.

Automated geocoding uses OpenStreetMap Nominatim and a local cache. Google links are
human research aids; OASIS does not call the Google Maps API or scrape Google results.

### Tabular Data

Tabular Data turns the selected run, batch, campaign, or sample set into one row per
occupancy count. It defaults to **Verified Only**, joining harvest leads with QAQC,
address enrichment, and saved geometry. For run-level debugging, **All Leads** shows
raw harvested leads before QAQC. Visible rows can be searched, sorted, copied, exported
as CSV from the browser, or opened directly in Geometry Studio when a geometry item is
available.

For sample sets, Tabular Data is also the human curation checkpoint. A reviewer may
approve the entire sample immediately without selecting individual rows or supplying
feedback. If unsuitable observations are found, the reviewer selects only those items,
assigns an exclusion reason, and may add a note. Exclusions are non-destructive: they
remain visible and can be restored, but are omitted from curated exports, geometry,
coverage calculations, and gap-fill input. The coverage and gap-fill agents receive
the rejected observations as bounded negative examples; when there are no exclusions,
no corrective prompt guidance is added. Changing an exclusion makes the prior approval
stale and requires reapproval before coverage can run.

## Artifacts and Data

OASIS stores work locally in the repository workspace:

| Directory | Contents |
| --- | --- |
| `work/` | Rendered prompts and temporary job inputs |
| `lead_runs/` | Proposed lead JSON |
| `strategy_runs/` | Strategy Scout recommendations |
| `agent_activity/` | Public Harvester activity sidecars |
| `harvest_runs/` | Single, batch, and campaign manifests |
| `harvest_logs/` | Runtime activity logs |
| `job_runs/` | Durable app job records for queued/running/completed/failed work |
| `qaqc_runs/` | Evidence-verification results |
| `address_runs/` | Address-enrichment results |
| `sample_sets/` | Combined sample manifests and rounds |
| `curation_runs/` | Durable human exclusions and sample-approval snapshots |
| `coverage_runs/` | Coverage reviews and recommendations |
| `geometry_reviews/` | Durable human spatial-review records |
| `geocode_cache/` | Cached geocoder responses |
| `exports/` | CSV, JSON, JSONL, and GeoJSON outputs |
| `runs/` | Promoted strict investigation records |

**Clear All** removes generated working history but preserves promoted observations,
exports, profiles, geometry reviews, curation reviews, and source code. Geometry and
curation review are considered durable human work.

Every app-launched background job also writes a `job_runs/*.job.json` record before
work starts. This lets the UI show queued or failed work even when a manifest has not
been produced yet, and old running jobs from a previous app session remain visible as
inactive/non-cancellable history.

New durable artifacts include `schema_version` fields. Legacy runtime JSON can be
inspected or conservatively upgraded with the artifact commands below; migrations add
missing versions and write `.bak` backups before mutating files.

The local browser app assumes an internet-connected user machine. Leaflet and
Leaflet.draw load from public CDN URLs, and map tiles come from the configured OSM/Esri
tile services.

The repository does not yet provide a shared global observation pool or hosted
multi-user database. Sharing and aggregating reviewed datasets is a future product
layer, not an implicit side effect of running the local app.

## Command-Line Examples

The CLI retains its original command and module names for compatibility.

Run one harvest:

```powershell
python -m pdt_observer harvest run `
  --country US `
  --locality Georgia `
  --facility-type manufacturing `
  --target 10
```

Run all enabled subtypes in a facility family:

```powershell
python -m pdt_observer harvest batch-run `
  --country US `
  --locality Georgia `
  --facility-type schools `
  --target 10
```

Run a campaign:

```powershell
python -m pdt_observer harvest campaign-run `
  --country PH `
  --locality Manila `
  --locality Makati `
  --facility-type schools `
  --facility-type restaurants `
  --target 10
```

Validate and summarize harvested leads:

```powershell
python -m pdt_observer leads validate lead_runs/<file>.json
python -m pdt_observer leads summarize lead_runs/<file>.json
```

Inspect or migrate local runtime artifacts:

```powershell
python -m pdt_observer artifacts inspect --workspace .
python -m pdt_observer artifacts migrate --workspace . --dry-run
python -m pdt_observer artifacts migrate --workspace .
```

Create a sample and prepare coverage work:

```powershell
python -m pdt_observer samples create-from-run <run-id> `
  --sample-set-id <sample-id>
python -m pdt_observer samples coverage-prompt <sample-id> `
  --output work/<sample-id>-coverage.md
```

Run `python -m pdt_observer --help` for the complete command tree.

## Optional API-Backed Mode

The default workflow uses Codex subscription authentication. A direct OpenAI Agents SDK
path remains available for experiments:

```powershell
.\.venv\Scripts\python -m pip install -e ".[api-agent,dev]"
python -m pdt_observer investigate-api examples/milltown_task.json
```

This optional mode requires `OPENAI_API_KEY`. Never commit keys or generated `.env`
files. The model can be overridden with `PDT_OBSERVER_MODEL`.

## Verification

The ordinary test suite is offline and does not require API credentials:

```powershell
pytest
ruff check .
mypy src
```

Tests must not make unapproved network calls. External services remain behind typed
interfaces, and model output must pass deterministic validation.

## Current Boundaries and Next Development Areas

Implemented now:

- End-to-end local human-AI workflow.
- Localized search guidance and multi-strategy harvesting.
- Concurrent campaign and coverage-gap jobs.
- Evidence QAQC, address research, and spatial validation.
- Optional non-destructive sample curation that informs coverage and gap fill.
- Human coordinate resolution and building-footprint area.
- Local sample, transcript, and verified-export artifacts.

Not implemented yet:

- Automatic nationwide locality planning.
- A hosted or shared multi-user observation pool.
- Background scheduling or unattended large-scale crawling.
- Automatic floor-count or total-floor-area estimation.
- Production authentication, permissions, database storage, or deployment.

Likely next steps are to harden the complete pilot workflow, improve recovery and
observability across long-running agent stages, expand evaluation datasets, and then
design floor-count/total-floor-area enrichment and shared-data architecture on top of
the validated observation pipeline.

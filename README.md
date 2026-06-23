# PDT Observation Harvester

This is a first runnable vertical slice of a Population Density Table observation-harvesting
agent. It performs one bounded investigation over mock data: find a source, retrieve it, extract
one explicit count of people physically present at a named place, geocode the place, and validate
the candidate before returning JSON.

## What Decides What

- The model decides which mock tools to call and proposes one final observation in agent mode.
- The tools search mock source records, fetch complete mock documents, and geocode against mock
  place records.
- Application code owns service interfaces, tool execution, Pydantic models, CLI behavior, and
  deterministic validation.
- State exists only during one process run: the task, mock documents, mock places, tool calls, and
  final proposed result.

Deterministic validation is required because model output is only a proposal. The validator checks
that the source exists, the quote is exact, the count appears in the quote, the geocoder result is
real, and the returned coordinates match the selected place. Offline tests do not call an LLM
because they are meant to prove the surrounding application loop without network, cost, or
non-determinism.

## Install

Use Python 3.12 or newer.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
```

Copy `.env.example` only if you want to run agent mode. Do not store a real key in source control.

## Run

Offline demo mode requires no API key and makes no network calls:

```powershell
python -m pdt_observer demo
```

Agent mode uses the OpenAI Agents SDK with the same mock services and validator:

```powershell
$env:OPENAI_API_KEY = "sk-..."
python -m pdt_observer investigate examples/milltown_task.json
```

The model defaults to `gpt-5.4-mini`. Override it with `PDT_OBSERVER_MODEL`.

## Verify

```powershell
pytest
ruff check .
mypy
```

The normal test suite is offline and deterministic. Any future live SDK test should be placed
behind an explicit marker and environment-variable gate.

## Replacing The Mocks Later

Real search and geocoding providers should implement the protocols in `ports.py`. The agent tools
should keep the same narrow shapes:

- `search_sources(query, max_results)`
- `fetch_source(document_id)`
- `geocode_place(name, locality, country)`

That keeps the model-facing tool surface stable while application code changes the backends.

## Outside This Proof Of Concept

This project does not include continuous scheduling, real web search, scraping, social media,
databases, building footprints, floor counting, occupancy estimation, Docker, orchestration
systems, multiple cooperating agents, or a graphical UI.

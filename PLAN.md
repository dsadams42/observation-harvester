# PDT Observation Harvester Plan

## Assumptions

- The first vertical slice is intentionally bounded to one in-memory investigation run.
- Offline demo mode is the primary proof of correctness and must never require `OPENAI_API_KEY`.
- Codex-operated prompted, scheduled, and profile-batch runs are the default subscription-backed
  workflow.
- Agent mode uses the same domain models, mock service interfaces, and deterministic validator as
  offline mode, but is optional and explicit.
- The repository is empty at start, so this project establishes the initial package structure and tooling.

## Architecture

1. `models.py` defines Pydantic v2 domain and tool-result models.
2. `ports.py` defines typed protocols for search, document fetching, and geocoding.
3. `mock_data.py` stores source documents and place records.
4. `mock_services.py` implements the protocols over the in-memory data.
5. `validation.py` checks proposed observations against stored source and place records.
6. `agent.py` contains agent instructions, tool wrappers, scripted offline demo logic, and the optional OpenAI Agents SDK runner.
7. `cli.py` provides the `demo`, `validate`, `summarize`, and `investigate-api` commands.
8. `workflow.py` and `profiles.py` provide file-backed batch/work/review orchestration.
9. `web.py` provides no-key direct URL fetching and fixture-testable parsing.
10. `prompts/` contains durable Codex prompts for prompted investigation, scheduled harvest,
    building-type agents, and candidate review.
9. Tests cover mock behavior, accepted-result validation, negative validation cases, review/not-found outcomes, model rules, and CLI JSON output.

## Planned File Tree

```text
AGENTS.md
README.md
PLAN.md
pyproject.toml
.env.example
.gitignore
examples/
    milltown_task.json
    milltown_codex_run.json
prompts/
    building_type_agent.md
    prompted_investigation.md
    review_candidate.md
    scheduled_harvest.md
profiles/
    public_venues.json
src/
    pdt_observer/
        __init__.py
        __main__.py
        agent.py
        cli.py
        config.py
        mock_data.py
        mock_services.py
        models.py
        ports.py
        validation.py
tests/
    test_cli.py
    test_mock_services.py
    test_models.py
    test_validation.py
```

## Acceptance Criteria

- `python -m pdt_observer demo` emits a validated accepted Blue Lantern JSON result with count `17`.
- `python -m pdt_observer validate examples/milltown_codex_run.json` validates a Codex-produced run artifact without API access.
- `python -m pdt_observer batch create --locality Milltown --country US --profiles public_venues`
  creates profile-specific work items.
- `python -m pdt_observer review ingest examples/milltown_codex_run.json` writes a review queue
  item.
- `python -m pdt_observer investigate-api examples/milltown_task.json` requires `OPENAI_API_KEY` before invoking the SDK.
- The package installs in editable mode.
- The ordinary test suite is deterministic, offline, and does not require an API key.
- `pytest`, `ruff`, and `mypy` all pass.
- README and AGENTS.md document setup, responsibilities, and repository rules.

## Final Implementation Notes

- Implemented the planned src-layout package with Pydantic v2 models, typed service protocols,
  in-memory source and geocoder mocks, deterministic extraction, deterministic validation, and
  a standard-library `argparse` CLI.
- Offline demo mode searches, fetches, extracts, geocodes, validates, and emits the accepted
  17-person Blue Lantern observation as formatted JSON without an API key.
- Codex-operated run validation is now the default no-API path: Codex writes an
  `InvestigationRun` JSON file and the CLI validates it locally.
- Added no-key Codex orchestration artifacts: public-venue building profiles, batch creation,
  work claiming, review queue ingestion/listing, JSONL export, and direct URL fetching/parsing.
- Added quota-governed work progress: each work item tracks accepted/review/not_found counts,
  source outcomes, run files, runtime, and deterministic stop reasons.
- Agent mode constructs an OpenAI Agents SDK `Agent` with three narrow function tools, centralizes
  the model name in settings, enforces `maximum_agent_turns`, and validates the model proposal
  before returning it.
- API-backed agent dependencies were moved behind an optional `api-agent` extra while remaining
  available in the `dev` extra for local type checking.
- Installed and verified with `openai-agents 0.17.6`, `pytest`, `ruff`, and `mypy` in a local
  `.venv`.
- The workspace directory was empty but not initialized as a Git repository; no commit was made.

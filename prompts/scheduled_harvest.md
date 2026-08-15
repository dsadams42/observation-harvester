# Scheduled PDT Harvest

Use this prompt for a Codex app automation attached to this repository.

Each run should:

1. Select one bounded locality and `people_present` investigation target from the user's backlog or
   explicit instructions.
2. Search for a recent source that may contain evidence matching the selected profile's count
   method: either direct people-present evidence or a source-backed component input.
3. Create one `InvestigationRun` or harvest evidence JSON artifact using the shape in
   `examples/milltown_codex_run.json`. Preserve exact observation-time text and supported
   `time_context` when available for direct observations; preserve unit, time basis, geography
   level, and exact quote for component inputs.
4. Run `python -m pdt_observer validate <run-file>`.
5. Run `python -m pdt_observer review ingest <run-file>`.
6. Report only meaningful findings:
   - accepted observations,
   - review cases that need human geocoding/source review,
   - validator failures that indicate the prompt or source bundle needs adjustment.

Do not run continuously. Do not scrape at scale. Do not make broad network changes or commit files
unless the user explicitly asks.
Do not calculate final occupancy estimates from component inputs in scheduled harvests.

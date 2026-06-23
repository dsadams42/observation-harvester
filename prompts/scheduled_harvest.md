# Scheduled PDT Harvest

Use this prompt for a Codex app automation attached to this repository.

Each run should:

1. Select one bounded locality and `people_present` investigation target from the user's backlog or
   explicit instructions.
2. Search for a recent source that may contain an explicit count of people physically present at a
   named place.
3. Create one `InvestigationRun` JSON artifact using the shape in
   `examples/milltown_codex_run.json`.
4. Run `python -m pdt_observer validate <run-file>`.
5. Run `python -m pdt_observer review ingest <run-file>`.
6. Report only meaningful findings:
   - accepted observations,
   - review cases that need human geocoding/source review,
   - validator failures that indicate the prompt or source bundle needs adjustment.

Do not run continuously. Do not scrape at scale. Do not make broad network changes or commit files
unless the user explicitly asks.

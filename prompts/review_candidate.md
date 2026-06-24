# Review Candidate Observation

Review an existing `InvestigationRun` JSON file.

Steps:

1. Run deterministic validation with `python -m pdt_observer validate <run-file>`.
2. Read each validator error.
3. Inspect the source bundle and candidate result.
4. Propose the smallest correction:
   - fix the quote only if an exact substring exists,
   - fix the count only if it appears in the exact quote,
   - fix `observed_time_text` or `time_context` only when it is copied from and consistent with
     the source quote,
   - fix the georeference only if the place match is unambiguous,
   - downgrade to `review` or `not_found` when the evidence does not satisfy the rules.
5. Re-run validation after changes.

Do not bypass the validator or invent missing source support.

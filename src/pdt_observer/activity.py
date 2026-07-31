from __future__ import annotations

from pathlib import Path

from pdt_observer.models import HarvesterActivityReport


def harvester_activity_path(root: Path, run_id: str) -> Path:
    return root / "agent_activity" / f"{run_id}.harvester.json"


def load_harvester_activity_report(path: Path) -> HarvesterActivityReport:
    return HarvesterActivityReport.model_validate_json(path.read_text(encoding="utf-8"))


def render_activity_prompt_instructions(
    *,
    run_id: str,
    activity_path: Path,
) -> str:
    return f"""## Public Harvester Activity Report

In addition to the strict lead JSON array, write a concise public activity report to:

`{activity_path}`

This is for the user-facing Agent Dialogue transcript. Report observable search activity and
decisions, not hidden chain-of-thought. Keep notes short and operational: which strategies you
tried, query examples, what was productive, what was only context, and what follow-up may help.

Use exactly this JSON schema for the sidecar file:

{{
  "run_id": "{run_id}",
  "overall_summary": "Two or three concise public sentences about the harvest.",
  "strategy_activity": [
    {{
      "strategy_id": "One orchestrator-recommended strategy ID",
      "outcome": "productive | partially_productive | unproductive | review_only",
      "query_examples": ["Search query or source family tried."],
      "notes": "Public summary of what this strategy found or why it was not useful.",
      "accepted_lead_count": 0
    }}
  ],
  "accepted_lead_count": 0,
  "rejected_or_context_notes": [
    "Example: licensed-bed pages were treated as context because they did not report occupied beds."
  ],
  "follow_up_suggestions": ["Concrete follow-up search direction, or empty array."]
}}
"""


def activity_report_dialogue_message(report: HarvesterActivityReport) -> tuple[str, str]:
    outcomes: list[str] = []
    for item in report.strategy_activity:
        outcomes.append(
            f"{item.strategy_id.value}: {item.outcome.value} ({item.accepted_lead_count})"
        )
    message = report.overall_summary
    rationale_parts: list[str] = []
    if outcomes:
        rationale_parts.append("Strategy outcomes: " + "; ".join(outcomes) + ".")
    if report.rejected_or_context_notes:
        rationale_parts.append(
            "Context/rejection notes: " + " ".join(report.rejected_or_context_notes[:3])
        )
    if report.follow_up_suggestions:
        rationale_parts.append(
            "Follow-up suggestions: " + " ".join(report.follow_up_suggestions[:3])
        )
    return message, " ".join(rationale_parts) if rationale_parts else ""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter

from pdt_observer.models import AgentDialogueEntry
from pdt_observer.storage import write_json_file

_ENTRY_LIST_ADAPTER: TypeAdapter[tuple[AgentDialogueEntry, ...]] = TypeAdapter(
    tuple[AgentDialogueEntry, ...]
)
_DIALOGUE_LOCK = threading.Lock()
_STAGE_LABELS = {
    "vernacular_review": "GEOGRAPHIC REVIEW",
    "job_dispatch": "JOB COORDINATION",
    "lead_harvest": "INITIAL HARVEST",
    "job_consolidation": "HARVEST CONSOLIDATION",
    "qaqc": "EVIDENCE QAQC",
    "address_enrichment": "ADDRESS ENRICHMENT",
    "address_spatial_retry": "ADDRESS-SPATIAL CORRECTION",
    "automated_geocoding": "AUTOMATED GEOCODING",
    "coordinate_assignment": "HUMAN COORDINATE ASSIGNMENT",
    "coverage_review": "COVERAGE ANALYSIS",
    "gap_fill": "COVERAGE GAP FILL",
}
_STAGE_ORDER = {stage: index for index, stage in enumerate(_STAGE_LABELS)}


def _utc_now_text() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def dialogue_path(root: Path, conversation_id: str) -> Path:
    return root / "agent_dialogue" / f"{conversation_id}.json"


def load_dialogue(root: Path, conversation_id: str) -> tuple[AgentDialogueEntry, ...]:
    path = dialogue_path(root, conversation_id)
    if not path.is_file():
        return ()
    return _ENTRY_LIST_ADAPTER.validate_json(path.read_text(encoding="utf-8"))


def append_dialogue(
    root: Path,
    conversation_id: str,
    *,
    speaker: str,
    stage: str,
    message: str,
    rationale: str | None = None,
) -> AgentDialogueEntry:
    entry = AgentDialogueEntry(
        speaker=speaker,
        stage=stage,
        message=message,
        rationale=rationale,
        created_at=_utc_now_text(),
    )
    path = dialogue_path(root, conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _DIALOGUE_LOCK:
        entries = load_dialogue(root, conversation_id)
        payload = [item.model_dump(mode="json") for item in (*entries, entry)]
        write_json_file(path, payload)
    return entry


def render_dialogue(entries: tuple[AgentDialogueEntry, ...]) -> str:
    if not entries:
        return ""
    grouped: dict[str, list[AgentDialogueEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.stage, []).append(entry)
    blocks: list[str] = []
    ordered_stages = sorted(
        grouped,
        key=lambda stage: (_STAGE_ORDER.get(stage, len(_STAGE_ORDER)), stage),
    )
    for stage in ordered_stages:
        blocks.append(f"=== {_STAGE_LABELS.get(stage, stage.replace('_', ' ').upper())} ===")
        for entry in sorted(grouped[stage], key=lambda item: item.created_at):
            block = f"[{entry.created_at}] {entry.speaker}: {entry.message}"
            if entry.rationale:
                block += f"\nWhy: {entry.rationale}"
            blocks.append(block)
    return "\n\n".join(blocks) + "\n"


def combine_dialogue(
    *entry_groups: tuple[AgentDialogueEntry, ...],
) -> tuple[AgentDialogueEntry, ...]:
    combined: list[AgentDialogueEntry] = []
    seen: set[tuple[str, str, str, str | None, str]] = set()
    for entries in entry_groups:
        group_identities: set[tuple[str, str, str, str | None, str]] = set()
        for entry in entries:
            identity = (
                entry.speaker,
                entry.stage,
                entry.message,
                entry.rationale,
                entry.created_at,
            )
            if identity in seen:
                continue
            combined.append(entry)
            group_identities.add(identity)
        seen.update(group_identities)
    return tuple(combined)

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pdt_observer.models import (
    CurationApproval,
    CurationDecision,
    CurationReasonCode,
    SampleCurationManifest,
)
from pdt_observer.workflow import slugify, utc_now_text, write_model

REASON_LABELS: dict[CurationReasonCode, str] = {
    CurationReasonCode.DUPLICATE: "Duplicate observation",
    CurationReasonCode.WRONG_FACILITY: "Wrong facility",
    CurationReasonCode.OUTSIDE_GEOGRAPHIC_SCOPE: "Outside geographic scope",
    CurationReasonCode.EVIDENCE_INSUFFICIENT: "Evidence insufficient",
    CurationReasonCode.INCORRECT_COUNT_MEANING: "Incorrect count meaning",
    CurationReasonCode.UNREPRESENTATIVE: "Unrepresentative or atypical observation",
    CurationReasonCode.ADDRESS_OR_COORDINATE_UNRESOLVED: (
        "Address or coordinate unresolved"
    ),
    CurationReasonCode.FACILITY_TYPE_NOT_RELEVANT: "Facility type not relevant",
    CurationReasonCode.OTHER: "Other",
}


def curation_path(root: Path, sample_set_id: str) -> Path:
    return root / "curation_runs" / f"{sample_set_id}.json"


def load_curation(root: Path, sample_set_id: str) -> SampleCurationManifest:
    path = curation_path(root, sample_set_id)
    if path.is_file():
        return SampleCurationManifest.model_validate_json(path.read_text(encoding="utf-8"))
    return SampleCurationManifest(
        sample_set_id=sample_set_id,
        updated_at=utc_now_text(),
    )


def save_curation(root: Path, manifest: SampleCurationManifest) -> SampleCurationManifest:
    write_model(curation_path(root, manifest.sample_set_id), manifest)
    return manifest


def excluded_item_ids(manifest: SampleCurationManifest) -> frozenset[str]:
    return frozenset(decision.item_id for decision in manifest.decisions)


def set_exclusions(
    root: Path,
    sample_set_id: str,
    *,
    item_ids: Sequence[str],
    reason_code: CurationReasonCode,
    reason_note: str | None = None,
) -> SampleCurationManifest:
    cleaned_note = reason_note.strip() if reason_note else None
    if reason_code == CurationReasonCode.OTHER and not cleaned_note:
        raise ValueError("a reason note is required when exclusion reason is other")
    requested = tuple(dict.fromkeys(item_id.strip() for item_id in item_ids if item_id.strip()))
    if not requested:
        raise ValueError("select at least one observation to exclude")
    manifest = load_curation(root, sample_set_id)
    decisions = {decision.item_id: decision for decision in manifest.decisions}
    now = utc_now_text()
    for item_id in requested:
        decisions[item_id] = CurationDecision(
            item_id=item_id,
            reason_code=reason_code,
            reason_note=cleaned_note,
            excluded_at=now,
        )
    return save_curation(
        root,
        manifest.model_copy(
            update={
                "decisions": tuple(decisions[key] for key in sorted(decisions)),
                "updated_at": now,
            }
        ),
    )


def restore_items(
    root: Path,
    sample_set_id: str,
    *,
    item_ids: Sequence[str],
) -> SampleCurationManifest:
    requested = frozenset(item_id.strip() for item_id in item_ids if item_id.strip())
    if not requested:
        raise ValueError("select at least one observation to restore")
    manifest = load_curation(root, sample_set_id)
    return save_curation(
        root,
        manifest.model_copy(
            update={
                "decisions": tuple(
                    decision
                    for decision in manifest.decisions
                    if decision.item_id not in requested
                ),
                "updated_at": utc_now_text(),
            }
        ),
    )


def curation_fingerprint(
    item_ids: Sequence[str],
    manifest: SampleCurationManifest,
) -> str:
    payload = {
        "item_ids": sorted(set(item_ids)),
        "decisions": [
            {
                "item_id": decision.item_id,
                "reason_code": decision.reason_code.value,
                "reason_note": decision.reason_note or "",
            }
            for decision in sorted(manifest.decisions, key=lambda item: item.item_id)
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def approval_status(
    manifest: SampleCurationManifest,
    item_ids: Sequence[str],
) -> str:
    if manifest.approval is None:
        return "not_approved"
    if manifest.approval.fingerprint != curation_fingerprint(item_ids, manifest):
        return "stale"
    return "approved"


def approve_curation(
    root: Path,
    sample_set_id: str,
    *,
    item_ids: Sequence[str],
) -> SampleCurationManifest:
    manifest = load_curation(root, sample_set_id)
    all_ids = tuple(sorted(set(item_ids)))
    excluded = excluded_item_ids(manifest).intersection(all_ids)
    now = utc_now_text()
    fingerprint = curation_fingerprint(all_ids, manifest)
    approval = CurationApproval(
        snapshot_id=slugify(
            f"{sample_set_id}-curation-{now}-{fingerprint[:12]}"
        ),
        fingerprint=fingerprint,
        approved_at=now,
        included_count=max(len(all_ids) - len(excluded), 0),
        excluded_count=len(excluded),
    )
    return save_curation(
        root,
        manifest.model_copy(update={"approval": approval, "updated_at": now}),
    )


def ensure_current_approval(
    manifest: SampleCurationManifest,
    item_ids: Sequence[str],
) -> CurationApproval:
    status = approval_status(manifest, item_ids)
    if status == "not_approved":
        raise ValueError("human curation approval is required before coverage analysis")
    if status == "stale":
        raise ValueError("human curation approval is stale; review and approve the sample again")
    assert manifest.approval is not None
    return manifest.approval


def curation_summary(
    manifest: SampleCurationManifest,
    item_ids: Sequence[str],
) -> dict[str, Any]:
    all_ids = frozenset(item_ids)
    decisions = tuple(
        decision for decision in manifest.decisions if decision.item_id in all_ids
    )
    excluded = frozenset(decision.item_id for decision in decisions)
    return {
        "sample_set_id": manifest.sample_set_id,
        "total_count": len(all_ids),
        "included_count": len(all_ids - excluded),
        "excluded_count": len(excluded),
        "approval_status": approval_status(manifest, tuple(all_ids)),
        "approval": (
            manifest.approval.model_dump(mode="json")
            if manifest.approval is not None
            else None
        ),
        "decisions": [decision.model_dump(mode="json") for decision in decisions],
    }


def rejected_examples(
    manifest: SampleCurationManifest,
    records: Sequence[dict[str, Any]],
    *,
    facility_type: str | None = None,
    locality: str | None = None,
    limit: int = 8,
) -> tuple[dict[str, Any], ...]:
    decisions = {decision.item_id: decision for decision in manifest.decisions}
    examples: list[tuple[int, dict[str, Any]]] = []
    for record in records:
        item_id = str(record.get("item_id") or "")
        decision = decisions.get(item_id)
        if decision is None:
            continue
        lead_value = record.get("lead")
        lead: dict[str, Any] = lead_value if isinstance(lead_value, dict) else {}
        location_value = lead.get("location")
        location: dict[str, Any] = (
            location_value if isinstance(location_value, dict) else {}
        )
        record_type = str(record.get("facility_type") or "")
        city = str(location.get("city_or_region") or "")
        match_score = 0
        if facility_type and record_type == facility_type:
            match_score += 2
        if locality and locality.casefold() in city.casefold():
            match_score += 1
        examples.append(
            (
                match_score,
                {
                    "item_id": item_id,
                    "facility_type": record_type,
                    "facility_name": location.get("facility_name") or "Unknown",
                    "city_or_region": city or "Unknown",
                    "source_url": lead.get("source_url") or "",
                    "strategy_id": lead.get("strategy_id") or "",
                    "count_semantics": lead.get("count_semantics") or "",
                    "reason_code": decision.reason_code.value,
                    "reason": REASON_LABELS[decision.reason_code],
                    "reason_note": decision.reason_note or "",
                },
            )
        )
    examples.sort(key=lambda item: (-item[0], str(item[1]["item_id"])))
    return tuple(example for _, example in examples[:limit])


def render_gap_fill_curation_guidance(examples: Sequence[dict[str, Any]]) -> str:
    if not examples:
        return ""
    return f"""## Human Curation Guidance From Earlier Rounds

The following observations were explicitly excluded by a human reviewer. Use them as bounded
negative examples for this gap-fill job. Do not rediscover the same source/facility observation,
and apply the stated correction without weakening the ordinary evidence requirements. These are
local project instructions, not permanent changes to the OASIS facility profile.

{json.dumps(list(examples), indent=2)}
"""

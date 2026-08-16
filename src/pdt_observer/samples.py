from __future__ import annotations

import json
import subprocess
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from pdt_observer.addresses import (
    bundle_is_addressable_candidate,
    bundle_readiness,
    merge_address_results,
)
from pdt_observer.curation import (
    curation_summary,
    ensure_current_approval,
    excluded_item_ids,
    load_curation,
    rejected_examples,
    render_gap_fill_curation_guidance,
)
from pdt_observer.dialogue import append_dialogue
from pdt_observer.geographer import run_geographer
from pdt_observer.geometry import approved_records_for_child, merge_geometry_items
from pdt_observer.harvest import CodexRunner, append_harvest_log, run_harvest
from pdt_observer.leads import load_evidence_set, load_qaqc_review_set
from pdt_observer.models import (
    CoverageDispersionStatus,
    CoverageSteeringReview,
    HarvestBatchRunManifest,
    HarvestCampaignRunManifest,
    HarvestRunManifest,
    HarvestRunStatus,
    RecommendedGapFillJob,
    SampleSetManifest,
    SampleSetRound,
    SampleSetRoundRole,
)
from pdt_observer.workflow import slugify, utc_now_text, write_model

COVERAGE_REVIEW_ADAPTER: TypeAdapter[CoverageSteeringReview] = TypeAdapter(
    CoverageSteeringReview
)


def sample_set_path(root: Path, sample_set_id: str) -> Path:
    return root / "sample_sets" / f"{sample_set_id}.json"


def coverage_output_path(root: Path, coverage_id: str) -> Path:
    return root / "coverage_runs" / f"{coverage_id}.json"


def coverage_prompt_path(root: Path, coverage_id: str) -> Path:
    return root / "work" / f"{coverage_id}.md"


def load_sample_set(root: Path, sample_set_id: str) -> SampleSetManifest:
    path = sample_set_path(root, sample_set_id)
    if not path.is_file():
        raise ValueError(f"sample set not found: {sample_set_id}")
    return SampleSetManifest.model_validate_json(path.read_text(encoding="utf-8"))


def save_sample_set(root: Path, sample_set: SampleSetManifest) -> SampleSetManifest:
    write_model(sample_set_path(root, sample_set.sample_set_id), sample_set)
    return sample_set


def load_coverage_review(path: Path) -> CoverageSteeringReview:
    return COVERAGE_REVIEW_ADAPTER.validate_json(path.read_text(encoding="utf-8"))


def coverage_review_to_json(review: CoverageSteeringReview) -> str:
    return json.dumps(review.model_dump(mode="json"), indent=2)


def _run_manifest_path(root: Path, run_id: str) -> Path:
    return root / "harvest_runs" / f"{run_id}.json"


def _batch_manifest_path(root: Path, batch_id: str) -> Path:
    return root / "harvest_runs" / f"{batch_id}.batch.json"


def _campaign_manifest_path(root: Path, campaign_id: str) -> Path:
    return root / "harvest_runs" / f"{campaign_id}.campaign.json"


def load_any_harvest_manifest(root: Path, run_id: str) -> Any:
    run_path = _run_manifest_path(root, run_id)
    if run_path.is_file():
        return HarvestRunManifest.model_validate_json(run_path.read_text(encoding="utf-8"))
    batch_path = _batch_manifest_path(root, run_id)
    if batch_path.is_file():
        return HarvestBatchRunManifest.model_validate_json(batch_path.read_text(encoding="utf-8"))
    campaign_path = _campaign_manifest_path(root, run_id)
    if campaign_path.is_file():
        return HarvestCampaignRunManifest.model_validate_json(
            campaign_path.read_text(encoding="utf-8")
        )
    raise ValueError(f"harvest manifest not found: {run_id}")


def _failed_gap_fill_manifest(
    *,
    root: Path,
    run_id: str,
    job: RecommendedGapFillJob,
    error_message: str,
) -> HarvestRunManifest:
    path = _run_manifest_path(root, run_id)
    try:
        manifest = HarvestRunManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        manifest = HarvestRunManifest(
            run_id=run_id,
            status=HarvestRunStatus.FAILED,
            country=job.country,
            locality=job.locality,
            profile_set=job.facility_type,
            profile_id=None,
            target=job.target,
            prompt_path=str(root / "work" / f"{run_id}.md"),
            lead_path=str(root / "lead_runs" / f"{run_id}.json"),
            started_at=utc_now_text(),
            validation_valid=False,
            log_path=str(root / "harvest_logs" / f"{run_id}.log"),
        )
    append_harvest_log(root, run_id, f"Gap-fill child failed: {error_message}.")
    write_model(
        path,
        manifest.model_copy(
            update={
                "status": HarvestRunStatus.FAILED,
                "completed_at": utc_now_text(),
                "validation_valid": False,
                "error_message": error_message,
            }
        ),
    )
    return HarvestRunManifest.model_validate_json(path.read_text(encoding="utf-8"))


def child_run_ids_for_manifest(manifest: Any) -> tuple[str, ...]:
    run_id = getattr(manifest, "run_id", None)
    if run_id is not None:
        return (str(run_id),)
    return tuple(str(run_id) for run_id in getattr(manifest, "child_run_ids", ()))


def _manifest_identity(manifest: Any) -> str:
    return str(
        getattr(manifest, "run_id", None)
        or getattr(manifest, "batch_id", None)
        or getattr(manifest, "campaign_id", None)
    )


def _manifest_facility_types(manifest: Any) -> tuple[str, ...]:
    facility_types = getattr(manifest, "facility_types", None)
    if facility_types:
        return tuple(str(item) for item in facility_types)
    profile_set = getattr(manifest, "profile_set", None)
    return (str(profile_set),) if profile_set else ()


def _manifest_localities(manifest: Any) -> tuple[str, ...]:
    localities = getattr(manifest, "localities", None)
    if localities:
        return tuple(str(item) for item in localities)
    locality = getattr(manifest, "locality", None)
    return (str(locality),) if locality else ()


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for value in values:
        cleaned = value.strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return tuple(seen)


def _component_bundle_source_leads(
    bundle: Any,
    component_leads: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        component_leads[index]
        for index in bundle.source_lead_indexes
        if 0 <= index < len(component_leads)
    ]


def _addressable_component_bundle_records_for_child(
    root: Path,
    manifest: HarvestRunManifest,
) -> tuple[dict[str, Any], ...]:
    qaqc_path = root / "qaqc_runs" / f"{manifest.run_id}-qaqc.json"
    if not qaqc_path.is_file():
        raise FileNotFoundError(f"QAQC review not found for run: {manifest.run_id}")
    evidence_set = load_evidence_set(Path(manifest.lead_path))
    review_set = load_qaqc_review_set(qaqc_path)
    reviews_by_index = {
        review.bundle_index: review for review in review_set.component_bundle_reviews
    }
    component_leads = [lead.model_dump(mode="json") for lead in evidence_set.component_leads]
    records: list[dict[str, Any]] = []
    for bundle_index, bundle in enumerate(evidence_set.component_bundles):
        review = reviews_by_index.get(bundle_index)
        source_lead_models = [
            evidence_set.component_leads[index]
            for index in bundle.source_lead_indexes
            if 0 <= index < len(evidence_set.component_leads)
        ]
        if review_set.component_bundle_reviews:
            if not bundle_is_addressable_candidate(
                bundle=bundle,
                review=review,
                source_leads=source_lead_models,
            ):
                continue
        elif not bundle.counts_toward_target:
            continue
        readiness = bundle_readiness(
            bundle=bundle,
            review=review,
            source_leads=source_lead_models,
        )
        records.append(
            {
                "item_id": f"{manifest.run_id}-component-bundle-{bundle_index}",
                "child_run_id": manifest.run_id,
                "lead_index": bundle_index,
                "bundle_index": bundle_index,
                "facility_type": manifest.profile_set,
                "component_bundle": bundle.model_dump(mode="json"),
                "component_bundle_qaqc_review": (
                    review.model_dump(mode="json") if review is not None else None
                ),
                "component_leads": _component_bundle_source_leads(bundle, component_leads),
                "bundle_readiness": readiness,
                "model_ready": readiness == "model_ready_bundle",
                "bundle_review_required": readiness != "model_ready_bundle",
            }
        )
    return tuple(records)


def _reviewable_records_for_child(
    root: Path,
    manifest: HarvestRunManifest,
) -> tuple[dict[str, Any], ...]:
    records = tuple(approved_records_for_child(root, manifest))
    records += _addressable_component_bundle_records_for_child(root, manifest)
    return tuple(merge_geometry_items(root, merge_address_results(root, records)))


def _sample_stage_summary(root: Path, child_run_ids: Sequence[str]) -> dict[str, object]:
    approved_count = 0
    geocoded_count = 0
    footprint_count = 0
    qaqc_completed_count = 0
    address_completed_count = 0
    for child_run_id in child_run_ids:
        if (root / "qaqc_runs" / f"{child_run_id}-qaqc.json").is_file():
            qaqc_completed_count += 1
        if (root / "address_runs" / f"{child_run_id}-address.json").is_file():
            address_completed_count += 1
        try:
            manifest = load_any_harvest_manifest(root, child_run_id)
            records = _reviewable_records_for_child(root, manifest)
        except (FileNotFoundError, ValueError):
            continue
        approved_count += len(records)
        for record in records:
            geometry = record.get("geometry")
            if isinstance(geometry, dict) and geometry.get("point") is not None:
                geocoded_count += 1
            if isinstance(geometry, dict) and geometry.get("polygon_geojson") is not None:
                footprint_count += 1
    return {
        "child_run_count": len(child_run_ids),
        "qaqc_completed_count": qaqc_completed_count,
        "address_completed_count": address_completed_count,
        "approved_count": approved_count,
        "geocoded_count": geocoded_count,
        "footprint_count": footprint_count,
    }


def refresh_sample_set(root: Path, sample_set: SampleSetManifest) -> SampleSetManifest:
    refreshed = sample_set.model_copy(
        update={
            "stage_summary": _sample_stage_summary(root, sample_set.combined_child_run_ids),
            "updated_at": utc_now_text(),
        }
    )
    return save_sample_set(root, refreshed)


def create_sample_set_from_run(
    *,
    root: Path,
    run_id: str,
    sample_set_id: str | None = None,
) -> SampleSetManifest:
    manifest = load_any_harvest_manifest(root, run_id)
    child_run_ids = child_run_ids_for_manifest(manifest)
    resolved_id = sample_set_id or slugify(f"{_manifest_identity(manifest)}-sample")
    now = utc_now_text()
    sample_set = SampleSetManifest(
        sample_set_id=resolved_id,
        country=str(manifest.country),
        requested_localities=_manifest_localities(manifest),
        facility_types=_manifest_facility_types(manifest),
        target=int(getattr(manifest, "target", 1)),
        rounds=(
            SampleSetRound(
                round_number=1,
                role=SampleSetRoundRole.INITIAL,
                source_run_ids=(_manifest_identity(manifest),),
                child_run_ids=child_run_ids,
                status=getattr(manifest, "status", HarvestRunStatus.COMPLETED),
                summary=getattr(manifest, "summary", None),
            ),
        ),
        combined_child_run_ids=child_run_ids,
        stage_summary=_sample_stage_summary(root, child_run_ids),
        created_at=now,
        updated_at=now,
    )
    return save_sample_set(root, sample_set)


def sample_records(
    root: Path,
    sample_set: SampleSetManifest,
    *,
    include_excluded: bool = False,
) -> tuple[dict[str, Any], ...]:
    curation = load_curation(root, sample_set.sample_set_id)
    curated_exclusions = frozenset() if include_excluded else excluded_item_ids(curation)
    child_to_round: dict[str, int] = {}
    for round_item in sample_set.rounds:
        for child_run_id in round_item.child_run_ids:
            child_to_round[child_run_id] = round_item.round_number
    records: list[dict[str, Any]] = []
    for child_run_id in sample_set.combined_child_run_ids:
        try:
            manifest = load_any_harvest_manifest(root, child_run_id)
            child_records = _reviewable_records_for_child(root, manifest)
        except (FileNotFoundError, ValueError):
            continue
        for record in child_records:
            if str(record["item_id"]) in curated_exclusions:
                continue
            payload = dict(record)
            payload["sample_set_id"] = sample_set.sample_set_id
            payload["sample_round"] = child_to_round.get(child_run_id)
            payload["facility_type"] = getattr(manifest, "profile_set", "")
            records.append(payload)
    return tuple(records)


def _record_location(record: dict[str, Any]) -> dict[str, Any]:
    lead = record.get("lead")
    if isinstance(lead, dict) and isinstance(lead.get("location"), dict):
        return dict(lead["location"])
    bundle = record.get("component_bundle")
    if isinstance(bundle, dict) and isinstance(bundle.get("location"), dict):
        return dict(bundle["location"])
    address = record.get("address_enrichment")
    if isinstance(address, dict):
        return {
            "facility_name": record.get("facility_name") or address.get("facility_name") or "",
            "city_or_region": address.get("city_or_region") or "",
            "country": address.get("country") or "",
        }
    return {}


def _record_source_url(record: dict[str, Any]) -> str:
    lead = record.get("lead")
    if isinstance(lead, dict):
        return str(lead.get("source_url") or "")
    for component_lead in record.get("component_leads", ()):
        if isinstance(component_lead, dict) and component_lead.get("source_url"):
            return str(component_lead["source_url"])
    return ""


def compute_coverage_summary(
    root: Path,
    sample_set: SampleSetManifest,
) -> dict[str, Any]:
    records = sample_records(root, sample_set)
    city_counts: Counter[str] = Counter()
    facility_counts: Counter[str] = Counter()
    locality_counts: Counter[str] = Counter()
    points: list[tuple[float, float]] = []
    out_of_scope_flags: list[dict[str, str | None]] = []
    duplicate_flags: list[dict[str, str | None]] = []
    duplicate_keys: dict[tuple[str, str], str] = {}
    for record in records:
        location = _record_location(record)
        city = str(location.get("city_or_region") or "Unknown")
        country = str(location.get("country") or "")
        city_counts[city] += 1
        facility_counts[str(record.get("facility_type") or "Unknown")] += 1
        matched_locality = "Unassigned"
        for locality in sample_set.requested_localities:
            if locality.casefold() in city.casefold() or city.casefold() in locality.casefold():
                matched_locality = locality
                break
        locality_counts[matched_locality] += 1
        if country and country.casefold() != sample_set.country.casefold():
            out_of_scope_flags.append(
                {
                    "item_id": str(record["item_id"]),
                    "reason": f"Lead country is {country}, not {sample_set.country}.",
                }
            )
        key = (
            _record_source_url(record),
            str(location.get("facility_name") or "").casefold(),
        )
        if key in duplicate_keys:
            duplicate_flags.append(
                {
                    "item_id": str(record["item_id"]),
                    "reason": f"Possible duplicate of {duplicate_keys[key]}.",
                }
            )
        else:
            duplicate_keys[key] = str(record["item_id"])
        geometry = record.get("geometry")
        point = geometry.get("point") if isinstance(geometry, dict) else None
        if isinstance(point, dict):
            points.append((float(point["latitude"]), float(point["longitude"])))

    top_city_count = max(city_counts.values(), default=0)
    dispersion_status = CoverageDispersionStatus.INSUFFICIENT_DATA.value
    if len(records) >= 3 and top_city_count / len(records) > 0.6:
        dispersion_status = CoverageDispersionStatus.CLUSTERED.value
    elif len(records) >= 3 and out_of_scope_flags:
        dispersion_status = CoverageDispersionStatus.IMBALANCED.value
    elif len(records) >= 3:
        dispersion_status = CoverageDispersionStatus.BALANCED.value
    bounding_box = None
    if points:
        latitudes = [point[0] for point in points]
        longitudes = [point[1] for point in points]
        bounding_box = {
            "min_latitude": min(latitudes),
            "min_longitude": min(longitudes),
            "max_latitude": max(latitudes),
            "max_longitude": max(longitudes),
        }
    return {
        "sample_set_id": sample_set.sample_set_id,
        "requested_scope": {
            "country": sample_set.country,
            "localities": list(sample_set.requested_localities),
            "facility_types": list(sample_set.facility_types),
        },
        "approved_count": len(records),
        "geocoded_count": len(points),
        "counts_by_city_or_region": dict(sorted(city_counts.items())),
        "counts_by_locality": dict(sorted(locality_counts.items())),
        "counts_by_facility_type": dict(sorted(facility_counts.items())),
        "out_of_scope_flags": out_of_scope_flags,
        "duplicate_or_cluster_flags": duplicate_flags,
        "bounding_box": bounding_box,
        "dispersion_status_hint": dispersion_status,
    }


def build_coverage_id(sample_set_id: str) -> str:
    return f"{sample_set_id}-coverage-{slugify(utc_now_text())}"


def render_coverage_steering_prompt(
    *,
    sample_set: SampleSetManifest,
    coverage_id: str,
    summary: dict[str, Any],
    records: tuple[dict[str, Any], ...],
    curation_context: dict[str, Any] | None = None,
) -> str:
    payload = {
        "sample_set": sample_set.model_dump(mode="json"),
        "coverage_id": coverage_id,
        "deterministic_summary": summary,
        "records": list(records),
        "human_curation": curation_context or {
            "excluded_count": 0,
            "rejected_examples": [],
        },
    }
    return f"""# Sample Set Coverage Steering

You are a coverage steering agent for a geospatial observation harvest. Your job is to inspect
the verified sample, judge whether it is geographically dispersed enough for the requested scope,
consider whether direct observations or population component fields are missing, and recommend
targeted gap-fill harvest jobs.

Use the deterministic summary and included records below. Treat this as steering guidance, not
formal statistical representativeness. Human-excluded observations are supplied only as bounded
negative examples: do not count them toward coverage or rediscover the same observation. Translate
their stated reasons into targeted corrective search guidance. If there are no rejected examples,
do not invent corrective guidance. Recommend locality-adjusted jobs that would improve geographic
spread. When component evidence is present or expected, note missing component fields such as
students, staff, beds, rooms, annual visitors, household size, operating schedules, or regional
statistics. Gap-fill jobs may target missing component inputs; do not ask the harvester to derive
final occupancy estimates.

Return strictly one valid JSON object. Do not wrap the JSON in markdown or prose. Use this schema:

{{
  "coverage_id": "{coverage_id}",
  "sample_set_id": "{sample_set.sample_set_id}",
  "dispersion_status": "imbalanced",
  "counts_by_locality": {{"String": 0}},
  "counts_by_city_or_region": {{"String": 0}},
  "counts_by_facility_type": {{"String": 0}},
  "out_of_scope_flags": [
    {{"item_id": "String or null", "flag_type": "out_of_scope", "reason": "String"}}
  ],
  "duplicate_or_cluster_flags": [
    {{"item_id": "String or null", "flag_type": "clustered", "reason": "String"}}
  ],
  "narrative_notes": "String",
  "recommended_child_jobs": [
    {{
      "country": "{sample_set.country}",
      "locality": "String or null",
      "facility_type": "String",
      "target": {sample_set.target},
      "reason": "String"
    }}
  ]
}}

Allowed `dispersion_status` values: `unknown`, `balanced`, `imbalanced`, `clustered`,
`insufficient_data`.

Allowed `flag_type` values: `out_of_scope`, `duplicate_candidate`, `clustered`, `undercovered`.

## Input

{json.dumps(payload, indent=2)}
"""


def run_coverage_steering(
    *,
    root: Path,
    sample_set_id: str,
    coverage_id: str | None = None,
    codex_bin: str = "codex",
    runner: CodexRunner | None = None,
) -> dict[str, Any]:
    sample_set = refresh_sample_set(root, load_sample_set(root, sample_set_id))
    resolved_coverage_id = coverage_id or build_coverage_id(sample_set_id)
    all_records = sample_records(root, sample_set, include_excluded=True)
    curation = load_curation(root, sample_set_id)
    approval = ensure_current_approval(
        curation,
        tuple(str(record["item_id"]) for record in all_records),
    )
    records = sample_records(root, sample_set)
    summary = compute_coverage_summary(root, sample_set)
    feedback = rejected_examples(curation, all_records)
    summary["curation"] = curation_summary(
        curation,
        tuple(str(record["item_id"]) for record in all_records),
    )
    prompt = render_coverage_steering_prompt(
        sample_set=sample_set,
        coverage_id=resolved_coverage_id,
        summary=summary,
        records=records,
        curation_context={
            "snapshot_id": approval.snapshot_id,
            "excluded_count": len(feedback),
            "rejected_examples": list(feedback),
        },
    )
    prompt_path = coverage_prompt_path(root, resolved_coverage_id)
    output_path = coverage_output_path(root, resolved_coverage_id)
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    command = (
        codex_bin,
        "--search",
        "exec",
        "--sandbox",
        "workspace-write",
        "--cd",
        str(root),
        "-o",
        str(output_path),
        "-",
    )
    append_harvest_log(root, sample_set_id, f"Launching coverage agent {resolved_coverage_id}.")
    active_runner = runner or _default_codex_runner
    result = active_runner(command, prompt, root)
    if result.returncode != 0:
        append_harvest_log(root, sample_set_id, f"Coverage agent failed: {result.stderr.strip()}.")
        append_dialogue(
            root,
            sample_set_id,
            speaker="Coverage Agent",
            stage="coverage_review",
            message="I could not complete the sample coverage review.",
            rationale=(
                result.stderr.strip()
                or result.stdout.strip()
                or "No error detail was returned."
            ),
        )
        return {
            "coverage_id": resolved_coverage_id,
            "status": "failed",
            "prompt_path": str(prompt_path),
            "coverage_path": str(output_path),
            "summary": summary,
            "error_message": result.stderr.strip() or result.stdout.strip(),
        }
    review = load_coverage_review(output_path).model_copy(
        update={
            "curation_snapshot_id": approval.snapshot_id,
            "curation_feedback_count": len(feedback),
        }
    )
    write_model(output_path, review)
    append_harvest_log(root, sample_set_id, f"Coverage agent completed {resolved_coverage_id}.")
    append_dialogue(
        root,
        sample_set_id,
        speaker="Coverage Agent",
        stage="coverage_review",
        message=(
            f"I assessed the sample as {review.dispersion_status.value} and recommended "
            f"{len(review.recommended_child_jobs)} gap-fill job(s)."
        ),
        rationale=review.narrative_notes,
    )
    return {
        "coverage_id": resolved_coverage_id,
        "status": "completed",
        "prompt_path": str(prompt_path),
        "coverage_path": str(output_path),
        "summary": summary,
        "review": review.model_dump(mode="json"),
        "error_message": None,
    }


def _default_codex_runner(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        input=prompt,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        cwd=cwd,
        check=False,
    )


def run_gap_fill(
    *,
    root: Path,
    sample_set_id: str,
    coverage_path: Path,
    codex_bin: str = "codex",
    runner: CodexRunner | None = None,
    max_concurrent_jobs: int = 3,
) -> SampleSetManifest:
    if max_concurrent_jobs < 1:
        raise ValueError("max_concurrent_jobs must be at least 1")
    sample_set = load_sample_set(root, sample_set_id)
    review = load_coverage_review(coverage_path)
    if review.sample_set_id != sample_set_id:
        raise ValueError("coverage review belongs to a different sample set")
    all_records = sample_records(root, sample_set, include_excluded=True)
    curation = load_curation(root, sample_set_id)
    approval = ensure_current_approval(
        curation,
        tuple(str(record["item_id"]) for record in all_records),
    )
    if review.curation_snapshot_id != approval.snapshot_id:
        raise ValueError(
            "coverage analysis is stale for the current human curation approval; rerun coverage"
        )
    round_number = len(sample_set.rounds) + 1
    jobs = tuple(
        (
            job,
            (
                f"{sample_set.sample_set_id}-r{round_number}-gap-{index}-"
                f"{slugify(job.locality or 'countrywide')}-{slugify(job.facility_type)}"
            ),
        )
        for index, job in enumerate(review.recommended_child_jobs, start=1)
    )
    child_results: list[tuple[HarvestRunManifest, str | None] | None] = [None] * len(jobs)
    active_runner = runner or _default_codex_runner
    worker_count = min(max_concurrent_jobs, len(jobs)) if jobs else 1
    append_harvest_log(
        root,
        sample_set_id,
        f"Dispatching {len(jobs)} gap-fill job(s) with up to {worker_count} active agents.",
    )
    append_dialogue(
        root,
        sample_set_id,
        speaker="Gap-Fill Coordinator",
        stage="job_dispatch",
        message=(
            f"I queued {len(jobs)} targeted coverage job(s), with up to "
            f"{worker_count} job teams working at once."
        ),
        rationale=(
            "Each team runs a locality-specific Geographer review before its Harvester Agent."
        ),
    )

    def run_job(
        job: RecommendedGapFillJob,
        run_id: str,
    ) -> tuple[HarvestRunManifest, str]:
        curation_guidance = render_gap_fill_curation_guidance(
            rejected_examples(
                curation,
                all_records,
                facility_type=job.facility_type,
                locality=job.locality,
            )
        )
        geographer_plan = run_geographer(
            root=root,
            plan_id=run_id,
            country=job.country,
            locality=job.locality,
            profile_set_name=job.facility_type,
            profile_id=None,
            codex_bin=codex_bin,
            runner=active_runner,
            conversation_id=sample_set_id,
        )
        manifest = run_harvest(
            root=root,
            country=job.country,
            locality=job.locality,
            profile_set_name=job.facility_type,
            profile_id=None,
            target=job.target,
            run_id=run_id,
            codex_bin=codex_bin,
            runner=active_runner,
            geographer_plan=geographer_plan,
            conversation_id=sample_set_id,
            curation_guidance=curation_guidance,
        )
        return manifest, geographer_plan.artifact_path

    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="gap-fill-harvester",
    ) as executor:
        future_indexes = {
            executor.submit(run_job, job, run_id): index
            for index, (job, run_id) in enumerate(jobs)
        }
        for future in as_completed(future_indexes):
            index = future_indexes[future]
            job, run_id = jobs[index]
            try:
                child_results[index] = future.result()
            except Exception as exc:
                manifest = _failed_gap_fill_manifest(
                    root=root,
                    run_id=run_id,
                    job=job,
                    error_message=str(exc) or exc.__class__.__name__,
                )
                child_results[index] = (manifest, None)

    completed_results = [result for result in child_results if result is not None]
    child_run_ids = [manifest.run_id for manifest, _ in completed_results]
    child_summaries: list[dict[str, object]] = [
        {
            "run_id": manifest.run_id,
            "status": manifest.status.value,
            "summary": manifest.summary or {},
            "geographer_plan_path": geographer_path,
        }
        for manifest, geographer_path in completed_results
    ]
    failed = [item for item in child_summaries if item["status"] != HarvestRunStatus.COMPLETED]
    append_dialogue(
        root,
        sample_set_id,
        speaker="Gap-Fill Coordinator",
        stage="job_consolidation",
        message=(
            f"I consolidated {len(child_summaries)} targeted job(s), including "
            f"{len(failed)} that did not complete successfully."
        ),
        rationale=(
            "The completed jobs were restored to the Coverage Agent's recommended order "
            "before being added as a new sample round."
        ),
    )
    round_item = SampleSetRound(
        round_number=round_number,
        role=SampleSetRoundRole.GAP_FILL,
        source_run_ids=tuple(child_run_ids),
        child_run_ids=tuple(child_run_ids),
        recommended_coverage_id=review.coverage_id,
        status=HarvestRunStatus.FAILED if failed else HarvestRunStatus.COMPLETED,
        summary={
            "planned_run_count": len(review.recommended_child_jobs),
            "completed_count": len(child_run_ids) - len(failed),
            "failed_count": len(failed),
            "child_summaries": child_summaries,
        },
    )
    combined = _unique((*sample_set.combined_child_run_ids, *child_run_ids))
    updated = sample_set.model_copy(
        update={
            "rounds": (*sample_set.rounds, round_item),
            "combined_child_run_ids": combined,
            "updated_at": utc_now_text(),
        }
    )
    return refresh_sample_set(root, updated)

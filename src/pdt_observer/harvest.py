from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pdt_observer.activity import (
    activity_report_dialogue_message,
    harvester_activity_path,
    load_harvester_activity_report,
)
from pdt_observer.dialogue import append_dialogue
from pdt_observer.leads import (
    load_evidence_set,
    render_lead_harvest_prompt,
    summarize_evidence_set,
)
from pdt_observer.models import (
    CountMethod,
    GeographerPlan,
    HarvestBatchRunManifest,
    HarvestCampaignRunManifest,
    HarvestRunManifest,
    HarvestRunStatus,
    StrategyPlan,
    StrategyScoutPlan,
)
from pdt_observer.profiles import get_profile_set, resolve_profile_set
from pdt_observer.strategies import build_strategy_plan
from pdt_observer.strategy_scout import (
    effective_strategy_plan,
    load_strategy_scout_plan,
    render_strategy_scout_prompt,
    strategy_scout_path,
)
from pdt_observer.workflow import slugify, utc_now_text, write_model

CodexRunner = Callable[[Sequence[str], str, Path], subprocess.CompletedProcess[str]]


def log_path_for_run(root: Path, run_id: str) -> Path:
    return root / "harvest_logs" / f"{run_id}.log"


def append_harvest_log(root: Path, run_id: str, message: str) -> None:
    path = log_path_for_run(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{path.read_text(encoding='utf-8') if path.is_file() else ''}"
        f"[{utc_now_text()}] {message}\n",
        encoding="utf-8",
    )


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


def _profile_set_slug(profile_set_name: str) -> str:
    path = Path(profile_set_name)
    return slugify(path.stem if path.suffix else profile_set_name)


def build_harvest_run_id(
    *,
    country: str,
    locality: str | None,
    profile_set_name: str,
    profile_id: str | None,
) -> str:
    scope = slugify(locality or "countrywide")
    profile = slugify(profile_id or _profile_set_slug(profile_set_name))
    timestamp = slugify(utc_now_text())
    return f"{country.casefold()}-{scope}-{profile}-{timestamp}"


def build_harvest_batch_id(
    *,
    country: str,
    locality: str | None,
    profile_set_name: str,
) -> str:
    started_at = utc_now_text()
    return (
        f"{country.casefold()}-{slugify(locality or 'countrywide')}-"
        f"{_profile_set_slug(profile_set_name)}-{slugify(started_at)}"
    )


def build_harvest_campaign_id(
    *,
    country: str,
    localities: Sequence[str],
    facility_types: Sequence[str],
) -> str:
    started_at = utc_now_text()
    locality_slug = "countrywide" if not localities else f"{len(localities)}-localities"
    facility_slug = f"{len(facility_types)}-facility-types"
    return f"{country.casefold()}-{locality_slug}-{facility_slug}-{slugify(started_at)}"


def _manifest_path(root: Path, run_id: str) -> Path:
    return root / "harvest_runs" / f"{run_id}.json"


def _batch_manifest_path(root: Path, batch_id: str) -> Path:
    return root / "harvest_runs" / f"{batch_id}.batch.json"


def _campaign_manifest_path(root: Path, campaign_id: str) -> Path:
    return root / "harvest_runs" / f"{campaign_id}.campaign.json"


def _write_manifest(root: Path, manifest: HarvestRunManifest) -> HarvestRunManifest:
    write_model(_manifest_path(root, manifest.run_id), manifest)
    return manifest


def _failed_child_manifest(
    *,
    root: Path,
    run_id: str,
    country: str,
    locality: str | None,
    profile_set_name: str,
    profile_id: str | None,
    count_method_override: CountMethod | None,
    target: int,
    started_at: str,
    error_message: str,
) -> HarvestRunManifest:
    try:
        manifest = HarvestRunManifest.model_validate_json(
            _manifest_path(root, run_id).read_text(encoding="utf-8")
        )
    except Exception:
        manifest = HarvestRunManifest(
            run_id=run_id,
            status=HarvestRunStatus.FAILED,
            country=country,
            locality=locality,
            profile_set=profile_set_name,
            profile_id=profile_id,
            count_method_override=count_method_override,
            target=target,
            prompt_path=str(root / "work" / f"{run_id}.md"),
            lead_path=str(root / "lead_runs" / f"{run_id}.json"),
            started_at=started_at,
            validation_valid=False,
            log_path=str(log_path_for_run(root, run_id)),
        )
    append_harvest_log(root, run_id, f"Child job failed with an unhandled error: {error_message}.")
    return _write_manifest(
        root,
        manifest.model_copy(
            update={
                "status": HarvestRunStatus.FAILED,
                "completed_at": utc_now_text(),
                "validation_valid": False,
                "error_message": error_message,
            }
        ),
    )


def _run_strategy_scout(
    *,
    root: Path,
    run_id: str,
    country: str,
    locality: str | None,
    profile_set_name: str,
    profile_id: str | None,
    count_method_override: CountMethod | None,
    deterministic_plan: StrategyPlan,
    geographer_plan: GeographerPlan | None,
    codex_bin: str,
    runner: CodexRunner,
) -> tuple[StrategyPlan, StrategyScoutPlan | None, int | None, str | None]:
    profile_set = resolve_profile_set(
        profile_set_name,
        profile_id=profile_id,
        count_method_override=count_method_override,
    )
    output_path = strategy_scout_path(root, run_id)
    prompt_path = root / "work" / f"{run_id}-strategy.md"
    prompt = render_strategy_scout_prompt(
        run_id=run_id,
        country=country,
        locality=locality,
        profile_set=profile_set,
        profile_id=profile_id,
        strategy_plan=deterministic_plan,
        geographer_plan=geographer_plan,
    )
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    append_harvest_log(root, run_id, f"Strategy Scout prompt written to {prompt_path}.")

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
    append_harvest_log(
        root,
        run_id,
        f"Launching Codex command (Strategy Scout): {' '.join(command)}",
    )
    result = runner(command, prompt, root)
    if result.stdout.strip():
        append_harvest_log(root, run_id, f"Strategy Scout stdout: {result.stdout.strip()}")
    if result.stderr.strip():
        append_harvest_log(root, run_id, f"Strategy Scout stderr: {result.stderr.strip()}")
    append_harvest_log(root, run_id, f"Strategy Scout exited with code {result.returncode}.")
    if result.returncode < 0:
        return deterministic_plan, None, result.returncode, "Strategy Scout cancelled by user."
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "Strategy Scout failed."
        append_harvest_log(root, run_id, f"Strategy Scout fallback: {message}")
        return deterministic_plan, None, result.returncode, None

    try:
        scout_plan = load_strategy_scout_plan(output_path)
        effective_plan = effective_strategy_plan(deterministic_plan, scout_plan)
    except Exception as exc:
        append_harvest_log(root, run_id, f"Strategy Scout validation failed; fallback: {exc}.")
        return deterministic_plan, None, result.returncode, None

    append_harvest_log(
        root,
        run_id,
        f"Strategy Scout completed with confidence {scout_plan.confidence.value}.",
    )
    return effective_plan, scout_plan, result.returncode, None


def run_harvest(
    *,
    root: Path,
    country: str,
    profile_set_name: str,
    target: int,
    locality: str | None = None,
    profile_id: str | None = None,
    count_method_override: CountMethod | None = None,
    run_id: str | None = None,
    codex_bin: str = "codex",
    runner: CodexRunner | None = None,
    geographer_plan: GeographerPlan | None = None,
    conversation_id: str | None = None,
    curation_guidance: str = "",
) -> HarvestRunManifest:
    profile_set = resolve_profile_set(
        profile_set_name,
        profile_id=profile_id,
        count_method_override=count_method_override,
    )
    deterministic_strategy_plan = build_strategy_plan(profile_set)
    resolved_run_id = run_id or build_harvest_run_id(
        country=country,
        locality=locality,
        profile_set_name=profile_set_name,
        profile_id=profile_id,
    )
    prompt_path = root / "work" / f"{resolved_run_id}.md"
    lead_path = root / "lead_runs" / f"{resolved_run_id}.json"
    log_path = log_path_for_run(root, resolved_run_id)
    scout_path = strategy_scout_path(root, resolved_run_id)
    activity_path = harvester_activity_path(root, resolved_run_id)
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    lead_path.parent.mkdir(parents=True, exist_ok=True)
    command = (
        codex_bin,
        "--search",
        "exec",
        "--sandbox",
        "workspace-write",
        "--cd",
        str(root),
        "-o",
        str(lead_path),
        "-",
    )
    started_at = utc_now_text()
    manifest = HarvestRunManifest(
        run_id=resolved_run_id,
        status=HarvestRunStatus.PREPARED,
        country=country,
        locality=locality,
        profile_set=profile_set_name,
        profile_id=profile_id,
        count_method_override=count_method_override,
        strategy_plan=deterministic_strategy_plan,
        geographer_plan_path=(
            geographer_plan.artifact_path if geographer_plan is not None else None
        ),
        target=target,
        prompt_path=str(prompt_path),
        lead_path=str(lead_path),
        started_at=started_at,
        codex_command=command,
        log_path=str(log_path),
        strategy_scout_path=str(scout_path),
        activity_path=str(activity_path),
    )
    _write_manifest(root, manifest)
    append_harvest_log(root, resolved_run_id, "Manifest prepared.")
    manifest = manifest.model_copy(update={"status": HarvestRunStatus.RUNNING})
    _write_manifest(root, manifest)
    resolved_conversation_id = conversation_id or resolved_run_id
    process_runner = runner or _default_codex_runner
    append_harvest_log(root, resolved_run_id, "Starting Strategy Scout.")
    (
        strategy_plan,
        strategy_scout_plan,
        scout_exit_code,
        scout_error_message,
    ) = _run_strategy_scout(
        root=root,
        run_id=resolved_run_id,
        country=country,
        locality=locality,
        profile_set_name=profile_set_name,
        profile_id=profile_id,
        count_method_override=count_method_override,
        deterministic_plan=deterministic_strategy_plan,
        geographer_plan=geographer_plan,
        codex_bin=codex_bin,
        runner=process_runner,
    )
    if scout_error_message is not None and scout_exit_code is not None and scout_exit_code < 0:
        append_harvest_log(root, resolved_run_id, "Harvest cancelled during Strategy Scout.")
        append_harvest_log(root, resolved_run_id, "Harvest cancelled.")
        append_dialogue(
            root,
            resolved_conversation_id,
            speaker="Strategy Scout",
            stage="lead_harvest",
            message=(
                "I stopped before harvest because cancellation was requested during "
                "strategy review."
            ),
        )
        return _write_manifest(
            root,
            manifest.model_copy(
                update={
                    "status": HarvestRunStatus.CANCELLED,
                    "completed_at": utc_now_text(),
                    "exit_code": scout_exit_code,
                    "validation_valid": False,
                    "error_message": "Harvest cancelled by user.",
                }
            ),
        )
    if strategy_scout_plan is not None:
        append_dialogue(
            root,
            resolved_conversation_id,
            speaker="Strategy Scout",
            stage="lead_harvest",
            message=(
                "I reviewed the facility scope and geography, then adjusted the harvest "
                "strategy order."
            ),
            rationale=strategy_scout_plan.overall_rationale,
        )
    else:
        append_dialogue(
            root,
            resolved_conversation_id,
            speaker="Strategy Scout",
            stage="lead_harvest",
            message=(
                "I used the deterministic strategy order because no valid Scout plan "
                "was available."
            ),
            rationale=(
                "The fallback preserves the configured facility-specific evidence rules and "
                "keeps the harvest moving."
            ),
        )
    append_harvest_log(root, resolved_run_id, "Rendering harvest prompt.")
    prompt = render_lead_harvest_prompt(
        country=country,
        profile_set_name=profile_set_name,
        target=target,
        locality=locality,
        profile_id=profile_id,
        count_method_override=count_method_override,
        geographer_plan=geographer_plan,
        strategy_plan=strategy_plan,
        strategy_scout_plan=strategy_scout_plan,
        run_id=resolved_run_id,
        activity_path=activity_path,
        curation_guidance=curation_guidance,
    )
    prompt_path.write_text(prompt, encoding="utf-8")
    append_harvest_log(root, resolved_run_id, f"Prompt written to {prompt_path}.")
    manifest = manifest.model_copy(update={"strategy_plan": strategy_plan})
    _write_manifest(root, manifest)
    strategy_labels = ", ".join(
        item.strategy_id.value for item in strategy_plan.recommendations
    )
    append_dialogue(
        root,
        resolved_conversation_id,
        speaker="Harvester Agent",
        stage="lead_harvest",
        message=(
            f"I started the {profile_id or profile_set_name} harvest for "
            f"{locality or country} using the recommended evidence strategies."
        ),
        rationale=f"I will try the bounded strategy sequence: {strategy_labels}.",
    )
    append_harvest_log(root, resolved_run_id, f"Launching Codex command: {' '.join(command)}")

    result = process_runner(command, prompt, root)
    completed_at = utc_now_text()
    if result.stdout.strip():
        append_harvest_log(root, resolved_run_id, f"Codex stdout: {result.stdout.strip()}")
    if result.stderr.strip():
        append_harvest_log(root, resolved_run_id, f"Codex stderr: {result.stderr.strip()}")
    append_harvest_log(root, resolved_run_id, f"Codex exited with code {result.returncode}.")
    if result.returncode < 0:
        append_harvest_log(root, resolved_run_id, "Harvest cancelled.")
        append_dialogue(
            root,
            resolved_conversation_id,
            speaker="Harvester Agent",
            stage="lead_harvest",
            message="I stopped before completing the harvest because cancellation was requested.",
        )
        return _write_manifest(
            root,
            manifest.model_copy(
                update={
                    "status": HarvestRunStatus.CANCELLED,
                    "completed_at": completed_at,
                    "exit_code": result.returncode,
                    "validation_valid": False,
                    "error_message": "Harvest cancelled by user.",
                }
            ),
        )
    if result.returncode != 0:
        append_harvest_log(root, resolved_run_id, "Harvest failed before lead validation.")
        append_dialogue(
            root,
            resolved_conversation_id,
            speaker="Harvester Agent",
            stage="lead_harvest",
            message="I could not produce a valid lead file because the harvest process failed.",
            rationale=(
                result.stderr.strip()
                or result.stdout.strip()
                or "No error detail was returned."
            ),
        )
        return _write_manifest(
            root,
            manifest.model_copy(
                update={
                    "status": HarvestRunStatus.FAILED,
                    "completed_at": completed_at,
                    "exit_code": result.returncode,
                    "validation_valid": False,
                    "error_message": result.stderr.strip() or result.stdout.strip(),
                }
            ),
        )

    try:
        append_harvest_log(root, resolved_run_id, "Validating lead output.")
        evidence_set = load_evidence_set(lead_path)
        summary = summarize_evidence_set(evidence_set)
        append_harvest_log(root, resolved_run_id, f"Validation completed: {summary}.")
    except Exception as exc:
        append_harvest_log(root, resolved_run_id, f"Validation failed: {exc}.")
        append_dialogue(
            root,
            resolved_conversation_id,
            speaker="Harvester Agent",
            stage="lead_harvest",
            message="I produced output, but it did not pass the lead-file validation step.",
            rationale=str(exc),
        )
        return _write_manifest(
            root,
            manifest.model_copy(
                update={
                    "status": HarvestRunStatus.FAILED,
                    "completed_at": completed_at,
                    "exit_code": result.returncode,
                    "validation_valid": False,
                    "error_message": str(exc),
                }
            ),
        )

    append_harvest_log(root, resolved_run_id, "Harvest completed.")
    lead_count = summary.get("lead_count", 0)
    budget_observation_count = summary.get("budget_observation_count", lead_count)
    counts_by_strategy = summary.get("counts_by_strategy", {})
    component_lead_count = summary.get("component_lead_count", 0)
    component_bundle_count = summary.get("component_bundle_count", 0)
    countable_component_observations = summary.get("countable_component_observations", 0)
    component_bundles_by_status = summary.get("component_bundles_by_status", {})
    count_method_text = (
        count_method_override.value if count_method_override is not None else "profile_default"
    )
    if activity_path.is_file():
        try:
            activity_report = load_harvester_activity_report(activity_path)
            message, rationale = activity_report_dialogue_message(activity_report)
            append_dialogue(
                root,
                resolved_conversation_id,
                speaker="Harvester Agent",
                stage="lead_harvest",
                message=message,
                rationale=rationale or None,
            )
            append_harvest_log(root, resolved_run_id, "Harvester activity report loaded.")
        except Exception as exc:
            append_harvest_log(
                root,
                resolved_run_id,
                f"Harvester activity report validation failed; transcript fallback used: {exc}.",
            )
    else:
        append_harvest_log(
            root,
            resolved_run_id,
            "No Harvester activity report was written; transcript fallback used.",
        )
    append_dialogue(
        root,
        resolved_conversation_id,
        speaker="Harvester Agent",
        stage="lead_harvest",
        message=(
            "I completed the search audit and returned "
            f"{budget_observation_count} budget-countable candidate observation(s): "
            f"{lead_count} direct lead(s), {component_lead_count} component lead(s), and "
            f"{component_bundle_count} component bundle(s)."
        ),
        rationale=(
            f"Evidence mode: {count_method_text}. Strategy counts: {counts_by_strategy}. "
            f"Countable component bundles before QAQC: {countable_component_observations}; "
            f"bundle statuses: {component_bundles_by_status}. These remain candidates until "
            "QAQC verifies source support, role semantics, and bundle completeness."
        ),
    )
    return _write_manifest(
        root,
        manifest.model_copy(
            update={
                "status": HarvestRunStatus.COMPLETED,
                "completed_at": completed_at,
                "exit_code": result.returncode,
                "validation_valid": True,
                "summary": summary,
            }
        ),
    )


def run_harvest_batch(
    *,
    root: Path,
    country: str,
    profile_set_name: str,
    target: int,
    locality: str | None = None,
    count_method_override: CountMethod | None = None,
    batch_id: str | None = None,
    codex_bin: str = "codex",
    runner: CodexRunner | None = None,
    geographer_plan: GeographerPlan | None = None,
) -> HarvestBatchRunManifest:
    started_at = utc_now_text()
    resolved_batch_id = batch_id or build_harvest_batch_id(
        country=country,
        locality=locality,
        profile_set_name=profile_set_name,
    )
    append_harvest_log(root, resolved_batch_id, "Starting batch harvest.")
    profile_set = get_profile_set(profile_set_name)
    enabled_profiles = tuple(profile for profile in profile_set.profiles if profile.enabled)
    write_model(
        _batch_manifest_path(root, resolved_batch_id),
        HarvestBatchRunManifest(
            batch_id=resolved_batch_id,
            status=HarvestRunStatus.RUNNING,
            country=country,
            locality=locality,
            profile_set=profile_set_name,
            count_method_override=count_method_override,
            geographer_plan_path=(
                geographer_plan.artifact_path if geographer_plan is not None else None
            ),
            target=target,
            child_run_ids=(),
            child_manifest_paths=(),
            started_at=started_at,
            log_path=str(log_path_for_run(root, resolved_batch_id)),
        ),
    )
    child_manifests: list[HarvestRunManifest] = []

    for profile in sorted(enabled_profiles, key=lambda item: (item.priority, item.profile_id)):
        child_run_id = f"{resolved_batch_id}-{profile.profile_id}"
        append_harvest_log(root, resolved_batch_id, f"Starting child run {child_run_id}.")
        child_manifests.append(
            run_harvest(
                root=root,
                country=country,
                profile_set_name=profile_set_name,
                target=target,
                locality=locality,
                profile_id=profile.profile_id,
                count_method_override=count_method_override,
                run_id=child_run_id,
                codex_bin=codex_bin,
                runner=runner,
                geographer_plan=geographer_plan,
                conversation_id=resolved_batch_id,
            )
        )
        append_harvest_log(
            root,
            resolved_batch_id,
            f"Child run {child_run_id} finished as {child_manifests[-1].status.value}.",
        )

    completed_at = utc_now_text()
    failed = [
        manifest
        for manifest in child_manifests
        if manifest.status != HarvestRunStatus.COMPLETED
    ]
    cancelled = [
        manifest for manifest in child_manifests if manifest.status == HarvestRunStatus.CANCELLED
    ]
    total_leads = 0
    total_budget_observations = 0
    for child_manifest in child_manifests:
        if child_manifest.summary is None:
            continue
        lead_count = child_manifest.summary.get("lead_count", 0)
        if isinstance(lead_count, int):
            total_leads += lead_count
        budget_count = child_manifest.summary.get("budget_observation_count", lead_count)
        if isinstance(budget_count, int):
            total_budget_observations += budget_count
    summary: dict[str, object] = {
        "run_count": len(child_manifests),
        "completed_count": len(child_manifests) - len(failed),
        "failed_count": len(failed),
        "lead_count": total_leads,
        "budget_observation_count": total_budget_observations,
    }
    manifest = HarvestBatchRunManifest(
        batch_id=resolved_batch_id,
        status=(
            HarvestRunStatus.CANCELLED
            if cancelled
            else (HarvestRunStatus.FAILED if failed else HarvestRunStatus.COMPLETED)
        ),
        country=country,
        locality=locality,
        profile_set=profile_set_name,
        count_method_override=count_method_override,
        geographer_plan_path=(
            geographer_plan.artifact_path if geographer_plan is not None else None
        ),
        target=target,
        child_run_ids=tuple(child.run_id for child in child_manifests),
        child_manifest_paths=tuple(
            str(_manifest_path(root, child.run_id)) for child in child_manifests
        ),
        started_at=started_at,
        completed_at=completed_at,
        summary=summary,
        error_message=(
            "One or more child harvest runs failed or were cancelled." if failed else None
        ),
        log_path=str(log_path_for_run(root, resolved_batch_id)),
    )
    append_harvest_log(root, resolved_batch_id, f"Batch harvest finished: {summary}.")
    write_model(_batch_manifest_path(root, resolved_batch_id), manifest)
    return manifest


def _unique_nonempty(values: Sequence[str]) -> tuple[str, ...]:
    unique: list[str] = []
    for value in values:
        cleaned = value.strip()
        if cleaned and cleaned not in unique:
            unique.append(cleaned)
    return tuple(unique)


def run_harvest_campaign(
    *,
    root: Path,
    country: str,
    facility_types: Sequence[str],
    target: int,
    localities: Sequence[str] = (),
    count_method_override: CountMethod | None = None,
    campaign_id: str | None = None,
    codex_bin: str = "codex",
    runner: CodexRunner | None = None,
    geographer_plan: GeographerPlan | None = None,
    max_concurrent_jobs: int = 3,
) -> HarvestCampaignRunManifest:
    if not facility_types:
        raise ValueError("provide at least one facility type")
    if max_concurrent_jobs < 1:
        raise ValueError("max_concurrent_jobs must be at least 1")

    started_at = utc_now_text()
    resolved_localities = _unique_nonempty(localities)
    resolved_facility_types = _unique_nonempty(facility_types)
    if not resolved_facility_types:
        raise ValueError("provide at least one facility type")
    for facility_type in resolved_facility_types:
        get_profile_set(facility_type)

    resolved_campaign_id = campaign_id or build_harvest_campaign_id(
        country=country,
        localities=resolved_localities,
        facility_types=resolved_facility_types,
    )
    append_harvest_log(root, resolved_campaign_id, "Starting campaign harvest.")
    locality_targets: tuple[str | None, ...] = resolved_localities or (None,)
    write_model(
        _campaign_manifest_path(root, resolved_campaign_id),
        HarvestCampaignRunManifest(
            campaign_id=resolved_campaign_id,
            status=HarvestRunStatus.RUNNING,
            country=country,
            localities=resolved_localities,
            facility_types=resolved_facility_types,
            count_method_override=count_method_override,
            geographer_plan_path=(
                geographer_plan.artifact_path if geographer_plan is not None else None
            ),
            target=target,
            child_run_ids=(),
            child_manifest_paths=(),
            started_at=started_at,
            log_path=str(log_path_for_run(root, resolved_campaign_id)),
        ),
    )
    jobs = tuple(
        (
            locality,
            facility_type,
            (
                f"{resolved_campaign_id}-"
                f"{slugify(locality or 'countrywide')}-"
                f"{_profile_set_slug(facility_type)}"
            ),
        )
        for locality in locality_targets
        for facility_type in resolved_facility_types
    )
    child_manifests_by_index: list[HarvestRunManifest | None] = [None] * len(jobs)
    worker_count = min(max_concurrent_jobs, len(jobs))
    append_harvest_log(
        root,
        resolved_campaign_id,
        f"Dispatching {len(jobs)} child job(s) with up to {worker_count} active agents.",
    )
    append_dialogue(
        root,
        resolved_campaign_id,
        speaker="Campaign Coordinator",
        stage="job_dispatch",
        message=(
            f"I queued {len(jobs)} locality and facility job(s), with up to "
            f"{worker_count} Harvester Agents working at once."
        ),
        rationale=(
            "Each job keeps its own prompt, strategy plan, validation, and artifacts while "
            "sharing the campaign's geographic guidance."
        ),
    )

    def run_child(
        locality: str | None,
        facility_type: str,
        child_run_id: str,
    ) -> HarvestRunManifest:
        return run_harvest(
            root=root,
            country=country,
            profile_set_name=facility_type,
            target=target,
            locality=locality,
            profile_id=None,
            count_method_override=count_method_override,
            run_id=child_run_id,
            codex_bin=codex_bin,
            runner=runner,
            geographer_plan=geographer_plan,
            conversation_id=resolved_campaign_id,
        )

    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="campaign-harvester",
    ) as executor:
        future_indexes = {}
        for index, (locality, facility_type, child_run_id) in enumerate(jobs):
            append_harvest_log(
                root,
                resolved_campaign_id,
                f"Queued child run {child_run_id}.",
            )
            future = executor.submit(run_child, locality, facility_type, child_run_id)
            future_indexes[future] = index
        for future in as_completed(future_indexes):
            index = future_indexes[future]
            locality, facility_type, child_run_id = jobs[index]
            try:
                child_manifest = future.result()
            except Exception as exc:
                child_manifest = _failed_child_manifest(
                    root=root,
                    run_id=child_run_id,
                    country=country,
                    locality=locality,
                    profile_set_name=facility_type,
                    profile_id=None,
                    count_method_override=count_method_override,
                    target=target,
                    started_at=started_at,
                    error_message=str(exc) or exc.__class__.__name__,
                )
            child_manifests_by_index[index] = child_manifest
            append_harvest_log(
                root,
                resolved_campaign_id,
                f"Child run {child_manifest.run_id} finished as {child_manifest.status.value}.",
            )
            completed_so_far = sum(item is not None for item in child_manifests_by_index)
            successful_so_far = sum(
                item is not None and item.status == HarvestRunStatus.COMPLETED
                for item in child_manifests_by_index
            )
            failed_so_far = sum(
                item is not None and item.status != HarvestRunStatus.COMPLETED
                for item in child_manifests_by_index
            )
            active_so_far = min(worker_count, len(jobs) - completed_so_far)
            progress_manifest = HarvestCampaignRunManifest(
                campaign_id=resolved_campaign_id,
                status=HarvestRunStatus.RUNNING,
                country=country,
                localities=resolved_localities,
                facility_types=resolved_facility_types,
                geographer_plan_path=(
                    geographer_plan.artifact_path if geographer_plan is not None else None
                ),
                target=target,
                child_run_ids=tuple(
                    item.run_id for item in child_manifests_by_index if item is not None
                ),
                child_manifest_paths=tuple(
                    str(_manifest_path(root, item.run_id))
                    for item in child_manifests_by_index
                    if item is not None
                ),
                started_at=started_at,
                summary={
                    "planned_run_count": len(jobs),
                    "completed_count": successful_so_far,
                    "failed_count": failed_so_far,
                    "finished_count": completed_so_far,
                    "active_count": active_so_far,
                    "queued_count": max(len(jobs) - completed_so_far - active_so_far, 0),
                },
                log_path=str(log_path_for_run(root, resolved_campaign_id)),
            )
            write_model(_campaign_manifest_path(root, resolved_campaign_id), progress_manifest)

    child_manifests = [
        manifest for manifest in child_manifests_by_index if manifest is not None
    ]

    completed_at = utc_now_text()
    failed = [
        manifest
        for manifest in child_manifests
        if manifest.status != HarvestRunStatus.COMPLETED
    ]
    cancelled = [
        manifest for manifest in child_manifests if manifest.status == HarvestRunStatus.CANCELLED
    ]
    total_leads = 0
    total_budget_observations = 0
    for child_manifest in child_manifests:
        if child_manifest.summary is None:
            continue
        lead_count = child_manifest.summary.get("lead_count", 0)
        if isinstance(lead_count, int):
            total_leads += lead_count
        budget_count = child_manifest.summary.get("budget_observation_count", lead_count)
        if isinstance(budget_count, int):
            total_budget_observations += budget_count

    summary: dict[str, object] = {
        "planned_run_count": len(locality_targets) * len(resolved_facility_types),
        "completed_count": len(child_manifests) - len(failed),
        "failed_count": len(failed),
        "lead_count": total_leads,
        "budget_observation_count": total_budget_observations,
    }
    manifest = HarvestCampaignRunManifest(
        campaign_id=resolved_campaign_id,
        status=(
            HarvestRunStatus.CANCELLED
            if cancelled
            else (HarvestRunStatus.FAILED if failed else HarvestRunStatus.COMPLETED)
        ),
        country=country,
        localities=resolved_localities,
        facility_types=resolved_facility_types,
        count_method_override=count_method_override,
        geographer_plan_path=(
            geographer_plan.artifact_path if geographer_plan is not None else None
        ),
        target=target,
        child_run_ids=tuple(child.run_id for child in child_manifests),
        child_manifest_paths=tuple(
            str(_manifest_path(root, child.run_id)) for child in child_manifests
        ),
        started_at=started_at,
        completed_at=completed_at,
        summary=summary,
        error_message=(
            "One or more child harvest runs failed or were cancelled." if failed else None
        ),
        log_path=str(log_path_for_run(root, resolved_campaign_id)),
    )
    append_harvest_log(root, resolved_campaign_id, f"Campaign harvest finished: {summary}.")
    append_dialogue(
        root,
        resolved_campaign_id,
        speaker="Campaign Coordinator",
        stage="job_consolidation",
        message=(
            f"I consolidated {len(child_manifests)} child job(s), including "
            f"{len(failed)} that did not complete successfully."
        ),
        rationale=(
            "Results were restored to the original locality and facility job order before "
            "the campaign manifest was finalized."
        ),
    )
    write_model(_campaign_manifest_path(root, resolved_campaign_id), manifest)
    return manifest

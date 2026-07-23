from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from pdt_observer.leads import load_leads, render_lead_harvest_prompt, summarize_leads
from pdt_observer.models import (
    HarvestBatchRunManifest,
    HarvestRunManifest,
    HarvestRunStatus,
)
from pdt_observer.profiles import get_profile_set
from pdt_observer.workflow import slugify, utc_now_text, write_model

CodexRunner = Callable[[Sequence[str], str, Path], subprocess.CompletedProcess[str]]


def _default_codex_runner(
    command: Sequence[str],
    prompt: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        input=prompt,
        text=True,
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


def _manifest_path(root: Path, run_id: str) -> Path:
    return root / "harvest_runs" / f"{run_id}.json"


def _write_manifest(root: Path, manifest: HarvestRunManifest) -> HarvestRunManifest:
    write_model(_manifest_path(root, manifest.run_id), manifest)
    return manifest


def run_harvest(
    *,
    root: Path,
    country: str,
    profile_set_name: str,
    target: int,
    locality: str | None = None,
    profile_id: str | None = None,
    run_id: str | None = None,
    codex_bin: str = "codex",
    runner: CodexRunner | None = None,
) -> HarvestRunManifest:
    resolved_run_id = run_id or build_harvest_run_id(
        country=country,
        locality=locality,
        profile_set_name=profile_set_name,
        profile_id=profile_id,
    )
    prompt_path = root / "work" / f"{resolved_run_id}.md"
    lead_path = root / "lead_runs" / f"{resolved_run_id}.json"
    prompt = render_lead_harvest_prompt(
        country=country,
        profile_set_name=profile_set_name,
        target=target,
        locality=locality,
        profile_id=profile_id,
    )
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    lead_path.parent.mkdir(parents=True, exist_ok=True)
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
        target=target,
        prompt_path=str(prompt_path),
        lead_path=str(lead_path),
        started_at=started_at,
        codex_command=command,
    )
    _write_manifest(root, manifest)
    manifest = manifest.model_copy(update={"status": HarvestRunStatus.RUNNING})
    _write_manifest(root, manifest)

    process_runner = runner or _default_codex_runner
    result = process_runner(command, prompt, root)
    completed_at = utc_now_text()
    if result.returncode != 0:
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
        leads = load_leads(lead_path)
        summary = summarize_leads(leads)
    except Exception as exc:
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
    batch_id: str | None = None,
    codex_bin: str = "codex",
    runner: CodexRunner | None = None,
) -> HarvestBatchRunManifest:
    started_at = utc_now_text()
    resolved_batch_id = batch_id or (
        f"{country.casefold()}-{slugify(locality or 'countrywide')}-"
        f"{_profile_set_slug(profile_set_name)}-{slugify(started_at)}"
    )
    profile_set = get_profile_set(profile_set_name)
    enabled_profiles = tuple(profile for profile in profile_set.profiles if profile.enabled)
    child_manifests: list[HarvestRunManifest] = []

    for profile in sorted(enabled_profiles, key=lambda item: (item.priority, item.profile_id)):
        child_run_id = f"{resolved_batch_id}-{profile.profile_id}"
        child_manifests.append(
            run_harvest(
                root=root,
                country=country,
                profile_set_name=profile_set_name,
                target=target,
                locality=locality,
                profile_id=profile.profile_id,
                run_id=child_run_id,
                codex_bin=codex_bin,
                runner=runner,
            )
        )

    completed_at = utc_now_text()
    failed = [
        manifest for manifest in child_manifests if manifest.status == HarvestRunStatus.FAILED
    ]
    total_leads = 0
    for child_manifest in child_manifests:
        if child_manifest.summary is None:
            continue
        lead_count = child_manifest.summary.get("lead_count", 0)
        if isinstance(lead_count, int):
            total_leads += lead_count
    summary: dict[str, object] = {
        "run_count": len(child_manifests),
        "completed_count": len(child_manifests) - len(failed),
        "failed_count": len(failed),
        "lead_count": total_leads,
    }
    manifest = HarvestBatchRunManifest(
        batch_id=resolved_batch_id,
        status=HarvestRunStatus.FAILED if failed else HarvestRunStatus.COMPLETED,
        country=country,
        locality=locality,
        profile_set=profile_set_name,
        target=target,
        child_run_ids=tuple(child.run_id for child in child_manifests),
        child_manifest_paths=tuple(
            str(_manifest_path(root, child.run_id)) for child in child_manifests
        ),
        started_at=started_at,
        completed_at=completed_at,
        summary=summary,
        error_message="One or more child harvest runs failed." if failed else None,
    )
    write_model(root / "harvest_runs" / f"{resolved_batch_id}.batch.json", manifest)
    return manifest

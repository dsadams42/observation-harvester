from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from pydantic import ValidationError

from pdt_observer.dialogue import append_dialogue
from pdt_observer.models import (
    GeographerPlan,
    GeographerPlanStatus,
    GeographerProposal,
    VernacularTerm,
)
from pdt_observer.profiles import get_profile_set, narrow_profile_set
from pdt_observer.workflow import utc_now_text, write_model

GeographerRunner = Callable[
    [Sequence[str], str, Path],
    subprocess.CompletedProcess[str],
]


def _bullet_list(values: tuple[str, ...]) -> str:
    return "\n".join(f"- {value}" for value in values) or "- None"


def render_geographer_prompt(
    *,
    country: str,
    locality: str | None,
    profile_set_name: str,
    profile_id: str | None = None,
    localities: Sequence[str] = (),
    facility_types: Sequence[str] = (),
) -> str:
    profile_sets = (
        tuple(get_profile_set(name) for name in facility_types)
        if facility_types
        else (get_profile_set(profile_set_name),)
    )
    if profile_id is not None:
        profile_sets = (narrow_profile_set(profile_sets[0], profile_id),)
    labels = tuple(
        profile.label
        for profile_set in profile_sets
        for profile in profile_set.profiles
        if profile.enabled
    )
    aliases = tuple(
        dict.fromkeys(
            alias
            for profile_set in profile_sets
            for profile in profile_set.profiles
            if profile.enabled
            for alias in profile.venue_aliases
        )
    )
    cleaned_localities = tuple(item.strip() for item in localities if item.strip())
    if cleaned_localities:
        scope = f"{country}; campaign localities: {', '.join(cleaned_localities)}"
    else:
        scope = f"{locality}, {country}" if locality else country
    return f"""# Minimal Geographic Vernacular Review

You are the geographic research colleague preparing an occupancy-harvest prompt for {scope}.
Use web search only to identify credible local terminology that would materially improve searches.
Do not harvest occupancy observations, create jobs, redesign strategies, or produce a country
report.

Facility scope:
{_bullet_list(labels)}

Existing facility aliases:
{_bullet_list(aliases)}

Review only:
- Languages that are useful for public web searches in this locality.
- The ISO 3166-1 alpha-2 country code and local-script or official country aliases.
- Local administrative terms that may replace generic words such as city, district, or province.
- Local address terms that may appear in geocoder results, such as road, village, subdistrict,
  district, province, building, campus, or municipality.
- Names or abbreviations used for police, fire, emergency, labor, or regulatory authorities.
- Local facility vernacular not already represented by the aliases.
- Local terms for census, enrollment, attendance, bed occupancy, staffing, hotel occupancy,
  visitor traffic, passenger traffic, and other component-statistic sources when relevant.
- For commercial, industrial, retail, hospitality, manufacturing, logistics, and similar scopes,
  major local or regional company players, corporate location-list pages, employer facility pages,
  store locators, business registries, industrial park tenant lists, association directories, and
  OSM/open-facility inventory terms that could help the Harvester find facility examples.
- Locality-specific differences within the campaign scope, when relevant.
- Short query adjustments that a later harvester can combine with its assigned evidence strategy.

Keep the changes minimal. Return empty arrays when the existing English vocabulary is adequate.
Do not infer a translation without reasonable source support. Source URLs support geographic
terminology only and never count as occupancy evidence.

Write `commentary` as two to four short first-person sentences suitable for a colleague transcript:
what you found and what you changed. Write `rationale` as a concise explanation of why those
changes should improve discovery. Report decisions and evidence, not hidden chain-of-thought.

Return exactly one JSON object with this schema and no Markdown:

{{
  "country_code": "ISO 3166-1 alpha-2 code, such as PH",
  "country_aliases": ["country names in local scripts or common romanizations"],
  "search_languages": ["language name or code"],
  "administrative_terms": [
    {{
      "standard_term": "police",
      "local_term": "local term or abbreviation",
      "language": "language",
      "usage_note": "how to use it in a search"
    }}
  ],
  "address_terms": [],
  "public_safety_terms": [],
  "facility_terms": [],
  "query_adjustments": ["short query adjustment"],
  "source_urls": ["https://source.example/context"],
  "anchor_organization_hints": [
    {{
      "name": "Major company/operator name",
      "reason": "Why this organization may expose relevant facility locations or staff counts",
      "suggested_queries": ["company location query"]
    }}
  ],
  "facility_universe_source_hints": [
    "Registry, association directory, industrial park tenant list, store locator, or OSM tag idea"
  ],
  "commentary": "I found ... so I adjusted ...",
  "rationale": "These changes should help because ..."
}}
"""


def _fallback_proposal(error_message: str) -> GeographerProposal:
    return GeographerProposal(
        commentary=(
            "I could not complete the local vernacular review, so I left the existing harvest "
            "prompt unchanged."
        ),
        rationale=(
            "The deterministic country context and facility vocabulary remain available as a "
            f"safe fallback. Geographer error: {error_message}"
        ),
    )


def run_geographer(
    *,
    root: Path,
    plan_id: str,
    country: str,
    locality: str | None,
    profile_set_name: str,
    profile_id: str | None,
    localities: Sequence[str] = (),
    facility_types: Sequence[str] = (),
    codex_bin: str,
    runner: GeographerRunner,
    conversation_id: str | None = None,
) -> GeographerPlan:
    prompt_path = root / "work" / f"{plan_id}-geographer.md"
    artifact_path = root / "geographer_runs" / f"{plan_id}.json"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    prompt = render_geographer_prompt(
        country=country,
        locality=locality,
        profile_set_name=profile_set_name,
        profile_id=profile_id,
        localities=localities,
        facility_types=facility_types,
    )
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
        str(artifact_path),
        "-",
    )
    result = runner(command, prompt, root)
    status = GeographerPlanStatus.COMPLETED
    error_message: str | None = None
    try:
        if result.returncode != 0:
            raise ValueError(result.stderr.strip() or result.stdout.strip() or "Codex failed")
        proposal = GeographerProposal.model_validate_json(
            artifact_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError, ValidationError) as exc:
        status = GeographerPlanStatus.FALLBACK
        error_message = str(exc)
        proposal = _fallback_proposal(error_message)

    plan = GeographerPlan(
        plan_id=plan_id,
        status=status,
        country=country,
        locality=locality,
        localities=tuple(localities),
        profile_set=profile_set_name,
        profile_id=profile_id,
        facility_types=tuple(facility_types),
        proposal=proposal,
        prompt_path=str(prompt_path),
        artifact_path=str(artifact_path),
        created_at=utc_now_text(),
        error_message=error_message,
    )
    write_model(artifact_path, plan)
    append_dialogue(
        root,
        conversation_id or plan_id,
        speaker="Geographer Agent",
        stage="vernacular_review",
        message=proposal.commentary,
        rationale=proposal.rationale,
    )
    return plan


def load_geographer_plan(root: Path, path_text: str) -> GeographerPlan:
    path = Path(path_text).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("geographer plan must be inside the application workspace")
    return GeographerPlan.model_validate_json(path.read_text(encoding="utf-8"))


def geographer_prompt_guidance(plan: GeographerPlan | None) -> str:
    if plan is None:
        return ""
    proposal = plan.proposal

    def terms(items: tuple[VernacularTerm, ...]) -> tuple[str, ...]:
        rendered: list[str] = []
        for item in items:
            rendered.append(
                f"{item.standard_term} -> {item.local_term} "
                f"({item.language}): {item.usage_note}"
            )
        return tuple(rendered)

    return f"""## Geographer Vernacular Adjustments

The geographer reviewed local terminology before this harvest. Use these additions as search
vocabulary only; they do not weaken the evidence rules or validate any occupancy claim.

Search languages:
{_bullet_list(proposal.search_languages)}

Administrative terminology:
{_bullet_list(terms(proposal.administrative_terms))}

Country aliases:
{_bullet_list(proposal.country_aliases)}

Address terminology:
{_bullet_list(terms(proposal.address_terms))}

Police, fire, emergency, labor, and regulator terminology:
{_bullet_list(terms(proposal.public_safety_terms))}

Additional facility terminology:
{_bullet_list(terms(proposal.facility_terms))}

Query adjustments:
{_bullet_list(proposal.query_adjustments)}

Anchor organization hints:
{_bullet_list(tuple(
    f"{item.name}: {item.reason}. Query ideas: {'; '.join(item.suggested_queries)}"
    for item in proposal.anchor_organization_hints
))}

Facility-universe source hints:
{_bullet_list(proposal.facility_universe_source_hints)}

Preserve exact source quotations in their original language. If translating for review, keep the
translation separate from the original evidence quote.
"""

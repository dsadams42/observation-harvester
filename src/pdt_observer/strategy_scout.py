from __future__ import annotations

from pathlib import Path

from pdt_observer.geographer import geographer_prompt_guidance
from pdt_observer.models import (
    BuildingProfileSet,
    EvidenceStrategyType,
    GeographerPlan,
    StrategyPlan,
    StrategyRecommendation,
    StrategyScoutPlan,
)
from pdt_observer.strategies import get_strategy
from pdt_observer.workflow import write_model


def strategy_scout_path(root: Path, run_id: str) -> Path:
    return root / "strategy_runs" / f"{run_id}-strategy.json"


def load_strategy_scout_plan(path: Path) -> StrategyScoutPlan:
    return StrategyScoutPlan.model_validate_json(path.read_text(encoding="utf-8"))


def _bullet_list(values: tuple[str, ...]) -> str:
    return "\n".join(f"- {value}" for value in values) or "- None"


def _profile_context(profile_set: BuildingProfileSet) -> str:
    lines: list[str] = []
    for profile in profile_set.profiles:
        if not profile.enabled:
            continue
        details: list[str] = [f"profile_id={profile.profile_id}", f"label={profile.label}"]
        details.append(f"count_method={profile.count_method.value}")
        if profile.land_use:
            details.append(f"land_use={profile.land_use}")
        if profile.facility_class:
            details.append(f"facility_class={profile.facility_class}")
        elif profile.pdt_subtype:
            details.append(f"facility_class={profile.pdt_subtype}")
        if profile.occupancy_groups:
            details.append(f"groups={', '.join(profile.occupancy_groups)}")
        if profile.day_occurrence:
            details.append(f"day/open={profile.day_occurrence}")
        if profile.night_occurrence:
            details.append(f"night/closed={profile.night_occurrence}")
        if profile.episodic_occurrence:
            details.append(f"episodic={', '.join(profile.episodic_occurrence)}")
        if profile.contextual_count_fields:
            details.append(f"context-only={', '.join(profile.contextual_count_fields)}")
        if profile.component_count_fields:
            details.append(f"component_inputs={', '.join(profile.component_count_fields)}")
        if profile.regional_stat_fields:
            details.append(f"regional_inputs={', '.join(profile.regional_stat_fields)}")
        if profile.venue_aliases:
            details.append(f"aliases={', '.join(profile.venue_aliases[:8])}")
        lines.append("- " + "; ".join(details))
    return "\n".join(lines) or "- None"


def _strategy_context(strategy_plan: StrategyPlan) -> str:
    sections: list[str] = []
    for recommendation in strategy_plan.recommendations:
        strategy = get_strategy(recommendation.strategy_id)
        sections.append(
            "- "
            f"{strategy.strategy_id.value}: {strategy.label}; "
            f"semantics={', '.join(strategy.accepted_count_semantics)}; "
            f"traps={', '.join(strategy.negative_traps)}"
        )
    return "\n".join(sections)


def render_strategy_scout_prompt(
    *,
    run_id: str,
    country: str,
    locality: str | None,
    profile_set: BuildingProfileSet,
    profile_id: str | None,
    strategy_plan: StrategyPlan,
    geographer_plan: GeographerPlan | None,
) -> str:
    strategy_ids = tuple(item.strategy_id.value for item in strategy_plan.recommendations)
    return f"""# Strategy Scout

You are the Strategy Scout for an OASIS count-role harvest. Your job is to review the selected
facility scope, country/geography, PDT occurrence hints, count method, and deterministic evidence
strategy plan, then recommend which existing strategies the Harvester Agent should try first.
When component-input strategies are present, evaluate likely source families for source-backed
component values rather than direct people-present observations.

Do not harvest observations. Do not create lead JSON. Do not invent evidence standards or new
strategy IDs. You may reorder, emphasize, or de-emphasize only these allowed strategies:
{_bullet_list(strategy_ids)}

Run: {run_id}
Country: {country}
Locality: {locality or "countrywide"}
Land use: {profile_set.label} (`{profile_set.profile_set_id}`)
Facility family: {profile_set.label} (`{profile_set.profile_set_id}`)
Selected facility class: {profile_id or "all enabled facility classes"}

Facility and PDT context:
{_profile_context(profile_set)}

Deterministic strategy plan:
{_strategy_context(strategy_plan)}

{geographer_prompt_guidance(geographer_plan)}

Return a concise public strategy recommendation. Report decisions and evidence-search
expectations, not hidden chain-of-thought. If the deterministic order is already suitable, return
the same order and explain why.

Return exactly one JSON object with this schema and no Markdown:

{{
  "run_id": "{run_id}",
  "country": "{country}",
  "locality": {f'"{locality}"' if locality is not None else "null"},
  "profile_set": "{profile_set.profile_set_id}",
  "profile_id": {f'"{profile_id}"' if profile_id is not None else "null"},
  "recommended_strategy_order": ["{strategy_ids[0]}"],
  "recommendations": [
    {{
      "strategy_id": "{strategy_ids[0]}",
      "emphasis": "primary | secondary | de_emphasized",
      "rationale": "Why this existing strategy should be tried in this scope.",
      "query_patterns": ["Short search pattern, not a full report."],
      "expected_traps": ["A likely false-positive pattern to watch."]
    }}
  ],
  "local_source_ideas": ["Local agency, record type, or source family to try."],
  "overall_rationale": "Two or three concise public sentences.",
  "confidence": "high | medium | low | unknown"
}}
"""


def effective_strategy_plan(
    deterministic_plan: StrategyPlan,
    scout_plan: StrategyScoutPlan,
) -> StrategyPlan:
    original = {item.strategy_id: item for item in deterministic_plan.recommendations}
    unknown = [
        strategy_id.value
        for strategy_id in scout_plan.recommended_strategy_order
        if strategy_id not in original
    ]
    if unknown:
        raise ValueError(
            "strategy scout recommended strategy id(s) outside the deterministic plan: "
            + ", ".join(unknown)
        )

    reordered_ids: list[EvidenceStrategyType] = []
    for strategy_id in scout_plan.recommended_strategy_order:
        if strategy_id not in reordered_ids:
            reordered_ids.append(strategy_id)
    for recommendation in deterministic_plan.recommendations:
        if recommendation.strategy_id not in reordered_ids:
            reordered_ids.append(recommendation.strategy_id)

    return StrategyPlan(
        planner="strategy_scout_guided_v1",
        recommendations=tuple(
            StrategyRecommendation(
                strategy_id=strategy_id,
                priority=index * 10,
                reason=original[strategy_id].reason,
            )
            for index, strategy_id in enumerate(reordered_ids, start=1)
        ),
    )


def write_strategy_scout_fallback(path: Path, plan: StrategyScoutPlan | dict[str, object]) -> None:
    write_model(path, plan)

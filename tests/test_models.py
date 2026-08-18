from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from pdt_observer.leads import bundle_is_allocated_shadow, summarize_evidence_set
from pdt_observer.models import (
    AddressConfidence,
    AddressEnrichmentResult,
    AddressEnrichmentStatus,
    AllocatedComponentQaqcReview,
    AllocatedPopulationComponentLead,
    AllocationMethod,
    BuildingTypeProfile,
    ComponentBundleStatus,
    CountMethod,
    CoverageSteeringReview,
    EvidenceRole,
    GeographyLevel,
    HarvestEvidenceSet,
    HarvestQaqcReviewSet,
    HarvestRunManifest,
    HarvestRunStatus,
    InvestigationResult,
    InvestigationRun,
    LeadConfidence,
    LeadQaqcRecommendedAction,
    LeadQaqcReview,
    LeadQaqcVerificationStatus,
    OccupancyLead,
    PopulationComponentLead,
    RecommendedGapFillJob,
    ResultStatus,
    SampleSetManifest,
    SampleSetRound,
    SampleSetRoundRole,
    SourceType,
    TimeBasis,
    WorkItem,
)


def test_accepted_result_cannot_omit_required_fields() -> None:
    with pytest.raises(ValidationError):
        InvestigationResult(status=ResultStatus.ACCEPTED, reason="Incomplete accepted result.")


def test_codex_run_model_loads_example() -> None:
    run = InvestigationRun.model_validate_json(
        Path("examples/milltown_codex_run.json").read_text(encoding="utf-8")
    )

    assert run.task.locality == "Milltown"
    assert run.candidate.produced_by == "codex"
    assert run.candidate.result.count == 17


def test_candidate_observation_can_record_strategy_attribution() -> None:
    payload = json.loads(Path("examples/milltown_codex_run.json").read_text(encoding="utf-8"))
    payload["candidate"]["strategy_id"] = "incident_evacuation"
    payload["candidate"]["count_semantics"] = "confirmed_inside"
    payload["candidate"]["representativeness"] = "incident_specific"

    run = InvestigationRun.model_validate(payload)

    assert run.candidate.strategy_id is not None
    assert run.candidate.strategy_id.value == "incident_evacuation"
    assert run.candidate.result.observed_time_text == "approximately 9:10 p.m."
    assert run.candidate.result.time_context is not None
    assert run.candidate.result.time_context.observed_time_local == "21:10"
    assert run.candidate.result.time_context.day_part == "night"
    assert run.source_bundle.documents
    assert run.source_bundle.places


def test_occupancy_lead_model_loads_example_array() -> None:
    payload = json.loads(Path("examples/ph_commercial_leads.json").read_text(encoding="utf-8"))
    lead = OccupancyLead.model_validate(payload[0])

    assert lead.is_valid_occupancy_report
    assert lead.occupancy_data[0].count == 83
    assert lead.location.country == "PH"
    assert lead.source_type == SourceType.UNKNOWN
    assert lead.confidence == LeadConfidence.UNKNOWN
    assert lead.review_flags == ()


def test_occupancy_lead_model_accepts_quality_fields() -> None:
    lead = OccupancyLead.model_validate(
        {
            "is_valid_occupancy_report": True,
            "source_url": "https://example.test/story",
            "source_title": "Workers evacuated",
            "source_type": "news",
            "evidence_quote": "Officials said 12 workers were evacuated.",
            "incident_date": "2026-01-02",
            "incident_time": "03:30 PM",
            "occupancy_data": [{"count": 12, "group_type": "workers"}],
            "location": {
                "facility_name": "Example Warehouse",
                "specific_address_or_landmark": "Industrial Drive",
                "city_or_region": "Tennessee",
                "country": "US",
            },
            "confidence": "high",
            "is_facility_level": True,
            "is_regional_aggregate": False,
            "review_flags": ["needs_geocode"],
            "review_notes": "Review geocode.",
            "strategy_id": "incident_evacuation",
            "count_semantics": "evacuated",
            "representativeness": "incident_specific",
        }
    )

    assert lead.source_type == SourceType.NEWS
    assert lead.confidence == LeadConfidence.HIGH
    assert lead.evidence_quote is not None
    assert lead.review_flags == ("needs_geocode",)
    assert lead.strategy_id is not None
    assert lead.strategy_id.value == "incident_evacuation"
    assert lead.count_semantics == "evacuated"


def test_harvest_evidence_set_accepts_component_leads() -> None:
    evidence_set = HarvestEvidenceSet.model_validate(
        {
            "schema_version": 1,
            "occupancy_leads": [],
            "component_leads": [
                {
                    "is_valid_component_report": True,
                    "source_url": "https://example.test/school",
                    "source_title": "School facts",
                    "source_type": "official",
                    "evidence_quote": "Enrollment was 512 students for school year 2025.",
                    "component_data": [
                        {
                            "component_type": "students",
                            "value": 512,
                            "unit": "people",
                            "time_basis": "school_year",
                            "geography_level": "facility",
                            "period_label": "SY 2025",
                        }
                    ],
                    "location": {
                        "facility_name": "Example School",
                        "specific_address_or_landmark": "10 Main Street",
                        "city_or_region": "Tennessee",
                        "country": "US",
                    },
                    "geography_name": "Example School",
                    "country": "US",
                }
            ],
        }
    )

    component = evidence_set.component_leads[0]
    assert isinstance(component, PopulationComponentLead)
    assert component.component_data[0].time_basis == TimeBasis.SCHOOL_YEAR
    assert component.component_data[0].geography_level == GeographyLevel.FACILITY


def test_component_time_basis_accepts_static_and_event_inputs() -> None:
    evidence_set = HarvestEvidenceSet.model_validate(
        {
            "schema_version": 1,
            "component_leads": [
                {
                    "is_valid_component_report": True,
                    "source_url": "https://example.test/arena",
                    "source_title": "Arena facts",
                    "source_type": "official",
                    "evidence_quote": "The arena has 55,000 seats and 300 event staff.",
                    "component_data": [
                        {
                            "component_type": "seating capacity",
                            "value": 55000,
                            "unit": "seats",
                            "time_basis": "current_static",
                            "geography_level": "facility",
                        },
                        {
                            "component_type": "staff",
                            "value": 300,
                            "unit": "people",
                            "time_basis": "event",
                            "geography_level": "facility",
                            "period_label": "sold-out event",
                        },
                    ],
                    "geography_name": "Example Arena",
                    "country": "US",
                }
            ],
        }
    )

    static, event = evidence_set.component_leads[0].component_data
    assert static.time_basis == TimeBasis.CURRENT_STATIC
    assert event.time_basis == TimeBasis.EVENT


def test_component_time_basis_accepts_monthly_inputs() -> None:
    evidence_set = HarvestEvidenceSet.model_validate(
        {
            "schema_version": 1,
            "component_leads": [
                {
                    "is_valid_component_report": True,
                    "source_url": "https://example.test/hotel-statistics",
                    "source_title": "Hotel statistics",
                    "source_type": "official",
                    "evidence_quote": "Hotel occupancy was 71.2 percent in May 2026.",
                    "component_data": [
                        {
                            "component_type": "hotel occupancy rate",
                            "value": 71.2,
                            "unit": "percent",
                            "time_basis": "monthly",
                            "geography_level": "country",
                            "period_label": "May 2026",
                        }
                    ],
                    "geography_name": "Netherlands",
                    "country": "Netherlands",
                }
            ],
        }
    )

    assert evidence_set.component_leads[0].component_data[0].time_basis == TimeBasis.MONTHLY


def test_harvest_evidence_set_accepts_countable_component_bundles() -> None:
    evidence_set = HarvestEvidenceSet.model_validate(
        {
            "schema_version": 1,
            "component_leads": [
                {
                    "is_valid_component_report": True,
                    "source_url": "https://example.test/library",
                    "source_title": "Library facts",
                    "source_type": "official",
                    "evidence_quote": "The library recorded 120000 visits and 14 staff.",
                    "component_data": [
                        {
                            "component_type": "Annual visitors",
                            "value": 120000,
                            "unit": "visits",
                            "time_basis": "annual",
                            "geography_level": "facility",
                            "period_label": "2025",
                        }
                    ],
                    "geography_name": "Example Public Library",
                    "country": "US",
                }
            ],
            "component_bundles": [
                {
                    "geography_name": "Example Public Library",
                    "country": "US",
                    "target_component_fields": ["Annual visitors", "library staff"],
                    "found_component_types": ["Annual visitors"],
                    "missing_component_types": ["library staff"],
                    "source_lead_indexes": [0],
                    "follow_up_searches_attempted": [
                        '"Example Public Library" "library staff"'
                    ],
                    "completion_status": "mostly_complete",
                    "counts_toward_target": True,
                    "confidence": "medium",
                    "completion_notes": "Annual visitors found; staffing remained missing.",
                }
            ],
        }
    )

    bundle = evidence_set.component_bundles[0]
    assert bundle.completion_status == ComponentBundleStatus.MOSTLY_COMPLETE
    assert bundle.counts_toward_target is True


def test_partial_component_bundle_cannot_count_toward_target() -> None:
    with pytest.raises(ValidationError, match="complete or mostly_complete"):
        HarvestEvidenceSet.model_validate(
            {
                "schema_version": 1,
                "component_bundles": [
                    {
                        "geography_name": "Example Public Library",
                        "country": "US",
                        "target_component_fields": ["Annual visitors", "library staff"],
                        "found_component_types": ["Annual visitors"],
                        "missing_component_types": ["library staff"],
                        "completion_status": "partial",
                        "counts_toward_target": True,
                        "completion_notes": "Only one seed value was found.",
                    }
                ],
            }
        )


def _allocated_component_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "evidence_role": "allocated_component_input",
        "is_valid_allocated_component_report": True,
        "facility_location": {
            "facility_name": "Example Factory",
            "specific_address_or_landmark": "Industrial Park",
            "city_or_region": "Bavaria",
            "country": "DE",
        },
        "facility_source_url": "https://example.test/factories",
        "facility_source_title": "Factory directory",
        "facility_source_type": "directory",
        "facility_evidence_quote": "Example Factory is listed in Bavaria.",
        "regional_source_url": "https://example.test/regional-employment",
        "regional_source_title": "Regional employment",
        "regional_source_type": "official",
        "regional_evidence_quote": "Manufacturing employment in Bavaria was 300.",
        "regional_geography_name": "Bavaria",
        "regional_geography_level": "region",
        "component_type": "employees",
        "regional_value": 300,
        "allocated_value": 100,
        "unit": "people",
        "time_basis": "annual",
        "period_label": "2025",
        "facility_universe_count": 3,
        "denominator_scope": "Three named factories found in Bavaria.",
        "allocation_method": "equal_weight_region_facility_count",
        "country": "DE",
        "counts_toward_target": True,
        "confidence": "low",
        "strategy_id": "regional_component_allocation",
        "count_semantics": "allocated_component_input",
        "representativeness": "allocated_component_input",
        "allocation_notes": "300 regional employees / 3 discovered factories = 100.",
    }
    payload.update(overrides)
    return payload


def test_harvest_evidence_set_accepts_allocated_component_leads() -> None:
    evidence_set = HarvestEvidenceSet.model_validate(
        {
            "schema_version": 1,
            "allocated_component_leads": [_allocated_component_payload()],
        }
    )

    allocated = evidence_set.allocated_component_leads[0]
    assert isinstance(allocated, AllocatedPopulationComponentLead)
    assert allocated.evidence_role == EvidenceRole.ALLOCATED_COMPONENT_INPUT
    assert allocated.allocation_method == AllocationMethod.EQUAL_WEIGHT_REGION_FACILITY_COUNT

    summary = summarize_evidence_set(evidence_set)
    assert summary["allocated_component_lead_count"] == 1
    assert summary["countable_allocated_component_observations"] == 1
    assert summary["budget_observation_count"] == 1
    assert summary["source_backed_facility_rows"] == 0
    assert summary["allocated_facility_rows"] == 1
    assert summary["allocated_counts_by_method"] == {
        "equal_weight_region_facility_count": 1
    }


def test_allocated_component_lead_rejects_bad_math() -> None:
    with pytest.raises(ValidationError, match="regional_value / facility_universe_count"):
        HarvestEvidenceSet.model_validate(
            {
                "schema_version": 1,
                "allocated_component_leads": [
                    _allocated_component_payload(allocated_value=80)
                ],
            }
        )


def test_harvest_qaqc_review_set_accepts_allocated_component_reviews() -> None:
    review_set = HarvestQaqcReviewSet.model_validate(
        {
            "schema_version": 1,
            "allocated_component_reviews": [
                {
                    "lead_index": 0,
                    "item_id": "run-allocated-component-0",
                    "verification_status": "verified",
                    "regional_source_reachable": True,
                    "facility_source_reachable": True,
                    "evidence_role_match": True,
                    "regional_value_match": True,
                    "regional_geography_match": True,
                    "facility_location_match": True,
                    "facility_type_match": True,
                    "denominator_match": True,
                    "allocation_method_match": True,
                    "allocation_math_match": True,
                    "counts_toward_target_approved": True,
                    "recommended_action": "keep",
                    "review_notes": "Regional source, facility source, and math all check out.",
                }
            ],
        }
    )

    review = review_set.allocated_component_reviews[0]
    assert isinstance(review, AllocatedComponentQaqcReview)
    assert review.counts_toward_target_approved is True


def test_evidence_summary_counts_only_complete_component_bundles_for_budget() -> None:
    evidence_set = HarvestEvidenceSet.model_validate(
        {
            "schema_version": 1,
            "component_bundles": [
                {
                    "geography_name": "Complete Library",
                    "country": "US",
                    "target_component_fields": ["Annual visitors", "library staff"],
                    "found_component_types": ["Annual visitors", "library staff"],
                    "missing_component_types": [],
                    "completion_status": "complete",
                    "counts_toward_target": True,
                    "completion_notes": "All target fields were found.",
                },
                {
                    "geography_name": "Partial Library",
                    "country": "US",
                    "target_component_fields": ["Annual visitors", "library staff"],
                    "found_component_types": ["Annual visitors"],
                    "missing_component_types": ["library staff"],
                    "completion_status": "partial",
                    "counts_toward_target": False,
                    "completion_notes": "Only a seed field was found.",
                },
            ],
        }
    )

    summary = summarize_evidence_set(evidence_set)

    assert summary["component_bundle_count"] == 2
    assert summary["countable_component_observations"] == 1
    assert summary["budget_observation_count"] == 1
    assert summary["source_backed_facility_rows"] == 1
    assert summary["component_bundles_by_status"] == {"complete": 1, "partial": 1}


def test_allocated_shadow_bundles_do_not_count_as_source_backed_rows() -> None:
    evidence_set = HarvestEvidenceSet.model_validate(
        {
            "schema_version": 1,
            "component_bundles": [
                {
                    "geography_name": "Example Factory",
                    "country": "DE",
                    "target_component_fields": ["employees", "shifts"],
                    "found_component_types": ["Employees (allocated)"],
                    "missing_component_types": ["shifts"],
                    "completion_status": "mostly_complete",
                    "counts_toward_target": True,
                    "completion_notes": "Employees are allocated from a regional statistic.",
                }
            ],
        }
    )

    bundle = evidence_set.component_bundles[0]
    summary = summarize_evidence_set(evidence_set)

    assert bundle_is_allocated_shadow(bundle)
    assert summary["countable_component_observations"] == 0
    assert summary["source_backed_facility_rows"] == 0
    assert summary["budget_observation_count"] == 0


def test_harvest_qaqc_review_set_accepts_component_reviews() -> None:
    review_set = HarvestQaqcReviewSet.model_validate(
        {
            "schema_version": 1,
            "occupancy_reviews": [],
            "component_reviews": [
                {
                    "lead_index": 0,
                    "source_url": "https://example.test/school",
                    "verification_status": "verified",
                    "source_reachable": True,
                    "evidence_role_match": True,
                    "component_type_match": True,
                    "geography_level_match": True,
                    "component_checks": [
                        {
                            "component_type": "students",
                            "value": 512,
                            "unit": "people",
                            "reported_value_found": True,
                            "quote_found": True,
                        }
                    ],
                    "recommended_action": "keep",
                    "review_notes": "Component input is source-backed.",
                }
            ],
        }
    )

    assert review_set.component_reviews[0].component_checks[0].value == 512


def test_harvest_qaqc_review_set_accepts_component_bundle_reviews() -> None:
    review_set = HarvestQaqcReviewSet.model_validate(
        {
            "schema_version": 1,
            "occupancy_reviews": [],
            "component_reviews": [],
            "component_bundle_reviews": [
                {
                    "bundle_index": 0,
                    "item_id": "run-component-bundle-0",
                    "geography_name": "Example Store",
                    "verification_status": "verified",
                    "source_lead_indexes_valid": True,
                    "same_facility_or_geography": True,
                    "component_fields_match": True,
                    "completion_status_match": True,
                    "counts_toward_target_approved": True,
                    "found_component_types": ["employees", "customers"],
                    "missing_component_types": [],
                    "source_lead_indexes": [0, 1],
                    "recommended_action": "keep",
                    "review_notes": "Bundle components describe the same facility.",
                }
            ],
        }
    )

    assert review_set.component_bundle_reviews[0].item_id == "run-component-bundle-0"
    assert review_set.component_bundle_reviews[0].counts_toward_target_approved is True


def test_harvest_qaqc_review_set_bundle_reviews_default_empty() -> None:
    review_set = HarvestQaqcReviewSet.model_validate(
        {
            "schema_version": 1,
            "occupancy_reviews": [],
            "component_reviews": [],
        }
    )

    assert review_set.component_bundle_reviews == ()


def test_building_type_profile_count_method_defaults_are_backward_compatible() -> None:
    profile = BuildingTypeProfile.model_validate(
        {
            "profile_id": "legacy",
            "label": "Legacy profile",
            "source_search_prompt": "Find direct counts.",
        }
    )

    assert profile.count_method == CountMethod.DIRECT_COUNT
    assert profile.component_count_fields == ()


def test_lead_qaqc_review_model_accepts_verification_fields() -> None:
    review = LeadQaqcReview.model_validate(
        {
            "lead_index": 0,
            "source_url": "https://example.test/story",
            "verification_status": "verified",
            "source_reachable": True,
            "facility_match": True,
            "location_match": True,
            "strategy_match": True,
            "count_checks": [
                {
                    "count": 12,
                    "group_type": "workers",
                    "reported_count_found": True,
                    "quote_found": True,
                    "supporting_quote": "Officials said 12 workers were evacuated.",
                    "notes": None,
                }
            ],
            "supporting_quote": "Officials said 12 workers were evacuated.",
            "recommended_action": "keep",
            "review_notes": "Count, facility, and location are supported.",
        }
    )

    assert review.verification_status == LeadQaqcVerificationStatus.VERIFIED
    assert review.recommended_action == LeadQaqcRecommendedAction.KEEP
    assert review.strategy_match
    assert review.count_checks[0].reported_count_found is True


def test_address_enrichment_result_model_accepts_address_fields() -> None:
    result = AddressEnrichmentResult.model_validate(
        {
            "lead_index": 0,
            "item_id": "us-tn-schools-0",
            "facility_name": "Example School",
            "formatted_address": "10 Main Street, Nashville, TN, US",
            "address_line1": "10 Main Street",
            "address_line2": None,
            "city_or_region": "Nashville",
            "state_or_province": "TN",
            "postal_code": "37201",
            "country": "US",
            "address_source_url": "https://example.test/school",
            "address_evidence_quote": "Example School, 10 Main Street.",
            "confidence": "high",
            "status": "found",
            "review_notes": "Official page address matches the facility.",
        }
    )

    assert result.status == AddressEnrichmentStatus.FOUND
    assert result.confidence == AddressConfidence.HIGH
    assert result.formatted_address is not None


def test_harvest_run_manifest_model() -> None:
    manifest = HarvestRunManifest(
        run_id="us-tn-factories",
        status=HarvestRunStatus.COMPLETED,
        country="US",
        locality="Tennessee",
        profile_set="commercial_business",
        profile_id="factories_warehouses",
        target=5,
        prompt_path="work/us-tn-factories.md",
        lead_path="lead_runs/us-tn-factories.json",
        started_at="2026-07-23T00:00:00Z",
        completed_at="2026-07-23T00:01:00Z",
        codex_command=("codex", "--search"),
        exit_code=0,
        validation_valid=True,
        summary={"lead_count": 1},
    )

    assert manifest.status == HarvestRunStatus.COMPLETED
    assert manifest.summary == {"lead_count": 1}


def test_sample_set_and_coverage_models_accept_augmented_round_fields() -> None:
    sample_set = SampleSetManifest(
        sample_set_id="us-tn-schools-sample",
        country="US",
        requested_localities=("Tennessee",),
        facility_types=("schools",),
        target=5,
        rounds=(
            SampleSetRound(
                round_number=1,
                role=SampleSetRoundRole.INITIAL,
                source_run_ids=("us-tn-schools-campaign",),
                child_run_ids=("us-tn-schools-child",),
                status=HarvestRunStatus.COMPLETED,
                summary={"lead_count": 3},
            ),
        ),
        combined_child_run_ids=("us-tn-schools-child",),
        stage_summary={"approved_count": 2},
        created_at="2026-07-24T00:00:00Z",
        updated_at="2026-07-24T00:00:00Z",
    )
    review = CoverageSteeringReview(
        coverage_id="us-tn-schools-coverage",
        sample_set_id=sample_set.sample_set_id,
        dispersion_status="imbalanced",
        narrative_notes="Western Tennessee is underrepresented.",
        recommended_child_jobs=(
            RecommendedGapFillJob(
                country="US",
                locality="Western Tennessee",
                facility_type="schools",
                target=3,
                reason="Pad the sample outside the initial cluster.",
            ),
        ),
    )

    assert sample_set.rounds[0].role == SampleSetRoundRole.INITIAL
    assert review.recommended_child_jobs[0].facility_type == "schools"


def test_work_item_defaults_are_backward_compatible() -> None:
    item = WorkItem.model_validate(
        {
            "work_item_id": "legacy-restaurants",
            "batch_id": "legacy",
            "locality": "Milltown",
            "country": "US",
            "profile_id": "restaurants_bars",
            "created_at": "2026-06-23T00:00:00Z",
            "updated_at": "2026-06-23T00:00:00Z",
        }
    )

    assert item.quota.target_accepted_count == 5
    assert item.quota.max_sources_examined == 40
    assert item.progress.accepted_count == 0
    assert item.progress.run_files == ()


def test_building_type_profile_source_type_defaults_are_backward_compatible() -> None:
    profile = BuildingTypeProfile.model_validate(
        {
            "profile_id": "legacy",
            "label": "Legacy",
            "source_search_prompt": "Find useful sources.",
        }
    )

    assert profile.preferred_source_types == ()
    assert profile.context_only_source_types == ()

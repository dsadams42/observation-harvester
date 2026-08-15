from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ObservationType(StrEnum):
    PEOPLE_PRESENT = "people_present"


class ResultStatus(StrEnum):
    ACCEPTED = "accepted"
    REVIEW = "review"
    NOT_FOUND = "not_found"


class WorkStatus(StrEnum):
    OPEN = "open"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    FAILED = "failed"


class StopReason(StrEnum):
    TARGET_MET = "target_met"
    REVIEW_LIMIT_REACHED = "review_limit_reached"
    SOURCE_LIMIT_REACHED = "source_limit_reached"
    FAILED_SOURCE_LIMIT_REACHED = "failed_source_limit_reached"
    EMPTY_SOURCE_LIMIT_REACHED = "empty_source_limit_reached"
    RUNTIME_LIMIT_REACHED = "runtime_limit_reached"
    MANUAL_COMPLETE = "manual_complete"


class SourceOutcome(StrEnum):
    EXAMINED = "examined"
    EMPTY = "empty"
    FAILED = "failed"


class EvidenceStrategyType(StrEnum):
    INCIDENT_EVACUATION = "incident_evacuation"
    ENFORCEMENT_INSPECTION = "enforcement_inspection"
    OFFICIAL_EVENT_ATTENDANCE = "official_event_attendance"
    ROUTINE_DATED_ATTENDANCE = "routine_dated_attendance"
    SHIFT_OPERATIONAL_PRESENCE = "shift_operational_presence"
    LEGAL_INVESTIGATIVE_RECORDS = "legal_investigative_records"
    TEMPORARY_USE_OCCUPANCY = "temporary_use_occupancy"
    RESEARCH_MEASURED_OCCUPANCY = "research_measured_occupancy"
    OFFICIAL_FACILITY_STATISTICS = "official_facility_statistics"
    REGIONAL_DEMOGRAPHIC_STATISTICS = "regional_demographic_statistics"
    OPERATIONAL_SCHEDULE_FACTORS = "operational_schedule_factors"
    VISITOR_TRAFFIC_VOLUME = "visitor_traffic_volume"


class CountMethod(StrEnum):
    DIRECT_COUNT = "direct_count"
    POPULATION_SUBCOMPONENT = "population_subcomponent"
    HYBRID = "hybrid"


class EvidenceRole(StrEnum):
    DIRECT_OCCUPANCY = "direct_occupancy"
    COMPONENT_INPUT = "component_input"


class GeographyLevel(StrEnum):
    FACILITY = "facility"
    LOCALITY = "locality"
    REGION = "region"
    COUNTRY = "country"


class TimeBasis(StrEnum):
    INSTANT = "instant"
    SHIFT = "shift"
    DAILY = "daily"
    EVENT = "event"
    ANNUAL = "annual"
    SCHOOL_YEAR = "school_year"
    CENSUS_YEAR = "census_year"
    OPERATING_PERIOD = "operating_period"
    CURRENT_STATIC = "current_static"
    UNKNOWN = "unknown"


class ComponentBundleStatus(StrEnum):
    COMPLETE = "complete"
    MOSTLY_COMPLETE = "mostly_complete"
    PARTIAL = "partial"
    SEED_ONLY = "seed_only"


class SourceType(StrEnum):
    NEWS = "news"
    OFFICIAL = "official"
    WIRE = "wire"
    ENCYCLOPEDIA = "encyclopedia"
    SOCIAL = "social"
    DIRECTORY = "directory"
    UNKNOWN = "unknown"


class LeadConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class StrategyScoutConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class StrategyScoutEmphasis(StrEnum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    DE_EMPHASIZED = "de_emphasized"


class HarvesterActivityOutcome(StrEnum):
    PRODUCTIVE = "productive"
    PARTIALLY_PRODUCTIVE = "partially_productive"
    UNPRODUCTIVE = "unproductive"
    REVIEW_ONLY = "review_only"


class LeadQaqcVerificationStatus(StrEnum):
    VERIFIED = "verified"
    AMBIGUOUS = "ambiguous"
    COUNT_NOT_FOUND = "count_not_found"
    FACILITY_MISMATCH = "facility_mismatch"
    SOURCE_UNREACHABLE = "source_unreachable"
    REJECT = "reject"


class LeadQaqcRecommendedAction(StrEnum):
    KEEP = "keep"
    REVIEW = "review"
    REJECT = "reject"
    RETRY = "retry"


class GeometryStatus(StrEnum):
    NEEDS_REVIEW = "needs_review"
    POINT_CONFIRMED = "point_confirmed"
    FOOTPRINT_DRAWN = "footprint_drawn"
    SKIPPED = "skipped"


class AddressEnrichmentStatus(StrEnum):
    FOUND = "found"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    NEEDS_REVIEW = "needs_review"


class AddressConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class HarvestRunStatus(StrEnum):
    PREPARED = "prepared"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobType(StrEnum):
    HARVEST = "harvest"
    BATCH = "batch"
    CAMPAIGN = "campaign"
    QAQC = "qaqc"
    ADDRESS = "address"
    COVERAGE = "coverage"
    GAP_FILL = "gap_fill"
    SAMPLE_QAQC_MISSING = "sample_qaqc_missing"
    SAMPLE_ADDRESS_MISSING = "sample_address_missing"


class JobStatus(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GeographerPlanStatus(StrEnum):
    COMPLETED = "completed"
    FALLBACK = "fallback"


class SampleSetRoundRole(StrEnum):
    INITIAL = "initial"
    GAP_FILL = "gap_fill"


class CoverageDispersionStatus(StrEnum):
    UNKNOWN = "unknown"
    BALANCED = "balanced"
    IMBALANCED = "imbalanced"
    CLUSTERED = "clustered"
    INSUFFICIENT_DATA = "insufficient_data"


class CoverageFlagType(StrEnum):
    OUT_OF_SCOPE = "out_of_scope"
    DUPLICATE_CANDIDATE = "duplicate_candidate"
    CLUSTERED = "clustered"
    UNDERCOVERED = "undercovered"


class CurationReasonCode(StrEnum):
    DUPLICATE = "duplicate"
    WRONG_FACILITY = "wrong_facility"
    OUTSIDE_GEOGRAPHIC_SCOPE = "outside_geographic_scope"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    INCORRECT_COUNT_MEANING = "incorrect_count_meaning"
    UNREPRESENTATIVE = "unrepresentative"
    ADDRESS_OR_COORDINATE_UNRESOLVED = "address_or_coordinate_unresolved"
    FACILITY_TYPE_NOT_RELEVANT = "facility_type_not_relevant"
    OTHER = "other"


class DayPart(StrEnum):
    EARLY_MORNING = "early_morning"
    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"
    NIGHT = "night"
    DAY = "day"
    UNKNOWN = "unknown"


class DaylightState(StrEnum):
    DAYLIGHT = "daylight"
    TWILIGHT = "twilight"
    DARK = "dark"
    UNKNOWN = "unknown"


class TimePrecision(StrEnum):
    EXACT = "exact"
    APPROXIMATE = "approximate"
    DAY_PART_ONLY = "day_part_only"
    UNKNOWN = "unknown"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InvestigationTask(StrictModel):
    task_id: str = Field(min_length=1)
    locality: str = Field(min_length=1)
    country: str = Field(min_length=2)
    observation_type: ObservationType
    maximum_agent_turns: int = Field(default=6, ge=1, le=20)


class Evidence(StrictModel):
    document_id: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    supporting_quote: str = Field(min_length=1)


class GeoReference(StrictModel):
    place_id: str = Field(min_length=1)
    place_name: str = Field(min_length=1)
    locality: str = Field(min_length=1)
    country: str = Field(min_length=2)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    method: str = Field(min_length=1)


class TimeContext(StrictModel):
    observed_time_local: str | None = Field(
        default=None,
        pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$",
    )
    time_precision: TimePrecision = TimePrecision.UNKNOWN
    day_part: DayPart = DayPart.UNKNOWN
    daylight_state: DaylightState = DaylightState.UNKNOWN
    timezone: str | None = None


class InvestigationResult(StrictModel):
    status: ResultStatus
    count: int | None = Field(default=None, ge=0)
    observation_type: ObservationType | None = None
    place_name: str | None = None
    observed_time_text: str | None = None
    time_context: TimeContext | None = None
    evidence: Evidence | None = None
    georeference: GeoReference | None = None
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def accepted_results_are_complete(self) -> Self:
        if self.status != ResultStatus.ACCEPTED:
            return self

        missing: list[str] = []
        if self.count is None:
            missing.append("count")
        if self.observation_type is None:
            missing.append("observation_type")
        if self.place_name is None:
            missing.append("place_name")
        if self.evidence is None:
            missing.append("evidence")
        if self.georeference is None:
            missing.append("georeference")
        if missing:
            fields = ", ".join(missing)
            raise ValueError(f"accepted result missing required field(s): {fields}")
        return self


class SourceSearchResult(StrictModel):
    document_id: str
    title: str
    source_url: str
    snippet: str
    score: int


class SourceDocument(StrictModel):
    document_id: str
    title: str
    source_url: str
    locality: str
    country: str
    text: str
    tags: tuple[str, ...] = ()


class PlaceRecord(StrictModel):
    place_id: str
    name: str
    locality: str
    country: str
    latitude: float
    longitude: float
    method: str


class CandidateObservation(StrictModel):
    result: InvestigationResult
    produced_by: str = Field(default="codex", min_length=1)
    notes: str | None = None
    strategy_id: EvidenceStrategyType | None = None
    count_semantics: str | None = None
    representativeness: str | None = None


class SourceBundle(StrictModel):
    documents: tuple[SourceDocument, ...]
    places: tuple[PlaceRecord, ...]


class InvestigationRun(StrictModel):
    task: InvestigationTask
    source_bundle: SourceBundle
    candidate: CandidateObservation


class LeadOccupancyDatum(StrictModel):
    count: int = Field(ge=0)
    group_type: str = Field(min_length=1)


class PopulationComponentDatum(StrictModel):
    component_type: str = Field(min_length=1)
    value: float = Field(ge=0)
    unit: str = Field(min_length=1)
    time_basis: TimeBasis = TimeBasis.UNKNOWN
    geography_level: GeographyLevel = GeographyLevel.FACILITY
    period_label: str | None = None


class LeadLocation(StrictModel):
    facility_name: str = Field(min_length=1)
    specific_address_or_landmark: str = Field(min_length=1)
    city_or_region: str = Field(min_length=1)
    country: str = Field(min_length=2)


class OccupancyLead(StrictModel):
    evidence_role: EvidenceRole = EvidenceRole.DIRECT_OCCUPANCY
    is_valid_occupancy_report: bool
    source_url: str = Field(min_length=1)
    source_title: str = ""
    source_type: SourceType = SourceType.UNKNOWN
    evidence_quote: str | None = None
    incident_date: str = Field(min_length=1)
    incident_time: str = Field(min_length=1)
    occupancy_data: tuple[LeadOccupancyDatum, ...] = Field(min_length=1)
    location: LeadLocation
    confidence: LeadConfidence = LeadConfidence.UNKNOWN
    is_facility_level: bool | None = None
    is_regional_aggregate: bool | None = None
    review_flags: tuple[str, ...] = ()
    review_notes: str | None = None
    strategy_id: EvidenceStrategyType | None = None
    count_semantics: str | None = None
    representativeness: str | None = None


class PopulationComponentLead(StrictModel):
    evidence_role: EvidenceRole = EvidenceRole.COMPONENT_INPUT
    is_valid_component_report: bool
    source_url: str = Field(min_length=1)
    source_title: str = ""
    source_type: SourceType = SourceType.UNKNOWN
    evidence_quote: str = Field(min_length=1)
    component_data: tuple[PopulationComponentDatum, ...] = Field(min_length=1)
    location: LeadLocation | None = None
    geography_name: str = Field(min_length=1)
    country: str = Field(min_length=2)
    confidence: LeadConfidence = LeadConfidence.UNKNOWN
    is_facility_level: bool | None = None
    is_regional_aggregate: bool | None = None
    review_flags: tuple[str, ...] = ()
    review_notes: str | None = None
    strategy_id: EvidenceStrategyType | None = None
    count_semantics: str | None = None
    representativeness: str | None = None


class ComponentFacilityBundle(StrictModel):
    evidence_role: EvidenceRole = EvidenceRole.COMPONENT_INPUT
    geography_name: str = Field(min_length=1)
    country: str = Field(min_length=2)
    location: LeadLocation | None = None
    target_component_fields: tuple[str, ...] = Field(min_length=1)
    found_component_types: tuple[str, ...] = ()
    missing_component_types: tuple[str, ...] = ()
    source_lead_indexes: tuple[int, ...] = ()
    follow_up_searches_attempted: tuple[str, ...] = ()
    completion_status: ComponentBundleStatus
    counts_toward_target: bool = False
    confidence: LeadConfidence = LeadConfidence.UNKNOWN
    completion_notes: str = Field(min_length=1)

    @model_validator(mode="after")
    def countable_bundles_must_be_complete_enough(self) -> Self:
        countable_statuses = {
            ComponentBundleStatus.COMPLETE,
            ComponentBundleStatus.MOSTLY_COMPLETE,
        }
        if self.counts_toward_target and self.completion_status not in countable_statuses:
            raise ValueError(
                "component bundle can count toward target only when complete or mostly_complete"
            )
        return self


class HarvestEvidenceSet(StrictModel):
    schema_version: int = Field(default=1, ge=1)
    occupancy_leads: tuple[OccupancyLead, ...] = ()
    component_leads: tuple[PopulationComponentLead, ...] = ()
    component_bundles: tuple[ComponentFacilityBundle, ...] = ()


class LeadQaqcCountCheck(StrictModel):
    count: int = Field(ge=0)
    group_type: str = Field(min_length=1)
    reported_count_found: bool
    quote_found: bool
    supporting_quote: str | None = None
    notes: str | None = None


class LeadQaqcReview(StrictModel):
    lead_index: int = Field(ge=0)
    source_url: str = Field(min_length=1)
    verification_status: LeadQaqcVerificationStatus
    source_reachable: bool
    facility_match: bool | None = None
    location_match: bool | None = None
    strategy_match: bool | None = None
    count_checks: tuple[LeadQaqcCountCheck, ...] = ()
    supporting_quote: str | None = None
    recommended_action: LeadQaqcRecommendedAction
    review_notes: str = Field(min_length=1)


class ComponentQaqcDatumCheck(StrictModel):
    component_type: str = Field(min_length=1)
    value: float = Field(ge=0)
    unit: str = Field(min_length=1)
    reported_value_found: bool
    quote_found: bool
    component_type_match: bool | None = None
    time_basis_match: bool | None = None
    geography_level_match: bool | None = None
    supporting_quote: str | None = None
    notes: str | None = None


class ComponentQaqcReview(StrictModel):
    lead_index: int = Field(ge=0)
    source_url: str = Field(min_length=1)
    verification_status: LeadQaqcVerificationStatus
    source_reachable: bool
    evidence_role_match: bool | None = None
    component_type_match: bool | None = None
    geography_level_match: bool | None = None
    location_match: bool | None = None
    strategy_match: bool | None = None
    component_checks: tuple[ComponentQaqcDatumCheck, ...] = ()
    supporting_quote: str | None = None
    recommended_action: LeadQaqcRecommendedAction
    review_notes: str = Field(min_length=1)


class HarvestQaqcReviewSet(StrictModel):
    schema_version: int = Field(default=1, ge=1)
    occupancy_reviews: tuple[LeadQaqcReview, ...] = ()
    component_reviews: tuple[ComponentQaqcReview, ...] = ()


class AddressEnrichmentResult(StrictModel):
    lead_index: int = Field(ge=0)
    item_id: str = Field(min_length=1)
    facility_name: str = Field(min_length=1)
    formatted_address: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city_or_region: str | None = None
    state_or_province: str | None = None
    postal_code: str | None = None
    country: str | None = None
    address_source_url: str | None = None
    address_evidence_quote: str | None = None
    confidence: AddressConfidence = AddressConfidence.UNKNOWN
    status: AddressEnrichmentStatus = AddressEnrichmentStatus.NEEDS_REVIEW
    review_notes: str = Field(min_length=1)


class GeometryPoint(StrictModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    source: str = Field(default="user", min_length=1)


class GeometryReviewItem(StrictModel):
    schema_version: int = Field(default=1, ge=1)
    item_id: str = Field(min_length=1)
    child_run_id: str = Field(min_length=1)
    lead_index: int = Field(ge=0)
    geocode_query: str = Field(min_length=1)
    geocode_result: dict[str, object] | None = None
    point: GeometryPoint | None = None
    polygon_geojson: dict[str, object] | None = None
    geometries: tuple[dict[str, object], ...] = ()
    area_m2: float | None = Field(default=None, ge=0)
    spatial_validation: dict[str, object] | None = None
    geometry_status: GeometryStatus = GeometryStatus.NEEDS_REVIEW
    review_notes: str | None = None


class BuildingTypeProfile(StrictModel):
    profile_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    pdt_subtype: str | None = None
    area_defined: str | None = None
    day_occurrence: str | None = None
    night_occurrence: str | None = None
    episodic_occurrence: tuple[str, ...] = ()
    occupancy_groups: tuple[str, ...] = ()
    contextual_count_fields: tuple[str, ...] = ()
    count_method: CountMethod = CountMethod.DIRECT_COUNT
    component_count_fields: tuple[str, ...] = ()
    regional_stat_fields: tuple[str, ...] = ()
    component_source_guidance: str | None = None
    preferred_strategy_ids: tuple[EvidenceStrategyType, ...] = ()
    source_search_prompt: str = Field(min_length=1)
    preferred_source_types: tuple[str, ...] = ()
    context_only_source_types: tuple[str, ...] = ()
    positive_evidence_patterns: tuple[str, ...] = ()
    negative_evidence_patterns: tuple[str, ...] = ()
    venue_aliases: tuple[str, ...] = ()
    priority: int = Field(default=100, ge=0)
    enabled: bool = True


class BuildingProfileSet(StrictModel):
    profile_set_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    profiles: tuple[BuildingTypeProfile, ...]


class VernacularTerm(StrictModel):
    standard_term: str = Field(min_length=1)
    local_term: str = Field(min_length=1)
    language: str = Field(min_length=1)
    usage_note: str = Field(min_length=1)


class GeographerProposal(StrictModel):
    country_code: str | None = None
    country_aliases: tuple[str, ...] = ()
    search_languages: tuple[str, ...] = ()
    administrative_terms: tuple[VernacularTerm, ...] = ()
    address_terms: tuple[VernacularTerm, ...] = ()
    public_safety_terms: tuple[VernacularTerm, ...] = ()
    facility_terms: tuple[VernacularTerm, ...] = ()
    query_adjustments: tuple[str, ...] = ()
    source_urls: tuple[str, ...] = ()
    commentary: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class GeographerPlan(StrictModel):
    schema_version: int = Field(default=1, ge=1)
    plan_id: str = Field(min_length=1)
    status: GeographerPlanStatus
    country: str = Field(min_length=2)
    locality: str | None = None
    localities: tuple[str, ...] = ()
    profile_set: str = Field(min_length=1)
    profile_id: str | None = None
    facility_types: tuple[str, ...] = ()
    proposal: GeographerProposal
    prompt_path: str = Field(min_length=1)
    artifact_path: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    error_message: str | None = None


class AgentDialogueEntry(StrictModel):
    speaker: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    message: str = Field(min_length=1)
    rationale: str | None = None
    created_at: str = Field(min_length=1)


class EvidenceStrategy(StrictModel):
    strategy_id: EvidenceStrategyType
    label: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    query_templates: tuple[str, ...] = Field(min_length=1)
    preferred_source_types: tuple[str, ...] = ()
    accepted_count_semantics: tuple[str, ...] = Field(min_length=1)
    negative_traps: tuple[str, ...] = ()
    default_representativeness: str = Field(min_length=1)


class StrategyRecommendation(StrictModel):
    strategy_id: EvidenceStrategyType
    priority: int = Field(ge=0)
    reason: str = Field(min_length=1)


class StrategyPlan(StrictModel):
    planner: str = Field(default="deterministic_strategy_planner_v1", min_length=1)
    recommendations: tuple[StrategyRecommendation, ...] = Field(min_length=1)


class StrategyScoutRecommendation(StrictModel):
    strategy_id: EvidenceStrategyType
    emphasis: StrategyScoutEmphasis = StrategyScoutEmphasis.SECONDARY
    rationale: str = Field(min_length=1)
    query_patterns: tuple[str, ...] = ()
    expected_traps: tuple[str, ...] = ()


class StrategyScoutPlan(StrictModel):
    schema_version: int = Field(default=1, ge=1)
    run_id: str = Field(min_length=1)
    country: str = Field(min_length=2)
    locality: str | None = None
    profile_set: str = Field(min_length=1)
    profile_id: str | None = None
    recommended_strategy_order: tuple[EvidenceStrategyType, ...] = Field(min_length=1)
    recommendations: tuple[StrategyScoutRecommendation, ...] = Field(min_length=1)
    local_source_ideas: tuple[str, ...] = ()
    overall_rationale: str = Field(min_length=1)
    confidence: StrategyScoutConfidence = StrategyScoutConfidence.UNKNOWN

    @model_validator(mode="after")
    def recommended_order_has_recommendations(self) -> Self:
        recommendation_ids = {item.strategy_id for item in self.recommendations}
        missing = [
            strategy_id.value
            for strategy_id in self.recommended_strategy_order
            if strategy_id not in recommendation_ids
        ]
        if missing:
            raise ValueError(
                "recommended_strategy_order contains strategy id(s) without "
                f"recommendations: {', '.join(missing)}"
            )
        return self


class HarvesterStrategyActivity(StrictModel):
    strategy_id: EvidenceStrategyType
    outcome: HarvesterActivityOutcome
    query_examples: tuple[str, ...] = ()
    notes: str = Field(min_length=1)
    accepted_lead_count: int = Field(default=0, ge=0)


class HarvesterActivityReport(StrictModel):
    schema_version: int = Field(default=1, ge=1)
    run_id: str = Field(min_length=1)
    overall_summary: str = Field(min_length=1)
    strategy_activity: tuple[HarvesterStrategyActivity, ...] = ()
    accepted_lead_count: int = Field(default=0, ge=0)
    rejected_or_context_notes: tuple[str, ...] = ()
    follow_up_suggestions: tuple[str, ...] = ()


class WorkQuota(StrictModel):
    target_accepted_count: int = Field(default=5, ge=1)
    max_review_count: int = Field(default=10, ge=0)
    max_sources_examined: int = Field(default=40, ge=0)
    max_failed_sources: int = Field(default=20, ge=0)
    max_empty_sources: int = Field(default=15, ge=0)
    max_runtime_minutes: int = Field(default=60, ge=0)


class WorkProgress(StrictModel):
    accepted_count: int = Field(default=0, ge=0)
    review_count: int = Field(default=0, ge=0)
    not_found_count: int = Field(default=0, ge=0)
    sources_examined: int = Field(default=0, ge=0)
    failed_sources: int = Field(default=0, ge=0)
    empty_sources: int = Field(default=0, ge=0)
    run_files: tuple[str, ...] = ()
    started_at: str | None = None
    last_activity_at: str | None = None
    stop_reason: StopReason | None = None


class WorkItem(StrictModel):
    schema_version: int = Field(default=1, ge=1)
    work_item_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    locality: str = Field(min_length=1)
    country: str = Field(min_length=2)
    profile_id: str = Field(min_length=1)
    observation_type: ObservationType = ObservationType.PEOPLE_PRESENT
    status: WorkStatus = WorkStatus.OPEN
    claimed_by: str | None = None
    source_hints: tuple[str, ...] = ()
    strategy_plan: StrategyPlan | None = None
    run_artifact_path: str | None = None
    quota: WorkQuota = Field(default_factory=WorkQuota)
    progress: WorkProgress = Field(default_factory=WorkProgress)
    created_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)


class WorkStatusReport(StrictModel):
    work_item_id: str = Field(min_length=1)
    status: WorkStatus
    should_continue: bool
    quota: WorkQuota
    progress: WorkProgress
    stop_reason: StopReason | None = None
    remaining: dict[str, int]


class HarvestBatch(StrictModel):
    schema_version: int = Field(default=1, ge=1)
    batch_id: str = Field(min_length=1)
    locality: str = Field(min_length=1)
    country: str = Field(min_length=2)
    profile_set_id: str = Field(min_length=1)
    profile_ids: tuple[str, ...]
    work_item_ids: tuple[str, ...]
    created_at: str = Field(min_length=1)


class HarvestRunManifest(StrictModel):
    schema_version: int = Field(default=1, ge=1)
    run_id: str = Field(min_length=1)
    status: HarvestRunStatus
    country: str = Field(min_length=2)
    locality: str | None = None
    profile_set: str = Field(min_length=1)
    profile_id: str | None = None
    count_method_override: CountMethod | None = None
    strategy_plan: StrategyPlan | None = None
    geographer_plan_path: str | None = None
    target: int = Field(ge=1)
    prompt_path: str = Field(min_length=1)
    lead_path: str = Field(min_length=1)
    started_at: str = Field(min_length=1)
    completed_at: str | None = None
    codex_command: tuple[str, ...] = ()
    exit_code: int | None = None
    validation_valid: bool | None = None
    summary: dict[str, object] | None = None
    error_message: str | None = None
    log_path: str | None = None
    strategy_scout_path: str | None = None
    activity_path: str | None = None


class HarvestBatchRunManifest(StrictModel):
    schema_version: int = Field(default=1, ge=1)
    batch_id: str = Field(min_length=1)
    status: HarvestRunStatus
    country: str = Field(min_length=2)
    locality: str | None = None
    profile_set: str = Field(min_length=1)
    count_method_override: CountMethod | None = None
    geographer_plan_path: str | None = None
    target: int = Field(ge=1)
    child_run_ids: tuple[str, ...]
    child_manifest_paths: tuple[str, ...]
    started_at: str = Field(min_length=1)
    completed_at: str | None = None
    summary: dict[str, object] | None = None
    error_message: str | None = None
    log_path: str | None = None


class HarvestCampaignRunManifest(StrictModel):
    schema_version: int = Field(default=1, ge=1)
    campaign_id: str = Field(min_length=1)
    status: HarvestRunStatus
    country: str = Field(min_length=2)
    localities: tuple[str, ...] = ()
    facility_types: tuple[str, ...] = Field(min_length=1)
    count_method_override: CountMethod | None = None
    geographer_plan_path: str | None = None
    target: int = Field(ge=1)
    child_run_ids: tuple[str, ...]
    child_manifest_paths: tuple[str, ...]
    started_at: str = Field(min_length=1)
    completed_at: str | None = None
    summary: dict[str, object] | None = None
    error_message: str | None = None
    log_path: str | None = None


class SampleSetRound(StrictModel):
    round_number: int = Field(ge=1)
    role: SampleSetRoundRole
    source_run_ids: tuple[str, ...] = ()
    child_run_ids: tuple[str, ...] = ()
    recommended_coverage_id: str | None = None
    status: HarvestRunStatus
    summary: dict[str, object] | None = None


class SampleSetManifest(StrictModel):
    schema_version: int = Field(default=1, ge=1)
    sample_set_id: str = Field(min_length=1)
    country: str = Field(min_length=2)
    requested_localities: tuple[str, ...] = ()
    facility_types: tuple[str, ...] = ()
    target: int = Field(ge=1)
    rounds: tuple[SampleSetRound, ...]
    combined_child_run_ids: tuple[str, ...]
    stage_summary: dict[str, object] | None = None
    created_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)


class CurationDecision(StrictModel):
    item_id: str = Field(min_length=1)
    reason_code: CurationReasonCode
    reason_note: str | None = None
    excluded_at: str = Field(min_length=1)


class CurationApproval(StrictModel):
    snapshot_id: str = Field(min_length=1)
    fingerprint: str = Field(min_length=64, max_length=64)
    approved_at: str = Field(min_length=1)
    included_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)


class SampleCurationManifest(StrictModel):
    schema_version: int = Field(default=1, ge=1)
    sample_set_id: str = Field(min_length=1)
    decisions: tuple[CurationDecision, ...] = ()
    approval: CurationApproval | None = None
    updated_at: str = Field(min_length=1)


class RecommendedGapFillJob(StrictModel):
    country: str = Field(min_length=2)
    locality: str | None = None
    facility_type: str = Field(min_length=1)
    target: int = Field(ge=1)
    reason: str = Field(min_length=1)


class CoverageFlag(StrictModel):
    item_id: str | None = None
    flag_type: CoverageFlagType
    reason: str = Field(min_length=1)


class CoverageSteeringReview(StrictModel):
    schema_version: int = Field(default=1, ge=1)
    coverage_id: str = Field(min_length=1)
    sample_set_id: str = Field(min_length=1)
    dispersion_status: CoverageDispersionStatus = CoverageDispersionStatus.UNKNOWN
    counts_by_locality: dict[str, int] = {}
    counts_by_city_or_region: dict[str, int] = {}
    counts_by_facility_type: dict[str, int] = {}
    out_of_scope_flags: tuple[CoverageFlag, ...] = ()
    duplicate_or_cluster_flags: tuple[CoverageFlag, ...] = ()
    narrative_notes: str = Field(min_length=1)
    recommended_child_jobs: tuple[RecommendedGapFillJob, ...] = ()
    curation_snapshot_id: str | None = None
    curation_feedback_count: int = Field(default=0, ge=0)


class ReviewQueueItem(StrictModel):
    review_item_id: str = Field(min_length=1)
    run_file: str = Field(min_length=1)
    status: ResultStatus
    validation_valid: bool
    validation_errors: tuple[str, ...] = ()
    reason: str = Field(min_length=1)
    source_url: str | None = None
    supporting_quote: str | None = None
    count: int | None = Field(default=None, ge=0)
    place_name: str | None = None
    observed_time_text: str | None = None
    time_context: TimeContext | None = None
    georeference_status: str = Field(min_length=1)
    ingested_at: str = Field(min_length=1)


class DirectFetchResult(StrictModel):
    url: str = Field(min_length=1)
    canonical_url: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    title: str = ""
    text: str
    content_type: str
    status_code: int = Field(ge=100, le=599)
    content_sha256: str = Field(min_length=64, max_length=64)
    fetched_at: str = Field(min_length=1)
    discovered_urls: tuple[str, ...] = ()


class JobRecord(StrictModel):
    schema_version: int = Field(default=1, ge=1)
    job_id: str = Field(min_length=1)
    job_type: JobType
    parent_id: str | None = None
    status: JobStatus = JobStatus.QUEUED
    created_at: str = Field(min_length=1)
    started_at: str | None = None
    completed_at: str | None = None
    updated_at: str = Field(min_length=1)
    manifest_path: str | None = None
    log_path: str | None = None
    error_message: str | None = None
    active_child_ids: tuple[str, ...] = ()
    summary: dict[str, object] | None = None

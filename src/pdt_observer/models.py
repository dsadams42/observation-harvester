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


class LeadLocation(StrictModel):
    facility_name: str = Field(min_length=1)
    specific_address_or_landmark: str = Field(min_length=1)
    city_or_region: str = Field(min_length=1)
    country: str = Field(min_length=2)


class OccupancyLead(StrictModel):
    is_valid_occupancy_report: bool
    source_url: str = Field(min_length=1)
    incident_date: str = Field(min_length=1)
    incident_time: str = Field(min_length=1)
    occupancy_data: tuple[LeadOccupancyDatum, ...] = Field(min_length=1)
    location: LeadLocation
    review_notes: str | None = None


class BuildingTypeProfile(StrictModel):
    profile_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
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
    work_item_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    locality: str = Field(min_length=1)
    country: str = Field(min_length=2)
    profile_id: str = Field(min_length=1)
    observation_type: ObservationType = ObservationType.PEOPLE_PRESENT
    status: WorkStatus = WorkStatus.OPEN
    claimed_by: str | None = None
    source_hints: tuple[str, ...] = ()
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
    batch_id: str = Field(min_length=1)
    locality: str = Field(min_length=1)
    country: str = Field(min_length=2)
    profile_set_id: str = Field(min_length=1)
    profile_ids: tuple[str, ...]
    work_item_ids: tuple[str, ...]
    created_at: str = Field(min_length=1)


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
